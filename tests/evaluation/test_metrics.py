"""14.4: every metric against hand-calculated fixtures. Pure functions, no database."""

from decimal import Decimal

import pytest

from app.evaluation.metrics import (
    GROUP_BY,
    METRIC_NAMES,
    AttemptData,
    CaseData,
    compute_grouped,
    compute_metrics,
    evidence_digest,
    group_key,
    metrics_by_name,
    summarize_cases,
)

A = AttemptData


def case(case_id, **kw):
    kw.setdefault("status", "completed")
    return CaseData(case_id=case_id, **kw)


# Ten cases, built so every number below can be checked by hand.
#
#  id  category    diag  attempts (valid/applied/regress/status)        first  final  heldout  expl  success cost  secs
#  c1  pagination  True  1: V/A/ok/verified                            T      T      T        T     T       0.10  10
#  c2  pagination  True  1: V/A/ok/verified                            T      T      F        None  F       0.20  20
#  c3  pricing     True  1: V/A/REG/rejected  2: V/A/ok/verified       F      T      T        F     T       0.30  30  repaired
#  c4  pricing     False 1: V/A/ok/rejected   2: V/A/ok/rejected       F      F      None     None  F       0.40  40  retried, not repaired
#  c5  pricing     True  1: invalid                                    F      F      None     None  F       0.05  5   (single attempt: not a retry)
#  c6  config      None  1: V/not applied/rejected                     F      F      None     None  F       0.05  5
#  c7  config      None  (diagnosis failed: nothing attempted)         None   None   None     None  None    0.02  3   status failed
#  c8  config      None  (never started: budget exhausted)            None   None   None     None  None    None  None status incomplete
#  c9  slug        True  1: V/A/ok/verified                            T      T      None     None  T       0.10  10
#  c10 slug        False 1: V/A/REG/rejected 2: V/A/REG/rejected       F      F      None     None  F       0.28  20  retried, not repaired
def fixture():
    return [
        case("c1", category="pagination", diagnosis_correct=True, first_attempt_verified=True, final_verified=True,
             heldout_passed=True, exploratory_passed=True, success=True, attempts_count=1,
             total_cost_usd=Decimal("0.10"), cost_kind="actual", total_latency_seconds=10,
             attempts=[A(1, "verified", True, True, False)]),
        case("c2", category="pagination", diagnosis_correct=True, first_attempt_verified=True, final_verified=True,
             heldout_passed=False, success=False, attempts_count=1, total_cost_usd=Decimal("0.20"),
             cost_kind="actual", total_latency_seconds=20, attempts=[A(1, "verified", True, True, False)]),
        case("c3", category="pricing", diagnosis_correct=True, first_attempt_verified=False, final_verified=True,
             repaired_by_retry=True, heldout_passed=True, exploratory_passed=False, success=True, attempts_count=2,
             total_cost_usd=Decimal("0.30"), cost_kind="actual", total_latency_seconds=30,
             attempts=[A(1, "rejected", True, True, True), A(2, "verified", True, True, False)]),
        case("c4", category="pricing", diagnosis_correct=False, first_attempt_verified=False, final_verified=False,
             repaired_by_retry=False, success=False, attempts_count=2, total_cost_usd=Decimal("0.40"),
             cost_kind="actual", total_latency_seconds=40,
             attempts=[A(1, "rejected", True, True, False), A(2, "rejected", True, True, False)]),
        case("c5", category="pricing", diagnosis_correct=True, first_attempt_verified=False, final_verified=False,
             repaired_by_retry=False, success=False, attempts_count=1, total_cost_usd=Decimal("0.05"),
             cost_kind="actual", total_latency_seconds=5, attempts=[A(1, "validation_failed", False, None, None)]),
        case("c6", category="config", first_attempt_verified=False, final_verified=False, repaired_by_retry=False,
             success=False, attempts_count=1, total_cost_usd=Decimal("0.05"), cost_kind="actual",
             total_latency_seconds=5, attempts=[A(1, "rejected", True, False, None)]),
        case("c7", status="failed", category="config", total_cost_usd=Decimal("0.02"), cost_kind="actual",
             total_latency_seconds=3),
        case("c8", status="incomplete", category="config"),
        case("c9", category="slug", diagnosis_correct=True, first_attempt_verified=True, final_verified=True,
             success=True, attempts_count=1, total_cost_usd=Decimal("0.10"), cost_kind="actual",
             total_latency_seconds=10, attempts=[A(1, "verified", True, True, False)]),
        case("c10", category="slug", diagnosis_correct=False, first_attempt_verified=False, final_verified=False,
             repaired_by_retry=False, success=False, attempts_count=2, total_cost_usd=Decimal("0.28"),
             cost_kind="actual", total_latency_seconds=20,
             attempts=[A(1, "rejected", True, True, True), A(2, "rejected", True, True, True)]),
    ]


