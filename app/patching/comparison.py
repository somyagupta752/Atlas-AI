"""Compare baseline and patched test results test by test, never by aggregate counts.

"10 failures became 5" says nothing on its own: five tests may have been fixed
while five others broke. Each test ID is classified individually:

    before          after            classification
    passed          passed           preserved
    failed/error    passed           fixed
    passed          failed/error     regressed
    failed/error    failed/error     still_failing
    passed          skipped          became_skipped   (coverage lost: counts as a regression)
    skipped         any non-failing  skipped
    absent          passed           new_passing
    absent          failed/error     new_failure      (counts as a regression)
    present         absent           missing_after    (not comparable)
    flaky baseline  any              flaky            (not comparable)

Bug tests are the tests that reproduced the bug in the baseline. They are
expected to move from failing to passing.
"""

from app.patching.models import (
    Classification,
    ComparisonResult,
    TestComparison,
    TestOutcome,
)

FAILING = frozenset({TestOutcome.FAILED, TestOutcome.ERROR})


def classify(before: TestOutcome | None, after: TestOutcome | None) -> Classification:
    if before is None:
        return "new_failure" if after in FAILING else "new_passing"
    if after is None:
        return "missing_after"
    if before == TestOutcome.SKIPPED:
        return "regressed" if after in FAILING else "skipped"
    if before == TestOutcome.PASSED:
        if after == TestOutcome.PASSED:
            return "preserved"
        return "became_skipped" if after == TestOutcome.SKIPPED else "regressed"
    # before failed or errored
    if after == TestOutcome.PASSED:
        return "fixed"
    return "still_failing" if after in FAILING else "missing_after"  # failing -> skipped hides the result


def compare_results(
    before: dict[str, TestOutcome],
    after: dict[str, TestOutcome],
    *,
    bug_tests: list[str],
    flaky: set[str] | frozenset[str] = frozenset(),
) -> ComparisonResult:
    entries: list[TestComparison] = []
    bug = set(bug_tests)
    for test_id in sorted(before.keys() | after.keys()):
        b, a = before.get(test_id), after.get(test_id)
        classification: Classification = "flaky" if test_id in flaky else classify(b, a)
        entries.append(
            TestComparison(test_id=test_id, before=b, after=a, classification=classification, is_bug_test=test_id in bug)
        )

    def ids(*kinds: str) -> list[str]:
        return [e.test_id for e in entries if e.classification in kinds]

    result = ComparisonResult(
        entries=entries,
        fixed=ids("fixed"),
        regressed=ids("regressed", "became_skipped", "new_failure"),
        still_failing=ids("still_failing"),
        new_failures=ids("new_failure"),
        skipped=ids("skipped"),
        not_comparable=ids("missing_after", "flaky"),
        bug_tests=sorted(bug),
        bug_tests_fixed=bool(bug) and all(after.get(t) == TestOutcome.PASSED for t in bug),
    )
    result.summary = summarize(result)
    return result


def summarize(result: ComparisonResult) -> str:
    preserved = sum(1 for e in result.entries if e.classification == "preserved")
    parts = [
        f"{len(result.fixed)} fixed",
        f"{preserved} preserved",
        f"{len(result.regressed)} regressed",
        f"{len(result.still_failing)} still failing",
    ]
    if result.new_failures:
        parts.append(f"{len(result.new_failures)} new failures")
    if result.skipped:
        parts.append(f"{len(result.skipped)} skipped")
    if result.not_comparable:
        parts.append(f"{len(result.not_comparable)} not comparable")
    bug = "all bug tests pass" if result.bug_tests_fixed else "bug tests not all passing"
    return ", ".join(parts) + f"; {bug}"
