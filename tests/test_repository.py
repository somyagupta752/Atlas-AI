from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import DataError

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
    first.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    second.created_at = datetime(2026, 1, 2, tzinfo=UTC)
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


def test_limit_returns_the_newest_runs(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    runs = [repo.save(_result()) for _ in range(3)]
    for hour, run in enumerate(runs, start=10):
        run.created_at = datetime(2026, 1, 1, hour, tzinfo=UTC)
    db_session.commit()

    limited = repo.list_by_case("bug-001-average", limit=2)

    assert [r.id for r in limited] == [runs[2].id, runs[1].id]


def test_equal_timestamps_are_ordered_deterministically_by_id(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    runs = [repo.save(_result()) for _ in range(5)]
    same_time = datetime(2026, 1, 1, 12, tzinfo=UTC)
    for run in runs:
        run.created_at = same_time
    db_session.commit()

    first = [r.id for r in repo.list_by_case("bug-001-average")]
    second = [r.id for r in repo.list_by_case("bug-001-average")]

    assert first == second == sorted(run.id for run in runs)


def test_failed_commit_rolls_back_and_leaves_the_session_usable(db_session):
    repo = BenchmarkExecutionRepository(db_session)
    good = repo.save(_result())
    bad = _result()
    bad.test_run.stdout = "contains a NUL byte: \x00"  # PostgreSQL text rejects NUL

    with pytest.raises(DataError):
        repo.save(bad)

    # No PendingRollbackError: the session can be used again, and only the good row exists.
    assert [r.id for r in repo.list_by_case("bug-001-average")] == [good.id]
    repo.save(_result())
    assert len(repo.list_by_case("bug-001-average")) == 2
