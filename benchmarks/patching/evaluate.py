"""Measure patch generation and verification on the benchmark bugs.

Usage (from the repository root):

    uv run python benchmarks/patching/evaluate.py                 # reference patches: free, no LLM, no DB
    uv run python benchmarks/patching/evaluate.py --mode live     # real model (costs money)
    uv run python benchmarks/patching/evaluate.py --mode live --diagnosis agent   # Task 12 agent first
    uv run python benchmarks/patching/evaluate.py --case bug-003-pagination --output results.json

Reference mode feeds each case's fixed patches (correct, no-fix, regression,
syntax error, test tampering, out of scope) through the full pipeline and checks
that each gets its expected verdict. It tests the verifier, not a model.

Live mode asks the configured model for a patch. The model's input is the
diagnosis (``diagnoses.json``, or a fresh Task 12 investigation with
``--diagnosis agent``) and the source. ``cases.json`` holds the expected
answers and never reaches the model. A real run needs LLM_ALLOW_EXTERNAL=true
and ANTHROPIC_API_KEY.

Tests run only in workspace copies; the benchmark directory is the only trusted
test root. The original benchmark files are hashed before and after every case.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agents.llm import LLMConfigurationError, create_llm_provider
from app.config import get_settings
from app.patching.budget import LLMBudget
from app.patching.evaluation import (
    PatchingBenchmarkCase,
    benchmark_investigation,
    cost_usd,
    reference_llm,
    run_case,
    run_exploratory,
    run_holdout,
    score_execution,
    score_without_patch,
    summarize,
)
from app.patching.models import PatchLimits
from app.patching.service import InvestigationNotEligibleError
from app.patching.workspace import WorkspaceManager, snapshot_tree

HERE = Path(__file__).resolve().parent
BENCHMARKS_DIR = ROOT / "benchmarks"


def load_cases(path: Path) -> list[PatchingBenchmarkCase]:
    return [PatchingBenchmarkCase.model_validate(item) for item in json.loads(path.read_text())]


def load_diagnoses(path: Path) -> dict[str, dict]:
    return {item["investigation_case"]: item for item in json.loads(path.read_text())}


def agent_investigation(case, entry, llm, agent_cases):
    """Run the Task 12 agent on the bug report; return its diagnosis and how good it was.

    Correctness uses the Task 12 benchmark's root-cause keywords (benchmarks/agent/cases.json).
    The agent receives only the bug report.
    """
    import time as _time

    from app.agents.agent import DebuggingAgent, StaticRepositoryResolver
    from app.agents.evaluation import root_cause_matches
    from app.agents.schemas import AgentLimits
    from app.agents.tool_registry import create_default_registry
    from app.patching.service import LoadedInvestigation

    root = (ROOT / case.repository_path).resolve()
    agent = DebuggingAgent(
        llm, create_default_registry(),
        StaticRepositoryResolver({case.repository_id: root}, allowed_roots=[BENCHMARKS_DIR]),
        limits=AgentLimits.from_settings(get_settings()), trusted_test_roots=[BENCHMARKS_DIR],
    )
    started = _time.monotonic()
    result = agent.investigate_bug(case.repository_id, entry["bug_report"])
    expected = agent_cases.get(case.investigation_case)
    diagnosis = {
        "status": result.status,
        "correct": (result.status == "completed" and expected is not None
                    and root_cause_matches(result.root_cause, expected.root_cause_keywords)),
        "cost_usd": cost_usd(result.model, result.usage),
        "seconds": round(_time.monotonic() - started, 3),
        "root_cause": result.root_cause,
        "affected_files": result.affected_files,
    }
    loaded = LoadedInvestigation(result.investigation_id, case.repository_id, result.status, entry["bug_report"],
                                 result)
    return loaded, result, diagnosis


def rescore(path: Path, cases: list[PatchingBenchmarkCase], limits: PatchLimits) -> None:
    """Rescore a saved live run with the current case definitions. An audit entry keeps the old summary."""
    from datetime import UTC, datetime

    from app.patching.evaluation import PatchCaseScore
    from app.patching.models import PatchExecution

    data = json.loads(path.read_text())
    by_id = {c.case_id: c for c in cases}
    executions = {e["repository_id"]: PatchExecution.model_validate(e) for e in data["executions"]}
    scores, changes = [], []
    with tempfile.TemporaryDirectory(prefix="atlas-patch-rescore-") as base:
        workspaces = WorkspaceManager(Path(base))
        for old in data["scores"]:
            case = by_id[old["case_id"]]
            execution = executions.get(case.repository_id)
            if execution is None:  # never reached patching: nothing to re-run
                scores.append(PatchCaseScore.model_validate(
                    {k: v for k, v in old.items() if k not in ("total_cost_usd", "total_seconds")}))
                continue
            root = (ROOT / case.repository_path).resolve()
            before = snapshot_tree(root).root_hash
            holdout = run_holdout(case, root, execution, HERE, workspaces=workspaces, limits=limits)
            exploratory = run_exploratory(case, root, execution, HERE, workspaces=workspaces, limits=limits)
            if snapshot_tree(root).root_hash != before:
                raise SystemExit(f"{case.case_id}: the original benchmark repository changed")
            diagnosis = None
            if old.get("diagnosis_status") is not None:
                diagnosis = {"status": old["diagnosis_status"], "correct": old["diagnosis_correct"],
                             "cost_usd": old["diagnosis_cost_usd"], "seconds": old["diagnosis_seconds"]}
            score = score_execution(case, execution, variant=old["variant"], holdout=holdout,
                                    exploratory=exploratory, diagnosis=diagnosis)
            if (old.get("holdout_passed"), old.get("success")) != (score.holdout_passed, score.success):
                changes.append(f"  {case.case_id}: holdout {old.get('holdout_passed')} -> {score.holdout_passed}, "
                               f"success {old.get('success')} -> {score.success}, "
                               f"exploratory {score.exploratory_passed}")
            scores.append(score)
    summary = summarize(scores)
    data.setdefault("rescores", []).append({
        "at": datetime.now(UTC).isoformat(),
        "reason": "Held-out tests check only specified behaviour. bug-008's trailing-comma test moved to an "
                  "exploratory test (reported, never scored). Patches unchanged; no model calls.",
        "previous_summary": data["summary"],
    })
    data["summary"] = summary.model_dump()
    data["scores"] = [{**s.model_dump(), "total_cost_usd": s.total_cost_usd, "total_seconds": s.total_seconds}
                      for s in scores]
    path.write_text(json.dumps(data, indent=2, default=str))
    print(path.name)
    print("\n".join(changes) or "  no per-case changes")
    previous = data["rescores"][-1]["previous_summary"]
    for key in ("holdout_pass_rate", "holdout_cases", "exploratory_pass_rate", "success_rate",
                "verified_patch_rate", "total_cost_usd"):
        print(f"  {key:<24} {previous.get(key)} -> {getattr(summary, key)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["reference", "live"], default="reference")
    parser.add_argument("--diagnosis", choices=["recorded", "agent"], default="recorded",
                        help="live mode: recorded diagnoses or a fresh Task 12 investigation")
    parser.add_argument("--cases", type=Path, default=HERE / "cases.json")
    parser.add_argument("--diagnoses", type=Path, default=HERE / "diagnoses.json")
    parser.add_argument("--case", action="append", help="Run only this case ID (repeatable)")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--no-full-suite", action="store_true")
    parser.add_argument("--context-tests", type=int, metavar="N",
                        help="Test files whose contents the model sees (0 = test-blind; tests still run). "
                             "Default: PatchLimits.max_context_test_files")
    parser.add_argument("--test-backend", choices=["local", "container"], default="local",
                        help="where repository tests run (container: docker/test-runner.Dockerfile image)")
    parser.add_argument("--max-cost-usd", type=float,
                        help="live mode: hard cap for the whole run, shared by every model call (reserved before each)")
    parser.add_argument("--output", type=Path, help="Write scores and executions as JSON")
    parser.add_argument("--rescore", type=Path, metavar="RESULTS_JSON",
                        help="Recompute held-out, exploratory and summary metrics for a saved run in place. "
                             "Re-applies each recorded final patch; no model calls; patches are not changed.")
    args = parser.parse_args()

    settings = get_settings()
    if args.rescore:
        rescore(args.rescore, load_cases(args.cases), PatchLimits.from_settings(settings))
        return
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.case_id in set(args.case)]
    if not cases:
        raise SystemExit("No cases selected")
    diagnoses = load_diagnoses(args.diagnoses)
    from app.agents.evaluation import DebuggingBenchmarkCase

    agent_cases = {c["case_id"]: DebuggingBenchmarkCase.model_validate(c)
                   for c in json.loads((BENCHMARKS_DIR / "agent" / "cases.json").read_text())}
    limits = PatchLimits.from_settings(settings)
    if args.context_tests is not None:
        limits = limits.model_copy(update={"max_context_test_files": args.context_tests})
    print(f"context test files shown to the model: {limits.max_context_test_files}")
    llm = None
    if args.mode == "live":
        try:
            llm = create_llm_provider(settings)
        except LLMConfigurationError as exc:
            raise SystemExit(f"LLM not available: {exc}") from None

    engine = None
    if args.test_backend == "container":
        from app.patching.container import ContainerLimits, ContainerTestRunner
        from app.patching.verification import VerificationEngine

        runner = ContainerTestRunner(limits, ContainerLimits(image=settings.patch_container_image))
        if not runner.available():
            raise SystemExit(f"Container image {settings.patch_container_image} is not available; build it first")
        engine = VerificationEngine(limits, runner=runner)
    scores, executions = [], []
    # One budget for the run: every patch call reserves its worst case against it first.
    shared = None
    if args.mode == "live" and args.max_cost_usd is not None:
        shared = LLMBudget(max_requests=10_000, max_seconds=86_400, max_cost_usd=args.max_cost_usd)
    with tempfile.TemporaryDirectory(prefix="atlas-patch-bench-") as base:
        workspaces = WorkspaceManager(Path(base))
        for case in cases:
            root = (ROOT / case.repository_path).resolve()
            entry = diagnoses[case.investigation_case]
            before = snapshot_tree(root).root_hash
            print(case.case_id, flush=True)
            if args.mode == "reference":
                runs = [(ref, reference_llm([(HERE / f).read_text() for f in ref.diff_files]))
                        for ref in case.reference_patches]
            else:
                runs = [(None, llm)]
            for ref, model in runs:
                name = ref.name if ref else "live"
                investigation = benchmark_investigation(case, entry)
                diagnosis = None
                if args.mode == "live" and args.diagnosis == "agent":
                    investigation, analysis, diagnosis = agent_investigation(case, entry, llm, agent_cases)
                    if shared is not None:
                        shared.charge(diagnosis["cost_usd"] or 0.0, f"{case.case_id} diagnosis")
                    print(f"  diagnosis: {analysis.status} correct={diagnosis['correct']} "
                          f"{diagnosis['seconds']:.1f}s ${diagnosis['cost_usd']} - {analysis.root_cause or analysis.summary}")
                    if analysis.status != "completed":
                        scores.append(score_without_patch(case, f"diagnosis_{analysis.status}", diagnosis=diagnosis))
                        continue
                attempts = (len(ref.diff_files) if ref else args.max_attempts)
                try:
                    execution = run_case(
                        case, root, model, investigation, workspaces=workspaces, limits=limits,
                        trusted_roots=[BENCHMARKS_DIR], max_attempts=attempts,
                        full_suite=not args.no_full_suite, llm_budget=shared, engine=engine,
                    )
                except InvestigationNotEligibleError as exc:
                    print(f"  not eligible: {exc}")
                    scores.append(score_without_patch(case, "not_eligible", variant=name, diagnosis=diagnosis,
                                                      reason=str(exc)))
                    continue
                holdout = run_holdout(case, root, execution, HERE, workspaces=workspaces, limits=limits,
                                      runner=engine.runner if engine else None)
                exploratory = run_exploratory(case, root, execution, HERE, workspaces=workspaces, limits=limits,
                                              runner=engine.runner if engine else None)
                score = score_execution(
                    case, execution, variant=name,
                    expected_status=ref.expected_status if ref else None,
                    expected_attempts=ref.expected_attempts if ref else None,
                    expected_holdout=ref.expected_holdout if ref else None,
                    holdout=holdout, exploratory=exploratory, diagnosis=diagnosis,
                )
                scores.append(score)
                executions.append(execution.model_dump(mode="json"))
                mark = "" if ref is None else ("  ok" if score.matches_expectation else
                                               f"  MISMATCH (expected {ref.expected_status})")
                print(f"  {name:<18} {score.status:<17} attempts={'>'.join(score.attempt_statuses) or 0} "
                      f"files={score.files_modified} +{score.lines_added}/-{score.lines_removed} "
                      f"bug_fixed={score.bug_fixed} holdout={score.holdout_passed} "
                      f"{'' if score.exploratory_passed is None else f'exploratory={score.exploratory_passed} '}"
                      f"success={score.success} "
                      f"{score.duration_seconds:.1f}s ${score.cost_usd}{mark}")
                for reason in score.rejected_reasons[:3]:
                    print("      " + " | ".join(line.strip() for line in reason.splitlines() if line.strip())[:220])
            if snapshot_tree(root).root_hash != before:
                raise SystemExit(f"{case.case_id}: the original benchmark repository changed")
            if executions and executions[-1].get("stop_reason") == "llm_budget_exhausted" and shared is not None:
                print(f"Budget stop after {case.case_id}: ${shared.cost_spent:.4f} of ${args.max_cost_usd:.2f} spent")
                break

    if not scores:
        raise SystemExit("No case was scored")
    summary = summarize(scores)
    print("\nSummary")
    for key, value in summary.model_dump().items():
        if key != "by_category":
            print(f"  {key:<28} {value}")
    for category, values in summary.by_category.items():
        print(f"  {category:<28} {values}")
    if args.output:
        args.output.write_text(json.dumps({"summary": summary.model_dump(),
                                           "scores": [{**s.model_dump(), "total_cost_usd": s.total_cost_usd,
                                                       "total_seconds": s.total_seconds} for s in scores],
                                           "executions": executions}, indent=2, default=str))
    if args.mode == "reference" and summary.expectation_match_rate != 1.0:
        raise SystemExit("Some reference patches did not get their expected verdict")


if __name__ == "__main__":
    main()
