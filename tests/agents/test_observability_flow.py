"""15.5: one workflow ID follows an investigation through the agent loop and its tool threads."""

from app.core import metrics
from tests.agents.conftest import standard_script


def test_workflow_id_is_the_investigation_id_on_every_log_line(build_agent, json_logs):
    agent, _ = build_agent(standard_script())
    before = metrics.REGISTRY.get_sample_value("atlas_agent_workflow_duration_seconds_count",
                                               {"status": "completed"}) or 0
    result = agent.investigate_bug("bench-average", "The average calculation crashes on an empty list.")
    started = json_logs.events("agent_workflow_started")[-1]
    done = json_logs.events("agent_workflow_completed")[-1]
    assert started["workflow_id"] == done["workflow_id"] == result.investigation_id
    assert done["status"] == "completed" and done["stop_reason"] == "final_answer" and done["duration_ms"] >= 0
    after = metrics.REGISTRY.get_sample_value("atlas_agent_workflow_duration_seconds_count",
                                              {"status": "completed"})
    assert after == before + 1
    # nothing from the bug report or the repository's code is in the logs
    assert "average calculation crashes" not in json_logs.raw


def test_the_workflow_scope_ends_with_the_investigation(build_agent, json_logs):
    import logging

    agent, _ = build_agent(standard_script())
    agent.investigate_bug("bench-average", "crash on empty list")
    logging.getLogger("after").info("later")
    assert json_logs.events("later")[-1]["workflow_id"] is None
