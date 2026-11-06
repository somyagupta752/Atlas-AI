"""Reciprocal Rank Fusion (RRF) across retrieval strategies.

Strategy scores live on different scales: ``ts_rank`` plus identifier boosts,
cosine similarity in [-1, 1], and graph path weights. Adding them would let
whichever scale is largest dominate. RRF uses only each candidate's *rank* in
each list::

    RRF(d) = sum over strategies r of  weight_r / (k + rank_r(d))

``k`` (60 by default) dampens the gap between rank 1 and rank 2, so one
strategy's top hit cannot outweigh agreement between strategies. A candidate
missing from a strategy's list contributes nothing for that strategy. Weights
default to 1.0 for every strategy.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.retrieval.models import (
    GraphEvidence,
    RetrievalResult,
    RetrievalSource,
    StrategyRank,
    UnresolvedEvidence,
)

DEFAULT_RRF_K = 60


@dataclass
class FusedCandidate:
    chunk_id: str
    representative: RetrievalResult  # content and location; identical across strategies
    ranks: dict[RetrievalSource, int] = field(default_factory=dict)
    scores: dict[RetrievalSource, float] = field(default_factory=dict)
    rrf_score: float = 0.0
    match_reasons: list[str] = field(default_factory=list)
    graph_evidence: list[GraphEvidence] = field(default_factory=list)
    unresolved_references: list[UnresolvedEvidence] = field(default_factory=list)

    @property
    def sources(self) -> list[RetrievalSource]:
        return [source for source in RetrievalSource if source in self.ranks]

    @property
    def best_rank(self) -> int:
        return min(self.ranks.values())

    def strategy_ranks(self) -> list[StrategyRank]:
        return [
            StrategyRank(source=source, rank=self.ranks[source], score=self.scores[source])
            for source in self.sources
        ]


def reciprocal_rank_fusion(
    result_lists: Mapping[RetrievalSource, Sequence[RetrievalResult]],
    k: int = DEFAULT_RRF_K,
    weights: Mapping[RetrievalSource, float] | None = None,
) -> list[FusedCandidate]:
    """Merge strategy lists into one list, deduplicated by chunk ID, best first.

    Each list is taken in the order given; a chunk repeated within one list
    keeps its first (best) position. Ties are broken by best single rank, then
    by chunk ID, so the output is deterministic.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    candidates: dict[str, FusedCandidate] = {}
    for source, results in result_lists.items():
        weight = 1.0 if weights is None else weights.get(source, 1.0)
        rank = 0
        seen: set[str] = set()
        for result in results:
            if result.chunk_id in seen:
                continue
            seen.add(result.chunk_id)
            rank += 1
            candidate = candidates.get(result.chunk_id)
            if candidate is None:
                candidate = FusedCandidate(chunk_id=result.chunk_id, representative=result)
                candidates[result.chunk_id] = candidate
            candidate.ranks[source] = rank
            candidate.scores[source] = result.score
            candidate.rrf_score += weight / (k + rank)
            for reason in result.match_reasons:
                if reason not in candidate.match_reasons:
                    candidate.match_reasons.append(reason)
            candidate.graph_evidence.extend(result.graph_evidence)
            for ref in result.unresolved_references:
                if ref not in candidate.unresolved_references:
                    candidate.unresolved_references.append(ref)
    return sorted(
        candidates.values(), key=lambda c: (-c.rrf_score, c.best_rank, c.chunk_id)
    )
