import pytest

from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.models import RetrievalResult, RetrievalSource

K, V, G = RetrievalSource.KEYWORD, RetrievalSource.VECTOR, RetrievalSource.GRAPH


def result(chunk_id: str, source: RetrievalSource, score: float = 1.0, reason: str = "") -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        repository_id="demo",
        file_path=f"{chunk_id}.py",
        content=f"def {chunk_id}(): pass",
        start_line=1,
        end_line=1,
        retrieval_source=source,
        score=score,
        match_reasons=[reason] if reason else [],
    )


def ranked(source: RetrievalSource, *chunk_ids: str) -> list[RetrievalResult]:
    return [result(c, source, score=100 - i) for i, c in enumerate(chunk_ids)]


def test_rrf_matches_the_formula_for_the_documented_example():
    # Keyword: get_user 1, delete_user 2, x 3, find_by_id 4
    # Vector:  find_by_id 1, y 2, get_user 3
    # Graph:   find_by_id 1, get_user 2, z 3, w 4, delete_user 5
    lists = {
        K: ranked(K, "get_user", "delete_user", "x", "find_by_id"),
        V: ranked(V, "find_by_id", "y", "get_user"),
        G: ranked(G, "find_by_id", "get_user", "z", "w", "delete_user"),
    }
    fused = {c.chunk_id: c for c in reciprocal_rank_fusion(lists, k=60)}

    assert fused["get_user"].rrf_score == pytest.approx(1 / 61 + 1 / 63 + 1 / 62)
    assert fused["find_by_id"].rrf_score == pytest.approx(1 / 64 + 1 / 61 + 1 / 61)
    assert fused["delete_user"].rrf_score == pytest.approx(1 / 62 + 1 / 65)
    assert fused["get_user"].ranks == {K: 1, V: 3, G: 2}
    assert fused["delete_user"].ranks == {K: 2, G: 5}


def test_output_is_sorted_and_raw_scores_do_not_matter():
    # Huge keyword scores must not dominate: only ranks count.
    lists = {
        K: [result("a", K, score=1e9), result("b", K, score=1e8)],
        V: [result("b", V, score=0.1), result("c", V, score=0.05)],
    }
    fused = reciprocal_rank_fusion(lists)

    assert [c.chunk_id for c in fused] == ["b", "a", "c"]
    scores = [c.rrf_score for c in fused]
    assert scores == sorted(scores, reverse=True)


def test_duplicates_are_merged_by_chunk_id_and_keep_provenance():
    lists = {
        K: [result("a", K, reason="exact identifier match 'a' (name)")],
        V: [result("a", V, score=0.8, reason="semantic similarity 0.800")],
        G: [result("a", G, score=1.5, reason="graph entity named in the query")],
    }
    (only,) = reciprocal_rank_fusion(lists)

    assert only.sources == [K, V, G]
    assert {r.source: r.score for r in only.strategy_ranks()} == {K: 1.0, V: 0.8, G: 1.5}
    assert len(only.match_reasons) == 3


def test_repeats_within_one_list_keep_the_first_position():
    lists = {K: [result("a", K), result("b", K), result("a", K)]}
    fused = {c.chunk_id: c for c in reciprocal_rank_fusion(lists, k=0)}

    assert fused["a"].ranks == {K: 1}
    assert fused["b"].ranks == {K: 2}
    assert fused["a"].rrf_score == pytest.approx(1.0)


def test_candidates_found_by_one_strategy_are_kept():
    lists = {K: ranked(K, "kw_only"), V: ranked(V, "vec_only"), G: []}
    fused = {c.chunk_id: c for c in reciprocal_rank_fusion(lists)}

    assert set(fused) == {"kw_only", "vec_only"}
    assert fused["kw_only"].sources == [K]
    assert fused["vec_only"].sources == [V]


def test_agreement_beats_a_single_first_place():
    lists = {K: ranked(K, "solo", "both"), V: ranked(V, "x", "both")}

    assert reciprocal_rank_fusion(lists)[0].chunk_id == "both"


def test_ties_are_broken_deterministically():
    lists = {K: ranked(K, "b"), V: ranked(V, "a")}

    assert [c.chunk_id for c in reciprocal_rank_fusion(lists)] == ["a", "b"]


def test_weights_and_empty_input():
    lists = {K: ranked(K, "a"), V: ranked(V, "b")}

    assert reciprocal_rank_fusion(lists, weights={V: 2.0})[0].chunk_id == "b"
    assert reciprocal_rank_fusion({}) == []
    with pytest.raises(ValueError):
        reciprocal_rank_fusion(lists, k=-1)
