"""Patch lifecycle data models.

Four separate questions, never folded into one success flag:

1. Was a proposal generated?            -> ``PatchProposal`` exists
2. Did it pass validation?              -> ``PatchValidationResult.valid``
3. Did it apply to the isolated copy?   -> ``PatchApplyResult.applied``
4. Did verification prove the fix?      -> ``PatchVerificationResult.status``

A patch can pass 1-3 and still be rejected at 4. ``PatchStatus`` records where
each proposal is in that lifecycle, and ``check_patch_transition`` enforces the
order: a patch that failed validation can never be applied, and a patch that
failed a required check can never become verified.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.llm import LLMUsage
from app.knowledge_graph.models import REPOSITORY_ID_PATTERN

# --- lifecycle ---------------------------------------------------------------------


class PatchStatus(StrEnum):
    GENERATED = "generated"  # the LLM produced a patch
    VALIDATED = "validated"  # passed structural and safety validation
    VALIDATION_FAILED = "validation_failed"
    APPLIED = "applied"  # applied to an isolated workspace
    VERIFYING = "verifying"  # checks are running
    VERIFIED = "verified"  # every mandatory check passed
    REJECTED = "rejected"  # a check found a definite failure
    INCONCLUSIVE = "inconclusive"  # correctness could not be established
    FAILED = "failed"  # an unrecoverable internal error


TERMINAL_PATCH_STATUSES = frozenset(
    {
        PatchStatus.VALIDATION_FAILED,
        PatchStatus.VERIFIED,
        PatchStatus.REJECTED,
        PatchStatus.INCONCLUSIVE,
        PatchStatus.FAILED,
    }
)

PATCH_TRANSITIONS: dict[PatchStatus, frozenset[PatchStatus]] = {
    PatchStatus.GENERATED: frozenset({PatchStatus.VALIDATED, PatchStatus.VALIDATION_FAILED, PatchStatus.FAILED}),
    # From validated: rejected when the patch does not apply (conflict) or repeats
    # an earlier attempt; inconclusive when the environment prevents application.
    PatchStatus.VALIDATED: frozenset(
        {PatchStatus.APPLIED, PatchStatus.REJECTED, PatchStatus.INCONCLUSIVE, PatchStatus.FAILED}
    ),
    PatchStatus.APPLIED: frozenset({PatchStatus.VERIFYING, PatchStatus.FAILED}),
    PatchStatus.VERIFYING: frozenset(
        {PatchStatus.VERIFIED, PatchStatus.REJECTED, PatchStatus.INCONCLUSIVE, PatchStatus.FAILED}
    ),
    **{status: frozenset() for status in TERMINAL_PATCH_STATUSES},
}


class InvalidPatchTransitionError(ValueError):
    pass


def check_patch_transition(current: PatchStatus | str, new: PatchStatus | str) -> None:
    current, new = PatchStatus(current), PatchStatus(new)
    if new not in PATCH_TRANSITIONS[current]:
        raise InvalidPatchTransitionError(f"Cannot move a patch from {current} to {new}")


VerificationStatus = Literal["verified", "rejected", "inconclusive", "failed"]
ExecutionStatus = Literal["verified", "rejected", "inconclusive", "failed", "validation_failed"]
TimeoutPolicy = Literal["reject", "inconclusive"]


# --- limits ------------------------------------------------------------------------


class PatchLimits(BaseModel):
    """Every bound of one generate-and-verify execution. Starting values, configurable."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=3, ge=1, le=10)
    max_total_seconds: float = Field(default=180.0, gt=0, le=3600)
    max_files: int = Field(default=5, ge=1, le=50)
    max_changed_lines: int = Field(default=500, ge=1, le=10_000)
    max_diff_bytes: int = Field(default=200_000, ge=1_000, le=2_000_000)
    max_file_bytes: int = Field(default=1_000_000, ge=1_000)
    max_check_output_bytes: int = Field(default=100_000, ge=1_000, le=10_000_000)
    check_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # Global LLM budget across every attempt and repair call of one execution.
    # 3 attempts x (1 call + 1 repair) = 6 requests at most by default.
    max_llm_requests: int = Field(default=6, ge=1, le=100)
    max_llm_seconds: float = Field(default=300.0, gt=0, le=7200)
    # None disables the cost cap (library use only; the API always sets one).
    max_llm_cost_usd: float | None = Field(default=1.0, gt=0, le=100)
    # Worst-case output tokens per call when the provider does not declare its own limit.
    llm_max_output_tokens: int = Field(default=16_000, ge=1, le=200_000)
    max_selected_test_files: int = Field(default=20, ge=1, le=500)
    # The targeted baseline runs this many times. Outcomes that differ are flaky
    # and are excluded from the comparison; a flaky bug test makes the run inconclusive.
    baseline_runs: int = Field(default=2, ge=1, le=5)
    # A patched test run that times out where the baseline completed:
    # "reject" treats it as a definite failure (a likely hang), "inconclusive" does not judge.
    timeout_policy: TimeoutPolicy = "reject"
    require_ruff: bool = True
    allow_new_files: bool = False
    allow_deleted_files: bool = False
    # Changing tests to make them pass hides bugs. Off unless a separately reviewed policy allows it.
    allow_test_changes: bool = False
    # Graph-based test discovery (suggestions only; validated like any other source).
    graph_test_discovery: bool = True
    graph_max_depth: int = Field(default=3, ge=1, le=6)
    max_graph_test_files: int = Field(default=10, ge=1, le=100)
    # Build a graph from the current source when Neo4j is missing, unreachable or stale.
    graph_in_memory_fallback: bool = True
    # When the baseline does not reproduce the bug, nothing can be verified. By
    # default the execution stops before spending money on generation.
    generate_when_baseline_unreproducible: bool = False
    # Process resource limits for test and lint runs (POSIX; best effort on macOS).
    process_cpu_seconds: int = Field(default=120, ge=1)
    process_memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2)
    process_file_bytes: int = Field(default=200 * 1024**2, ge=1024**2)
    # Feedback sent to the next generation attempt.
    max_feedback_bytes: int = Field(default=12_000, ge=1_000, le=100_000)
    max_context_source_bytes: int = Field(default=80_000, ge=1_000, le=1_000_000)
    # Test files whose contents are shown to the model. 0 = test-blind: the model sees only the
    # modifiable source and learns test-encoded rules from failure feedback. Tests always run.
    max_context_test_files: int = Field(default=5, ge=0, le=50)

    @classmethod
    def from_settings(cls, settings: Any) -> "PatchLimits":
        return cls(
            max_attempts=settings.patch_max_attempts,
            max_total_seconds=settings.patch_max_total_seconds,
            max_files=settings.patch_max_files,
            max_changed_lines=settings.patch_max_changed_lines,
            max_check_output_bytes=settings.patch_max_check_output_bytes,
            check_timeout_seconds=settings.patch_check_timeout_seconds,
            llm_timeout_seconds=settings.patch_llm_timeout_seconds,
            max_llm_requests=settings.patch_max_llm_requests,
            max_llm_seconds=settings.patch_max_llm_seconds,
            max_llm_cost_usd=settings.patch_max_llm_cost_usd,
            llm_max_output_tokens=settings.llm_max_tokens,
            graph_test_discovery=settings.patch_graph_test_discovery,
            graph_max_depth=settings.patch_graph_max_depth,
            max_graph_test_files=settings.patch_max_graph_test_files,
            baseline_runs=settings.patch_baseline_runs,
            timeout_policy=settings.patch_timeout_policy,
            require_ruff=settings.patch_require_ruff,
        )


