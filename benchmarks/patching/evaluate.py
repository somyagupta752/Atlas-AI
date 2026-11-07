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
from app.patching.evaluation import (
    PatchingBenchmarkCase,
    benchmark_investigation,
    reference_llm,
    run_case,
    score_execution,
    summarize,
)
from app.patching.models import PatchLimits
from app.patching.workspace import WorkspaceManager, snapshot_tree

HERE = Path(__file__).resolve().parent
BENCHMARKS_DIR = ROOT / "benchmarks"


def load_cases(path: Path) -> list[PatchingBenchmarkCase]:
    return [PatchingBenchmarkCase.model_validate(item) for item in json.loads(path.read_text())]


def load_diagnoses(path: Path) -> dict[str, dict]:
    return {item["investigation_case"]: item for item in json.loads(path.read_text())}


def agent_investigation(case, entry, llm):
    """Run the Task 12 agent on the bug report and use its diagnosis (no retrieval index needed)."""
    from app.agents.agent import DebuggingAgent, StaticRepositoryResolver
    from app.agents.schemas import AgentLimits
    from app.agents.tool_registry import create_default_registry
    from app.patching.service import LoadedInvestigation

    root = (ROOT / case.repository_path).resolve()
    agent = DebuggingAgent(
        llm, create_default_registry(),
        StaticRepositoryResolver({case.repository_id: root}, allowed_roots=[BENCHMARKS_DIR]),
        limits=AgentLimits.from_settings(get_settings()), trusted_test_roots=[BENCHMARKS_DIR],
    )
    result = agent.investigate_bug(case.repository_id, entry["bug_report"])
    return LoadedInvestigation(result.investigation_id, case.repository_id, result.status, entry["bug_report"],
                               result), result


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
    parser.add_argument("--max-cost-usd", type=float, help="live mode: stop before a case that could exceed this")
    parser.add_argument("--output", type=Path, help="Write scores and executions as JSON")
    args = parser.parse_args()

    settings = get_settings()
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.case_id in set(args.case)]
    if not cases:
        raise SystemExit("No cases selected")
    diagnoses = load_diagnoses(args.diagnoses)
    limits = PatchLimits.from_settings(settings)
    llm = None
    if args.mode == "live":
        try:
            llm = create_llm_provider(settings)
        except LLMConfigurationError as exc:
            raise SystemExit(f"LLM not available: {exc}") from None

    scores, executions = [], []
    spent = costliest = 0.0
    with tempfile.TemporaryDirectory(prefix="atlas-patch-bench-") as base:
        workspaces = WorkspaceManager(Path(base))
        for case in cases:
            root = (ROOT / case.repository_path).resolve()
            entry = diagnoses[case.investigation_case]
            before = snapshot_tree(root).root_hash
            print(case.case_id, flush=True)
            if args.mode == "reference":
                runs = [(ref.name, ref.expected_status, reference_llm((HERE / ref.file).read_text()))
                        for ref in case.reference_patches]
            else:
                if args.max_cost_usd is not None and spent + 2 * costliest > args.max_cost_usd:
                    print(f"Budget stop before {case.case_id}: spent ${spent:.4f}")
                    break
                runs = [("live", None, llm)]
            for name, expected, model in runs:
                investigation = benchmark_investigation(case, entry)
                if args.mode == "live" and args.diagnosis == "agent":
                    investigation, analysis = agent_investigation(case, entry, llm)
                    print(f"  diagnosis: {analysis.status} - {analysis.root_cause or analysis.summary}")
                    if analysis.status != "completed":
                        continue
                execution = run_case(
                    case, root, model, investigation, workspaces=workspaces, limits=limits,
                    trusted_roots=[BENCHMARKS_DIR],
                    max_attempts=1 if args.mode == "reference" else args.max_attempts,
                    full_suite=not args.no_full_suite,
                )
                score = score_execution(case, execution, variant=name, expected_status=expected)
                scores.append(score)
                executions.append(execution.model_dump(mode="json"))
                cost = score.cost_usd or 0.0
                spent += cost
                costliest = max(costliest, cost)
                mark = "" if expected is None else ("  ok" if score.matches_expectation else f"  MISMATCH (expected {expected})")
                print(f"  {name:<13} {score.status:<17} attempts={score.attempts} files={score.files_modified} "
                      f"+{score.lines_added}/-{score.lines_removed} bug_fixed={score.bug_fixed} "
                      f"{score.duration_seconds:.1f}s ${score.cost_usd}{mark}")
                for reason in score.rejected_reasons[:2]:
                    print(f"      {reason[:160]}")
            if snapshot_tree(root).root_hash != before:
                raise SystemExit(f"{case.case_id}: the original benchmark repository changed")

    if not scores:
        raise SystemExit("No case was scored")
    summary = summarize(scores)
    print("\nSummary")
    for key, value in summary.model_dump().items():
        print(f"  {key:<26} {value}")
    if args.output:
        args.output.write_text(json.dumps({"summary": summary.model_dump(), "scores": [s.model_dump() for s in scores],
                                           "executions": executions}, indent=2, default=str))
    if args.mode == "reference" and summary.expectation_match_rate != 1.0:
        raise SystemExit("Some reference patches did not get their expected verdict")


if __name__ == "__main__":
    main()
