"""HTTP endpoints for the evaluation laboratory. Thin: wiring, bounds and error mapping.

Nothing here starts a model call except the explicit run endpoint.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.access import current_user, visible_to
from app.auth.models import User
from app.config import Settings, get_settings
from app.database import SessionLocal, get_db
from app.evaluation.comparisons import RunInput, compare
from app.evaluation.configuration import ConfigurationSpec, SecretFoundError
from app.evaluation.metrics import summarize_cases
from app.evaluation.models import (
    EvaluationAttempt,
    EvaluationCaseResult,
    EvaluationConfiguration,
    EvaluationRun,
)
from app.evaluation.repository import (
    CaseNotFoundError,
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    ConfigurationRepository,
    RunNotFoundError,
    RunRepository,
)
from app.evaluation.runner import EvaluationSetupError
from app.evaluation.schemas import (
    AttemptOut,
    CaseDetailOut,
    CaseListOut,
    CaseOut,
    CloneRequest,
    CompareRequest,
    ConfigurationListOut,
    ConfigurationOut,
    MetricOut,
    MetricsOut,
    Page,
    RunListOut,
    RunOut,
)
from app.evaluation.service import (
    EvaluationService,
    RunConflictError,
    RunDispatcher,
    RunRequest,
    RunRequestError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluations", tags=["evaluation"], dependencies=[Depends(current_user)])

MAX_PAGE_SIZE = 100
MAX_OFFSET = 1_000_000


def safe_validation_errors(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    """Field, message and type only. Never the input: a rejected request may contain a secret."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else exc.errors(
        include_url=False, include_input=False, include_context=False)
    return [{"loc": [str(part) for part in error["loc"]], "msg": str(error["msg"]), "type": str(error["type"])}
            for error in errors]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/evaluations"):
            return JSONResponse(status_code=422, content={"detail": safe_validation_errors(exc)})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(SQLAlchemyError)
    async def database_handler(request: Request, exc: SQLAlchemyError):
        logger.error("Database error on %s: %s", request.url.path, type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "The evaluation database failed"})


def get_settings_dependency() -> Settings:
    return get_settings()


def get_configuration_repository(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dependency)
) -> ConfigurationRepository:
    return ConfigurationRepository(db, settings)


def configuration_out(row: EvaluationConfiguration, runs_using: int = 0) -> ConfigurationOut:
    return ConfigurationOut(
        id=row.id, name=row.name, version=row.version, parent_configuration_id=row.parent_configuration_id,
        config=row.config, config_hash=row.config_hash, model_provider=row.model_provider,
        model_id=row.model_id, retrieval_strategy=row.retrieval_strategy, test_visibility=row.test_visibility,
        test_backend=row.test_backend, created_at=row.created_at, runs_using=runs_using,
    )


def _commit(db: Session) -> None:
    db.commit()


# --- configurations ----------------------------------------------------------------


@router.post("/configurations", status_code=201, response_model=ConfigurationOut)
def create_configuration(
    spec: ConfigurationSpec,
    db: Session = Depends(get_db),
    repository: ConfigurationRepository = Depends(get_configuration_repository),
    user: User = Depends(current_user),
) -> ConfigurationOut:
    try:
        row = repository.create(spec, owner_id=user.id)
        _commit(db)
    except ConfigurationConflictError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    except SecretFoundError as exc:
        raise HTTPException(422, str(exc)) from None
    return configuration_out(row)


@router.get("/configurations", response_model=ConfigurationListOut)
def list_configurations(
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    name: str | None = Query(default=None, max_length=200),
    repository: ConfigurationRepository = Depends(get_configuration_repository),
    user: User = Depends(current_user),
) -> ConfigurationListOut:
    rows, total = repository.list_all(limit=limit, offset=offset, name=name, viewer=user)
    counts = repository.run_counts([r.id for r in rows])
    return ConfigurationListOut(items=[configuration_out(r, counts.get(r.id, 0)) for r in rows],
                                page=Page(limit=limit, offset=offset, total=total))


@router.get("/configurations/{configuration_id}", response_model=ConfigurationOut)
def get_configuration(
    configuration_id: uuid.UUID, repository: ConfigurationRepository = Depends(get_configuration_repository),
    user: User = Depends(current_user),
) -> ConfigurationOut:
    try:
        row = repository.get(configuration_id, viewer=user)
    except ConfigurationNotFoundError:
        raise HTTPException(404, "Configuration not found") from None
    return configuration_out(row, repository.run_counts([row.id]).get(row.id, 0))


@router.post("/configurations/{configuration_id}/clone", status_code=201, response_model=ConfigurationOut)
def clone_configuration(
    configuration_id: uuid.UUID,
    body: CloneRequest,
    db: Session = Depends(get_db),
    repository: ConfigurationRepository = Depends(get_configuration_repository),
    user: User = Depends(current_user),
) -> ConfigurationOut:
    try:
        row = repository.clone(configuration_id, body.overrides, body.name, owner=user)
        _commit(db)
    except ConfigurationNotFoundError:
        raise HTTPException(404, "Configuration not found") from None
    except ConfigurationConflictError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(422, safe_validation_errors(exc)) from None
    except SecretFoundError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    return configuration_out(row)


