"""Run the debugging agent on the benchmark bugs and measure it.

Usage (from the repository root, with Postgres running and migrated):

    uv run python benchmarks/agent/evaluate.py --dry-run     # check cases, index repos; no LLM
    uv run python benchmarks/agent/evaluate.py               # real investigations (costs money)
    uv run python benchmarks/agent/evaluate.py --case bug-003-pagination --output results.json

A real run needs LLM_ALLOW_EXTERNAL=true and ANTHROPIC_API_KEY. Each case:

1. Index the case's repository (local hashing embeddings, incremental).
2. Check that every expected file and symbol exists, so a stale case fails loudly.
3. Investigate with only the bug report. The expected answer never reaches the agent.
4. Score root cause, evidence precision and coverage, tool use, latency and cost.

Tests may run: the benchmark directory is passed as the only trusted test root.
The call graph is built in memory from the case's files.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agents.agent import (
    DebuggingAgent,
    SessionScopedRetrieval,
    StaticRepositoryResolver,
)
from app.agents.evaluation import (
    CaseScore,
    DebuggingBenchmarkCase,
    score_case,
    summarize,
)
from app.agents.events import EventType, InMemoryEventSink
from app.agents.llm import LLMConfigurationError, create_llm_provider
from app.agents.schemas import AgentLimits
from app.agents.tool_registry import create_default_registry
from app.code_intelligence import analyze_repository
from app.config import get_settings
from app.database import SessionLocal
from app.knowledge_graph.builder import build_code_graph
from app.retrieval.embeddings import create_embedding_provider
from app.retrieval.graph_search import InMemoryGraphContextSource
from app.retrieval.indexing import RepositoryIndexer
from app.retrieval.repository import ChunkRepository
from app.tools.get_symbol import symbol_matches

BENCHMARKS_DIR = ROOT / "benchmarks"


def load_cases(path: Path) -> list[DebuggingBenchmarkCase]:
    return [DebuggingBenchmarkCase.model_validate(item) for item in json.loads(path.read_text())]


def check_case(case: DebuggingBenchmarkCase, root: Path) -> None:
    for file_path in case.expected_files:
        if not (root / file_path).is_file():
            raise SystemExit(f"{case.case_id}: expected file {file_path} does not exist")
    analysis = analyze_repository(root)
    names = [
        (entity.qualified_name, entity.name, parsed.file_path)
        for parsed in analysis.files
        for entity in [*parsed.functions, *parsed.classes]
    ]
    for symbol in case.expected_symbols:
        if not any(symbol_matches(symbol, q, n, f) for q, n, f in names):
            raise SystemExit(f"{case.case_id}: expected symbol {symbol} does not exist")


def index(case: DebuggingBenchmarkCase, root: Path, provider) -> None:
    with SessionLocal() as session:
        result = RepositoryIndexer(ChunkRepository(session), provider).index_repository(case.repository_id, root)
    print(f"  indexed {case.repository_id}: {result.chunks_total} chunks")


def investigate(case: DebuggingBenchmarkCase, root: Path, llm, provider, limits: AgentLimits) -> tuple[CaseScore, object]:
    graph = InMemoryGraphContextSource(build_code_graph(analyze_repository(root), case.repository_id))
    agent = DebuggingAgent(
        llm,
        create_default_registry(),
        StaticRepositoryResolver({case.repository_id: root}, allowed_roots=[BENCHMARKS_DIR]),
        limits=limits,
        retrieval=SessionScopedRetrieval(SessionLocal, provider, graph),
        graph=None,  # call-graph tools build the same graph in memory
        trusted_test_roots=[BENCHMARKS_DIR],
    )
    result = agent.investigate_bug(case.repository_id, case.bug_report)
    events = agent.last_events
    assert isinstance(events, InMemoryEventSink)
    contents = {p["id"]: p["content"] for t, p in events.events if t == EventType.OBSERVATION}
    return score_case(case, result, contents), result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--case", action="append", help="Run only this case ID (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and index only; no LLM calls")
    parser.add_argument("--output", type=Path, help="Write scores and results as JSON")
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        help="Stop before a case when spent + 2x the costliest case so far would exceed this",
    )
    args = parser.parse_args()

    settings = get_settings()
    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.case_id in set(args.case)]
    if not cases:
        raise SystemExit("No cases selected")
    provider = create_embedding_provider(settings)

    llm = None
    if not args.dry_run:
        try:
            llm = create_llm_provider(settings)
        except LLMConfigurationError as exc:
            raise SystemExit(f"LLM not available: {exc}") from None
    limits = AgentLimits.from_settings(settings)

    scores: list[CaseScore] = []
    results = []
    spent = 0.0
    costliest = 0.0
    for case in cases:
        if llm is not None and args.max_cost_usd is not None and spent + 2 * costliest > args.max_cost_usd:
            print(f"Budget stop before {case.case_id}: spent ${spent:.4f} of ${args.max_cost_usd:.2f}")
            break
        root = (ROOT / case.repository_path).resolve()
        print(f"{case.case_id}", flush=True)
        check_case(case, root)
        index(case, root, provider)
        if llm is None:
            continue
        score, result = investigate(case, root, llm, provider, limits)
        scores.append(score)
        results.append(result.model_dump(mode="json"))
        case_cost = score.cost_usd or 0.0
        spent += case_cost
        costliest = max(costliest, case_cost)
        print(
            f"  {score.status:<13} correct={score.root_cause_correct!s:<5} coverage={score.evidence_coverage:.2f} "
            f"precision={score.evidence_precision} tools={score.tool_calls} iters={score.iterations} "
            f"{score.duration_seconds:.1f}s ${score.cost_usd} (total ${spent:.4f})"
        )
        print(f"  root cause: {result.root_cause or '-'}")
        if result.failure_reason:
            print(f"  failure: {result.failure_reason}")
        if score.missing_evidence:
            print(f"  missing evidence: {', '.join(score.missing_evidence)}")
        print(flush=True)

    if not scores:
        print("Dry run complete: cases are valid and repositories are indexed.")
        return
    summary = summarize(scores)
    print("\nSummary")
    for key, value in summary.model_dump().items():
        print(f"  {key:<26} {value}")
    if args.output:
        args.output.write_text(
            json.dumps(
                {"summary": summary.model_dump(), "scores": [s.model_dump() for s in scores], "results": results},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
