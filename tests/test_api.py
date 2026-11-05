import sys

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.repository import BenchmarkExecutionRepository


@pytest.fixture
def client(db_session):
    # Route the API to the rollback-wrapped session so tests leave no rows behind.
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_run_existing_benchmark(client):
    response = client.post("/benchmarks/run", json={"case_id": "bug-001-average"})

    assert response.status_code == 200
    body = response.json()
    assert body["case_id"] == "bug-001-average"
    assert body["outcome"] == "reproduced"
    assert body["test_run"]["timed_out"] is False
    assert body["test_run"]["exit_code"] != 0
    assert "ZeroDivisionError" in body["test_run"]["stdout"]


def test_unknown_benchmark_returns_404(client):
    response = client.post("/benchmarks/run", json={"case_id": "does-not-exist"})

    assert response.status_code == 404


def test_missing_case_id_returns_422(client):
    response = client.post("/benchmarks/run", json={})

    assert response.status_code == 422


def test_run_persists_execution(client, db_session):
    response = client.post("/benchmarks/run", json={"case_id": "bug-001-average"})

    runs = BenchmarkExecutionRepository(db_session).list_by_case("bug-001-average")
    assert len(runs) == 1
    assert runs[0].outcome == response.json()["outcome"] == "reproduced"
    assert runs[0].exit_code == response.json()["test_run"]["exit_code"]
    assert "ZeroDivisionError" in runs[0].stdout
    assert runs[0].timed_out is False


def test_each_run_creates_a_new_record(client, db_session):
    for _ in range(3):
        client.post("/benchmarks/run", json={"case_id": "bug-001-average"})

    assert len(BenchmarkExecutionRepository(db_session).list_by_case("bug-001-average")) == 3


def test_unknown_benchmark_persists_nothing(client, db_session):
    client.post("/benchmarks/run", json={"case_id": "does-not-exist"})

    assert BenchmarkExecutionRepository(db_session).list_by_case("does-not-exist") == []


def test_history_returns_stored_runs(client):
    for _ in range(3):
        client.post("/benchmarks/run", json={"case_id": "bug-001-average"})

    response = client.get("/benchmarks/bug-001-average/runs")

    assert response.status_code == 200
    runs = response.json()
    assert len(runs) == 3
    assert len({r["id"] for r in runs}) == 3
    for run in runs:
        assert run["case_id"] == "bug-001-average"
        assert run["outcome"] == "reproduced"
        assert run["timed_out"] is False
        assert "ZeroDivisionError" in run["stdout"]
        assert run["created_at"]


def test_history_empty_for_registered_case_with_no_runs(client):
    response = client.get("/benchmarks/bug-001-average/runs")

    assert response.status_code == 200
    assert response.json() == []


def test_history_unknown_benchmark_returns_404(client):
    assert client.get("/benchmarks/does-not-exist/runs").status_code == 404


def test_history_limit(client):
    for _ in range(3):
        client.post("/benchmarks/run", json={"case_id": "bug-001-average"})

    response = client.get("/benchmarks/bug-001-average/runs", params={"limit": 2})

    assert len(response.json()) == 2


def test_history_rejects_invalid_limit(client):
    assert client.get("/benchmarks/bug-001-average/runs", params={"limit": 0}).status_code == 422
    assert client.get("/benchmarks/bug-001-average/runs", params={"limit": 101}).status_code == 422


def _register(monkeypatch, case_id, code):
    from app.benchmark import BenchmarkCase
    from app.benchmark_registry import BENCHMARKS

    monkeypatch.setitem(
        BENCHMARKS,
        case_id,
        BenchmarkCase(
            case_id=case_id,
            description="synthetic",
            command=[sys.executable, "-c", code],
            expected_failure_text="ZeroDivisionError",
        ),
    )


def test_not_reproduced_outcome_is_persisted(client, db_session, monkeypatch):
    _register(monkeypatch, "synthetic-ok", "print('fine')")

    response = client.post("/benchmarks/run", json={"case_id": "synthetic-ok"})

    assert response.json()["outcome"] == "not_reproduced"
    [run] = BenchmarkExecutionRepository(db_session).list_by_case("synthetic-ok")
    assert run.outcome == "not_reproduced"
    assert run.exit_code == 0
    assert run.stdout.strip() == "fine"


def test_timed_out_outcome_is_persisted(client, db_session, monkeypatch):
    import app.main

    _register(monkeypatch, "synthetic-slow", "import time; time.sleep(5)")
    monkeypatch.setattr(app.main, "BENCHMARK_TIMEOUT_SECONDS", 0.5)

    response = client.post("/benchmarks/run", json={"case_id": "synthetic-slow"})

    assert response.json()["outcome"] == "timed_out"
    [run] = BenchmarkExecutionRepository(db_session).list_by_case("synthetic-slow")
    assert run.outcome == "timed_out"
    assert run.exit_code is None
    assert run.timed_out is True
