"""14.4/14.9: metrics derived from stored rows, snapshots append-only, recalculation reproducible."""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.evaluation.metrics import compute_metrics, metrics_by_name
from app.evaluation.repository import RunRepository


@pytest.fixture
def stored_run(db_session, make_config, make_run, make_case, make_attempt):
    run = make_run(make_config(), selected_case_ids=["a", "b", "c"], total_cases=3)
    a = make_case(run, "a", category="x", first_attempt_verified=False, final_verified=True, success=True,
                  repaired_by_retry=True, attempts_count=2, total_cost_usd=Decimal("0.30"), cost_kind="actual",
                  total_latency_seconds=12.0, heldout_passed=True)
    make_attempt(a, 1, status="rejected", patch_valid=True, patch_applied=True, regression_detected=True)
    make_attempt(a, 2, status="verified", patch_valid=True, patch_applied=True, regression_detected=False,
                 feedback_from_attempts=[1])
    b = make_case(run, "b", category="y", first_attempt_verified=True, final_verified=True, success=True,
                  attempts_count=1, total_cost_usd=Decimal("0.10"), cost_kind="actual", total_latency_seconds=8.0)
    make_attempt(b, 1, patch_valid=True, patch_applied=True, regression_detected=False)
    make_case(run, "c", category="y", status="incomplete")
    db_session.flush()
    return run


def values(calculation):
    return {(m.metric_name, m.grouping_key): (m.metric_value, m.numerator, m.denominator)
            for m in calculation.metrics}


def test_stored_metrics_match_a_fresh_calculation_from_the_rows(db_session, stored_run):
    repo = RunRepository(db_session)
    calculation = repo.calculate_metrics(stored_run.id, "initial")
    fresh = metrics_by_name(compute_metrics(repo.load_case_data([stored_run.id])))
    stored = values(calculation)
    for name, result in fresh.items():
        assert stored[(name, None)] == (result.value, result.numerator, result.denominator), name
    assert stored[("final_verified_rate", None)] == (1.0, 2, 2)
    assert stored[("completion_rate", None)][1:] == (2, 3)  # the incomplete case stays in the denominator
    assert stored[("final_verified_rate", "category:y")] == (1.0, 1, 1)
    assert stored[("regression_rate", None)] == (1 / 3, 1, 3)
    assert stored[("total_cost_usd", None)][0] == pytest.approx(0.4)


def test_recalculating_adds_a_version_and_keeps_the_raw_evidence_identical(db_session, stored_run):
    repo = RunRepository(db_session)
    first = repo.calculate_metrics(stored_run.id, "initial")
    rows_before = db_session.execute(text(
        "SELECT id, final_verified, total_cost_usd FROM evaluation_case_results ORDER BY id")).all()
    second = repo.calculate_metrics(stored_run.id, "recalculation requested")
    rows_after = db_session.execute(text(
        "SELECT id, final_verified, total_cost_usd FROM evaluation_case_results ORDER BY id")).all()

    assert (first.version, second.version) == (1, 2)
    assert first.evidence_sha256 == second.evidence_sha256  # same evidence in, same digest
    assert rows_before == rows_after
    assert values(first) == values(second)  # deterministic
    assert repo.latest_calculation(stored_run.id).version == 2
    assert [c.reason for c in repo.calculations(stored_run.id)] == ["initial", "recalculation requested"]
    assert len(first.metrics) > 0  # the first calculation's snapshots still exist


def test_metrics_can_be_recalculated_after_the_run_is_finished(db_session, stored_run):
    repo = RunRepository(db_session)
    stored_run.status = "completed"
    db_session.flush()
    assert repo.calculate_metrics(stored_run.id, "after completion").version == 1


def test_the_digest_detects_changed_evidence(db_session, stored_run, make_case):
    repo = RunRepository(db_session)
    first = repo.calculate_metrics(stored_run.id, "initial")
    db_session.execute(text("UPDATE evaluation_case_results SET final_verified = false WHERE case_id = 'b'"))
    db_session.expire_all()
    second = repo.calculate_metrics(stored_run.id, "after edit")
    assert first.evidence_sha256 != second.evidence_sha256
    assert values(second)[("final_verified_rate", None)][1:] == (1, 2)


def test_cross_run_grouping_from_stored_rows(db_session, make_config, make_run, make_case):
    def run_with(model, strategy, verified):
        config = make_config(model_id=model)
        run = make_run(config, configuration_snapshot={
            "model": {"model_id": model}, "retrieval": {"strategy": strategy},
            "test_execution": {"test_visibility": "blind"}})
        make_case(run, "a", final_verified=verified, first_attempt_verified=verified, attempts_count=1)
        return run

    r1, r2 = run_with("claude-opus-5-5", "hybrid", True), run_with("claude-sonnet-5-5", "keyword", False)
    from app.evaluation.metrics import compute_grouped

    data = RunRepository(db_session).load_case_data([r1.id, r2.id])
    by_model = compute_grouped(data, "model")
    assert metrics_by_name(by_model["model:claude-opus-5-5"])["final_verified_rate"].value == 1.0
    assert metrics_by_name(by_model["model:claude-sonnet-5-5"])["final_verified_rate"].value == 0.0
    assert set(compute_grouped(data, "retrieval_strategy")) == {"retrieval_strategy:hybrid",
                                                                "retrieval_strategy:keyword"}
    assert len(compute_grouped(data, "run")) == 2
