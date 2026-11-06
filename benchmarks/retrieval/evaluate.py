"""Measure retrieval quality: keyword, vector, graph and hybrid, on cases.json.

Usage (from the repository root, with Postgres running and migrated):

    uv run python benchmarks/retrieval/evaluate.py
    uv run python benchmarks/retrieval/evaluate.py --graph neo4j --output results.json

Steps: index the repository (incremental, so reruns are cheap), check that
every expected entity exists in the index, run each case once per strategy,
then print Precision@K, Recall@K and MRR per strategy, and the misses.

The graph comes from an in-memory build by default, so a run needs no Neo4j.
``--graph neo4j`` reads the graph stored in Neo4j instead; build it first with
POST /repositories/graph/build using the same repository ID.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.code_intelligence import analyze_repository
from app.config import get_settings
from app.database import SessionLocal
from app.knowledge_graph.builder import build_code_graph
from app.retrieval.embeddings import create_embedding_provider
from app.retrieval.evaluation import (
    BenchmarkCase,
    CaseScore,
    StrategySummary,
    score_results,
    summarize,
)
from app.retrieval.graph_search import (
    GraphContextSource,
    GraphSearcher,
    InMemoryGraphContextSource,
)
from app.retrieval.indexing import RepositoryIndexer
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.models import RetrievalSource
from app.retrieval.repository import ChunkRepository
from app.retrieval.service import RetrievalService
from app.retrieval.vector_search import VectorSearcher

MODES: dict[str, set[RetrievalSource]] = {
    "keyword": {RetrievalSource.KEYWORD},
    "vector": {RetrievalSource.VECTOR},
    "graph": {RetrievalSource.GRAPH},
    "hybrid": set(RetrievalSource),
}


def load_cases(path: Path) -> list[BenchmarkCase]:
    return [BenchmarkCase.model_validate(item) for item in json.loads(path.read_text())]


def graph_source(kind: str, repository_id: str, repository_path: Path) -> GraphContextSource:
    if kind == "memory":
        return InMemoryGraphContextSource(build_code_graph(analyze_repository(repository_path), repository_id))
    from app.knowledge_graph.neo4j_client import Neo4jConfig, create_driver
    from app.knowledge_graph.neo4j_repository import Neo4jGraphRepository

    config = Neo4jConfig.from_settings(get_settings())
    if config is None:
        raise SystemExit("NEO4J_* settings are required for --graph neo4j")
    return Neo4jGraphRepository(create_driver(config), config.database)


def run(args: argparse.Namespace) -> dict:
    cases = [c for c in load_cases(args.cases) if c.repository_id == args.repository_id]
    ks = sorted(set(args.k))
    provider = create_embedding_provider(get_settings())

    with SessionLocal() as session:
        chunks = ChunkRepository(session)
        index = RepositoryIndexer(chunks, provider).index_repository(args.repository_id, args.repository_path)
        print(
            f"Indexed {index.chunks_total} chunks from {index.files_indexed} files "
            f"(v{index.index_version}, {index.embeddings_generated} embedded, "
            f"{index.embeddings_reused} reused, model {index.embedding_model})"
        )

        expected = {e for case in cases for e in case.expected_chunks}
        present = {c.entity_id for c in chunks.chunks_for_entities(args.repository_id, expected)}
        stale = sorted(expected - present)
        if stale:
            raise SystemExit("Expected entities missing from the index (stale cases?):\n  " + "\n  ".join(stale))

        source = graph_source(args.graph, args.repository_id, args.repository_path)
        service = RetrievalService(
            chunks,
            KeywordSearcher(session),
            VectorSearcher(session, provider),
            GraphSearcher(source, chunks),
        )

        per_mode: dict[str, list[CaseScore]] = {}
        for mode, strategies in MODES.items():
            per_mode[mode] = []
            for case in cases:
                response = service.retrieve_code(case.repository_id, case.query, max(ks), strategies)
                per_mode[mode].append(score_results(case, response.results, ks))
            session.rollback()  # end the read transaction between modes

    summaries = [summarize(mode, scores, ks) for mode, scores in per_mode.items()]
    print_report(cases, per_mode, summaries, ks)
    return {
        "repository_id": args.repository_id,
        "index": index.model_dump(),
        "ks": ks,
        "summary": [s.model_dump() for s in summaries],
        "cases": {mode: [s.model_dump() for s in scores] for mode, scores in per_mode.items()},
    }


def print_report(
    cases: list[BenchmarkCase],
    per_mode: dict[str, list[CaseScore]],
    summaries: list[StrategySummary],
    ks: list[int],
) -> None:
    header = f"{'strategy':<9}" + "".join(f"  P@{k:<3} R@{k:<3}" for k in ks) + "   MRR"
    print(f"\n{len(cases)} cases\n" + header + "\n" + "-" * len(header))
    for s in summaries:
        cells = "".join(f"  {s.precision[k]:.2f}  {s.recall[k]:.2f} " for k in ks)
        print(f"{s.strategy:<9}{cells}  {s.mrr:.3f}")

    print("\nFirst relevant rank per case (- = not in top results):")
    print(f"{'case':<30}{'category':<18}" + "".join(f"{m:>9}" for m in per_mode))
    for i, case in enumerate(cases):
        ranks = [per_mode[m][i].first_relevant_rank for m in per_mode]
        print(f"{case.id:<30}{case.category:<18}" + "".join(f"{r if r else '-':>9}" for r in ranks))

    hybrid = {s.case_id: s for s in per_mode["hybrid"]}
    misses = [(c, hybrid[c.id].missing) for c in cases if hybrid[c.id].missing]
    if misses:
        print(f"\nHybrid misses at K={max(ks)}:")
        for case, missing in misses:
            print(f"  {case.id}: " + ", ".join(m.split("::")[-1] if "::" in m else m for m in missing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--repository-path", type=Path, default=ROOT)
    parser.add_argument("--repository-id", default="atlas-ai")
    parser.add_argument("--graph", choices=["memory", "neo4j"], default="memory")
    parser.add_argument("-k", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument("--output", type=Path, help="write the full results as JSON")
    args = parser.parse_args()
    results = run(args)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2, default=str))
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