# --- runs ------------------------------------------------------------------------------

_dispatcher = RunDispatcher()
SETUP_STATUS = {"prompts_changed": 409, "embedding_mismatch": 409, "backend_unavailable": 503,
                "llm_unavailable": 503, "pricing": 503, "budget_required": 422}


def get_dispatcher() -> RunDispatcher:
    return _dispatcher


def get_session_factory():
    return SessionLocal


def get_runner_factory():
    from app.evaluation.runner import EvaluationRunner

    return EvaluationRunner


def get_run_repository(db: Session = Depends(get_db)) -> RunRepository:
    return RunRepository(db)


def visible_run(run_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    """404 for a run that is missing or belongs to someone else: its existence is not revealed."""
    run = db.get(EvaluationRun, run_id)
    if run is None or not visible_to(run.owner_id, user):
        raise HTTPException(404, "Evaluation not found")


def _money(value) -> str | None:
    return None if value is None else format(value, "f")


def run_out(run: EvaluationRun, counts: dict[str, int] | None = None) -> RunOut:
    return RunOut(
        id=run.id, name=run.name, status=run.status, evaluation_mode=run.evaluation_mode,
        diagnosis_mode=run.diagnosis_mode, configuration_id=run.configuration_id,
        configuration_snapshot=run.configuration_snapshot, benchmark_version=run.benchmark_version,
        selected_case_ids=run.selected_case_ids, total_cases=run.total_cases, case_status_counts=counts or {},
        environment=run.environment, budget_limit_usd=_money(run.budget_limit_usd), started_at=run.started_at,
        completed_at=run.completed_at, total_cost_usd=_money(run.total_cost_usd), cost_kind=run.cost_kind,
        total_duration_seconds=run.total_duration_seconds, failure_reason=run.failure_reason,
        is_historical=bool(run.is_historical), source_filename=run.source_filename, imported_at=run.imported_at,
        created_at=run.created_at)


def case_out(c: EvaluationCaseResult) -> CaseOut:
    fields = {name: getattr(c, name) for name in CaseOut.model_fields
              if name not in ("total_cost_usd",)}
    return CaseOut(**fields, total_cost_usd=_money(c.total_cost_usd))


def attempt_out(a: EvaluationAttempt) -> AttemptOut:
    fields = {name: getattr(a, name) for name in AttemptOut.model_fields if name != "cost_usd"}
    return AttemptOut(**fields, cost_usd=_money(a.cost_usd))


def _run_or_404(repo: RunRepository, run_id: uuid.UUID) -> EvaluationRun:
    try:
        return repo.get_run(run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Evaluation not found") from None


@router.post("/run", status_code=202, response_model=RunOut)
def start_run(
    body: RunRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dependency),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
    session_factory=Depends(get_session_factory),
    runner_factory=Depends(get_runner_factory),
    user: User = Depends(current_user),
) -> RunOut:
    """Validate, record and start an evaluation in the background. Poll GET /evaluations/{id}."""
    service = EvaluationService(db, settings, session_factory, dispatcher, runner_factory=runner_factory)
    try:
        run, _created = service.start(body, user)
    except ConfigurationNotFoundError:
        raise HTTPException(404, "Configuration not found") from None
    except RunRequestError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    except EvaluationSetupError as exc:
        db.rollback()
        raise HTTPException(SETUP_STATUS.get(exc.kind, 503), str(exc)) from None
    except RunConflictError as exc:
        db.rollback()
        detail = {"message": str(exc)}
        if exc.existing_run_id is not None and exc.existing_run_owned_by_caller:
            detail["existing_run_id"] = str(exc.existing_run_id)
        raise HTTPException(409, detail) from None
    return run_out(run, RunRepository(db).case_counts([run.id]).get(run.id))


@router.post("/compare")
def compare_runs(body: CompareRequest, repo: RunRepository = Depends(get_run_repository),
                 user: User = Depends(current_user)) -> dict:
    ids = list(dict.fromkeys(body.evaluation_run_ids))
    if len(ids) < 2:
        raise HTTPException(422, "Compare at least two different evaluations")
    inputs = []
    for run_id in ids:
        run = _run_or_404(repo, run_id)
        if not visible_to(run.owner_id, user):
            raise HTTPException(404, "Evaluation not found")
        if run.status in ("pending", "running"):
            raise HTTPException(409, f"Evaluation {run_id} is still running")
        inputs.append(RunInput(str(run.id), run.name, run.status, run.benchmark_version, run.evaluation_mode,
                               repo.load_case_data([run.id]), run.selected_case_ids))
    return compare(inputs)


@router.get("", response_model=RunListOut)
def list_runs(
    limit: int = Query(default=25, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    status: str | None = Query(default=None, max_length=20),
    mode: str | None = Query(default=None, max_length=20),
    configuration_id: uuid.UUID | None = None,
    repo: RunRepository = Depends(get_run_repository),
    user: User = Depends(current_user),
) -> RunListOut:
    rows, total = repo.list_runs(limit=limit, offset=offset, status=status, mode=mode,
                                 configuration_id=configuration_id, viewer=user)
    counts = repo.case_counts([r.id for r in rows])
    return RunListOut(items=[run_out(r, counts.get(r.id)) for r in rows],
                      page=Page(limit=limit, offset=offset, total=total))


@router.get("/{run_id}", response_model=RunOut, dependencies=[Depends(visible_run)])
def get_run(run_id: uuid.UUID, repo: RunRepository = Depends(get_run_repository)) -> RunOut:
    run = _run_or_404(repo, run_id)
    return run_out(run, repo.case_counts([run.id]).get(run.id))


@router.get("/{run_id}/cases", response_model=CaseListOut, dependencies=[Depends(visible_run)])
def list_cases(
    run_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
    status: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=100),
    repo: RunRepository = Depends(get_run_repository),
) -> CaseListOut:
    _run_or_404(repo, run_id)
    rows, total = repo.list_cases(run_id, limit=limit, offset=offset, status=status, category=category)
    return CaseListOut(items=[case_out(c) for c in rows], page=Page(limit=limit, offset=offset, total=total))


@router.get("/{run_id}/cases/{case_id}", response_model=CaseDetailOut, dependencies=[Depends(visible_run)])
def get_case(
    run_id: uuid.UUID, case_id: str,
    variant: str | None = Query(default=None, max_length=100),
    repo: RunRepository = Depends(get_run_repository),
) -> CaseDetailOut:
    _run_or_404(repo, run_id)
    try:
        row = repo.get_case(run_id, case_id, variant)
    except CaseNotFoundError:
        raise HTTPException(404, "Case not found in this evaluation") from None
    variants = repo.case_variants(run_id, case_id)
    return CaseDetailOut(**case_out(row).model_dump(), evidence=row.evidence,
                         attempts=[attempt_out(a) for a in row.attempts],
                         other_variants=[v for v in variants if v != row.variant])


@router.get("/{run_id}/metrics", response_model=MetricsOut, dependencies=[Depends(visible_run)])
def get_metrics(run_id: uuid.UUID, repo: RunRepository = Depends(get_run_repository)) -> MetricsOut:
    run = _run_or_404(repo, run_id)
    calculation = repo.latest_calculation(run_id)
    data = repo.load_case_data([run_id])
    if calculation is None:
        return MetricsOut(evaluation_run_id=run.id, calculation_version=None, calculated_at=None, reason=None,
                          metrics_version=None, evidence_sha256=None, populations=summarize_cases(data),
                          metrics=[], by_category={})
    overall, by_category = [], {}
    for m in calculation.metrics:
        out = MetricOut(name=m.metric_name, numerator=m.numerator, denominator=m.denominator,
                        value=m.metric_value, grouping_key=m.grouping_key)
        if m.grouping_key is None:
            overall.append(out)
        else:
            by_category.setdefault(m.grouping_key.removeprefix("category:"), []).append(out)
    return MetricsOut(
        evaluation_run_id=run.id, calculation_version=calculation.version, calculated_at=calculation.calculated_at,
        reason=calculation.reason, metrics_version=calculation.metrics_version,
        evidence_sha256=calculation.evidence_sha256, populations=summarize_cases(data), metrics=overall,
        by_category=by_category)


@router.post("/{run_id}/recalculate", response_model=MetricsOut, dependencies=[Depends(visible_run)])
def recalculate(run_id: uuid.UUID, db: Session = Depends(get_db),
                repo: RunRepository = Depends(get_run_repository)) -> MetricsOut:
    run = _run_or_404(repo, run_id)
    if run.status in ("pending", "running"):
        raise HTTPException(409, "The evaluation is still running; metrics can be recalculated when it ends")
    repo.calculate_metrics(run_id, "recalculation requested through the API")
    db.commit()
    return get_metrics(run_id, repo)


@router.post("/{run_id}/cancel", status_code=202, response_model=RunOut, dependencies=[Depends(visible_run)])
def cancel_run(run_id: uuid.UUID, repo: RunRepository = Depends(get_run_repository),
               dispatcher: RunDispatcher = Depends(get_dispatcher)) -> RunOut:
    run = _run_or_404(repo, run_id)
    if run.status not in ("pending", "running") or not dispatcher.cancel(run_id):
        raise HTTPException(409, "The evaluation is not running in this server")
    return run_out(run, repo.case_counts([run.id]).get(run.id))
