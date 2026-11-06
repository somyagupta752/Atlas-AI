import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class AnalyzeRepositoryRequest(BaseModel):
    repository_path: str = Field(min_length=1, description="Absolute path to a local repository")

    @field_validator("repository_path")
    @classmethod
    def must_be_absolute(cls, value: str) -> str:
        # Relative paths would resolve against the server's working directory.
        if not Path(value).expanduser().is_absolute():
            raise ValueError("repository_path must be an absolute path")
        return value
