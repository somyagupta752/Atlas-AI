import pytest

from app.patching.comparison import classify, compare_results
from app.patching.models import TestOutcome

P, F, S, E = TestOutcome.PASSED, TestOutcome.FAILED, TestOutcome.SKIPPED, TestOutcome.ERROR


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (P, P, "preserved"),
        (F, P, "fixed"),
        (E, P, "fixed"),
        (P, F, "regressed"),
        (P, E, "regressed"),
        (F, F, "still_failing"),
        (F, E, "still_failing"),
        (P, S, "became_skipped"),
        (S, S, "skipped"),
        (S, P, "skipped"),
        (S, F, "regressed"),
        (F, S, "missing_after"),
        (None, P, "new_passing"),
        (None, F, "new_failure"),
        (P, None, "missing_after"),
    ],
)
def test_classification(before, after, expected):
    assert classify(before, after) == expected


def test_the_average_example():
    before = {"t::normal": P, "t::single": P, "t::empty": F, "t::negative": P}
    after = {"t::normal": P, "t::single": P, "t::empty": P, "t::negative": P}
    result = compare_results(before, after, bug_tests=["t::empty"])
    assert result.fixed == ["t::empty"] and result.bug_tests_fixed
    assert not result.regressed and not result.not_comparable
    assert {e.test_id: e.classification for e in result.entries}["t::normal"] == "preserved"
    assert result.summary.startswith("1 fixed, 3 preserved, 0 regressed")


def test_fewer_failures_can_still_hide_regressions():
    before = {f"t::{i}": (F if i < 10 else P) for i in range(20)}
    after = dict(before)
    for i in range(7):  # 7 fixed ...
        after[f"t::{i}"] = P
    after["t::10"] = F  # ... 2 broken: 10 failures became 5, and two of those are new
    after["t::11"] = E
    result = compare_results(before, after, bug_tests=["t::0"])
    assert sum(o in (F, E) for o in after.values()) == 5
    assert result.regressed == ["t::10", "t::11"]
    assert len(result.fixed) == 7 and len(result.still_failing) == 3


def test_skipped_is_never_passed_and_new_failures_count():
    before = {"a": P, "b": S}
    after = {"a": S, "b": S, "c": F}
    result = compare_results(before, after, bug_tests=[])
    assert result.regressed == ["a", "c"] and result.new_failures == ["c"] and result.skipped == ["b"]
    assert not result.bug_tests_fixed  # no bug tests: nothing was shown fixed


def test_missing_and_flaky_tests_are_not_comparable():
    result = compare_results({"a": P, "b": F, "c": P}, {"b": P, "c": F}, bug_tests=["b"], flaky={"c"})
    assert result.not_comparable == ["a", "c"]
    assert result.regressed == [] and result.bug_tests_fixed


def test_bug_test_must_pass_not_just_change():
    result = compare_results({"bug": F}, {"bug": E}, bug_tests=["bug"])
    assert not result.bug_tests_fixed and result.still_failing == ["bug"]
