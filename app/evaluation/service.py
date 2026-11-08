"""Starting, tracking and recovering evaluation runs (Task 14.3).

``EvaluationService.start`` validates everything that can be checked without spending money,
creates the ``pending`` record, and hands execution to a dispatcher. The dispatcher is a plain
background thread: the simplest mechanism that fits a single-process API. There is no queue
and no second worker, so a restart interrupts a running evaluation. ``recover_interrupted_runs``
then closes it honestly as ``partial`` or ``failed``.
"""

import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.evaluation.benchmarks import BenchmarkSuite, UnknownBenchmarkError, load_suite
from app.evaluation.environment import sha256_json
from app.evaluation.models import EvaluationCaseResult, EvaluationRun, RunStatus
from app.evaluation.repository import ConfigurationRepository, RunRepository
from app.evaluation.runner import (
    EvaluationRunner,
    RunContext,
    environment_record,
)

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
MAX_CASES = 100


class RunRequest(BaseModel):
    """Body of POST /evaluations/run. Case IDs come from the registry; paths and commands are not accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    configuration_id: uuid.UUID
    # Reference mode may omit this (all cases). A paid run must name its cases.
    benchmark_ids: list[str] | None = Field(default=None, min_length=1, max_length=MAX_CASES)
    mode: Literal["reference", "live"]
    diagnosis_mode: Literal["recorded", "agent"] = "recorded"
    # Live only: the hard cap for every model call of this run. Must not exceed the configuration's budget.
    max_cost_usd: float | None = Field(default=None, gt=0, le=100)
    case_limit: int | None = Field(default=None, ge=1, le=MAX_CASES)
    # A paid run does nothing until this is true.
    confirm_paid: bool = False
    idempotency_key: str | None = None

    @field_validator("benchmark_ids")
    @classmethod
    def _unique(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("benchmark_ids must not repeat a case")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _key(cls, value):
        if value is not None and not KEY_PATTERN.match(value):
            raise ValueError("idempotency_key may contain letters, digits and _ . : - (max 100)")
        return value


class RunConflictError(Exception):
    def __init__(self, message: str, existing_run_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.existing_run_id = existing_run_id


class RunRequestError(ValueError):
    """The request is well formed but cannot be run (unknown case, live without budget...)."""


class RunDispatcher:
    """Runs evaluations on background threads and lets callers cancel or wait for them."""

    def __init__(self) -> None:
        self._threads: dict[uuid.UUID, tuple[threading.Thread, EvaluationRunner]] = {}
        self._lock = threading.Lock()

    def submit(self, run_id: uuid.UUID, runner: EvaluationRunner, context: RunContext) -> None:
        thread = threading.Thread(target=runner.execute, args=(run_id, context), name=f"evaluation-{run_id}",
                                  daemon=True)
        with self._lock:
            self._threads[run_id] = (thread, runner)
        thread.start()

    def cancel(self, run_id: uuid.UUID) -> bool:
        with self._lock:
            entry = self._threads.get(run_id)
        if entry is None or not entry[0].is_alive():
            return False
        entry[1].cancel_event.set()
        return True

    def is_active(self, run_id: uuid.UUID) -> bool:
        with self._lock:
            entry = self._threads.get(run_id)
        return bool(entry and entry[0].is_alive())

    def join(self, run_id: uuid.UUID, timeout: float | None = None) -> None:
        with self._lock:
            entry = self._threads.get(run_id)
        if entry:
            entry[0].join(timeout)


class InlineDispatcher(RunDispatcher):
    """Runs the evaluation before ``submit`` returns. For tests and scripts."""

    def submit(self, run_id: uuid.UUID, runner: EvaluationRunner, context: RunContext) -> None:
        runner.execute(run_id, context)

    def cancel(self, run_id: uuid.UUID) -> bool:
        return False


@dataclass
class EvaluationService:
    session: Session
    settings: Settings
    session_factory: Callable[[], Session]
    dispatcher: RunDispatcher
    suite: BenchmarkSuite = field(default_factory=load_suite)
    runner_factory: Callable[..., EvaluationRunner] = EvaluationRunner

    def start(self, request: RunRequest) -> tuple[EvaluationRun, bool]:
        """Create the run and start it. Returns ``(run, created)``; ``created`` is False for an idempotent repeat."""
        config_row = ConfigurationRepository(self.session, self.settings).get(request.configuration_id)
        config = config_row.config
        self._check_request(request, config)
        try:
            selected = self.suite.select(request.benchmark_ids)
        except UnknownBenchmarkError as exc:
            raise RunRequestError(str(exc)) from None
        if request.case_limit:
            selected = selected[: request.case_limit]
        budget = request.max_cost_usd if request.mode == "live" else None

        runner = self.runner_factory(session_factory=self.session_factory, settings=self.settings, suite=self.suite)
        context = runner.prepare(config, request.mode, request.diagnosis_mode, budget)  # may raise EvaluationSetupError

        units = sum(max(len(c.reference_patches), 1) if request.mode == "reference" else 1 for c in selected)
        fingerprint = sha256_json({
            "configuration": str(request.configuration_id), "mode": request.mode,
            "diagnosis": request.diagnosis_mode, "cases": [c.case_id for c in selected], "budget": budget})
        snapshot = {**config, "name": config_row.name, "configuration_version": config_row.version,
                    "configuration_hash": config_row.config_hash}

        # Serialise run creation, so two simultaneous requests cannot both pass the checks below.
        self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext('atlas-evaluation-start'))"))
        if request.idempotency_key:
            existing = self.session.scalar(select(EvaluationRun).where(
                EvaluationRun.idempotency_key == request.idempotency_key))
            if existing is not None:
                if existing.environment.get("request_fingerprint") != fingerprint:
                    raise RunConflictError("This idempotency_key was used for a different request", existing.id)
                self.session.rollback()
                return existing, False
        active = self.session.scalar(select(EvaluationRun).where(
            EvaluationRun.status.in_(("pending", "running"))).order_by(EvaluationRun.created_at).limit(1))
        if active is not None:
            same = active.environment.get("request_fingerprint") == fingerprint
            raise RunConflictError(
                "An identical evaluation is already running" if same else
                "Another evaluation is still running; wait for it to finish or cancel it", active.id)

        run = EvaluationRun(
            name=request.name, status=RunStatus.PENDING.value, evaluation_mode=request.mode,
            diagnosis_mode=request.diagnosis_mode, configuration_id=config_row.id, configuration_snapshot=snapshot,
            benchmark_version=self.suite.version, selected_case_ids=[c.case_id for c in selected],
            total_cases=units,
            environment={**environment_record(config, self.settings, self.suite, context.engine, request.mode,
                                              request.diagnosis_mode), "request_fingerprint": fingerprint},
            budget_limit_usd=None if budget is None else _decimal(budget), idempotency_key=request.idempotency_key)
        RunRepository(self.session).add_run(run)
        self.session.commit()
        self.dispatcher.submit(run.id, runner, context)
        self.session.expire_all()
        return run, True

    @staticmethod
    def _check_request(request: RunRequest, config: dict) -> None:
        if request.mode == "reference":
            if request.diagnosis_mode == "agent":
                raise RunRequestError("Reference runs use recorded diagnoses; diagnosis_mode 'agent' needs mode 'live'")
            if request.max_cost_usd is not None:
                raise RunRequestError("max_cost_usd applies to live runs only; reference runs make no model calls")
            return
        if not request.benchmark_ids:
            raise RunRequestError("A live evaluation must name its benchmark_ids")
        if request.max_cost_usd is None:
            raise RunRequestError("A live evaluation needs an explicit max_cost_usd")
        if request.max_cost_usd > config["budget"]["max_cost_usd"]:
            raise RunRequestError(
                f"max_cost_usd {request.max_cost_usd} exceeds the configuration's budget "
                f"{config['budget']['max_cost_usd']}")
        if not request.confirm_paid:
            raise RunRequestError("A live evaluation makes paid model calls; set confirm_paid to true")


def _decimal(value: float):
    from decimal import Decimal

    return Decimal(str(value))


def recover_interrupted_runs(session: Session, active: Callable[[uuid.UUID], bool], started_before: datetime) -> int:
    """Close runs left ``pending``/``running`` by a stopped process. Returns how many were closed.

    Completed cases are kept; the rest are marked incomplete, so the run reads ``partial`` (or ``failed``
    when nothing finished), never ``completed``.
    """
    repo = RunRepository(session)
    stale = session.scalars(select(EvaluationRun).where(
        EvaluationRun.status.in_(("pending", "running")), EvaluationRun.created_at < started_before)).all()
    closed = 0
    for run in stale:
        if active(run.id):
            continue
        rows = repo.list_cases(run.id, limit=10_000, offset=0)[0]
        recorded = {(c.case_id, c.variant) for c in rows}
        for row in rows:
            if row.status in ("running", "pending"):
                row.status, row.failure_reason = "incomplete", "interrupted: the server stopped during this case"
        suite = load_suite()
        for case in suite.select(run.selected_case_ids):
            names = [r.name for r in case.reference_patches] if run.evaluation_mode == "reference" else ["live"]
            for name in names:
                if (case.case_id, name) not in recorded:
                    session.add(EvaluationCaseResult(
                        evaluation_run_id=run.id, case_id=case.case_id, variant=name, category=case.category,
                        status="incomplete", failure_reason="interrupted: the server stopped before this case",
                        evidence={}, attempts_count=0))
        session.flush()
        finished = session.scalar(select(func.count()).select_from(EvaluationCaseResult).where(
            EvaluationCaseResult.evaluation_run_id == run.id, EvaluationCaseResult.status == "completed")) or 0
        repo.calculate_metrics(run.id, "closed after the server stopped during the run")
        run.status = RunStatus.PARTIAL.value if finished else RunStatus.FAILED.value
        run.failure_reason = "interrupted: the server stopped while this run was active"
        run.completed_at = datetime.now(UTC)
        closed += 1
    session.commit()
    return closed
