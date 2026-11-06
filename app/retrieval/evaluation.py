"""Retrieval quality metrics: Precision@K, Recall@K and Mean Reciprocal Rank.

A returned result *covers* an expected entity when its ``entity_id`` is that
entity, or when the entity's chunk was folded into it (``merged``). The code is
then present in what the caller receives.

- Precision@K = covering results in the top K / K. Fewer than K results still
  divide by K, so returning less is not rewarded.
- Recall@K = expected entities covered by the top K / expected entities.
- Reciprocal rank = 1 / position of the first covering result, or 0.
- MRR = mean reciprocal rank over the cases.
"""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from app.retrieval.models import RetrievedEvidence


class BenchmarkCase(BaseModel):
    id: str
    query: str
    repository_id: str
    expected_chunks: list[str] = Field(min_length=1)  # graph entity IDs
    category: str = "general"


class CaseScore(BaseModel):
    case_id: str
    precision: dict[int, float]
    recall: dict[int, float]
    reciprocal_rank: float
    first_relevant_rank: int | None
    missing: list[str]  # expected entities not covered at the largest K


class StrategySummary(BaseModel):
    strategy: str
    cases: int
    precision: dict[int, float]
    recall: dict[int, float]
    mrr: float


def entity_id_of_chunk(chunk_id: str) -> str:
    """``chunk:function:r:a.py::f#part2`` -> ``function:r:a.py::f``."""
    body = chunk_id.removeprefix("chunk:")
    return body.split("#part", 1)[0]


def covered_entities(result: RetrievedEvidence) -> set[str]:
    covered = {result.entity_id} if result.entity_id else set()
    covered |= {entity_id_of_chunk(m.chunk_id) for m in result.merged}
    return covered


def score_ranking(
    case_id: str, ranked: Sequence[set[str]], expected: Sequence[str], ks: Sequence[int]
) -> CaseScore:
    """Score one ranking. ``ranked[i]`` is the set of entities result ``i + 1`` covers."""
    if not expected:
        raise ValueError("a case needs at least one expected entity")
    if any(k < 1 for k in ks):
        raise ValueError("K must be at least 1")
    wanted = set(expected)
    precision, recall = {}, {}
    for k in ks:
        top = ranked[:k]
        relevant_results = sum(1 for covered in top if covered & wanted)
        found = set().union(*top) & wanted if top else set()
        precision[k] = relevant_results / k
        recall[k] = len(found) / len(wanted)
    first = next((i for i, covered in enumerate(ranked, start=1) if covered & wanted), None)
    largest = ranked[: max(ks)]
    found_at_largest = set().union(*largest) & wanted if largest else set()
    return CaseScore(
        case_id=case_id,
        precision=precision,
        recall=recall,
        reciprocal_rank=1.0 / first if first else 0.0,
        first_relevant_rank=first,
        missing=sorted(wanted - found_at_largest),
    )


def score_results(
    case: BenchmarkCase, results: Sequence[RetrievedEvidence], ks: Sequence[int]
) -> CaseScore:
    return score_ranking(case.id, [covered_entities(r) for r in results], case.expected_chunks, ks)


def summarize(strategy: str, scores: Sequence[CaseScore], ks: Sequence[int]) -> StrategySummary:
    count = len(scores)
    if count == 0:
        return StrategySummary(
            strategy=strategy, cases=0, precision=dict.fromkeys(ks, 0.0), recall=dict.fromkeys(ks, 0.0), mrr=0.0
        )
    return StrategySummary(
        strategy=strategy,
        cases=count,
        precision={k: sum(s.precision[k] for s in scores) / count for k in ks},
        recall={k: sum(s.recall[k] for s in scores) / count for k in ks},
        mrr=sum(s.reciprocal_rank for s in scores) / count,
    )
