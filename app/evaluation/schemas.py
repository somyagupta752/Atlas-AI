"""Response and request models for the evaluation API. No secrets, no paths, no internals."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A different name starts version 1 of that name. Omitted: the next version of the same name.
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Nested partial configuration, merged into the source, then validated as a whole.
    overrides: dict[str, Any] = Field(default_factory=dict)


class ConfigurationOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    parent_configuration_id: uuid.UUID | None
    config: dict[str, Any]
    config_hash: str
    model_provider: str
    model_id: str
    retrieval_strategy: str
    test_visibility: str
    test_backend: str
    created_at: datetime
    runs_using: int = 0


class Page(BaseModel):
    limit: int
    offset: int
    total: int


class ConfigurationListOut(BaseModel):
    items: list[ConfigurationOut]
    page: Page


class RunOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    evaluation_mode: str
    diagnosis_mode: str
    configuration_id: uuid.UUID | None
    configuration_snapshot: dict[str, Any]
    benchmark_version: str
    selected_case_ids: list[str]
    total_cases: int
    case_status_counts: dict[str, int] = Field(default_factory=dict)
    environment: dict[str, Any]
    budget_limit_usd: str | None
    started_at: datetime | None
    completed_at: datetime | None
    total_cost_usd: str | None
    cost_kind: str | None
    total_duration_seconds: float | None
    failure_reason: str | None
    is_historical: bool
    source_filename: str | None
    imported_at: datetime | None
    created_at: datetime


class RunListOut(BaseModel):
    items: list[RunOut]
    page: Page


class AttemptOut(BaseModel):
    id: uuid.UUID
    attempt_number: int
    status: str
    patch_valid: bool | None
    patch_applied: bool | None
    required_tests_passed: bool | None
    heldout_tests_passed: bool | None
    regression_detected: bool | None
    feedback_from_attempts: list[int]
    feedback_reasons: list[str]
    failure_reason: str | None
    unified_diff: str | None
    diff_sha256: str | None
    cost_usd: str | None
    cost_kind: str | None
    latency_seconds: float | None
    evidence: dict[str, Any]
    created_at: datetime


class CaseOut(BaseModel):
    id: uuid.UUID
    case_id: str
    variant: str
    category: str | None
    status: str
    execution_status: str | None
    diagnosis_status: str | None
    diagnosis_correct: bool | None
    patch_generated: bool | None
    patch_valid: bool | None
    patch_applied: bool | None
    first_attempt_verified: bool | None
    final_verified: bool | None
    repaired_by_retry: bool | None
    success: bool | None
    heldout_passed: bool | None
    exploratory_passed: bool | None
    regression_detected: bool | None
    attempts_count: int
    total_cost_usd: str | None
    cost_kind: str | None
    total_latency_seconds: float | None
    diagnosis_record_id: uuid.UUID | None
    failure_reason: str | None
    created_at: datetime


class CaseDetailOut(CaseOut):
    evidence: dict[str, Any]
    attempts: list[AttemptOut]
    other_variants: list[str] = Field(default_factory=list)


class CaseListOut(BaseModel):
    items: list[CaseOut]
    page: Page


class MetricOut(BaseModel):
    name: str
    numerator: float
    denominator: float
    value: float | None
    grouping_key: str | None = None


class MetricsOut(BaseModel):
    evaluation_run_id: uuid.UUID
    calculation_version: int | None
    calculated_at: datetime | None
    reason: str | None
    metrics_version: str | None
    evidence_sha256: str | None
    populations: dict[str, int]
    metrics: list[MetricOut]
    by_category: dict[str, list[MetricOut]]


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_run_ids: list[uuid.UUID] = Field(min_length=2, max_length=10)