@pytest.fixture
def m():
    return metrics_by_name(compute_metrics(fixture()))


def check(m, name, numerator, denominator, value):
    got = m[name]
    assert (got.numerator, got.denominator) == (numerator, denominator), name
    if value is None:
        assert got.value is None, name
    else:
        assert got.value == pytest.approx(value), name


def test_every_metric_is_reported(m):
    assert list(m) == list(METRIC_NAMES)


def test_diagnosis_accuracy_counts_only_scored_diagnoses(m):
    # scored: c1 c2 c3 c4 c5 c9 c10 = 7; correct: c1 c2 c3 c5 c9 = 5. c6-c8 never had a scored diagnosis.
    check(m, "diagnosis_accuracy", 5, 7, 5 / 7)


def test_patch_validity_counts_generated_patches_only(m):
    # attempts that produced a patch: all 14 attempts (c1:1 c2:1 c3:2 c4:2 c5:1 c6:1 c9:1 c10:2 = 11).
    # invalid: only c5's. c7 and c8 have no attempts and are not "invalid patches".
    check(m, "patch_validity_rate", 10, 11, 10 / 11)


def test_patch_application_rate_uses_valid_patches_only(m):
    # valid patches: 10. applied: all but c6's = 9.
    check(m, "patch_application_rate", 9, 10, 0.9)


def test_first_attempt_and_final_verified_use_cases_with_an_attempt(m):
    # attempted: c1-c6, c9, c10 = 8. First attempt verified: c1 c2 c9 = 3. Finally verified: c1 c2 c3 c9 = 4.
    check(m, "first_attempt_verified_rate", 3, 8, 3 / 8)
    check(m, "final_verified_rate", 4, 8, 0.5)


def test_heldout_and_exploratory_use_only_applicable_cases(m):
    check(m, "heldout_pass_rate", 2, 3, 2 / 3)  # c1 c3 pass, c2 fails; others have no held-out tests
    check(m, "exploratory_pass_rate", 1, 2, 0.5)  # c1 passes, c3 fails


def test_regression_rate_is_per_applied_attempt(m):
    # applied attempts with a known result: 9 (c1:1 c2:1 c3:2 c4:2 c9:1 c10:2 = 9; c5 not applied, c6 unknown).
    # regressions: c3 att1, c10 att1, c10 att2 = 3.
    check(m, "regression_rate", 3, 9, 3 / 9)


def test_retry_success_needs_a_failed_first_attempt_and_a_retry(m):
    # retried: c3 c4 c10 (c5 and c6 failed once but were not retried). Repaired: c3.
    check(m, "retry_success_rate", 1, 3, 1 / 3)


def test_success_rate_and_completion(m):
    check(m, "success_rate", 3, 8, 3 / 8)  # c1 c3 c9; c7 and c8 reached no verdict
    check(m, "completion_rate", 8, 10, 0.8)  # c7 failed and c8 never started: they stay in the denominator


def test_means(m):
    check(m, "average_attempts", 11, 8, 11 / 8)
    check(m, "average_latency_seconds", 143, 9, 143 / 9)  # c8 never ran: no latency, not 0 s
    check(m, "average_cost_per_case", 1.5, 9, 1.5 / 9)


