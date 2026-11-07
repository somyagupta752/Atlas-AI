import pytest

from app.agents.evidence import validate_evidence
from app.agents.schemas import Evidence
from tests.agents.test_diagnosis import make_state


def evidence(**overrides):
    value = {"id": "E1", "source_type": "source_code", "observation_ids": ["obs-2"], "file_path": "solution.py",
             "qualified_name": "calculate_average", "start_line": 1, "end_line": 2, "observation": "divides by len"}
    value.update(overrides)
    return Evidence(**value)


def test_grounded_source_evidence():
    assert validate_evidence(evidence(supports_hypothesis_ids=["H1"]), make_state()) == []


def test_execution_evidence_must_cite_run_tests():
    assert validate_evidence(evidence(source_type="execution", observation_ids=["obs-3"], file_path="test_solution.py",
                                      start_line=None, end_line=None, qualified_name=None), make_state()) == []
    errors = validate_evidence(evidence(source_type="execution"), make_state())
    assert any("run_tests" in e for e in errors)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"observation_ids": ["obs-42"]}, "unknown observation"),
        ({"file_path": "other.py"}, "does not appear"),
        ({"start_line": 1, "end_line": 30}, "were not shown"),
        ({"qualified_name": "UserService.get_user"}, "does not appear"),
        ({"source_type": "graph"}, "get_callers"),
    ],
)
def test_ungrounded_evidence_is_rejected(overrides, fragment):
    errors = validate_evidence(evidence(**overrides), make_state())
    assert any(fragment in e for e in errors), errors


def test_failed_observation_cannot_back_evidence():
    state = make_state()
    state.observations[1].status = "tool_error"
    assert any("did not succeed" in e for e in validate_evidence(evidence(), state))


def test_model_level_rules():
    with pytest.raises(ValueError):
        evidence(observation_ids=[])
    with pytest.raises(ValueError):
        evidence(start_line=5, end_line=2)
    with pytest.raises(ValueError):
        evidence(file_path=None)
    with pytest.raises(ValueError):
        evidence(supports_hypothesis_ids=["H1"], contradicts_hypothesis_ids=["H1"])


def test_lack_of_a_test_is_not_execution_evidence():
    # A search that found no test is a source_code/test observation, never execution.
    errors = validate_evidence(evidence(source_type="execution", observation_ids=["obs-1"], file_path=None,
                                        start_line=None, end_line=None, qualified_name=None), make_state())
    assert errors
