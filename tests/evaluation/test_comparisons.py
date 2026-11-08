"""14.5: comparison arithmetic, honesty about samples and missing cases, and the API."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.evaluation.comparisons import (
    SMALL_SAMPLE,
    RunInput,
    compare,
    metric_difference,
)
from app.evaluation.metrics import AttemptData, CaseData
from app.main import app


def case(cid, success, *, variant="live", category="x", attempts=1, regression=False, cost="0.1"):
    return CaseData(case_id=cid, status="completed", category=category, variant=variant,
                    first_attempt_verified=success and attempts == 1, final_verified=success, success=success,
                    attempts_count=attempts, total_cost_usd=Decimal(cost), cost_kind="actual",
                    total_latency_seconds=10.0,
                    attempts=(AttemptData(1, "verified" if success else "rejected", True, True, regression),))


def run(rid, cases, version="v1", status="completed", mode="live"):
    return RunInput(rid, rid, status, version, mode, cases, [c.case_id for c in cases])


A = run("A", [case("c1", True), case("c2", True), case("c3", False), case("c4", False, attempts=2),
              case("c5", True, category="y")])
B = run("B", [case("c1", True), case("c2", False, regression=True), case("c3", True), case("c4", False),
              case("c5", True, category="y")])


def test_case_level_buckets():
    cases = compare([A, B])["cases"][0]
    assert cases["both_solved"] == [["c1", "live"], ["c5", "live"]]
    assert cases["only_base_solved"] == [["c2", "live"]]
    assert cases["only_other_solved"] == [["c3", "live"]]
    assert cases["neither_solved"] == [["c4", "live"]]
    assert cases["regression_in_other_only"] == [["c2", "live"]]
    assert cases["retries_in_base_only"] == [["c4", "live"]]
    assert cases["compared"] == 5


def test_metric_differences_show_counts_and_warn_on_small_samples():
    result = compare([A, B])["metrics"]["final_verified_rate"]
    assert [c["numerator"] for c in result["runs"]] == [3, 3] and result["runs"][0]["denominator"] == 5
    diff = result["differences_from_first"][0]
    assert diff["absolute"] == 0 and diff["direction"] is None and diff["sample_sizes"] == [5, 5]
    assert "not statistically meaningful" in diff["warning"] and "n=5" in diff["warning"]
    assert "significant" not in str(compare([A, B])).replace("not statistically", "")


def test_direction_relative_and_null_handling():
    base = {"numerator": 1, "denominator": 100, "value": 0.5}
    up = metric_difference("final_verified_rate", base, {"numerator": 60, "denominator": 100, "value": 0.6})
    assert up["absolute"] == pytest.approx(0.1) and up["relative"] == pytest.approx(0.2)
    assert up["direction"] == "better" and "warning" not in up
    cost_up = metric_difference("total_cost_usd", base, {"numerator": 1, "denominator": 100, "value": 0.9})
    assert cost_up["direction"] == "worse"
    from_zero = metric_difference("regression_rate", {"numerator": 0, "denominator": 9, "value": 0.0},
                                  {"numerator": 3, "denominator": 9, "value": 3 / 9})
    assert from_zero["relative"] is None and from_zero["direction"] == "worse"
    missing = metric_difference("final_verified_rate", base, {"numerator": 0, "denominator": 0, "value": None})
    assert missing["absolute"] is None and "not comparable" in missing["note"]
    assert SMALL_SAMPLE == 30


def test_missing_cases_are_not_failures_and_are_reported():
    partial = run("P", [case("c1", True), case("c2", True)])
    result = compare([A, partial])
    c = result["cases"][0]
    assert c["compared"] == 2 and len(c["missing_in_other"]) == 3 and c["neither_solved"] == []
    assert any("do not cover the same cases" in w for w in result["warnings"])


def test_unjudged_cases_are_neither_solved_nor_unsolved():
    unjudged = CaseData(case_id="c1", status="incomplete")
    c = compare([run("A", [case("c1", True)]), run("B", [unjudged])])["cases"][0]
    assert c["unjudged"] == [["c1", "live"]] and c["both_solved"] == [] and c["only_base_solved"] == []


def test_incompatible_benchmarks_and_modes_are_flagged():
    result = compare([A, run("B2", B.cases, version="v2", mode="reference")])
    assert result["compatible"] is False and result["benchmark_versions"] == ["v1", "v2"]
    assert len(result["warnings"]) >= 2


def test_category_breakdown_and_links():
    result = compare([A, B])
    assert set(result["by_category"]) == {"x", "y"}
    x = result["by_category"]["x"]["final_verified_rate"]
    assert [r["numerator"] for r in x["runs"]] == [2, 2] and x["runs"][0]["denominator"] == 4
    assert [r["link"] for r in result["runs"]] == ["/evaluations/A", "/evaluations/B"]


def test_three_runs_compare_each_to_the_first():
    result = compare([A, B, A])
    assert len(result["cases"]) == 2 and len(result["metrics"]["success_rate"]["differences_from_first"]) == 2


def test_needs_two_runs():
    with pytest.raises(ValueError):
        compare([A])


# --- API ---


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def stored(db_session, make_config, make_run, make_case, successes, status="completed"):
    run = make_run(make_config(), status="running", selected_case_ids=[f"c{i}" for i in range(len(successes))],
                   total_cases=len(successes), benchmark_version="v1")
    for i, ok in enumerate(successes):
        make_case(run, f"c{i}", category="cat", final_verified=ok, first_attempt_verified=ok, success=ok,
                  attempts_count=1, total_cost_usd=Decimal("0.1"), cost_kind="actual")
    run.status = status
    db_session.flush()
    return run


def test_compare_endpoint_detects_a_deliberate_regression(client, db_session, make_config, make_run, make_case):
    good = stored(db_session, make_config, make_run, make_case, [True, True, True, True])
    bad = stored(db_session, make_config, make_run, make_case, [True, False, False, True])
    body = client.post("/evaluations/compare", json={"evaluation_run_ids": [str(good.id), str(bad.id)]}).json()
    diff = body["metrics"]["final_verified_rate"]["differences_from_first"][0]
    assert diff["absolute"] == pytest.approx(-0.5) and diff["direction"] == "worse"
    assert body["cases"][0]["only_base_solved"] == [["c1", "live"], ["c2", "live"]]


def test_compare_endpoint_errors(client, db_session, make_config, make_run, make_case):
    done = stored(db_session, make_config, make_run, make_case, [True])
    active = stored(db_session, make_config, make_run, make_case, [True], status="running")
    ids = lambda *r: {"evaluation_run_ids": [str(x) for x in r]}
    assert client.post("/evaluations/compare", json=ids(done.id)).status_code == 422
    assert client.post("/evaluations/compare", json=ids(done.id, done.id)).status_code == 422
    assert client.post("/evaluations/compare", json=ids(done.id, active.id)).status_code == 409
    assert client.post("/evaluations/compare", json=ids(done.id, "00000000-0000-0000-0000-000000000000")
                       ).status_code == 404
    assert client.post("/evaluations/compare", json={}).status_code == 422
