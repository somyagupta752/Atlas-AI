"""14.3/14.6: reference runs end to end through the API (real pipeline, local backend, no model calls)."""

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.evaluation.routes import get_dispatcher, get_session_factory
from app.evaluation.service import InlineDispatcher
from app.main import app
from tests.evaluation.test_configuration import mutate


class SharedSession:
    """Lets runner code 'open and close' the rollback-wrapped test session."""

    def __init__(self, session):
        self._s = session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def __enter__(self):
        return self._s

    def __exit__(self, *a):
        return False

    def close(self):
        pass


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_dispatcher] = lambda: InlineDispatcher()
    app.dependency_overrides[get_session_factory] = lambda: (lambda: SharedSession(db_session))
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def config_id(client):
    body = mutate(name="run-cfg", test_execution={"backend": "local", "test_visibility": "blind"})
    return client.post("/evaluations/configurations", json=body).json()["id"]


def start(client, config_id, **kw):
    body = {"name": "ref", "configuration_id": config_id, "mode": "reference",
            "benchmark_ids": ["bug-001-average"], **kw}
    return client.post("/evaluations/run", json=body)


def test_reference_run_persists_cases_attempts_and_metrics(client, config_id):
    response = start(client, config_id)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    run = client.get(f"/evaluations/{run_id}").json()
    assert run["status"] == "completed" and run["total_cases"] == 5
    assert run["case_status_counts"] == {"completed": 5}
    assert run["environment"]["application"]["commit"] and run["benchmark_version"].startswith("patching-")
    assert run["configuration_snapshot"]["model"]["model_id"] == "claude-opus-5-5"
    assert run["total_cost_usd"] is None  # no model calls: unknown/not applicable, not $0

    cases = client.get(f"/evaluations/{run_id}/cases").json()
    assert {c["variant"] for c in cases["items"]} == {"correct", "no_fix", "regression", "syntax_error",
                                                       "test_tamper"}
    by = {c["variant"]: c for c in cases["items"]}
    assert by["correct"]["final_verified"] is True and by["correct"]["heldout_passed"] is True
    assert by["no_fix"]["final_verified"] is False
    assert by["syntax_error"]["patch_valid"] is False or by["syntax_error"]["final_verified"] is False

    detail = client.get(f"/evaluations/{run_id}/cases/bug-001-average", params={"variant": "correct"}).json()
    assert detail["attempts"][0]["status"] == "verified" and detail["attempts"][0]["unified_diff"]
    assert detail["evidence"]["score"]["verified"] is True
    assert len(detail["other_variants"]) == 4

    metrics = client.get(f"/evaluations/{run_id}/metrics").json()
    final = next(m for m in metrics["metrics"] if m["name"] == "final_verified_rate")
    assert final["value"] == final["numerator"] / final["denominator"] and final["numerator"] == 1
    assert metrics["calculation_version"] == 1 and metrics["populations"]["selected"] == 5

    again = client.post(f"/evaluations/{run_id}/recalculate").json()
    assert again["calculation_version"] == 2 and again["evidence_sha256"] == metrics["evidence_sha256"]


def test_finished_runs_cannot_be_changed(client, config_id, db_session):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    run_id = start(client, config_id).json()["id"]
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text("UPDATE evaluation_case_results SET final_verified = true "
                                "WHERE evaluation_run_id = :i"), {"i": run_id})


def test_idempotency_key_returns_the_same_run(client, config_id):
    first = start(client, config_id, idempotency_key="k-1").json()
    second = start(client, config_id, idempotency_key="k-1")
    assert second.status_code == 202 and second.json()["id"] == first["id"]
    other = start(client, config_id, idempotency_key="k-1", benchmark_ids=["bug-003-pagination"])
    assert other.status_code == 409


def test_active_run_blocks_a_duplicate(client, config_id, db_session):
    run_id = start(client, config_id).json()["id"]
    from sqlalchemy import text
    # Simulate a run still in progress: reopen is impossible by design, so insert one directly.
    db_session.execute(text("""INSERT INTO evaluation_runs (id, name, status, evaluation_mode, diagnosis_mode,
        configuration_snapshot, benchmark_version, selected_case_ids, total_cases, environment)
        VALUES (gen_random_uuid(), 'active', 'running', 'reference', 'recorded', '{}', 'v', '[]', 0, '{}')"""))
    blocked = start(client, config_id)
    assert blocked.status_code == 409 and "still running" in blocked.json()["detail"]["message"]
    assert run_id


@pytest.mark.parametrize("body,status", [
    ({"benchmark_ids": ["nope"]}, 422),
    ({"mode": "live"}, 422),  # no cases / budget / confirmation
    ({"mode": "live", "benchmark_ids": None, "max_cost_usd": 1, "confirm_paid": True}, 422),  # live must name its cases
    ({"mode": "live", "benchmark_ids": ["bug-001-average"], "max_cost_usd": 1}, 422),  # not confirmed
    ({"mode": "live", "benchmark_ids": ["bug-001-average"], "max_cost_usd": 50, "confirm_paid": True}, 422),
    ({"diagnosis_mode": "agent"}, 422),
    ({"max_cost_usd": 1}, 422),
    ({"shell": "ls"}, 422),
    ({"benchmark_ids": []}, 422),
])
def test_invalid_run_requests_start_nothing(client, config_id, body, status):
    before = client.get("/evaluations").json()["page"]["total"]
    assert start(client, config_id, **body).status_code == status
    assert client.get("/evaluations").json()["page"]["total"] == before


def test_unknown_ids_and_pagination(client, config_id):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/evaluations/{missing}").status_code == 404
    assert client.get(f"/evaluations/{missing}/cases").status_code == 404
    assert client.get(f"/evaluations/{missing}/metrics").status_code == 404
    assert client.post(f"/evaluations/{missing}/recalculate").status_code == 404
    assert client.post("/evaluations/run", json={"name": "x", "configuration_id": missing,
                                                 "mode": "reference"}).status_code == 404
    run_id = start(client, config_id).json()["id"]
    assert client.get(f"/evaluations/{run_id}/cases/bug-999").status_code == 404
    page = client.get(f"/evaluations/{run_id}/cases", params={"limit": 2, "offset": 4}).json()
    assert len(page["items"]) == 1 and page["page"]["total"] == 5
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get(f"/evaluations/{run_id}/cases", params=bad).status_code == 422
    assert client.get("/evaluations", params={"status": "completed"}).json()["page"]["total"] >= 1


def test_benchmark_catalogue_is_read_only_and_versioned(client):
    body = client.get("/evaluations/benchmarks").json()
    assert body["version"].startswith("patching-") and len(body["cases"]) == 13
    assert "single_file" in body["categories"] and body["cases"][0]["reference_variants"]
