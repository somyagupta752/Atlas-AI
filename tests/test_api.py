import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.benchmark import BenchmarkExecutionResult
from app.database import engine, get_db
from app.main import app
from app.models import BenchmarkExecution
from app.repository import BenchmarkExecutionRepository
from app.test_runner import TestRunResult


@pytest.fixture(autouse=True)
def _enable_host_benchmarks(monkeypatch):
    """POST /benchmarks/run is off by default; these tests are about its behaviour."""
    from app.config import get_settings as real

    monkeypatch.setattr("app.main.get_settings", lambda: real().model_copy(update={"benchmarks_endpoint_enabled": True}))



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
    _register(monkeypatch, "synthetic-slow", "import time; time.sleep(5)")
    monkeypatch.setattr("app.main.BENCHMARK_TIMEOUT_SECONDS", 0.5)

    response = client.post("/benchmarks/run", json={"case_id": "synthetic-slow"})

    assert response.json()["outcome"] == "timed_out"
    [run] = BenchmarkExecutionRepository(db_session).list_by_case("synthetic-slow")
    assert run.outcome == "timed_out"
    assert run.exit_code is None
    assert run.timed_out is True


def test_history_is_ordered_newest_first(client, db_session):
    repo = BenchmarkExecutionRepository(db_session)
    result = BenchmarkExecutionResult(
        case_id="bug-001-average",
        outcome="reproduced",
        test_run=TestRunResult(
            command=["pytest"],
            exit_code=1,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
        ),
    )
    # Insert out of chronological order (B, C, A) so the expected order
    # cannot come from insertion order or primary-key order by accident.
    run_b = repo.save(result)
    run_c = repo.save(result)
    run_a = repo.save(result)
    run_a.created_at = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    run_b.created_at = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    run_c.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    db_session.commit()

    response = client.get("/benchmarks/bug-001-average/runs")

    assert response.status_code == 200
    assert [run["id"] for run in response.json()] == [
        str(run_c.id),
        str(run_b.id),
        str(run_a.id),
    ]


# --- Task 8.3: database failures --------------------------------------------


def test_history_database_failure_returns_500_and_closes_the_session(monkeypatch):
    session = Session(bind=engine, expire_on_commit=False)
    try:
        # Like a session that is mid-request: it already holds a pooled connection.
        session.execute(text("SELECT 1"))
        assert session.in_transaction()
        checked_out_before = engine.pool.checkedout()

        def lost_connection(*args, **kwargs):
            raise OperationalError("SELECT ...", {}, Exception("connection lost"))

        monkeypatch.setattr("app.database.SessionLocal", lambda: session)
        monkeypatch.setattr(session, "scalars", lost_connection)
        failing_client = TestClient(app, raise_server_exceptions=False)

        response = failing_client.get("/benchmarks/bug-001-average/runs")

        assert response.status_code == 500
        assert "connection lost" not in response.text  # internals are not leaked
        assert not session.in_transaction()
        assert engine.pool.checkedout() == checked_out_before - 1
    finally:
        session.close()


# --- Task 8.4: history API contract -----------------------------------------


def _insert_runs(db_session, count, case_id="bug-001-average"):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.add_all(
        BenchmarkExecution(
            case_id=case_id,
            outcome="reproduced",
            exit_code=1,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(count)
    )
    db_session.commit()


def test_history_default_limit_is_50(client, db_session):
    _insert_runs(db_session, 101)

    response = client.get("/benchmarks/bug-001-average/runs")

    assert response.status_code == 200
    assert len(response.json()) == 50


def test_history_minimum_limit_is_1(client, db_session):
    _insert_runs(db_session, 3)

    response = client.get("/benchmarks/bug-001-average/runs", params={"limit": 1})

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_history_maximum_limit_is_100(client, db_session):
    _insert_runs(db_session, 101)

    response = client.get("/benchmarks/bug-001-average/runs", params={"limit": 100})

    assert response.status_code == 200
    assert len(response.json()) == 100


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc"])
def test_history_rejects_out_of_range_or_invalid_limit(client, limit):
    response = client.get("/benchmarks/bug-001-average/runs", params={"limit": limit})

    assert response.status_code == 422


def test_history_response_has_exactly_the_declared_fields(client, db_session):
    _insert_runs(db_session, 1)

    [run] = client.get("/benchmarks/bug-001-average/runs").json()

    assert set(run) == {
        "id",
        "case_id",
        "outcome",
        "exit_code",
        "stdout",
        "stderr",
        "duration_seconds",
        "timed_out",
        "created_at",
    }


def test_history_response_does_not_leak_undeclared_attributes(client, monkeypatch):
    leaky = SimpleNamespace(
        id="3f2b0c1e-7d34-4c6a-9d11-0a1b2c3d4e5f",
        case_id="bug-001-average",
        outcome="reproduced",
        exit_code=1,
        stdout="",
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        internal_secret="must-not-appear",
    )
    monkeypatch.setattr(
        BenchmarkExecutionRepository, "list_by_case", lambda self, case_id, limit: [leaky]
    )

    response = client.get("/benchmarks/bug-001-average/runs")

    assert response.status_code == 200
    assert "internal_secret" not in response.json()[0]
    assert "must-not-appear" not in response.text
