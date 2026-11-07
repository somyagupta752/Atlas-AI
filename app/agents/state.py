"""What one investigation remembers while it runs.

- ``messages``: the conversation sent to the LLM.
- ``observations``: what tools returned, one per executed (or rejected) call.
- ``hypotheses`` / ``evidence``: the claims drawn from observations. They come
  from the final diagnosis and are validated against the observations.

The state is plain data, so it can be persisted and, later, resumed.
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.llm import LLMUsage
from app.agents.schemas import (
    Evidence,
    Hypothesis,
    InvestigationStatus,
    SourceLocation,
    StopReason,
    check_transition,
)

ObservationStatus = Literal[
    "ok",
    "unknown_tool",
    "invalid_arguments",
    "permission_denied",
    "tool_error",
    "timeout",
    "internal_error",
    "duplicate",
    "skipped",
]


class Observation(BaseModel):
    """The result of one tool call, as the model saw it."""

    id: str  # "obs-1", "obs-2", ... cited by evidence
    iteration: int
    tool_call_id: str
    tool_name: str
    arguments: Any
    status: ObservationStatus
    # Structured result for successful calls; None otherwise.
    data: dict[str, Any] | None = None
    error: str | None = None
    # The exact text returned to the model (bounded).
    content: str
    truncated: bool = False
    duration_seconds: float = 0.0
    # File ranges this observation actually showed. Evidence is checked against them.
    locations: list[SourceLocation] = Field(default_factory=list)
    # For "duplicate": the earlier observation that already answered this call.
    duplicate_of: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class InvestigationState(BaseModel):
    investigation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    repository_id: str
    bug_report: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    iteration: int = 0
    tool_calls: int = 0
    status: InvestigationStatus = InvestigationStatus.RUNNING
    stop_reason: StopReason | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = Field(default_factory=time.monotonic, exclude=True)

    def next_observation_id(self) -> str:
        return f"obs-{len(self.observations) + 1}"

    def add_observation(self, observation: Observation) -> None:
        self.observations.append(observation)
        if observation.status not in ("duplicate", "skipped", "unknown_tool", "invalid_arguments"):
            # Counts executions. Rejected and deduplicated calls did not run.
            self.tool_calls += 1

    def get_observation(self, observation_id: str) -> Observation | None:
        return next((o for o in self.observations if o.id == observation_id), None)

    def transition(self, status: InvestigationStatus) -> None:
        check_transition(self.status, status)
        self.status = status

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic
