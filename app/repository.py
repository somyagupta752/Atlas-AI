from sqlalchemy import select
from sqlalchemy.orm import Session

from app.benchmark import BenchmarkExecutionResult
from app.models import BenchmarkExecution


class BenchmarkExecutionRepository:
    """All database access for benchmark executions lives here, not in routes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, result: BenchmarkExecutionResult) -> BenchmarkExecution:
        run = result.test_run
        execution = BenchmarkExecution(
            case_id=result.case_id,
            outcome=result.outcome,
            exit_code=run.exit_code,
            stdout=run.stdout,
            stderr=run.stderr,
            duration_seconds=run.duration_seconds,
            timed_out=run.timed_out,
        )
        self._session.add(execution)
        self._session.commit()
        self._session.refresh(execution)  # loads id and the database-set created_at
        return execution

    def list_by_case(self, case_id: str, limit: int = 100) -> list[BenchmarkExecution]:
        """Newest first."""
        statement = (
            select(BenchmarkExecution)
            .where(BenchmarkExecution.case_id == case_id)
            .order_by(BenchmarkExecution.created_at.desc(), BenchmarkExecution.id)
            .limit(limit)
        )
        return list(self._session.scalars(statement))
