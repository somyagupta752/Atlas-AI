import json
from pathlib import Path

import pytest

from app.retrieval.evaluation import (
    BenchmarkCase,
    entity_id_of_chunk,
    score_ranking,
    summarize,
)

CASES = Path(__file__).resolve().parent.parent / "benchmarks" / "retrieval" / "cases.json"


def test_precision_recall_and_reciprocal_rank():
    ranked = [{"x"}, {"a"}, {"y"}, {"b"}, {"z"}]
    score = score_ranking("c", ranked, ["a", "b", "c"], ks=[1, 3, 5])

    assert score.precision == {1: 0.0, 3: pytest.approx(1 / 3), 5: pytest.approx(2 / 5)}
    assert score.recall == {1: 0.0, 3: pytest.approx(1 / 3), 5: pytest.approx(2 / 3)}
    assert score.first_relevant_rank == 2
    assert score.reciprocal_rank == 0.5
    assert score.missing == ["c"]


def test_short_result_lists_still_divide_by_k():
    score = score_ranking("c", [{"a"}], ["a"], ks=[5])

    assert score.precision[5] == pytest.approx(0.2)
    assert score.recall[5] == 1.0


def test_no_relevant_result():
    score = score_ranking("c", [{"x"}, {"y"}], ["a"], ks=[2])

    assert score.reciprocal_rank == 0.0
    assert score.first_relevant_rank is None
    assert score.recall[2] == 0.0
    assert score_ranking("c", [], ["a"], ks=[2]).precision[2] == 0.0


def test_a_result_covering_folded_chunks_counts_them():
    score = score_ranking("c", [{"class", "class.method"}], ["class.method"], ks=[1])

    assert score.recall[1] == 1.0
    assert score.precision[1] == 1.0


def test_mrr_is_the_mean_over_cases():
    scores = [
        score_ranking("a", [{"x"}, {"hit"}], ["hit"], ks=[2]),  # 1/2
        score_ranking("b", [{"hit"}], ["hit"], ks=[2]),  # 1
        score_ranking("c", [{"x"}], ["hit"], ks=[2]),  # 0
    ]
    summary = summarize("hybrid", scores, ks=[2])

    assert summary.mrr == pytest.approx(0.5)
    assert summary.cases == 3
    assert summarize("empty", [], ks=[2]).mrr == 0.0


def test_invalid_inputs():
    with pytest.raises(ValueError):
        score_ranking("c", [], [], ks=[1])
    with pytest.raises(ValueError):
        score_ranking("c", [], ["a"], ks=[0])


def test_entity_id_of_chunk():
    assert entity_id_of_chunk("chunk:function:r:a.py::f") == "function:r:a.py::f"
    assert entity_id_of_chunk("chunk:function:r:a.py::f#part2") == "function:r:a.py::f"
    assert entity_id_of_chunk("chunk:file:r:a.py") == "file:r:a.py"


def test_benchmark_cases_are_valid_and_cover_the_required_categories():
    cases = [BenchmarkCase.model_validate(c) for c in json.loads(CASES.read_text())]

    assert len(cases) >= 10
    assert len({c.id for c in cases}) == len(cases)
    required = {
        "exact_function", "class_name", "error_message", "natural_language", "dependencies",
        "database", "api_endpoint", "inheritance", "imports", "graph_expansion",
    }
    assert required <= {c.category for c in cases}
    for case in cases:
        for entity_id in case.expected_chunks:
            assert entity_id.split(":", 2)[1] == case.repository_id
