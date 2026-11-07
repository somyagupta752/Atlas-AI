"""Investigation data models: limits, hypotheses, evidence and the final diagnosis.

Two layers for the diagnosis, kept apart on purpose:

- ``DiagnosisDraft``: what the model proposes in its final answer. Untrusted.
- ``RootCauseAnalysis``: what the application returns, after the draft was
  validated against the observations actually collected. Fields the model
  must not decide (status on a limit, tests run, counters) come from the
  application, never from the draft.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.llm import LLMUsage
from app.knowledge_graph.models import REPOSITORY_ID_PATTERN

MAX_BUG_REPORT_LENGTH = 10_000


class InvestigationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"  # a validated, evidence-backed diagnosis
    INCONCLUSIVE = "inconclusive"  # the evidence was not sufficient
    LIMIT_REACHED = "limit_reached"  # a configured limit stopped the investigation
    FAILED = "failed"  # an unrecoverable error
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(InvestigationStatus) - {InvestigationStatus.RUNNING}

# Allowed lifecycle transitions. Terminal states are final.
ALLOWED_TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    InvestigationStatus.RUNNING: frozenset(TERMINAL_STATUSES),
    **{status: frozenset() for status in TERMINAL_STATUSES},
}


class InvalidTransitionError(ValueError):
    pass


def check_transition(current: InvestigationStatus, new: InvestigationStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"Cannot move an investigation from {current} to {new}")


class StopReason(StrEnum):
    """Why the agent loop stopped. Recorded on every result."""

    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    MAX_TOOL_CALLS = "max_tool_calls"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REPEATED_TOOL_CALLS = "repeated_tool_calls"
    INVALID_TOOL_CALLS = "invalid_tool_calls"
    EMPTY_RESPONSES = "empty_responses"
    REFUSAL = "refusal"
    LLM_ERROR = "llm_error"


LIMIT_STOP_REASONS = frozenset(
    {
        StopReason.MAX_ITERATIONS,
        StopReason.MAX_TOOL_CALLS,
        StopReason.TIMEOUT,
        StopReason.REPEATED_TOOL_CALLS,
        StopReason.INVALID_TOOL_CALLS,
        StopReason.EMPTY_RESPONSES,
    }
)


class AgentLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_iterations: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=20, ge=1)
    tool_timeout_seconds: float = Field(default=15.0, gt=0)
    max_tool_output_bytes: int = Field(default=20_000, ge=1_000)
    max_duration_seconds: float = Field(default=120.0, gt=0)
    # A repeated identical call is answered from the earlier observation, not re-run.
    # After this many repeats the loop stops.
    max_repeated_tool_calls: int = Field(default=3, ge=1)
    # Consecutive responses whose every tool call was rejected (unknown tool, bad arguments).
    max_consecutive_invalid_responses: int = Field(default=3, ge=1)
    max_empty_responses: int = Field(default=2, ge=1)
    # One extra model call asking to correct an invalid final diagnosis.
    diagnosis_repair_attempts: int = Field(default=1, ge=0, le=3)
    # On a limit, one extra call without tools to summarise what was found.
    finalize_on_limit: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> "AgentLimits":
        return cls(
            max_iterations=settings.agent_max_iterations,
            max_tool_calls=settings.agent_max_tool_calls,
            tool_timeout_seconds=settings.agent_tool_timeout_seconds,
            max_tool_output_bytes=settings.agent_max_tool_output_bytes,
            max_duration_seconds=settings.agent_max_duration_seconds,
        )


class SourceLocation(BaseModel):
    """A file range a tool actually returned. ``None`` lines mean file-level."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    start_line: int | None = None
    end_line: int | None = None

    def covers(self, file_path: str, start: int | None, end: int | None) -> bool:
        if file_path != self.file_path:
            return False
        if start is None and end is None:
            return True
        if self.start_line is None or self.end_line is None:
            return False
        start = start if start is not None else end
        end = end if end is not None else start
        return self.start_line <= start and end <= self.end_line


# --- hypotheses and evidence ----------------------------------------------------

HypothesisStatus = Literal["proposed", "supported", "rejected", "inconclusive"]
EvidenceSourceType = Literal["source_code", "test", "graph", "retrieval", "execution"]

_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,40}$"


class Hypothesis(BaseModel):
    id: str = Field(pattern=_ID_PATTERN)
    description: str = Field(min_length=1, max_length=2000)
    status: HypothesisStatus = "proposed"
    supporting_evidence: list[str] = Field(default_factory=list)  # evidence IDs
    contradicting_evidence: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """A fact the agent derived from one or more tool observations.

    An observation is what a tool returned. Evidence is the claim drawn from
    it, linked to the hypotheses it supports or contradicts.
    """

    id: str = Field(pattern=_ID_PATTERN)
    source_type: EvidenceSourceType
    # The tool observations this claim is drawn from. At least one is required.
    observation_ids: list[str] = Field(min_length=1)
    file_path: str | None = None
    qualified_name: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    observation: str = Field(min_length=1, max_length=2000)
    supports_hypothesis_ids: list[str] = Field(default_factory=list)
    contradicts_hypothesis_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _lines(self) -> "Evidence":
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        if (self.start_line is not None or self.end_line is not None) and not self.file_path:
            raise ValueError("line numbers need a file_path")
        overlap = set(self.supports_hypothesis_ids) & set(self.contradicts_hypothesis_ids)
        if overlap:
            raise ValueError(f"evidence cannot both support and contradict {sorted(overlap)}")
        return self


class TestRunRecord(BaseModel):
    """A test execution, copied from the run_tests observation. Never from the model."""

    __test__ = False

    observation_id: str
    test_path: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    outcome: Literal["passed", "failed", "timed_out", "error"]
    summary: str | None = None


class DiagnosisDraft(BaseModel):
    """The model's proposed final answer. Validated strictly: unknown fields are errors."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "inconclusive"]
    summary: str = Field(min_length=1, max_length=4000)
    root_cause: str | None = Field(default=None, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=20)
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    affected_files: list[str] = Field(default_factory=list, max_length=50)
    suggested_fix: str | None = Field(default=None, max_length=4000)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("confidence", mode="before")
    @classmethod
    def _numeric(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a number between 0 and 1")  # noqa: TRY004 - pydantic needs ValueError
        return value


class RootCauseAnalysis(BaseModel):
    investigation_id: str
    repository_id: str
    status: Literal["completed", "inconclusive", "limit_reached", "failed", "cancelled"]
    summary: str
    root_cause: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    # Task 12 never modifies files: a suggestion is only ever a suggestion.
    fix_applied: Literal[False] = False
    tests_run: list[TestRunRecord] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    failure_reason: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0
    model: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)

    @model_validator(mode="after")
    def _consistent(self) -> "RootCauseAnalysis":
        if self.status == "completed" and not self.root_cause:
            raise ValueError("a completed investigation needs a root cause")
        if self.status in ("inconclusive", "failed", "cancelled") and self.root_cause is not None:
            raise ValueError(f"a {self.status} investigation must not claim a root cause")
        return self


class InvestigateRequest(BaseModel):
    """Body of POST /debug/investigate."""

    repository_id: str = Field(pattern=REPOSITORY_ID_PATTERN)
    bug_report: str = Field(min_length=1, max_length=MAX_BUG_REPORT_LENGTH)

    @field_validator("bug_report")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("bug_report must not be empty")
        return value.strip()
