"""Turn fused candidates into the final, deduplicated evidence list.

- Order: the fused RRF order, unchanged. The ranker does not re-score.
- Overlap: a candidate whose lines lie entirely inside an already selected,
  higher-ranked chunk of the same file adds no new code. It is folded into
  that chunk's ``merged`` list, with its name, lines and reason, instead of
  being returned again. A container ranked *below* its child is kept,
  because it adds surrounding context (for example the ``__init__`` that
  sets ``self.repository``).
- Reasons describe why a chunk was retrieved. They never say a chunk is the
  cause of a bug: retrieval relevance is not causation.
"""

from app.retrieval.fusion import FusedCandidate
from app.retrieval.models import MergedEvidence, RetrievalSource, RetrievedEvidence

MAX_GRAPH_REASONS = 2


def rank_evidence(
    candidates: list[FusedCandidate], limit: int, merge_contained: bool = True
) -> list[RetrievedEvidence]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    selected: list[RetrievedEvidence] = []
    for candidate in candidates:
        if merge_contained:
            container = _container(selected, candidate)
            if container is not None:
                result = candidate.representative
                container.merged.append(
                    MergedEvidence(
                        chunk_id=candidate.chunk_id,
                        qualified_name=result.qualified_name,
                        start_line=result.start_line,
                        end_line=result.end_line,
                        retrieval_sources=candidate.sources,
                        reason=explain(candidate),
                    )
                )
                continue
        if len(selected) >= limit:
            break
        selected.append(_evidence(candidate))
    return selected


def explain(candidate: FusedCandidate) -> str:
    """One sentence on why the chunk was retrieved, from its provenance only."""
    parts: list[str] = []
    reasons = candidate.match_reasons
    if RetrievalSource.KEYWORD in candidate.ranks:
        keyword = [r for r in reasons if r.startswith(("exact identifier", "full-text"))]
        parts.append("; ".join(keyword) if keyword else "keyword match")
    if RetrievalSource.VECTOR in candidate.ranks:
        similarity = candidate.scores[RetrievalSource.VECTOR]
        rank = candidate.ranks[RetrievalSource.VECTOR]
        parts.append(f"semantic similarity {similarity:.2f} (vector rank {rank})")
    if RetrievalSource.GRAPH in candidate.ranks:
        graph = [r for r in reasons if r.startswith("graph")][:MAX_GRAPH_REASONS]
        parts.append("; ".join(graph) if graph else "graph relationship")
    sources = "+".join(str(s) for s in candidate.sources)
    return f"Retrieved by {sources}: " + "; ".join(parts)


def _container(selected: list[RetrievedEvidence], candidate: FusedCandidate) -> RetrievedEvidence | None:
    result = candidate.representative
    for evidence in selected:
        if (
            evidence.file_path == result.file_path
            and evidence.start_line <= result.start_line
            and result.end_line <= evidence.end_line
        ):
            return evidence
    return None


def _evidence(candidate: FusedCandidate) -> RetrievedEvidence:
    result = candidate.representative
    return RetrievedEvidence(
        chunk_id=candidate.chunk_id,
        repository_id=result.repository_id,
        file_path=result.file_path,
        qualified_name=result.qualified_name,
        entity_id=result.entity_id,
        entity_type=result.entity_type,
        start_line=result.start_line,
        end_line=result.end_line,
        content=result.content,
        score=round(candidate.rrf_score, 6),
        retrieval_sources=candidate.sources,
        strategy_ranks=candidate.strategy_ranks(),
        reason=explain(candidate),
        match_reasons=list(candidate.match_reasons),
        graph_relationships=list(candidate.graph_evidence),
        unresolved_references=list(candidate.unresolved_references),
    )
