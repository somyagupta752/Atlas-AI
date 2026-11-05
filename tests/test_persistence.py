import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import delete

from app import models  # noqa: F401
from app.benchmark import BenchmarkExecutionResult
from app.database import Base, SessionLocal, engine
from app.models import BenchmarkExecution
from app.repository import BenchmarkExecutionRepository
from app.test_runner import TestRunResult


def test_committed_data_is_visible_to_a_fresh_session():
    """Real commits (no rollback fixture), as after a server restart."""
    case_id = f"persistence-{uuid.uuid4()}"
    result = BenchmarkExecutionResult(
        case_id=case_id,
        outcome="reproduced",
        test_run=TestRunResult(
            command=["x"],
            exit_code=1,
            stdout="out",
            stderr="",
            duration_seconds=0.1,
            timed_out=False,
        ),
    )
    try:
        with SessionLocal() as writer:
            saved_id = BenchmarkExecutionRepository(writer).save(result).id

        with SessionLocal() as reader:
            [row] = BenchmarkExecutionRepository(reader).list_by_case(case_id)
        assert row.id == saved_id
        assert row.outcome == "reproduced"
    finally:
        with SessionLocal() as cleanup:
            cleanup.execute(
                delete(BenchmarkExecution).where(BenchmarkExecution.case_id == case_id)
            )
            cleanup.commit()


def test_database_schema_matches_models():
    """Fails if a model changed without a migration, or migrations were not applied."""
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)

    assert diff == []
