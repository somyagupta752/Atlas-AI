"""Persistent evaluation records (Task 14.1).

Raw case and attempt rows are the source of truth. Metric snapshots are derived,
append-only copies kept for history and fast display.

Conventions
-----------
* ``NULL`` means *not recorded* or *not applicable*, never "false" or "zero". A case
  whose diagnosis failed has ``final_verified IS NULL``: no patch was attempted.
* Money uses ``NUMERIC(12, 6)``. ``cost_kind`` says whether it is provider-reported
  (``actual``) or computed from a price table (``estimated``). Unknown cost is NULL.
* History is never deleted by cascade. Foreign keys are ``RESTRICT``. Database
  triggers (see the migration) reject edits to a configuration, to a finished run
  and its case results and attempts, and to metric snapshots.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

MONEY = Numeric(12, 6)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELLED}
)


class EvaluationMode(StrEnum):
    REFERENCE = "reference"
    LIVE = "live"


class DiagnosisMode(StrEnum):
    AGENT = "agent"
    RECORDED = "recorded"


class CaseStatus(StrEnum):
    PENDING = "pending"  # selected, not started
    RUNNING = "running"
    COMPLETED = "completed"  # the pipeline ran to an outcome (verified or not)
    FAILED = "failed"  # a provider, system or diagnosis failure stopped this case
    INCOMPLETE = "incomplete"  # cut short: budget exhausted, timeout, interruption, never started


class CostKind(StrEnum):
    ACTUAL = "actual"
    ESTIMATED = "estimated"


class AttemptStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    VALIDATION_FAILED = "validation_failed"
    INCOMPLETE = "incomplete"


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationConfiguration(Base):
    """One immutable version of an experiment setup. Cloning creates a new row."""

    __tablename__ = "evaluation_configurations"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_evaluation_configurations_name_version"),
        CheckConstraint("version >= 1", name="ck_evaluation_configurations_version"),
        Index("ix_evaluation_configurations_created_at", "created_at"),
        Index("ix_evaluation_configurations_model", "model_provider", "model_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_configurations.id", ondelete="RESTRICT"), nullable=True
    )
    # Normalised, validated, secret-free configuration. The source of truth.
    config: Mapped[dict] = mapped_column(JSONB)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    # Denormalised for filtering and grouping. Always derived from ``config``.
    model_provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(200))
    retrieval_strategy: Mapped[str] = mapped_column(String(50))
    test_visibility: Mapped[str] = mapped_column(String(50))
    test_backend: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = _created_at()

    runs: Mapped[list["EvaluationRun"]] = relationship(back_populates="configuration")


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RunStatus), name="ck_evaluation_runs_status"),
        CheckConstraint(_in("evaluation_mode", EvaluationMode), name="ck_evaluation_runs_mode"),
        CheckConstraint(_in("diagnosis_mode", DiagnosisMode), name="ck_evaluation_runs_diagnosis_mode"),
        CheckConstraint("total_cases >= 0", name="ck_evaluation_runs_total_cases"),
        CheckConstraint(
            "cost_kind IS NULL OR " + _in("cost_kind", CostKind), name="ck_evaluation_runs_cost_kind"
        ),
        Index("ix_evaluation_runs_created_at", "created_at"),
        Index("ix_evaluation_runs_status", "status"),
        Index("ix_evaluation_runs_configuration_id", "configuration_id"),
        Index("ix_evaluation_runs_benchmark_version", "benchmark_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.PENDING.value)
    evaluation_mode: Mapped[str] = mapped_column(String(20))
    diagnosis_mode: Mapped[str] = mapped_column(String(20))
    # NULL for imported historical runs that predate configurations.
    configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_configurations.id", ondelete="RESTRICT"), nullable=True
    )
    # Complete copy of the configuration (or, for imports, only what the source recorded).
    configuration_snapshot: Mapped[dict] = mapped_column(JSONB)
    benchmark_version: Mapped[str] = mapped_column(String(100))
    # Case IDs selected at creation. The denominator for "did every case finish?".
    selected_case_ids: Mapped[list] = mapped_column(JSONB)
    total_cases: Mapped[int] = mapped_column(Integer)
    # Git commit, dependency and image identity, and the test backend. See runner.environment_info.
    environment: Mapped[dict] = mapped_column(JSONB, default=dict)
    budget_limit_usd: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    cost_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    total_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Blocks duplicate accidental runs: one run per key.
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    # Historical import provenance. ``is_historical`` rows were not produced by this system's runner.
    is_historical: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    configuration: Mapped[EvaluationConfiguration | None] = relationship(back_populates="runs")
    cases: Mapped[list["EvaluationCaseResult"]] = relationship(
        back_populates="run", order_by="EvaluationCaseResult.case_id, EvaluationCaseResult.variant"
    )
    calculations: Mapped[list["EvaluationMetricCalculation"]] = relationship(
        back_populates="run", order_by="EvaluationMetricCalculation.version"
    )


class EvaluationCaseResult(Base):
    """One benchmark case (and variant) in a run. Nullable columns are unknown or not applicable."""

    __tablename__ = "evaluation_case_results"
    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "case_id", "variant", name="uq_evaluation_cases_run_case_variant"),
        CheckConstraint(_in("status", CaseStatus), name="ck_evaluation_cases_status"),
        CheckConstraint("attempts_count >= 0", name="ck_evaluation_cases_attempts"),
        CheckConstraint(
            "cost_kind IS NULL OR " + _in("cost_kind", CostKind), name="ck_evaluation_cases_cost_kind"
        ),
        Index("ix_evaluation_cases_run", "evaluation_run_id"),
        Index("ix_evaluation_cases_case_id", "case_id"),
        Index("ix_evaluation_cases_status", "status"),
        Index("ix_evaluation_cases_created_at", "created_at"),
        Index("ix_evaluation_cases_category", "category"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="RESTRICT")
    )
    case_id: Mapped[str] = mapped_column(String(100))
    # Reference mode scores several fixed patches per case; live runs use "live".
    variant: Mapped[str] = mapped_column(String(100), default="live", server_default="live")
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=CaseStatus.PENDING.value)
    # Patch execution outcome (verified, rejected, ...) when a patch pipeline ran.
    execution_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    diagnosis_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    diagnosis_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    diagnosis_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    patch_generated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    patch_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    patch_applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_attempt_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    final_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Evidence-based: first attempt failed, a later attempt received its feedback and verified.
    repaired_by_retry: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Verified, correct files and not failing held-out tests. The strict success criterion.
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    heldout_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exploratory_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    regression_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attempts_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_cost_usd: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    cost_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Links into the existing records, when this case was persisted there.
    diagnosis_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="RESTRICT"), nullable=True
    )
    patch_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Everything else the pipeline recorded: diagnosis, held-out and exploratory outcomes per test,
    # files modified, budget ledger. For imports, the original score object, unchanged.
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created_at()

    run: Mapped[EvaluationRun] = relationship(back_populates="cases")
    attempts: Mapped[list["EvaluationAttempt"]] = relationship(
        back_populates="case_result", order_by="EvaluationAttempt.attempt_number"
    )


class EvaluationAttempt(Base):
    __tablename__ = "evaluation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "evaluation_case_result_id", "attempt_number", name="uq_evaluation_attempts_case_number"
        ),
        CheckConstraint("attempt_number >= 1", name="ck_evaluation_attempts_number"),
        CheckConstraint(_in("status", AttemptStatus), name="ck_evaluation_attempts_status"),
        CheckConstraint(
            "cost_kind IS NULL OR " + _in("cost_kind", CostKind), name="ck_evaluation_attempts_cost_kind"
        ),
        Index("ix_evaluation_attempts_case", "evaluation_case_result_id"),
        Index("ix_evaluation_attempts_created_at", "created_at"),
        Index("ix_evaluation_attempts_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evaluation_case_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_case_results.id", ondelete="RESTRICT")
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    patch_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    patch_applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    required_tests_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Held-out tests run once, on the final patch, so this is NULL for earlier attempts.
    heldout_tests_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    regression_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Earlier attempts whose failure output this attempt was given, and the exact reasons.
    feedback_from_attempts: Mapped[list] = mapped_column(JSONB, default=list)
    feedback_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    unified_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    cost_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Validation errors, verification checks, test outcomes: the proof behind the booleans.
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created_at()

    case_result: Mapped[EvaluationCaseResult] = relationship(back_populates="attempts")


class EvaluationMetricCalculation(Base):
    """One scoring pass over a run. Recalculating adds a new version; old ones stay."""

    __tablename__ = "evaluation_metric_calculations"
    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "version", name="uq_evaluation_calculations_run_version"),
        CheckConstraint("version >= 1", name="ck_evaluation_calculations_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    # Why and how: "initial", "recalculation requested through the API", metrics definition version.
    reason: Mapped[str] = mapped_column(String(300))
    metrics_version: Mapped[str] = mapped_column(String(50))
    # Hash of the case and attempt rows that were scored, to show the evidence did not change.
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    calculated_at: Mapped[datetime] = _created_at()

    run: Mapped[EvaluationRun] = relationship(back_populates="calculations")
    metrics: Mapped[list["EvaluationMetricSnapshot"]] = relationship(
        back_populates="calculation", order_by="EvaluationMetricSnapshot.metric_name"
    )


class EvaluationMetricSnapshot(Base):
    __tablename__ = "evaluation_metric_snapshots"
    __table_args__ = (
        # NULLS NOT DISTINCT: the overall metric (grouping_key NULL) is unique too.
        UniqueConstraint(
            "calculation_id", "metric_name", "grouping_key",
            name="uq_evaluation_metrics_calculation_name_group", postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("denominator >= 0 AND numerator >= 0", name="ck_evaluation_metrics_counts"),
        Index("ix_evaluation_metrics_run", "evaluation_run_id"),
        Index("ix_evaluation_metrics_name", "metric_name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="RESTRICT")
    )
    calculation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_metric_calculations.id", ondelete="RESTRICT")
    )
    metric_name: Mapped[str] = mapped_column(String(100))
    # NULL when the denominator is zero: the metric is undefined, not 0%.
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    numerator: Mapped[float] = mapped_column(Float)
    denominator: Mapped[float] = mapped_column(Float)
    # NULL: the run as a whole. Otherwise "category:pagination" and similar.
    grouping_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    calculated_at: Mapped[datetime] = _created_at()

    calculation: Mapped[EvaluationMetricCalculation] = relationship(back_populates="metrics")


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "AttemptStatus",
    "CaseStatus",
    "CostKind",
    "DiagnosisMode",
    "EvaluationAttempt",
    "EvaluationCaseResult",
    "EvaluationConfiguration",
    "EvaluationMetricCalculation",
    "EvaluationMetricSnapshot",
    "EvaluationMode",
    "EvaluationRun",
    "RunStatus",
]


class EvaluationBaseline(Base):
    """A completed run designated as the reference for later comparisons. Append-only; the run is untouched."""

    __tablename__ = "evaluation_baselines"
    __table_args__ = (
        Index("ix_evaluation_baselines_created_at", "created_at"),
        Index("ix_evaluation_baselines_benchmark_version", "benchmark_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evaluation_runs.id", ondelete="RESTRICT")
    )
    benchmark_version: Mapped[str] = mapped_column(String(100))
    configuration_snapshot: Mapped[dict] = mapped_column(JSONB)
    baseline_metrics: Mapped[list] = mapped_column(JSONB)
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")
    created_at: Mapped[datetime] = _created_at()
