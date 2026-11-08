"""14.8: threshold logic, safeguards and the baseline API."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import get_db
from app.evaluation.metrics import AttemptData, CaseData
from app.evaluation.regression import (
    IncompatibleBenchmarksError,
    RegressionThresholds,
    detect_regressions,
)
from app.main import app


def case(cid, ok, category="a", regression=False, cost="1", seconds=10.0):
    return CaseData(case_id=cid, status="completed", category=category, final_verified=ok,
                    first_attempt_verified=ok, success=ok, heldout_passed=ok, attempts_count=1,
                    total_cost_usd=Decimal(cost), cost_kind="actual", total_latency_seconds=seconds,
                    attempts=(AttemptData(1, "verified" if ok else "rejected", True, True, regression),))


GOOD = [case("c1", True), case("c2", True), case("c3", True, "b"), case("c4", True, "b"), case("c5", True, "b")]
BAD = [case("c1", True), case("c2", False, regression=True), case("c3", False, "b"), case("c4", False, "b"),
       case("c5", True, "b")]
T = RegressionThresholds()


def check(base=GOOD, cand=BAD, t=T, versions=("v1", "v1")):
    return detect_regressions(versions[0], versions[1], base, cand, t)


def by(result, metric, scope="overall"):
    return next((f for f in result["findings"] if f["metric"] == metric and f["scope"] == scope), None)


def test_each_threshold_names_its_metric_counts_and_supporting_cases():
    result = check()
    assert result["status"] == "regressed"
    verified = by(result, "final_verified_rate")
    assert verified["delta"] == pytest.approx(-60) and verified["baseline"]["numerator"] == 5
    assert verified["candidate"]["numerator"] == 2 and verified["threshold"] == 10
    assert verified["supporting_case_ids"] == [["c2", "live"], ["c3", "live"], ["c4", "live"]]
    assert by(result, "heldout_pass_rate")
    reg = by(result, "regression_rate")
    assert reg["supporting_case_ids"] == [["c2", "live"]] and "regression_rate rose" in reg["message"]
    category = by(result, "final_verified_rate", "category:b")
    assert category["delta"] == pytest.approx(-200 / 3) and category["supporting_case_ids"]
    assert by(result, "final_verified_rate", "category:a")["delta"] == pytest.approx(-50)


def test_thresholds_are_configurable():
    loose = RegressionThresholds(verified_rate_drop_pp=70, heldout_rate_drop_pp=70, regression_rate_increase_pp=50,
                                 category_verified_drop_pp=90,
                                 cost_per_repair_increase_pct=10_000)
    result = check(t=loose)
    assert result["status"] == "ok" and result["findings"] == []
    assert result["thresholds"]["verified_rate_drop_pp"] == 70
    with pytest.raises(ValueError):
        RegressionThresholds(verified_rate_drop_pp=-1)
    with pytest.raises(ValueError):
        RegressionThresholds(unknown=1)


def test_cost_and_latency_increases_trigger_only_above_the_limit():
    pricey = [case(c.case_id, c.success, c.category, cost="1.3", seconds=14.0) for c in GOOD]
    result = check(cand=pricey)
    assert by(result, "cost_per_successful_repair")["delta"] == pytest.approx(30)
    assert by(result, "average_latency_seconds")["delta"] == pytest.approx(40)
    assert check(cand=[case(c.case_id, c.success, c.category, cost="1.2", seconds=12.0) for c in GOOD]
                 )["status"] == "ok"


def test_an_improvement_is_not_a_regression():
    assert check(base=BAD, cand=GOOD)["status"] == "ok"


def test_small_samples_are_marked_low_confidence():
    finding = by(check(), "final_verified_rate")
    assert finding["low_confidence"] is True and "not as statistical evidence" in finding["warning"]


def test_missing_cases_are_not_failures():
    result = check(cand=BAD[:2])
    assert result["compared_cases"] == 2
    assert len(result["missing_in_candidate"]) == 3
    verified = by(result, "final_verified_rate")
    assert verified["candidate"]["denominator"] == 2  # c3-c5 are not counted as failures


def test_different_benchmark_versions_are_refused():
    with pytest.raises(IncompatibleBenchmarksError):
        check(versions=("v1", "v2"))


# --- API ---


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def stored(db_session, make_config, make_run, make_case):
    def build(flags, version="v1", status="completed"):
        run = make_run(make_config(), status="running", benchmark_version=version, total_cases=len(flags),
                       selected_case_ids=[f"c{i}" for i in range(len(flags))])
        for i, ok in enumerate(flags):
            make_case(run, f"c{i}", category="cat", final_verified=ok, first_attempt_verified=ok, success=ok,
                      heldout_passed=ok, attempts_count=1, total_cost_usd=Decimal("0.1"), cost_kind="actual")
        run.status = status
        db_session.flush()
        return run

    return build


def test_baseline_lifecycle_and_deliberate_regression(client, stored, db_session):
    base, bad = stored([True] * 5), stored([True, False, False, True, True])
    created = client.post("/evaluations/baselines", json={"evaluation_run_id": str(base.id), "note": "ref"})
    assert created.status_code == 201
    baseline = created.json()
    assert baseline["benchmark_version"] == "v1" and baseline["baseline_metrics"]
    assert client.get(f"/evaluations/baselines/{baseline['id']}").json()["id"] == baseline["id"]
    assert client.get("/evaluations/baselines").json()["page"]["total"] >= 1

    result = client.post(f"/evaluations/{bad.id}/regression-check", json={"baseline_id": baseline["id"]}).json()
    assert result["status"] == "regressed"
    assert any(f["metric"] == "final_verified_rate" and f["supporting_case_ids"] for f in result["findings"])
    relaxed = client.post(f"/evaluations/{bad.id}/regression-check", json={
        "baseline_id": baseline["id"],
        "thresholds": {"verified_rate_drop_pp": 90, "heldout_rate_drop_pp": 90, "category_verified_drop_pp": 90,
                       "cost_per_repair_increase_pct": 10000},
    }).json()
    assert relaxed["status"] == "ok"

    db_session.refresh(base)
    assert base.status == "completed"  # the original run is not mutated
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text("UPDATE evaluation_baselines SET name = 'x'"))


def test_baseline_and_check_errors(client, stored):
    done, other_version = stored([True]), stored([True], version="v2")
    active = stored([True], status="running")
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post("/evaluations/baselines", json={"evaluation_run_id": missing}).status_code == 404
    assert client.post("/evaluations/baselines", json={"evaluation_run_id": str(active.id)}).status_code == 409
    baseline = client.post("/evaluations/baselines", json={"evaluation_run_id": str(done.id)}).json()
    body = {"baseline_id": baseline["id"]}
    assert client.post(f"/evaluations/{other_version.id}/regression-check", json=body).status_code == 409
    assert client.post(f"/evaluations/{done.id}/regression-check", json=body).status_code == 422
    assert client.post(f"/evaluations/{active.id}/regression-check", json=body).status_code == 409
    assert client.post(f"/evaluations/{missing}/regression-check", json=body).status_code == 404
    assert client.post(f"/evaluations/{done.id}/regression-check", json={"baseline_id": missing}).status_code == 404
    assert client.get(f"/evaluations/baselines/{missing}").status_code == 404
    assert client.post("/evaluations/baselines", json={"evaluation_run_id": str(done.id), "x": 1}).status_code == 422
