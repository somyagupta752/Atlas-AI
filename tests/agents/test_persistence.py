import uuid

import pytest

from app.agents.repository import (
    InvestigationNotFoundError,
    InvestigationRepository,
    json_safe,
)
from app.agents.schemas import InvalidTransitionError, RootCauseAnalysis
from app.agents.state import InvestigationState
from tests.agents.conftest import standard_script


def test_investigation_and_ordered_events_are_persisted(build_agent, db_session):
    repository = InvestigationRepository(db_session)
    agent, _ = build_agent(standard_script(), repository=repository)
    result = agent.investigate_bug("bench-average", "crash on empty list")

    record = repository.get(result.investigation_id)
    assert record.status == "completed" and record.root_cause == result.root_cause
    assert record.result["evidence"][0]["id"] == "E1" and record.completed_at is not None

    events = repository.list_events(result.investigation_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    types = [e.event_type for e in events]
    assert types[0] == "investigation_created" and types[-1] == "diagnosis_completed"
    assert types.index("tool_call") < types.index("observation") < types.index("hypothesis")
    observations = [e.payload for e in events if e.event_type == "observation"]
    assert [o["tool_name"] for o in observations] == ["search_code", "read_file", "run_tests"]
    assert "ZeroDivisionError" in observations[2]["content"]


def test_failed_investigation_keeps_earlier_observations(build_agent, db_session):
    from app.agents.llm import LLMProviderError, tool_call

    repository = InvestigationRepository(db_session)
    agent, _ = build_agent([tool_call("read_file", {"file_path": "solution.py"}), LLMProviderError("down")], repository=repository)
    result = agent.investigate_bug("bench-average", "crash")

    record = repository.get(result.investigation_id)
    assert record.status == "failed" and record.result["failure_reason"] == "llm_provider_error"
    assert [e.event_type for e in repository.list_events(result.investigation_id, ["observation"])] == ["observation"]


def test_malformed_diagnosis_is_not_stored_as_completed(build_agent, db_session):
    from app.agents.schemas import AgentLimits

    repository = InvestigationRepository(db_session)
    agent, _ = build_agent(standard_script(final="not json"), AgentLimits(diagnosis_repair_attempts=0), repository=repository)
    result = agent.investigate_bug("bench-average", "crash")
    record = repository.get(result.investigation_id)
    assert record.status == "failed" and record.root_cause is None
    assert any(e.event_type == "diagnosis_invalid" for e in repository.list_events(result.investigation_id))


def test_events_are_isolated_per_investigation(db_session):
    repository = InvestigationRepository(db_session)
    first = InvestigationState(repository_id="repo-a", bug_report="a")
    second = InvestigationState(repository_id="repo-b", bug_report="b")
    repository.create(first)
    repository.create(second)
    repository.append_event(first.investigation_id, "x", {"n": 1})
    repository.append_event(second.investigation_id, "x", {"n": 2})
    repository.append_event(first.investigation_id, "x", {"n": 3})

    assert [e.payload["n"] for e in repository.list_events(first.investigation_id)] == [1, 3]
    assert [e.payload["n"] for e in repository.list_events(second.investigation_id)] == [2]
    with pytest.raises(InvestigationNotFoundError):
        repository.get(first.investigation_id, repository_id="repo-b")


def test_unknown_and_malformed_ids(db_session):
    repository = InvestigationRepository(db_session)
    for bad in [str(uuid.uuid4()), "not-a-uuid"]:
        with pytest.raises(InvestigationNotFoundError):
            repository.get(bad)


def test_terminal_status_cannot_change(db_session):
    repository = InvestigationRepository(db_session)
    state = InvestigationState(repository_id="repo-a", bug_report="a")
    repository.create(state)
    done = RootCauseAnalysis(investigation_id=state.investigation_id, repository_id="repo-a", status="inconclusive",
                             summary="s", root_cause=None, confidence=0.1)
    repository.finish(done)
    with pytest.raises(InvalidTransitionError):
        repository.finish(done.model_copy(update={"status": "failed"}))


def test_json_safe_drops_nul_characters():
    assert json_safe({"a": "x\x00y"}) == {"a": "xy"}
