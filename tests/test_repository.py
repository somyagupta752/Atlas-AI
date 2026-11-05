from datetime import datetime, timezone

from app.benchmark import BenchmarkExecutionResult
from app.repository import BenchmarkExecutionRepository
from app.test_runner import TestRunResult


def _result(case_id="bug-001-average", outcome="reproduced", exit_code=1, timed_out=False):
    return BenchmarkExecutionResult(
        case_id=case_id,
        outcome=outcome,
        test_run=TestRunResult(
            command=["pytest"],
            exit_code=exit_code,
            stdout="out",
            stderr="err",
            duration_seconds=0.25,
            timed_out=timed_out,
        ),
    )


def test_save_persists_all_fields(db_session):
    repo = BenchmarkExecutionRepository(db_session)

    saved = repo.save(_result())

    assert saved.id is not None
    assert saved.created_at is not None
    assert saved.case_id == "bug-001-average"
    assert saved.outcome == "reproduced"
    assert saved.exit_code == 1
    assert saved.stdout == "out"
    assert saved.stderr == "err"
    assert saved.duration_seconds == 0.25
    assert saved.timed_out is False


def test_save_timed_out_run_stores_null_exit_code(db_session):
    repo = BenchmarkExecutionRepository(db_session)

    saved = repo.save(_result(outcome="timed_out", exit_code=None, timed_out=True))

    assert saved.exit_code is None
    assert saved.timed_out is True


def test_list_by_case_returns_only_that_case_newest_first(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    first = repo.save(_result(outcome="not_reproduced", exit_code=0))
    second = repo.save(_result())
    repo.save(_result(case_id="other-case"))
    # now() is constant inside the test's outer transaction, so set distinct times.
    first.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    db_session.commit()

    runs = repo.list_by_case("bug-001-average")

    assert [r.id for r in runs] == [second.id, first.id]


def test_list_by_case_respects_limit(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    for _ in range(3):
        repo.save(_result())

    assert len(repo.list_by_case("bug-001-average", limit=2)) == 2


def test_list_by_case_unknown_returns_empty(db_session):
    repo = BenchmarkExecutionRepository(db_session)

    assert repo.list_by_case("nope") == []


def test_tests_leave_database_clean(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    repo.save(_result())
    db_session.close()

    from sqlalchemy import text
    from app.database import SessionLocal

    with SessionLocal() as other:
        count = other.execute(text("select count(*) from benchmark_executions")).scalar()
    assert count == 0