# --- proposal ----------------------------------------------------------------------


class PatchDraft(BaseModel):
    """What the model returns. Untrusted: validated strictly, then validated again as a diff."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=4000)
    unified_diff: str = Field(min_length=1, max_length=2_000_000)
    affected_files: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("unified_diff")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("unified_diff must not be empty")
        return value


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class PatchProposal(BaseModel):
    patch_id: str = Field(default_factory=new_id)
    investigation_id: str
    repository_id: str
    execution_id: str
    attempt_number: int = Field(ge=1)
    description: str
    unified_diff: str
    # Files the diff modifies. Set from the parsed diff, not from the model's claim.
    affected_files: list[str] = Field(default_factory=list)
    rationale: str
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    status: PatchStatus = PatchStatus.GENERATED
    created_at: datetime = Field(default_factory=utcnow)
    # Provenance: which model produced this change, at what cost.
    provider: str | None = None
    model: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)


class PatchValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    # SHA-256 of the canonical diff that would be applied. Identical patches share it.
    diff_sha256: str | None = None


class PatchApplyResult(BaseModel):
    applied: bool
    files_changed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # conflict: the patch does not match the source. unexpected_changes: files
    # outside the patch changed. environment: the patch tool is unavailable or failed.
    error_kind: Literal["conflict", "unexpected_changes", "content_mismatch", "environment", "invalid"] | None = None
    unexpected_changes: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


# --- test results ------------------------------------------------------------------


class TestOutcome(StrEnum):
    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class TestCaseResult(BaseModel):
    __test__ = False

    test_id: str  # stable: "path/test_file.py::Class::test_name"
    outcome: TestOutcome
    message: str | None = None
    duration_seconds: float = 0.0
    # "No module named 'x'" in the error: an environment problem unless x is part of the repository.
    missing_module: str | None = None


class TestSuiteRun(BaseModel):
    """One pytest invocation. ``status`` describes the run, ``tests`` the individual results."""

    __test__ = False

    label: str  # "baseline-1", "patched", ...
    kind: Literal["targeted", "full_regression"]
    # Workspace-independent command identity: the same for baseline and patched runs.
    command: list[str]
    targets: list[str] = Field(default_factory=list)
    status: Literal["completed", "timed_out", "no_tests", "error"]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    duration_seconds: float = 0.0
    started_at: datetime = Field(default_factory=utcnow)
    tests: dict[str, TestCaseResult] = Field(default_factory=dict)
    error: str | None = None

    def outcome(self, test_id: str) -> TestOutcome | None:
        result = self.tests.get(test_id)
        return result.outcome if result else None


Classification = Literal[
    "preserved",  # passed -> passed
    "fixed",  # failed/error -> passed
    "regressed",  # passed -> failed/error
    "still_failing",  # failed/error -> failed/error
    "became_skipped",  # passed -> skipped: coverage lost
    "skipped",  # skipped -> skipped (or skipped -> anything not failing)
    "new_passing",  # absent before, passes after
    "new_failure",  # absent before, fails after
    "missing_after",  # ran before, absent after: not comparable
    "flaky",  # baseline outcome not reproducible: not comparable
]


class TestComparison(BaseModel):
    __test__ = False

    test_id: str
    before: TestOutcome | None
    after: TestOutcome | None
    classification: Classification
    is_bug_test: bool = False


class ComparisonResult(BaseModel):
    entries: list[TestComparison] = Field(default_factory=list)
    fixed: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)  # includes new failures and lost coverage
    still_failing: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    not_comparable: list[str] = Field(default_factory=list)
    bug_tests: list[str] = Field(default_factory=list)
    bug_tests_fixed: bool = False
    summary: str = ""


# --- verification ------------------------------------------------------------------

CheckName = Literal[
    "patch_application",
    "syntax_check",
    "original_bug_test",
    "regression_tests",
    "ruff",
    "full_test_suite",
    "original_repository_unchanged",
    "duplicate_patch",
]


class VerificationCheck(BaseModel):
    name: CheckName
    status: Literal["passed", "failed", "skipped", "timed_out"]
    # A required check that is skipped or timed out prevents "verified".
    required: bool = True
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PatchVerificationResult(BaseModel):
    verification_id: str = Field(default_factory=new_id)
    patch_id: str
    status: VerificationStatus
    checks: list[VerificationCheck] = Field(default_factory=list)
    before_results: dict[str, Any] = Field(default_factory=dict)
    after_results: dict[str, Any] = Field(default_factory=dict)
    comparison: ComparisonResult | None = None
    summary: str
    # Short, specific reasons for a non-verified status. Fed back to the next attempt.
    failure_reasons: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    # What is needed to reproduce the result: interpreter, tool versions, commands, snapshot.
    environment: dict[str, Any] = Field(default_factory=dict)

    def check(self, name: str) -> VerificationCheck | None:
        return next((c for c in self.checks if c.name == name), None)


# --- execution ---------------------------------------------------------------------


class SourceSnapshot(BaseModel):
    """Identity of the repository content a workspace was copied from."""

    root_hash: str
    file_count: int
    total_bytes: int
    revision: str | None = None  # git HEAD, when the source is in a git repository
    dirty: bool | None = None  # uncommitted changes under the source root
    skipped: list[str] = Field(default_factory=list)  # excluded entries, with reasons
    captured_at: datetime = Field(default_factory=utcnow)


class TestSelectionItem(BaseModel):
    __test__ = False

    target: str  # test file relative to the repository root
    kind: Literal["targeted"] = "targeted"
    reasons: list[str] = Field(default_factory=list)
    # Specific tests in this file that reproduced the bug during the investigation.
    # Empty with reproduces_bug=True means the whole file was run.
    bug_test_names: list[str] = Field(default_factory=list)
    reproduces_bug: bool = False
    priority: int = 0
    score: float = 0.0  # ranking within a priority (graph suggestions); higher first


class TestSelection(BaseModel):
    __test__ = False

    items: list[TestSelectionItem] = Field(default_factory=list)
    full_suite: bool = False
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    # Graph discovery report: source (neo4j / in_memory / unavailable / disabled), seeds, suggestions, warnings.
    graph: dict[str, Any] | None = None

    @property
    def targets(self) -> list[str]:
        return [item.target for item in self.items]

    @property
    def bug_items(self) -> list[TestSelectionItem]:
        return [item for item in self.items if item.reproduces_bug]


class PatchAttempt(BaseModel):
    """One generation -> validation -> workspace -> application -> verification cycle."""

    attempt_number: int
    status: ExecutionStatus | Literal["generated", "validated", "applied", "verifying"]
    proposal: PatchProposal | None = None
    validation: PatchValidationResult | None = None
    workspace: dict[str, Any] | None = None  # label, snapshot hash, retained; never a path
    application: PatchApplyResult | None = None
    verification: PatchVerificationResult | None = None
    failure_reason: str | None = None
    summary: str = ""
    # Provenance of a retry: which earlier attempts' failures this attempt was given, and the
    # exact reasons. Lets the record prove a revision responded to real failure output.
    feedback_from: list[int] = Field(default_factory=list)
    feedback_reasons: list[str] = Field(default_factory=list)
    # Wall-clock time of the whole attempt (model call, validation, application, verification).
    # None in records written before this field existed: unknown, not zero.
    duration_seconds: float | None = None


class PatchExecution(BaseModel):
    execution_id: str = Field(default_factory=new_id)
    investigation_id: str
    repository_id: str
    status: ExecutionStatus
    final_patch_id: str | None = None
    attempts: list[PatchAttempt] = Field(default_factory=list)
    source_snapshot: SourceSnapshot | None = None
    test_selection: TestSelection | None = None
    baseline: dict[str, Any] | None = None
    summary: str
    failure_reason: str | None = None
    stop_reason: str | None = None
    duration_seconds: float = 0.0
    model: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    # The original repository is never written. Recorded explicitly and checked after every attempt.
    original_repository_modified: bool = False
    # Requests, model time and cost against the execution's global LLM budget, call by call.
    llm_budget: dict[str, Any] | None = None

    @property
    def final_attempt(self) -> PatchAttempt | None:
        return self.attempts[-1] if self.attempts else None


class GeneratePatchRequest(BaseModel):
    """Body of POST /debug/investigations/{id}/patches. The diagnosis comes from storage, never from here."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    run_full_test_suite: bool = True
    # Optional scoping: when given, it must match the investigation's repository.
    repository_id: str | None = Field(default=None, pattern=REPOSITORY_ID_PATTERN)
