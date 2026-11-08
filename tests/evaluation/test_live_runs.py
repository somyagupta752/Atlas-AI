"""14.3: live-mode behaviour with a scripted model. No network, no cost."""

from functools import partial

import pytest
from fastapi.testclient import TestClient

from app.agents.llm import FakeLLM
from app.database import get_db
from app.evaluation.routes import (
    get_dispatcher,
    get_runner_factory,
    get_session_factory,
)
from app.evaluation.runner import EvaluationRunner
from app.evaluation.service import InlineDispatcher
from app.main import app
from tests.evaluation.test_configuration import mutate
from tests.evaluation.test_runs_api import SharedSession
from tests.patching.conftest import AVERAGE_FIX, AVERAGE_NO_FIX, submit

MODEL = "claude-sonnet-5-5"


def install(db_session, llm):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_dispatcher] = lambda: InlineDispatcher()
    app.dependency_overrides[get_session_factory] = lambda: (lambda: SharedSession(db_session))
    app.dependency_overrides[get_runner_factory] = lambda: partial(
        EvaluationRunner, llm_factory=lambda settings, model: llm)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    yield
    app.dependency_overrides.clear()


def config(client):
    body = mutate(name="live-cfg", test_execution={"backend": "local", "test_visibility": "blind"},
                  patching={"max_attempts": 2})
    return client.post("/evaluations/configurations", json=body).json()["id"]


def live(client, cid, **kw):
    body = {"name": "live", "configuration_id": cid, "mode": "live", "benchmark_ids": ["bug-001-average"],
            "max_cost_usd": 1.0, "confirm_paid": True, **kw}
    return client.post("/evaluations/run", json=body)


def test_live_run_records_every_attempt_with_its_feedback(db_session):
    llm = FakeLLM([submit(AVERAGE_NO_FIX), submit(AVERAGE_FIX)], model=MODEL)
    client = install(db_session, llm)
    run_id = live(client, config(client)).json()["id"]
    run = client.get(f"/evaluations/{run_id}").json()
    assert run["status"] == "completed" and run["total_cases"] == 1 and run["budget_limit_usd"] == "1.000000"
    case = client.get(f"/evaluations/{run_id}/cases/bug-001-average").json()
    assert [a["status"] for a in case["attempts"]] == ["rejected", "verified"]
    assert case["attempts"][1]["feedback_from_attempts"] == [1] and case["attempts"][1]["feedback_reasons"]
    assert case["first_attempt_verified"] is False and case["final_verified"] is True
    assert case["attempts_count"] == 2 and case["cost_kind"] == "actual"
    assert case["attempts"][0]["latency_seconds"] is not None
    assert run["environment"]["budget_ledger"]["requests"] == 2


def test_budget_exhaustion_is_partial_never_a_false_success(db_session):
    llm = FakeLLM([submit(AVERAGE_FIX)] * 4, model=MODEL, pricing=None)  # real prices: a call cannot fit $0.0001
    client = install(db_session, llm)
    cid = config(client)
    run_id = live(client, cid, benchmark_ids=["bug-001-average", "bug-003-pagination"],
                  max_cost_usd=0.0001).json()["id"]
    run = client.get(f"/evaluations/{run_id}").json()
    assert run["status"] == "partial" and llm.calls == []
    cases = client.get(f"/evaluations/{run_id}/cases").json()["items"]
    assert len(cases) == 2 and all(c["status"] == "incomplete" for c in cases)
    assert all(c["final_verified"] is None and c["success"] is None for c in cases)
    metrics = {m["name"]: m for m in client.get(f"/evaluations/{run_id}/metrics").json()["metrics"]}
    assert metrics["final_verified_rate"]["value"] is None and metrics["completion_rate"]["numerator"] == 0
    assert metrics["completion_rate"]["denominator"] == 2


def test_live_runs_need_a_working_model_and_a_known_price(db_session):
    from app.agents.llm import LLMConfigurationError

    def broken(settings, model):
        raise LLMConfigurationError("no key")

    client = install(db_session, FakeLLM([]))
    cid = config(client)
    before = client.get("/evaluations", params={"mode": "live"}).json()["page"]["total"]
    app.dependency_overrides[get_runner_factory] = lambda: partial(EvaluationRunner, llm_factory=broken)
    assert live(client, cid).status_code == 503
    unpriced = FakeLLM([], model="mystery", pricing=None)
    app.dependency_overrides[get_runner_factory] = lambda: partial(
        EvaluationRunner, llm_factory=lambda s, m: unpriced)
    assert live(client, cid).status_code == 503
    assert client.get("/evaluations", params={"mode": "live"}).json()["page"]["total"] == before


def test_interrupted_runs_are_closed_as_partial(db_session, make_config, make_run, make_case):
    from datetime import UTC, datetime, timedelta

    from app.evaluation.service import recover_interrupted_runs

    run = make_run(make_config(), status="running", evaluation_mode="live",
                   selected_case_ids=["bug-001-average", "bug-003-pagination"], total_cases=2)
    make_case(run, "bug-001-average", variant="live", status="completed", final_verified=True,
              first_attempt_verified=True, attempts_count=1, success=True)
    closed = recover_interrupted_runs(db_session, lambda _id: False, datetime.now(UTC) + timedelta(seconds=5))
    assert closed == 1
    db_session.refresh(run)
    assert run.status == "partial" and "interrupted" in run.failure_reason
    statuses = {c.case_id: c.status for c in run.cases}
    assert statuses == {"bug-001-average": "completed", "bug-003-pagination": "incomplete"}
