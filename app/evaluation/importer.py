"""Import historical result files into the evaluation schema (decision A).

Rules:
* The JSON file is only read. Its SHA-256 and file name are stored; importing the same bytes twice is a no-op.
* A field the file never recorded stays ``NULL``. Nothing is derived from a guess: where a value follows
  from recorded attempts (the first attempt's status) it is read from them, and otherwise left unknown.
* Imported runs are marked historical, carry ``benchmark_version = 'unknown-historical'`` (the benchmark
  files of that day are not recoverable), and have no configuration row. A regression check against
  a current baseline therefore refuses them, instead of comparing different benchmarks.
* Totals are validated against the file's own summary. A mismatch aborts the import.
* The caller owns the transaction; nothing is committed here.
"""

import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation.benchmarks import BenchmarkSuite, load_suite
from app.evaluation.models import EvaluationAttempt, EvaluationCaseResult, EvaluationRun
from app.evaluation.repository import RunRepository
from app.evaluation.runner import attempt_row
from app.patching.models import PatchExecution

RESULTS_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "patching" / "results"
HISTORICAL_VERSION = "unknown-historical"


class ImportRejectedError(ValueError):
    pass


def _get(score: dict, *names: str) -> Any:
    for name in names:
        if name in score:
            return score[name]
    return None


def import_result_file(session: Session, path: Path, suite: BenchmarkSuite | None = None) -> tuple[EvaluationRun, bool]:
    """Returns ``(run, created)``. ``created`` is False when this exact file was imported before."""
    suite = suite or load_suite()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    existing = session.scalar(select(EvaluationRun).where(EvaluationRun.source_sha256 == digest))
    if existing is not None:
        return existing, False
    data = json.loads(raw)
    if not isinstance(data, dict) or "scores" not in data or "summary" not in data:
        raise ImportRejectedError(f"{path.name} is not a patching result file (no 'scores' and 'summary')")
    scores: list[dict] = data["scores"]
    summary: dict = data["summary"]
    by_repository = {}
    for ex in data.get("executions", []):
        by_repository.setdefault(ex["repository_id"], []).append(PatchExecution.model_validate(ex))

    models = {e.model for items in by_repository.values() for e in items if e.model}
    modes = {s.get("variant", "live") == "live" for s in scores}
    run = EvaluationRun(
        name=path.stem, status="running", evaluation_mode="live" if modes == {True} else "reference",
        diagnosis_mode="agent" if any(s.get("diagnosis_status") is not None for s in scores) else "recorded",
        configuration_id=None,
        configuration_snapshot={"reconstructed": True, "model": {"model_id": min(models)} if models else {},
                                "note": "Only what the result file recorded; no configuration existed."},
        benchmark_version=HISTORICAL_VERSION,
        selected_case_ids=sorted({s["case_id"] for s in scores}), total_cases=len(scores),
        environment={"imported": True, "source_summary": summary, "rescores": data.get("rescores")},
        is_historical=True, source_filename=path.name, source_sha256=digest, imported_at=datetime.now(UTC))
    repo = RunRepository(session)
    repo.add_run(run)
    for score in scores:
        _add_case(session, run, score, suite, by_repository)
    session.flush()
    _validate(session, run, summary)
    repo.calculate_metrics(run.id, f"initial calculation when importing {path.name}")
    total = session.scalar(select(EvaluationRun.total_cost_usd).where(EvaluationRun.id == run.id))
    costs = [c.total_cost_usd for c in repo.list_cases(run.id, limit=10_000, offset=0)[0]
             if c.total_cost_usd is not None]
    run.total_cost_usd = sum(costs, Decimal(0)) if costs else total
    run.cost_kind = "actual" if costs else None
    run.status = "completed"
    session.flush()
    return run, True