def test_cost_metrics_use_exact_decimal_sums(m):
    check(m, "total_cost_usd", 1.5, 9, 1.5)
    check(m, "cost_per_successful_repair", 1.5, 3, 0.5)  # all spend, failed cases included, over 3 successes
    ten_tenths = [case(f"x{i}", total_cost_usd=Decimal("0.1"), cost_kind="actual") for i in range(10)]
    assert metrics_by_name(compute_metrics(ten_tenths))["total_cost_usd"].value == 1.0  # 0.1 * 10, no float drift


def test_unstarted_cases_are_incomplete_not_failures_or_successes():
    only_unstarted = [case("u1", status="incomplete"), case("u2", status="incomplete")]
    got = metrics_by_name(compute_metrics(only_unstarted))
    check(got, "completion_rate", 0, 2, 0.0)  # a real zero: two cases, none finished
    for name in ("final_verified_rate", "success_rate", "first_attempt_verified_rate", "diagnosis_accuracy"):
        assert got[name].denominator == 0 and got[name].value is None, name


@pytest.mark.parametrize("name", [n for n in METRIC_NAMES])
def test_zero_denominator_is_null_not_zero_percent(name):
    got = metrics_by_name(compute_metrics([]))[name]
    assert got.denominator == 0 and got.value is None


def test_zero_numerator_with_a_real_denominator_is_zero_not_null():
    got = metrics_by_name(compute_metrics([case("z", final_verified=False, first_attempt_verified=False,
                                                attempts_count=1)]))
    assert got["final_verified_rate"].value == 0.0 and got["final_verified_rate"].denominator == 1


def test_no_retry_needed_is_not_a_retry_failure():
    cases = [case("ok", first_attempt_verified=True, final_verified=True, attempts_count=1, repaired_by_retry=False)]
    assert metrics_by_name(compute_metrics(cases))["retry_success_rate"].denominator == 0


def test_a_failed_first_attempt_without_a_retry_is_not_a_retry_failure():
    cases = [case("budget", first_attempt_verified=False, final_verified=False, attempts_count=1,
                  repaired_by_retry=False)]
    assert metrics_by_name(compute_metrics(cases))["retry_success_rate"].denominator == 0


def test_unknown_is_not_false():
    """Records that never stored a field must not be scored as if the field were false."""
    legacy = [case("old", final_verified=True, attempts_count=1, success=True)]  # nothing else was recorded
    got = metrics_by_name(compute_metrics(legacy))
    assert got["final_verified_rate"].value == 1.0
    for name in ("diagnosis_accuracy", "heldout_pass_rate", "first_attempt_verified_rate", "regression_rate",
                 "patch_validity_rate", "average_latency_seconds", "total_cost_usd"):
        assert got[name].value is None and got[name].denominator == 0, name


def test_cost_notes_distinguish_estimated_from_actual_and_missing():
    mixed = [case("a", total_cost_usd=Decimal(1), cost_kind="actual", attempts_count=1),
             case("b", total_cost_usd=Decimal(2), cost_kind="estimated", attempts_count=1),
             case("c", attempts_count=1)]
    got = metrics_by_name(compute_metrics(mixed))["total_cost_usd"]
    assert got.notes == {"cost_kind": "mixed", "cases_missing_cost": 1}
    only_estimate = metrics_by_name(compute_metrics([mixed[1]]))["total_cost_usd"]
    assert only_estimate.notes["cost_kind"] == "estimated"
    assert "cost_kind" not in metrics_by_name(compute_metrics([mixed[0]]))["total_cost_usd"].notes


def test_category_breakdown_uses_each_groups_own_population():
    groups = compute_grouped(fixture(), "category")
    assert list(groups) == ["category:config", "category:pagination", "category:pricing", "category:slug"]
    pagination = metrics_by_name(groups["category:pagination"])
    check(pagination, "final_verified_rate", 2, 2, 1.0)
    check(pagination, "heldout_pass_rate", 1, 2, 0.5)
    assert pagination["final_verified_rate"].grouping_key == "category:pagination"
    pricing = metrics_by_name(groups["category:pricing"])
    check(pricing, "final_verified_rate", 1, 3, 1 / 3)
    check(pricing, "retry_success_rate", 1, 2, 0.5)  # c3 and c4; c5 had no retry
    check(pricing, "regression_rate", 1, 4, 1 / 4)  # c3 x2 and c4 x2 applied; c5 never applied
    config = metrics_by_name(groups["category:config"])
    check(config, "final_verified_rate", 0, 1, 0.0)  # only c6 attempted; c7 and c8 are outside the denominator
    check(config, "completion_rate", 1, 3, 1 / 3)
    slug = metrics_by_name(groups["category:slug"])
    check(slug, "regression_rate", 2, 3, 2 / 3)


