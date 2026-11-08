"""Baseline designation and regression checks (Task 14.8): repository and endpoints.

Designating a baseline copies the run's configuration and metrics into a new row; the run itself is
not touched. Baselines are append-only and are kept even when the run performed badly.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.access import current_user, visible_to
from app.auth.models import User
from app.database import get_db
from app.evaluation.models import EvaluationBaseline
from app.evaluation.regression import (
    IncompatibleBenchmarksError,
    RegressionThresholds,
    detect_regressions,
)
from app.evaluation.repository import RunNotFoundError, RunRepository
from app.evaluation.schemas import Page

router = APIRouter(prefix="/evaluations", tags=["evaluation"], dependencies=[Depends(current_user)])


class BaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_run_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


class RegressionCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_id: uuid.UUID
    thresholds: RegressionThresholds = Field(default_factory=RegressionThresholds)


def baseline_out(b: EvaluationBaseline) -> dict[str, Any]:
    return {"id": str(b.id), "name": b.name, "evaluation_run_id": str(b.evaluation_run_id),
            "benchmark_version": b.benchmark_version, "configuration_snapshot": b.configuration_snapshot,
            "baseline_metrics": b.baseline_metrics, "note": b.note, "created_at": b.created_at}


@router.post("/baselines", status_code=201)
def create_baseline(body: BaselineRequest, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict[str, Any]:
    repo = RunRepository(db)
    try:
        run = repo.get_run(body.evaluation_run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Evaluation not found") from None
    if not visible_to(run.owner_id, user):
        raise HTTPException(404, "Evaluation not found")
    if run.status != "completed":
        raise HTTPException(409, f"Only a completed evaluation can be a baseline; this one is {run.status}")
    calculation = repo.latest_calculation(run.id)
    if calculation is None:
        calculation = repo.calculate_metrics(run.id, "calculated when designated as a baseline")
        db.flush()
    metrics = [{"name": m.metric_name, "numerator": m.numerator, "denominator": m.denominator,
                "value": m.metric_value, "grouping_key": m.grouping_key} for m in calculation.metrics]
    row = EvaluationBaseline(name=body.name or run.name, evaluation_run_id=run.id,
                             benchmark_version=run.benchmark_version,
                             configuration_snapshot=run.configuration_snapshot, baseline_metrics=metrics,
                             note=body.note, owner_id=user.id)
    db.add(row)
    db.commit()
    return baseline_out(row)


@router.get("/baselines")
def list_baselines(limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1_000_000),
                   db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, Any]:
    base = select(EvaluationBaseline)
    if not user.is_admin:
        base = base.where((EvaluationBaseline.owner_id == user.id) | (EvaluationBaseline.owner_id.is_(None)))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(EvaluationBaseline.created_at.desc(),
                                                          EvaluationBaseline.id).limit(limit).offset(offset)).all()
    return {"items": [baseline_out(r) for r in rows], "page": Page(limit=limit, offset=offset, total=total)}


@router.get("/baselines/{baseline_id}")
def get_baseline(baseline_id: uuid.UUID, db: Session = Depends(get_db),
                 user: User = Depends(current_user)) -> dict[str, Any]:
    row = db.get(EvaluationBaseline, baseline_id)
    if row is None or not visible_to(row.owner_id, user):
        raise HTTPException(404, "Baseline not found")
    return baseline_out(row)


@router.post("/{run_id}/regression-check")
def regression_check(run_id: uuid.UUID, body: RegressionCheckRequest, db: Session = Depends(get_db),
                     user: User = Depends(current_user)) -> dict[str, Any]:
    repo = RunRepository(db)
    baseline = db.get(EvaluationBaseline, body.baseline_id)
    if baseline is None or not visible_to(baseline.owner_id, user):
        raise HTTPException(404, "Baseline not found")
    try:
        run = repo.get_run(run_id)
    except RunNotFoundError:
        raise HTTPException(404, "Evaluation not found") from None
    if not visible_to(run.owner_id, user):
        raise HTTPException(404, "Evaluation not found")
    if run.status in ("pending", "running"):
        raise HTTPException(409, "The evaluation is still running")
    if run.id == baseline.evaluation_run_id:
        raise HTTPException(422, "An evaluation cannot be checked against itself")
    try:
        result = detect_regressions(baseline.benchmark_version, run.benchmark_version,
                                    repo.load_case_data([baseline.evaluation_run_id]),
                                    repo.load_case_data([run.id]), body.thresholds)
    except IncompatibleBenchmarksError as exc:
        raise HTTPException(409, str(exc)) from None
    return {**result, "baseline_id": str(baseline.id), "baseline_run_id": str(baseline.evaluation_run_id),
            "candidate_run_id": str(run.id), "candidate_status": run.status}


@router.get("/benchmarks")
def list_benchmarks() -> dict[str, Any]:
    """The cases a run can select, with the version a run would record. Read-only; starts nothing."""
    from app.evaluation.benchmarks import load_suite

    suite = load_suite()
    return {
        "version": suite.version,
        "cases": [{"case_id": c.case_id, "category": c.category, "difficulty": c.difficulty,
                   "reference_variants": [r.name for r in c.reference_patches],
                   "has_heldout_tests": bool(c.holdout_tests)} for c in suite.cases.values()],
        "categories": suite.categories(),
    }
