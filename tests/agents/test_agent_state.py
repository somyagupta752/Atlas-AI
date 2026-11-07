import pytest

from app.agents.schemas import InvalidTransitionError, InvestigationStatus
from app.agents.state import InvestigationState, Observation


def observation(status, n):
    return Observation(id=f"obs-{n}", iteration=1, tool_call_id=str(n), tool_name="t", arguments={}, status=status, content="")


def test_observation_ids_and_tool_call_counting():
    state = InvestigationState(repository_id="r", bug_report="b")
    assert state.next_observation_id() == "obs-1"
    for n, status in enumerate(["ok", "tool_error", "unknown_tool", "invalid_arguments", "duplicate", "skipped", "timeout"], 1):
        state.add_observation(observation(status, n))
    assert state.next_observation_id() == "obs-8"
    assert state.tool_calls == 3  # ok, tool_error, timeout actually executed
    assert state.get_observation("obs-2").status == "tool_error"
    assert state.get_observation("obs-99") is None


def test_lifecycle_transitions():
    state = InvestigationState(repository_id="r", bug_report="b")
    assert state.status == InvestigationStatus.RUNNING
    state.transition(InvestigationStatus.LIMIT_REACHED)
    with pytest.raises(InvalidTransitionError):
        state.transition(InvestigationStatus.COMPLETED)


def test_state_is_serialisable():
    state = InvestigationState(repository_id="r", bug_report="b", messages=[{"role": "user", "content": "x"}])
    state.add_observation(observation("ok", 1))
    restored = InvestigationState.model_validate_json(state.model_dump_json())
    assert restored.observations[0].id == "obs-1" and restored.investigation_id == state.investigation_id
