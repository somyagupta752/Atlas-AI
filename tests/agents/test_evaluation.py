import json
from pathlib import Path

from app.agents.evaluation import (
    DebuggingBenchmarkCase,
    cost_usd,
    root_cause_matches,
    score_case,
    summarize,
)
from app.agents.llm import LLMUsage
from app.agents.schemas import Evidence, RootCauseAnalysis

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "agent" / "cases.json"


def cases():
    return [DebuggingBenchmarkCase.model_validate(c) for c in json.loads(CASES.read_text())]


def result(status="completed", root_cause="Divides by zero when the list is empty.", evidence=None):
    return RootCauseAnalysis(
        investigation_id="i", repository_id="r", status=status, summary="s",
        root_cause=root_cause if status in ("completed", "limit_reached") else None, confidence=0.8,
        evidence=evidence if evidence is not None else [
            Evidence(id="E1", source_type="source_code", observation_ids=["obs-2"], file_path="solution.py", observation="x"),
            Evidence(id="E2", source_type="execution", observation_ids=["obs-3"], file_path="test_solution.py", observation="y"),
            Evidence(id="E3", source_type="source_code", observation_ids=["obs-1"], file_path="unrelated.py", observation="z"),
        ],
        tool_calls=3, iterations=4, model="claude-opus-5-5",
        usage=LLMUsage(input_tokens=1_000_000, output_tokens=100_000),
    )


def test_at_least_five_cases_with_answers_kept_out_of_the_report():
    loaded = cases()
    assert len(loaded) >= 5
    for case in loaded:
        assert case.expected_root_cause not in case.bug_report
        assert (CASES.parents[1].parent / case.repository_path).is_dir()


def test_successful_case_score():
    case = cases()[0]
    score = score_case(case, result(), {"obs-3": "E   ZeroDivisionError: division by zero"})

    assert score.root_cause_correct and score.valid_diagnosis and score.success
    assert score.evidence_coverage == 1.0
    assert score.evidence_precision == round(2 / 3, 4)
    assert score.cost_usd == 6.0  # 1M input * $4 + 0.1M output * $20


def test_correct_sentence_without_evidence_is_not_a_success():
    score = score_case(cases()[0], result(evidence=[]), {})
    assert score.root_cause_correct and not score.success
    assert score.evidence_coverage == 0.0 and score.evidence_precision is None


def test_execution_requirement_needs_the_observed_output():
    score = score_case(cases()[0], result(), {"obs-3": "1 passed"})
    assert score.missing_evidence == ["execution:ZeroDivisionError"] and not score.success


def test_root_cause_keywords():
    groups = [["divi", "zero"], ["empty", "len"]]
    assert root_cause_matches("Division by zero on an EMPTY list", groups)
    assert not root_cause_matches("Off-by-one in pagination", groups)
    assert not root_cause_matches(None, groups)


def test_summary_and_unknown_model_cost():
    case = cases()[0]
    scores = [score_case(case, result(), {"obs-3": "ZeroDivisionError"}),
              score_case(case, result(status="inconclusive"), {})]
    summary = summarize(scores)
    assert summary.cases == 2 and summary.success_rate == 0.5
    assert summary.mean_tool_calls == 3
    assert cost_usd("unknown-model", LLMUsage(input_tokens=5)) is None
