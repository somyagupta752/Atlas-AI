import pytest

from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.models import (
    Confidence,
    GraphEvidence,
    RetrievalResult,
    RetrievalSource,
)
from app.retrieval.ranker import rank_evidence

K, V, G = RetrievalSource.KEYWORD, RetrievalSource.VECTOR, RetrievalSource.GRAPH


def result(chunk_id, source, start=1, end=5, path="app/a.py", score=1.0, reasons=(), graph=()):
    return RetrievalResult(
        chunk_id=chunk_id,
        repository_id="demo",
        file_path=path,
        content=f"# {chunk_id}",
        start_line=start,
        end_line=end,
        retrieval_source=source,
        score=score,
        qualified_name=chunk_id,
        entity_id=f"function:demo:{path}::{chunk_id}",
        match_reasons=list(reasons),
        graph_evidence=list(graph),
    )


def test_results_keep_the_fused_order_and_are_limited():
    lists = {K: [result(f"c{i}", K, start=i * 10 + 1, end=i * 10 + 5) for i in range(6)]}
    evidence = rank_evidence(reciprocal_rank_fusion(lists), limit=3)

    assert [e.chunk_id for e in evidence] == ["c0", "c1", "c2"]
    assert evidence[0].score > evidence[1].score > evidence[2].score


def test_provenance_and_source_location_are_packaged():
    graph = GraphEvidence(
        seed_entity_id="function:demo:app/a.py::seed",
        entity_id="function:demo:app/a.py::target",
        relation="caller",
        depth=1,
        confidence=Confidence.CONFIRMED,
        score=1.0,
    )
    lists = {
        K: [result("target", K, reasons=["exact identifier match 'target' (name)"])],
        V: [result("target", V, score=0.75, reasons=["semantic similarity 0.750"])],
        G: [result("target", G, reasons=["graph: caller of seed (1 hop, confirmed)"], graph=[graph])],
    }
    (evidence,) = rank_evidence(reciprocal_rank_fusion(lists), limit=5)

    assert evidence.retrieval_sources == [K, V, G]
    assert [r.rank for r in evidence.strategy_ranks] == [1, 1, 1]
    assert (evidence.file_path, evidence.start_line, evidence.end_line) == ("app/a.py", 1, 5)
    assert evidence.graph_relationships == [graph]
    assert evidence.reason.startswith("Retrieved by keyword+vector+graph:")
    assert "exact identifier match 'target'" in evidence.reason
    assert "semantic similarity 0.75" in evidence.reason
    assert "caller of seed" in evidence.reason


def test_reasons_never_claim_causation():
    lists = {K: [result("a", K, reasons=["exact identifier match 'a' (name)"])]}
    (evidence,) = rank_evidence(reciprocal_rank_fusion(lists), limit=1)

    for word in ("cause", "root", "bug", "responsible"):
        assert word not in evidence.reason.lower()


def test_chunk_inside_a_higher_ranked_chunk_is_folded_in():
    lists = {
        K: [
            result("UserService", K, start=1, end=20),
            result("UserService.get_user", K, start=5, end=8),
            result("other", K, start=30, end=40),
        ]
    }
    evidence = rank_evidence(reciprocal_rank_fusion(lists), limit=5)

    assert [e.chunk_id for e in evidence] == ["UserService", "other"]
    (merged,) = evidence[0].merged
    assert merged.chunk_id == "UserService.get_user"
    assert (merged.start_line, merged.end_line) == (5, 8)
    assert merged.retrieval_sources == [K]
    assert merged.reason.startswith("Retrieved by keyword")


def test_container_ranked_below_its_child_is_kept_for_context():
    lists = {K: [result("method", K, start=5, end=8), result("class", K, start=1, end=20)]}
    evidence = rank_evidence(reciprocal_rank_fusion(lists), limit=5)

    assert [e.chunk_id for e in evidence] == ["method", "class"]


def test_same_lines_in_different_files_are_not_merged():
    lists = {K: [result("a", K, path="app/a.py"), result("b", K, path="app/b.py")]}

    assert len(rank_evidence(reciprocal_rank_fusion(lists), limit=5)) == 2


def test_merging_can_be_disabled():
    lists = {K: [result("outer", K, start=1, end=20), result("inner", K, start=2, end=3)]}

    assert len(rank_evidence(reciprocal_rank_fusion(lists), limit=5, merge_contained=False)) == 2


def test_invalid_limit():
    with pytest.raises(ValueError):
        rank_evidence([], limit=0)