def _add_case(session: Session, run: EvaluationRun, score: dict, suite: BenchmarkSuite,
              by_repository: dict[str, list[PatchExecution]]) -> None:
    case_def = suite.cases.get(score["case_id"])
    executions = by_repository.get(case_def.repository_id, []) if case_def else []
    execution = executions[0] if len(executions) == 1 else None
    attempts_count = score.get("attempts", 0)
    attempted = attempts_count > 0
    statuses = score.get("attempt_statuses") or ([str(a.status) for a in execution.attempts] if execution else [])
    cost = _get(score, "total_cost_usd", "cost_usd")
    seconds = _get(score, "total_seconds", "duration_seconds")
    row = EvaluationCaseResult(
        evaluation_run_id=run.id, case_id=score["case_id"], variant=score.get("variant", "live"),
        category=score.get("category"), status="completed" if attempted else "failed",
        execution_status=score.get("status"), diagnosis_status=score.get("diagnosis_status"),
        diagnosis_correct=score.get("diagnosis_correct"),
        patch_generated=score.get("generated") if attempted else None,
        patch_valid=score.get("validated") if score.get("generated") else None,
        patch_applied=score.get("applied") if score.get("validated") else None,
        first_attempt_verified=(statuses[0] == "verified") if statuses else None,
        final_verified=score.get("verified") if attempted else None,
        repaired_by_retry=score.get("repaired_by_retry") if attempted else None,
        success=score.get("success") if attempted else None,
        heldout_passed=score.get("holdout_passed"), exploratory_passed=score.get("exploratory_passed"),
        regression_detected=(score["attempts_with_regressions"] > 0) if score.get("attempts_applied") else None,
        attempts_count=attempts_count,
        total_cost_usd=None if cost is None else Decimal(str(cost)), cost_kind="actual" if cost is not None else None,
        input_tokens=score.get("input_tokens"), output_tokens=score.get("output_tokens"),
        total_latency_seconds=seconds, failure_reason=score.get("failure_reason"),
        evidence={"imported_score": score, "recorded_fields": sorted(score)})
    session.add(row)
    session.flush()
    if execution is not None and case_def is not None:
        for attempt in execution.attempts:
            session.add(attempt_row(row.id, attempt, case_def, execution))
        final = execution.final_attempt
        if final is not None and score.get("holdout_passed") is not None:
            session.flush()
            last = session.scalar(select(EvaluationAttempt).where(
                EvaluationAttempt.evaluation_case_result_id == row.id,
                EvaluationAttempt.attempt_number == final.attempt_number))
            if last is not None:
                last.heldout_tests_passed = score["holdout_passed"]


def _validate(session: Session, run: EvaluationRun, summary: dict) -> None:
    rows = list(session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.evaluation_run_id == run.id)))
    problems = []
    if len(rows) != summary["cases"]:
        problems.append(f"cases: file says {summary['cases']}, imported {len(rows)}")
    for field, column in (("success_rate", "success"), ("verified_patch_rate", "final_verified")):
        if field in summary and summary[field] is not None:
            got = round(sum(1 for r in rows if getattr(r, column) is True) / len(rows), 4)
            if abs(got - summary[field]) > 1e-4:
                problems.append(f"{field}: file says {summary[field]}, imported rows give {got}")
    if summary.get("total_cost_usd") is not None:
        total = sum((r.total_cost_usd or Decimal(0)) for r in rows)
        if abs(float(total) - summary["total_cost_usd"]) > 1e-5:
            problems.append(f"total_cost_usd: file says {summary['total_cost_usd']}, imported {total}")
    if problems:
        raise ImportRejectedError("; ".join(problems))


def main(argv: list[str]) -> int:
    """``python -m app.evaluation.importer [file ...]``: files inside the results directory; default: all."""
    from app.database import SessionLocal

    names = argv or sorted(p.name for p in RESULTS_DIR.glob("*.json"))
    with SessionLocal() as session:
        for name in names:
            path = (RESULTS_DIR / name).resolve()
            if path.parent != RESULTS_DIR.resolve() or not path.is_file():
                print(f"{name}: not a file in {RESULTS_DIR}", file=sys.stderr)
                return 2
            try:
                run, created = import_result_file(session, path)
            except ImportRejectedError as exc:
                session.rollback()
                print(f"{name}: skipped - {exc}")
                continue
            session.commit()
            print(f"{name}: {'imported as ' + str(run.id) if created else 'already imported (' + str(run.id) + ')'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

