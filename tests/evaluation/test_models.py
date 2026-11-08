"""14.1: constraints, relationships, nullable outcomes and immutability on real PostgreSQL."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.evaluation.models import (
    EvaluationMetricCalculation,
    EvaluationMetricSnapshot,
    EvaluationRun,
)


def expect_rejected(session, action, error=(IntegrityError, DBAPIError)):
    """Run ``action`` inside a savepoint so the test transaction survives the failure."""
    with pytest.raises(error), session.begin_nested():
        action()


def test_run_case_attempt_relationships(db_session, make_config, make_run, make_case, make_attempt):
    config = make_config()
    run = make_run(config)
    case = make_case(run)
    make_attempt(case, 1, status="rejected")
    make_attempt(case, 2, feedback_from_attempts=[1], feedback_reasons=["required test failed"])
    db_session.expire_all()

    loaded = db_session.get(EvaluationRun, run.id)
    assert loaded.configuration.id == config.id
    assert [c.case_id for c in loaded.cases] == ["bug-001"]
    assert [a.attempt_number for a in loaded.cases[0].attempts] == [1, 2]
    assert loaded.cases[0].attempts[1].feedback_from_attempts == [1]
    assert config.runs[0].id == run.id


def test_uuid_primary_keys_and_defaults(make_run):
    run = make_run()
    assert isinstance(run.id, uuid.UUID)
    assert run.status == "running"
    assert run.is_historical is False or run.is_historical is None or run.is_historical == False


def test_money_is_decimal_not_float(db_session, make_run):
    run = make_run(total_cost_usd=Decimal("0.100000"), cost_kind="actual")
    run_id = run.id
    db_session.expire_all()
    value = db_session.get(EvaluationRun, run_id).total_cost_usd
    assert isinstance(value, Decimal) and value == Decimal("0.1")
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")  # exact, unlike 0.1 + 0.2 in binary floating point


def test_unknown_outcomes_stay_null(db_session, make_run, make_case):
    """A case that never reached patching has no verdict: not False, not 0."""
    case = make_case(make_run(), status="failed", diagnosis_status="failed", failure_reason="diagnosis failed")
    case_id = case.id
    db_session.expire_all()
    case = db_session.get(type(case), case_id)
    for column in ("final_verified", "first_attempt_verified", "patch_valid", "patch_applied", "heldout_passed",
                   "exploratory_passed", "regression_detected", "total_cost_usd", "total_latency_seconds",
                   "diagnosis_correct"):
        assert getattr(case, column) is None, column
    assert case.attempts_count == 0


def test_attempt_number_unique_within_a_case(db_session, make_run, make_case, make_attempt):
    case = make_case(make_run())
    make_attempt(case, 1)
    expect_rejected(db_session, lambda: make_attempt(case, 1))
    other = make_case(case.run, "bug-002")
    make_attempt(other, 1)  # the same number in another case is fine


def test_case_variant_unique_within_a_run(db_session, make_run, make_case):
    run = make_run()
    make_case(run, "bug-001", variant="correct")
    make_case(run, "bug-001", variant="no_fix")
    expect_rejected(db_session, lambda: make_case(run, "bug-001", variant="correct"))


def test_metric_name_and_grouping_unique_per_calculation_including_null_group(db_session, make_run):
    run = make_run()
    calc = EvaluationMetricCalculation(evaluation_run_id=run.id, version=1, reason="initial",
                                       metrics_version="1", evidence_sha256="0" * 64)
    db_session.add(calc)
    db_session.flush()

    def add(group):
        db_session.add(EvaluationMetricSnapshot(evaluation_run_id=run.id, calculation_id=calc.id,
                                                metric_name="final_verified_rate", metric_value=0.5,
                                                numerator=1, denominator=2, grouping_key=group))
        db_session.flush()

    add(None)
    add("category:pagination")
    expect_rejected(db_session, lambda: add(None))
    expect_rejected(db_session, lambda: add("category:pagination"))


def test_recalculation_adds_a_version_without_touching_the_first(db_session, make_run):
    run = make_run()
    for version in (1, 2):
        db_session.add(EvaluationMetricCalculation(evaluation_run_id=run.id, version=version, reason="r",
                                                   metrics_version="1", evidence_sha256="0" * 64))
        db_session.flush()
    expect_rejected(db_session, lambda: (db_session.add(EvaluationMetricCalculation(
        evaluation_run_id=run.id, version=2, reason="dup", metrics_version="1", evidence_sha256="0" * 64)),
        db_session.flush()))


@pytest.mark.parametrize("field,value", [
    ("status", "done"), ("evaluation_mode", "paid"), ("diagnosis_mode", "magic"), ("cost_kind", "guess"),
    ("total_cases", -1),
])
def test_run_check_constraints(db_session, make_run, field, value):
    expect_rejected(db_session, lambda: make_run(**{field: value}))


def test_case_and_attempt_check_constraints(db_session, make_run, make_case, make_attempt):
    run = make_run()
    expect_rejected(db_session, lambda: make_case(run, status="bogus"))
    expect_rejected(db_session, lambda: make_case(run, attempts_count=-1))
    case = make_case(run)
    expect_rejected(db_session, lambda: make_attempt(case, 0))
    expect_rejected(db_session, lambda: make_attempt(case, 1, status="bogus"))


def test_foreign_keys_are_enforced_and_never_cascade(db_session, make_config, make_run, make_case):
    expect_rejected(db_session, lambda: make_case(type("R", (), {"id": uuid.uuid4()})()))
    config = make_config()
    run = make_run(config, status="running")
    make_case(run)
    for target in (run, config):
        expect_rejected(db_session, lambda t=target: (db_session.delete(t), db_session.flush()))


def test_idempotency_key_and_source_hash_prevent_duplicates(db_session, make_run):
    make_run(idempotency_key="k1", source_sha256="a" * 64)
    expect_rejected(db_session, lambda: make_run(idempotency_key="k1"))
    expect_rejected(db_session, lambda: make_run(source_sha256="a" * 64))
    make_run()  # NULL keys never collide
    make_run()


def test_configuration_name_version_unique_and_rows_immutable(db_session, make_config):
    config = make_config("opus", 1)
    expect_rejected(db_session, lambda: make_config("opus", 1))
    make_config("opus", 2)

    def edit():
        db_session.execute(text("UPDATE evaluation_configurations SET model_id = 'x' WHERE id = :i"),
                           {"i": config.id})

    expect_rejected(db_session, edit)
    expect_rejected(db_session, lambda: db_session.execute(
        text("DELETE FROM evaluation_configurations WHERE id = :i"), {"i": config.id}))


@pytest.mark.parametrize("final", ["completed", "partial", "failed", "cancelled"])
def test_finished_runs_and_their_evidence_are_immutable(db_session, make_run, make_case, make_attempt, final):
    run = make_run()
    case = make_case(run)
    attempt = make_attempt(case, 1)
    run.status = final
    run.completed_at = datetime.now(UTC)
    db_session.flush()  # running -> finished is the one allowed transition

    def update(table, column, value, key):
        return lambda: db_session.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :i"),
                                          {"v": value, "i": key})

    expect_rejected(db_session, update("evaluation_runs", "name", "renamed", run.id))
    expect_rejected(db_session, update("evaluation_case_results", "final_verified", True, case.id))
    expect_rejected(db_session, update("evaluation_attempts", "status", "rejected", attempt.id))
    expect_rejected(db_session, lambda: db_session.execute(
        text("DELETE FROM evaluation_attempts WHERE id = :i"), {"i": attempt.id}))
    expect_rejected(db_session, lambda: db_session.execute(
        text("DELETE FROM evaluation_runs WHERE id = :i"), {"i": run.id}))
    expect_rejected(db_session, lambda: make_case(run, "bug-002"))  # no late additions either
    expect_rejected(db_session, lambda: make_attempt(case, 2))


def test_metric_snapshots_are_append_only_even_for_finished_runs(db_session, make_run):
    run = make_run()
    calc = EvaluationMetricCalculation(evaluation_run_id=run.id, version=1, reason="initial",
                                       metrics_version="1", evidence_sha256="0" * 64)
    db_session.add(calc)
    db_session.flush()
    snapshot = EvaluationMetricSnapshot(evaluation_run_id=run.id, calculation_id=calc.id, metric_name="m",
                                        metric_value=None, numerator=0, denominator=0)
    db_session.add(snapshot)
    db_session.flush()
    assert snapshot.metric_value is None  # zero denominator: undefined, not 0%
    expect_rejected(db_session, lambda: db_session.execute(
        text("UPDATE evaluation_metric_snapshots SET metric_value = 1 WHERE id = :i"), {"i": snapshot.id}))
    expect_rejected(db_session, lambda: db_session.execute(
        text("DELETE FROM evaluation_metric_calculations WHERE id = :i"), {"i": calc.id}))
    # Finishing the run does not freeze it against new calculations.
    run.status = "completed"
    db_session.flush()
    db_session.add(EvaluationMetricCalculation(evaluation_run_id=run.id, version=2, reason="recalc",
                                               metrics_version="1", evidence_sha256="0" * 64))
    db_session.flush()
    assert db_session.scalar(select(EvaluationMetricCalculation.version)
                             .where(EvaluationMetricCalculation.evaluation_run_id == run.id)
                             .order_by(EvaluationMetricCalculation.version.desc())) == 2


def test_historical_and_native_runs_are_distinguishable(db_session, make_run):
    imported = make_run(is_historical=True, source_filename="old.json", source_sha256="b" * 64,
                        imported_at=datetime.now(UTC))
    native = make_run()
    assert imported.is_historical is True
    assert not native.is_historical
