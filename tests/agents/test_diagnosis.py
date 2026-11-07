import json

import pytest

from app.agents.diagnosis import (
    DiagnosisError,
    extract_json,
    parse_draft,
    validate_draft,
)
from app.agents.diagnosis import tests_run_from as collect_test_runs
from app.agents.schemas import RootCauseAnalysis, SourceLocation
from app.agents.state import InvestigationState, Observation
from tests.agents.conftest import diagnosis


def make_state():
    state = InvestigationState(repository_id="demo", bug_report="x")
    state.add_observation(Observation(id="obs-1", iteration=1, tool_call_id="a", tool_name="search_code", arguments={},
                                      status="ok", content="calculate_average", locations=[]))
    state.add_observation(Observation(
        id="obs-2", iteration=2, tool_call_id="b", tool_name="read_file", arguments={}, status="ok",
        content="    1 | def calculate_average(numbers):", locations=[SourceLocation(file_path="solution.py", start_line=1, end_line=2)]))
    state.add_observation(Observation(
        id="obs-3", iteration=3, tool_call_id="c", tool_name="run_tests", arguments={}, status="ok",
        content="ZeroDivisionError",
        data={"test_path": "test_solution.py", "exit_code": 1, "timed_out": False, "duration_seconds": 0.2,
              "outcome": "failed", "summary": "1 failed, 3 passed"},
        locations=[SourceLocation(file_path="test_solution.py")]))
    return state


def test_extract_json_from_fenced_and_surrounded_text():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 1} thanks') == {"a": 1}


@pytest.mark.parametrize("text", [None, "", "no json here", "{broken", "[1, 2]"])
def test_extract_json_failures(text):
    with pytest.raises(DiagnosisError):
        extract_json(text)


def test_valid_draft_has_no_errors():
    draft = parse_draft(json.dumps(diagnosis()))
    hypotheses, errors = validate_draft(draft, make_state())
    assert errors == []
    assert {h.id: h.supporting_evidence for h in hypotheses}["H1"] == ["E1", "E2"]


@pytest.mark.parametrize("confidence", [-0.1, 1.01, True, "0.5", None])
def test_confidence_must_be_between_zero_and_one(confidence):
    with pytest.raises(DiagnosisError, match="confidence"):
        parse_draft(json.dumps(diagnosis(confidence=confidence)))


def test_completed_needs_strong_supported_evidence():
    evidence = [{**diagnosis()["evidence"][0], "source_type": "retrieval", "observation_ids": ["obs-1"],
                 "file_path": None, "start_line": None, "end_line": None}]
    hypotheses = [{"id": "H1", "description": "d", "status": "supported", "supporting_evidence": ["E1"], "contradicting_evidence": []}]
    draft = parse_draft(json.dumps(diagnosis(evidence=evidence, hypotheses=hypotheses, affected_files=[])))
    _, errors = validate_draft(draft, make_state())
    assert any("is 'retrieval' but cites none of: retrieve_evidence" in e for e in errors)
    assert any("supported hypothesis backed by source_code" in e for e in errors)


def test_completed_without_evidence_is_rejected():
    draft = parse_draft(json.dumps(diagnosis(evidence=[], affected_files=[])))
    _, errors = validate_draft(draft, make_state())
    assert any("unknown evidence" in e for e in errors)
    assert any("supported hypothesis" in e for e in errors)


def test_tests_run_come_from_observations_only():
    [record] = collect_test_runs(make_state())
    assert (record.observation_id, record.outcome, record.summary) == ("obs-3", "failed", "1 failed, 3 passed")


def test_root_cause_analysis_rules():
    base = {"investigation_id": "i", "repository_id": "r", "summary": "s", "confidence": 0.5}
    with pytest.raises(ValueError):
        RootCauseAnalysis(**base, status="completed", root_cause=None)
    with pytest.raises(ValueError):
        RootCauseAnalysis(**base, status="inconclusive", root_cause="guess")
    with pytest.raises(ValueError):
        RootCauseAnalysis(**{**base, "confidence": 2}, status="limit_reached", root_cause=None)
    with pytest.raises(ValueError):
        RootCauseAnalysis(**base, status="limit_reached", root_cause=None, fix_applied=True)
