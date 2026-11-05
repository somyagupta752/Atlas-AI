import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RunBenchmarkRequest(BaseModel):
    case_id: str


class BenchmarkExecutionRecord(BaseModel):
    """A stored benchmark execution, as returned by the history endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: str
    outcome: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    created_at: datetime
