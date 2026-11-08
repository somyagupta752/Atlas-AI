"""Hybrid retrieval orchestration. Deterministic: no LLM anywhere in this path.

``retrieve_code`` runs:

1. Validate the repository ID, query and limit; require a completed index.
2. Keyword retrieval (identifiers + full text).
3. Vector retrieval (cosine similarity).
4. Graph seeds: entities named in the query, plus the top keyword/vector hits.
5. Graph expansion around those seeds.
6. Reciprocal Rank Fusion of the three lists.
7. Final ranking: overlap folding and evidence packaging.

Each strategy is optional and independently callable, so benchmarks can
measure every strategy alone and the hybrid. A strategy that cannot run (graph
store down, embedding provider failing) is reported in ``strategies`` and the
others still return results.
"""

import logging
import time
from collections.abc import Collection

from app.core import metrics
from app.knowledge_graph.models import is_valid_repository_id
from app.knowledge_graph.repository import GraphStoreError
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.fusion import DEFAULT_RRF_K, reciprocal_rank_fusion
from app.retrieval.graph_search import DEFAULT_MAX_DEPTH, GraphSearcher
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.models import (
    RetrievalResponse,
    RetrievalResult,
    RetrievalSource,
    StrategyReport,
)
from app.retrieval.ranker import rank_evidence
from app.retrieval.repository import ChunkRepository
from app.retrieval.vector_search import VectorSearcher

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 4000
MAX_LIMIT = 50
GRAPH_SEEDS_FROM_OTHER_STRATEGIES = 5
ALL_STRATEGIES = frozenset(RetrievalSource)


class InvalidRetrievalRequestError(ValueError):
    """The repository ID, query or limit is invalid."""


class RepositoryNotIndexedError(Exception):
    """The repository has no completed index."""


def candidate_pool_size(limit: int) -> int:
    """How many candidates each strategy contributes before fusion."""
    return max(limit * 3, 20)


class RetrievalService:
    def __init__(
        self,
        chunks: ChunkRepository,
        keyword: KeywordSearcher,
        vector: VectorSearcher | None = None,
        graph: GraphSearcher | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        graph_max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self._chunks = chunks
        self._keyword = keyword
        self._vector = vector
        self._graph = graph
        self._rrf_k = rrf_k
        self._graph_max_depth = graph_max_depth

    def retrieve_code(
        self,
        repository_id: str,
        query: str,
        limit: int = 10,
        strategies: Collection[RetrievalSource] = ALL_STRATEGIES,
    ) -> RetrievalResponse:
        started = time.perf_counter()
        outcome = "ok"
        try:
            return self._retrieve_code(repository_id, query, limit, strategies)
        except Exception:
            outcome = "error"
            raise
        finally:
            metrics.RETRIEVAL_LATENCY.labels(outcome).observe(time.perf_counter() - started)

    def _retrieve_code(self, repository_id: str, query: str, limit: int,
                       strategies: Collection[RetrievalSource]) -> RetrievalResponse:
        query = _validate(repository_id, query, limit)
        state = self._chunks.get_index_state(repository_id)
        if state is None or not state.is_searchable:
            raise RepositoryNotIndexedError(f"Repository {repository_id!r} has not been indexed")

        pool = candidate_pool_size(limit)
        lists: dict[RetrievalSource, list[RetrievalResult]] = {}
        reports: list[StrategyReport] = []

        if RetrievalSource.KEYWORD in strategies:
            lists[RetrievalSource.KEYWORD] = self._keyword.search(repository_id, query, pool)
            reports.append(_ok(RetrievalSource.KEYWORD, lists[RetrievalSource.KEYWORD]))
        else:
            reports.append(_skipped(RetrievalSource.KEYWORD))

        reports.append(self._run_vector(repository_id, query, pool, strategies, lists))
        reports.append(self._run_graph(repository_id, query, pool, strategies, lists))

        fused = reciprocal_rank_fusion(lists, k=self._rrf_k)
        results = rank_evidence(fused, limit)
        return RetrievalResponse(
            query=query,
            repository_id=repository_id,
            results=results,
            total_results=len(results),
            strategies=reports,
            index_version=state.index_version,
        )

    # --- strategies ----------------------------------------------------------

    def _run_vector(
        self,
        repository_id: str,
        query: str,
        pool: int,
        strategies: Collection[RetrievalSource],
        lists: dict[RetrievalSource, list[RetrievalResult]],
    ) -> StrategyReport:
        source = RetrievalSource.VECTOR
        if source not in strategies:
            return _skipped(source)
        if self._vector is None:
            return StrategyReport(source=source, status="unavailable", detail="No embedding provider")
        try:
            lists[source] = self._vector.search(repository_id, query, pool)
        except EmbeddingError as exc:
            logger.warning("Vector retrieval failed: %s", exc)
            return StrategyReport(source=source, status="error", detail=str(exc))
        return _ok(source, lists[source])

    def _run_graph(
        self,
        repository_id: str,
        query: str,
        pool: int,
        strategies: Collection[RetrievalSource],
        lists: dict[RetrievalSource, list[RetrievalResult]],
    ) -> StrategyReport:
        source = RetrievalSource.GRAPH
        if source not in strategies:
            return _skipped(source)
        if self._graph is None:
            return StrategyReport(source=source, status="unavailable", detail="No graph store")
        seeds = _external_seeds(lists)
        try:
            outcome = self._graph.search(
                repository_id, query, pool, seed_entity_ids=seeds, max_depth=self._graph_max_depth
            )
        except GraphStoreError as exc:
            logger.warning("Graph retrieval failed: %s", exc)
            return StrategyReport(source=source, status="unavailable", detail="Graph store unavailable")
        lists[source] = outcome.results
        detail = f"{len(outcome.seeds)} seed(s)"
        if outcome.entities_without_chunks:
            detail += f"; {len(outcome.entities_without_chunks)} related entities have no indexed chunk"
        return StrategyReport(source=source, status="ok", candidates=len(outcome.results), detail=detail)


def _external_seeds(lists: dict[RetrievalSource, list[RetrievalResult]]) -> list[str]:
    """Top keyword/vector hits, fused, as graph seeds (entity IDs, best first)."""
    seed_lists = {s: r for s, r in lists.items() if s != RetrievalSource.GRAPH}
    if not seed_lists:
        return []
    seeds: list[str] = []
    for candidate in reciprocal_rank_fusion(seed_lists):
        entity_id = candidate.representative.entity_id
        if entity_id and entity_id not in seeds:
            seeds.append(entity_id)
        if len(seeds) == GRAPH_SEEDS_FROM_OTHER_STRATEGIES:
            break
    return seeds


def _validate(repository_id: str, query: str, limit: int) -> str:
    if not is_valid_repository_id(repository_id):
        raise InvalidRetrievalRequestError(f"Invalid repository id: {repository_id!r}")
    if not isinstance(query, str) or not query.strip():
        raise InvalidRetrievalRequestError("Query must not be empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidRetrievalRequestError(f"Query must be at most {MAX_QUERY_LENGTH} characters")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRetrievalRequestError(f"limit must be between 1 and {MAX_LIMIT}")
    return query.strip()


def _ok(source: RetrievalSource, results: list[RetrievalResult]) -> StrategyReport:
    return StrategyReport(source=source, status="ok", candidates=len(results))


def _skipped(source: RetrievalSource) -> StrategyReport:
    return StrategyReport(source=source, status="skipped")
