"""Database access for evaluation records. No HTTP, no model calls."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.evaluation.configuration import (
    ConfigurationSpec,
    build_configuration,
    configuration_hash,
    denormalised_fields,
    merge_overrides,
    spec_from_document,
)
from app.evaluation.metrics import (
    METRICS_VERSION,
    AttemptData,
    CaseData,
    compute_grouped,
    compute_metrics,
    evidence_digest,
)
from app.evaluation.models import (
    EvaluationCaseResult,
    EvaluationConfiguration,
    EvaluationMetricCalculation,
    EvaluationMetricSnapshot,
    EvaluationRun,
)


def _can_see(owner_id: uuid.UUID | None, viewer: Any) -> bool:
    return viewer.is_admin or owner_id is None or owner_id == viewer.id


def _scope(query, column, viewer):
    """Restrict a query to the viewer's own rows and system rows (NULL owner). Operators see everything."""
    if viewer is None or viewer.is_admin:
        return query
    return query.where((column == viewer.id) | (column.is_(None)))


class ConfigurationNotFoundError(LookupError):
    pass


class ConfigurationConflictError(Exception):
    pass


class ConfigurationRepository:
    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def create(self, spec: ConfigurationSpec, owner_id: uuid.UUID | None = None) -> EvaluationConfiguration:
        """Version 1 of a new name. An existing name is a conflict: clone it to make a new version."""
        if self._session.scalar(select(func.count()).select_from(EvaluationConfiguration)
                                .where(EvaluationConfiguration.name == spec.name)):
            raise ConfigurationConflictError(
                f"A configuration named {spec.name!r} exists. Clone it to create a new version.")
        return self._insert(spec, version=1, parent=None, owner_id=owner_id)

    def clone(self, source_id: uuid.UUID, overrides: dict[str, Any] | None = None,
              name: str | None = None, owner: Any = None) -> EvaluationConfiguration:
        """A new row from an existing one. Same name: next version. New name: version 1 of that name.

        Prompt fingerprints are recomputed, which is how a configuration picks up changed prompts.
        """
        source = self.get(source_id, viewer=owner)
        owner_id = owner.id if owner is not None else source.owner_id
        document = merge_overrides(
            {k: v for k, v in source.config.items() if k in ConfigurationSpec.model_fields},
            overrides or {},
        )
        document.pop("name", None)
        spec = spec_from_document(name or source.name, document)
        if name and name != source.name:
            if self._session.scalar(select(func.count()).select_from(EvaluationConfiguration)
                                    .where(EvaluationConfiguration.name == name)):
                raise ConfigurationConflictError(f"A configuration named {name!r} exists")
            return self._insert(spec, version=1, parent=source, owner_id=owner_id)
        latest = self._session.scalar(select(func.max(EvaluationConfiguration.version))
                                      .where(EvaluationConfiguration.name == source.name)) or 0
        return self._insert(spec, version=latest + 1, parent=source, owner_id=owner_id)

    def _insert(self, spec: ConfigurationSpec, *, version: int, parent: EvaluationConfiguration | None,
                owner_id: uuid.UUID | None = None):
        document = build_configuration(spec, self._settings)
        row = EvaluationConfiguration(
            name=spec.name, version=version, parent_configuration_id=parent.id if parent else None,
            owner_id=owner_id,
            config=document, config_hash=configuration_hash(document), **denormalised_fields(spec),
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            raise ConfigurationConflictError("The configuration version already exists; retry") from None
        return row

    def get(self, configuration_id: uuid.UUID, viewer: Any = None) -> EvaluationConfiguration:
        row = self._session.get(EvaluationConfiguration, configuration_id)
        if row is not None and viewer is not None and not _can_see(row.owner_id, viewer):
            row = None
        if row is None:
            raise ConfigurationNotFoundError(str(configuration_id))
        return row

    def list_all(self, *, limit: int, offset: int, name: str | None = None,
                 viewer: Any = None) -> tuple[list[EvaluationConfiguration], int]:
        query = _scope(select(EvaluationConfiguration), EvaluationConfiguration.owner_id, viewer)
        if name:
            query = query.where(EvaluationConfiguration.name == name)
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self._session.scalars(
            query.order_by(EvaluationConfiguration.created_at.desc(), EvaluationConfiguration.id)
            .limit(limit).offset(offset)
        ).all()
        return list(rows), total

    def run_counts(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not ids:
            return {}
        rows = self._session.execute(
            select(EvaluationRun.configuration_id, func.count()).where(EvaluationRun.configuration_id.in_(ids))
            .group_by(EvaluationRun.configuration_id)
        ).all()
        return {cid: count for cid, count in rows}


# --- runs, case results and metric snapshots -------------------------------------------


class RunNotFoundError(LookupError):
    pass


class CaseNotFoundError(LookupError):
    pass


class RunStateError(Exception):
    """The requested change is not allowed in the run's current state."""


def case_data(case: "EvaluationCaseResult", run: EvaluationRun | None = None) -> CaseData:
    """Plain metric input from a stored case (and its run's grouping attributes)."""
    run = run or case.run
    snapshot = run.configuration_snapshot or {}
    model = (snapshot.get("model") or {}).get("model_id")
    return CaseData(
        case_id=case.case_id, status=case.status, category=case.category, variant=case.variant,
        diagnosis_correct=case.diagnosis_correct, first_attempt_verified=case.first_attempt_verified,
        final_verified=case.final_verified, repaired_by_retry=case.repaired_by_retry, success=case.success,
        heldout_passed=case.heldout_passed, exploratory_passed=case.exploratory_passed,
        attempts_count=case.attempts_count, total_cost_usd=case.total_cost_usd, cost_kind=case.cost_kind,
        regression_detected=case.regression_detected,
        total_latency_seconds=case.total_latency_seconds,
        attempts=tuple(AttemptData(a.attempt_number, a.status, a.patch_valid, a.patch_applied,
                                   a.regression_detected, a.cost_usd) for a in case.attempts),
        run_id=str(run.id), configuration_id=str(run.configuration_id) if run.configuration_id else None,
        model_id=model, retrieval_strategy=(snapshot.get("retrieval") or {}).get("strategy"),
        test_visibility=(snapshot.get("test_execution") or {}).get("test_visibility"),
    )


class RunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_run(self, run: EvaluationRun) -> EvaluationRun:
        self._session.add(run)
        self._session.flush()
        return run

    def get_run(self, run_id: uuid.UUID) -> EvaluationRun:
        run = self._session.get(EvaluationRun, run_id)
        if run is None:
            raise RunNotFoundError(str(run_id))
        return run

    def list_runs(self, *, limit: int, offset: int, status: str | None = None, mode: str | None = None,
                  configuration_id: uuid.UUID | None = None, viewer: Any = None) -> tuple[list[EvaluationRun], int]:
        query = _scope(select(EvaluationRun), EvaluationRun.owner_id, viewer)
        if status:
            query = query.where(EvaluationRun.status == status)
        if mode:
            query = query.where(EvaluationRun.evaluation_mode == mode)
        if configuration_id:
            query = query.where(EvaluationRun.configuration_id == configuration_id)
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self._session.scalars(query.order_by(EvaluationRun.created_at.desc(), EvaluationRun.id)
                                     .limit(limit).offset(offset)).all()
        return list(rows), total

    def list_cases(self, run_id: uuid.UUID, *, limit: int, offset: int, status: str | None = None,
                   category: str | None = None) -> tuple[list[EvaluationCaseResult], int]:
        self.get_run(run_id)
        query = select(EvaluationCaseResult).where(EvaluationCaseResult.evaluation_run_id == run_id)
        if status:
            query = query.where(EvaluationCaseResult.status == status)
        if category:
            query = query.where(EvaluationCaseResult.category == category)
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self._session.scalars(query.order_by(EvaluationCaseResult.case_id, EvaluationCaseResult.variant)
                                     .limit(limit).offset(offset)).all()
        return list(rows), total

    def get_case(self, run_id: uuid.UUID, case_id: str, variant: str | None = None) -> EvaluationCaseResult:
        self.get_run(run_id)
        query = select(EvaluationCaseResult).where(EvaluationCaseResult.evaluation_run_id == run_id,
                                                   EvaluationCaseResult.case_id == case_id)
        if variant:
            query = query.where(EvaluationCaseResult.variant == variant)
        rows = self._session.scalars(query.order_by(EvaluationCaseResult.variant)).all()
        if not rows:
            raise CaseNotFoundError(case_id)
        return rows[0]

    def case_variants(self, run_id: uuid.UUID, case_id: str) -> list[str]:
        return list(self._session.scalars(select(EvaluationCaseResult.variant).where(
            EvaluationCaseResult.evaluation_run_id == run_id, EvaluationCaseResult.case_id == case_id
        ).order_by(EvaluationCaseResult.variant)))

    def case_counts(self, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
        """Case rows per run and status, for list pages (no N+1)."""
        if not run_ids:
            return {}
        rows = self._session.execute(
            select(EvaluationCaseResult.evaluation_run_id, EvaluationCaseResult.status, func.count())
            .where(EvaluationCaseResult.evaluation_run_id.in_(run_ids))
            .group_by(EvaluationCaseResult.evaluation_run_id, EvaluationCaseResult.status)
        ).all()
        out: dict[uuid.UUID, dict[str, int]] = {}
        for run_id, status, count in rows:
            out.setdefault(run_id, {})[status] = count
        return out

    def load_case_data(self, run_ids: list[uuid.UUID]) -> list[CaseData]:
        """Metric input for these runs, straight from the stored case and attempt rows."""
        runs = {r.id: r for r in self._session.scalars(select(EvaluationRun).where(EvaluationRun.id.in_(run_ids)))}
        cases = self._session.scalars(
            select(EvaluationCaseResult).where(EvaluationCaseResult.evaluation_run_id.in_(run_ids))
            .options(selectinload(EvaluationCaseResult.attempts))
            .order_by(EvaluationCaseResult.evaluation_run_id, EvaluationCaseResult.case_id,
                      EvaluationCaseResult.variant)
        ).all()
        return [case_data(c, runs[c.evaluation_run_id]) for c in cases]

    # --- metrics -----------------------------------------------------------------------

    def calculate_metrics(self, run_id: uuid.UUID, reason: str) -> EvaluationMetricCalculation:
        """Score the run's stored evidence and append a new calculation. Never edits case rows.

        Every call adds a version, so the history shows when and why numbers were produced.
        """
        run = self.get_run(run_id)
        data = self.load_case_data([run_id])
        latest = self._session.scalar(select(func.max(EvaluationMetricCalculation.version))
                                      .where(EvaluationMetricCalculation.evaluation_run_id == run_id)) or 0
        calculation = EvaluationMetricCalculation(
            evaluation_run_id=run_id, version=latest + 1, reason=reason[:300], metrics_version=METRICS_VERSION,
            evidence_sha256=evidence_digest(data),
        )
        self._session.add(calculation)
        self._session.flush()
        results = list(compute_metrics(data))
        for results_for_group in compute_grouped(data, "category").values():
            results.extend(results_for_group)
        for result in results:
            self._session.add(EvaluationMetricSnapshot(
                evaluation_run_id=run.id, calculation_id=calculation.id, metric_name=result.name,
                metric_value=result.value, numerator=result.numerator, denominator=result.denominator,
                grouping_key=result.grouping_key,
            ))
        self._session.flush()
        return calculation

    def latest_calculation(self, run_id: uuid.UUID) -> EvaluationMetricCalculation | None:
        return self._session.scalar(
            select(EvaluationMetricCalculation).where(EvaluationMetricCalculation.evaluation_run_id == run_id)
            .order_by(EvaluationMetricCalculation.version.desc()).limit(1)
            .options(selectinload(EvaluationMetricCalculation.metrics))
        )

    def calculations(self, run_id: uuid.UUID) -> list[EvaluationMetricCalculation]:
        return list(self._session.scalars(
            select(EvaluationMetricCalculation).where(EvaluationMetricCalculation.evaluation_run_id == run_id)
            .order_by(EvaluationMetricCalculation.version)))