def test_category_numerators_add_up_to_the_overall_numerator():
    overall = metrics_by_name(compute_metrics(fixture()))
    groups = compute_grouped(fixture(), "category")
    for name in ("final_verified_rate", "diagnosis_accuracy", "patch_validity_rate", "regression_rate",
                 "completion_rate"):
        assert sum(metrics_by_name(g)[name].numerator for g in groups.values()) == overall[name].numerator
        assert sum(metrics_by_name(g)[name].denominator for g in groups.values()) == overall[name].denominator


def test_grouping_across_runs_by_model_retrieval_visibility_configuration_and_run():
    def at(case_id, **run):
        return case(case_id, final_verified=True, first_attempt_verified=True, attempts_count=1, **run)

    cases = [
        at("a", model_id="opus", retrieval_strategy="hybrid", test_visibility="blind", configuration_id="c1",
           run_id="r1"),
        at("a", model_id="opus", retrieval_strategy="keyword", test_visibility="blind", configuration_id="c2",
           run_id="r2"),
        case("b", final_verified=False, first_attempt_verified=False, attempts_count=1, model_id="sonnet",
             retrieval_strategy="hybrid", test_visibility="visible", configuration_id="c3", run_id="r3"),
    ]
    expected = {
        "model": {"model:opus": (2, 2), "model:sonnet": (0, 1)},
        "retrieval_strategy": {"retrieval_strategy:hybrid": (1, 2), "retrieval_strategy:keyword": (1, 1)},
        "test_visibility": {"test_visibility:blind": (2, 2), "test_visibility:visible": (0, 1)},
        "configuration": {"configuration:c1": (1, 1), "configuration:c2": (1, 1), "configuration:c3": (0, 1)},
        "run": {"run:r1": (1, 1), "run:r2": (1, 1), "run:r3": (0, 1)},
    }
    assert set(expected) | {"category"} == set(GROUP_BY)
    for by, groups in expected.items():
        got = compute_grouped(cases, by)
        assert {k: (metrics_by_name(v)["final_verified_rate"].numerator,
                    metrics_by_name(v)["final_verified_rate"].denominator) for k, v in got.items()} == groups
    assert group_key(case("x"), "category") == "category:unknown"
    with pytest.raises(ValueError, match="unknown grouping"):
        group_key(case("x"), "weather")


def test_metrics_do_not_depend_on_input_order_or_mutate_it():
    cases = fixture()
    forward = [m.as_dict() for m in compute_metrics(cases)]
    backward = [m.as_dict() for m in compute_metrics(list(reversed(cases)))]
    assert forward == backward
    assert evidence_digest(cases) == evidence_digest(list(reversed(cases)))


def test_evidence_digest_changes_when_any_evidence_changes():
    base = fixture()
    digest = evidence_digest(base)
    tampered = list(base)
    tampered[0] = case("c1", category="pagination", diagnosis_correct=True, first_attempt_verified=True,
                       final_verified=False, attempts_count=1, total_cost_usd=Decimal("0.10"), cost_kind="actual",
                       total_latency_seconds=10, attempts=[A(1, "verified", True, True, False)])
    assert evidence_digest(tampered) != digest


def test_population_counts_are_reported_with_the_metrics():
    assert summarize_cases(fixture()) == {"selected": 10, "completed": 8, "failed": 1, "incomplete": 1,
                                          "with_attempted_patch": 8}


def test_as_dict_has_the_documented_shape():
    result = metrics_by_name(compute_metrics(fixture()))["final_verified_rate"].as_dict()
    assert result == {"name": "final_verified_rate", "numerator": 4, "denominator": 8, "value": 0.5}
