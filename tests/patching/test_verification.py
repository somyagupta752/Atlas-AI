"""Verification scenarios on a real copy of bug_001_average, with real pytest and ruff runs.

A runner subclass injects timeouts, flakiness or extra failures where a real
repository cannot produce them deterministically.
"""

import time

import pytest

from app.patching import test_execution
from app.patching.applier import apply_patch
from app.patching.models import (
    PatchApplyResult,
    PatchLimits,
    TestOutcome,
    VerificationCheck,
)
from app.patching.test_execution import PytestRunner, RuffRunner
from app.patching.test_selector import select_tests
from app.patching.validator import PatchScope, validate_patch
from app.patching.verification import VerificationEngine, decide
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_NO_FIX,
    AVERAGE_REGRESSION,
    AVERAGE_SYNTAX_ERROR,
    FAST,
    make_analysis,
)

EMPTY = "test_solution.py::test_average_of_empty_list"
HANG = AVERAGE_FIX.replace("+        return 0.0", "+        while True:\n+            pass")
UNUSED_IMPORT = AVERAGE_FIX.replace("@@ -1,2 +1,4 @@\n", "@@ -1,2 +1,7 @@\n+import os\n+\n+\n")


class ScriptedRunner(PytestRunner):
    """The real runner, with per-label rewrites of its results."""

    def __init__(self, limits, overrides):
        super().__init__(limits)
        self.overrides = overrides
        self.labels = []

    def run(self, workspace, targets, *, label, kind, timeout):
        self.labels.append(label)
        result = super().run(workspace, targets, label=label, kind=kind, timeout=timeout)
        rewrite = self.overrides.get(label)
        return rewrite(result) if rewrite else result


def timed_out(run):
    return run.model_copy(update={"status": "timed_out", "tests": {}, "exit_code": None})


def outcome(test_id, value):
    def rewrite(run):
        tests = dict(run.tests)
        tests[test_id] = tests[test_id].model_copy(update={"outcome": value, "message": "injected"})
        return run.model_copy(update={"tests": tests})

    return rewrite


@pytest.fixture
def verify(average_repo, workspaces):
    def run(diff, *, limits=FAST, runner=None, ruff=None, analysis=None, full_suite=True, repo=None,
            unchanged=lambda: True, deadline_seconds=120.0, application=None):
        root = repo or average_repo
        engine = VerificationEngine(limits, runner=runner, ruff=ruff)
        baseline_ws = workspaces.create(root, execution_id="e", label="baseline")
        selection = select_tests(baseline_ws.repo, analysis or make_analysis("i"), affected_files=["solution.py"],
                                 include_full_suite=full_suite)
        baseline = engine.capture_baseline(baseline_ws, selection, time.monotonic() + 120)
        result, validated = validate_patch(diff, PatchScope(baseline_ws.repo, frozenset({"solution.py"})), limits)
        assert validated is not None, result.errors
        workspace = workspaces.create(root, execution_id="e", label="patched-1")
        applied = application or apply_patch(workspace, validated)
        verification = engine.verify(
            patch_id="p", patch=validated, application=applied, workspace=workspace, baseline_workspace=baseline_ws,
            baseline=baseline, selection=selection, deadline=time.monotonic() + deadline_seconds,
            original_unchanged=unchanged,
        )
        return verification, baseline

    return run


def statuses(verification):
    return {c.name: c.status for c in verification.checks}


def test_scenario_a_correct_patch_is_verified(verify):
    verification, baseline = verify(AVERAGE_FIX)
    assert baseline.reproducible and baseline.bug_tests == [EMPTY]
    assert baseline.outcomes[EMPTY] == TestOutcome.FAILED
    assert verification.status == "verified", verification.summary
    assert statuses(verification) == {
        "patch_application": "passed", "syntax_check": "passed", "original_bug_test": "passed",
        "regression_tests": "passed", "ruff": "passed", "full_test_suite": "passed",
        "original_repository_unchanged": "passed",
    }
    assert verification.comparison.fixed == [EMPTY] and not verification.comparison.regressed
    assert verification.after_results["targeted"]["tests"][EMPTY]["outcome"] == "passed"
    assert verification.before_results["targeted_runs"][0]["tests"][EMPTY]["outcome"] == "failed"
    assert verification.environment["test_command"][:3] == ["python", "-m", "pytest"]
    assert verification.environment["python"] and not verification.failure_reasons


def test_baseline_and_patched_runs_use_the_same_command(verify):
    verification, _ = verify(AVERAGE_FIX)
    assert verification.before_results["targeted_runs"][0]["command"] == verification.after_results["targeted"]["command"]


def test_scenario_b_patch_that_does_not_fix_the_bug_is_rejected(verify):
    verification, _ = verify(AVERAGE_NO_FIX)
    assert verification.status == "rejected"
    check = verification.check("original_bug_test")
    assert check.status == "failed" and EMPTY in check.reason and "ZeroDivisionError" in check.reason
    assert verification.check("regression_tests").status == "passed"


def test_scenario_c_fix_that_breaks_another_test_is_rejected(verify):
    verification, _ = verify(AVERAGE_REGRESSION)
    assert verification.status == "rejected"
    assert verification.check("original_bug_test").status == "passed"
    regression = verification.check("regression_tests")
    assert regression.status == "failed"
    assert "test_solution.py::test_average_of_multiple_numbers" in regression.details["regressed"]
    assert verification.check("full_test_suite").status == "failed"


def test_scenario_d_real_hang_times_out_and_is_never_verified(verify):
    limits = PatchLimits(baseline_runs=1, check_timeout_seconds=3)
    started = time.monotonic()
    verification, _ = verify(HANG, limits=limits, full_suite=False)
    assert time.monotonic() - started < 20
    assert verification.check("original_bug_test").status == "timed_out"
    assert verification.status == "rejected"  # the baseline completed, so the policy treats it as a hang
    assert "likely hangs" in verification.failure_reasons[0]


def test_scenario_d_timeout_with_inconclusive_policy(verify):
    limits = PatchLimits(baseline_runs=1, timeout_policy="inconclusive")
    runner = ScriptedRunner(limits, {"patched": timed_out})
    verification, _ = verify(AVERAGE_FIX, limits=limits, runner=runner)
    assert verification.status == "inconclusive"


def test_baseline_timeout_makes_the_result_inconclusive(verify):
    runner = ScriptedRunner(FAST, {"baseline-1": timed_out})
    verification, baseline = verify(AVERAGE_FIX, runner=runner)
    assert not baseline.reproducible and "did not complete" in baseline.problems[0]
    assert verification.status == "inconclusive"
    assert verification.check("original_bug_test").status == "skipped"


def test_scenario_e_bug_not_reproducible_is_inconclusive(verify, average_repo):
    (average_repo / "solution.py").write_text(
        "def calculate_average(numbers):\n    if not numbers:\n        return 0.0\n    return sum(numbers) / len(numbers)\n"
    )
    already_fixed = AVERAGE_FIX.replace(
        "@@ -1,2 +1,4 @@\n def calculate_average(numbers):\n+    if not numbers:\n+        return 0.0\n",
        "@@ -1,4 +1,4 @@\n def calculate_average(numbers):\n     if not numbers:\n-        return 0.0\n+        return 0\n",
    )
    verification, baseline = verify(already_fixed)
    assert not baseline.reproducible and any("not reproduced" in p for p in baseline.problems)
    assert verification.status == "inconclusive"
    assert "fixes" not in verification.summary
    assert verification.check("original_bug_test").status == "skipped"


def test_no_failing_test_in_the_investigation_is_inconclusive(verify):
    verification, baseline = verify(AVERAGE_FIX, analysis=make_analysis("i", tests_run=[]))
    assert not baseline.reproducible and "no failing test" in baseline.problems[0]
    assert verification.status == "inconclusive"


def test_flaky_bug_test_is_inconclusive(verify):
    limits = PatchLimits(baseline_runs=2)
    runner = ScriptedRunner(limits, {"baseline-2": outcome(EMPTY, TestOutcome.PASSED)})
    verification, baseline = verify(AVERAGE_FIX, limits=limits, runner=runner)
    assert EMPTY in baseline.flaky and not baseline.reproducible
    assert verification.status == "inconclusive"


def test_flaky_regression_test_is_excluded_not_counted(verify):
    limits = PatchLimits(baseline_runs=2)
    other = "test_solution.py::test_average_of_single_number"
    runner = ScriptedRunner(limits, {"baseline-2": outcome(other, TestOutcome.FAILED),
                                     "patched": outcome(other, TestOutcome.FAILED)})
    verification, _ = verify(AVERAGE_FIX, limits=limits, runner=runner, full_suite=False)
    assert verification.check("regression_tests").details["flaky_excluded"] == [other]
    assert verification.status == "verified"


def test_syntax_error_is_rejected_without_running_tests(verify):
    runner = ScriptedRunner(FAST, {})
    verification, _ = verify(AVERAGE_SYNTAX_ERROR, runner=runner)
    assert verification.status == "rejected"
    assert verification.check("syntax_check").status == "failed"
    assert verification.check("original_bug_test").status == "skipped"
    assert "patched" not in runner.labels


def test_new_lint_violation_is_rejected(verify):
    verification, _ = verify(UNUSED_IMPORT)
    check = verification.check("ruff")
    assert verification.status == "rejected" and check.status == "failed"
    assert check.details["new_violations"][0]["code"] == "F401"
    assert verification.check("original_bug_test").status == "passed"


def test_pre_existing_lint_violations_are_not_blamed_on_the_patch(verify, average_repo):
    (average_repo / "solution.py").write_text("import os\n\n\n" + (average_repo / "solution.py").read_text())
    shifted = AVERAGE_FIX.replace("@@ -1,2 +1,4 @@", "@@ -4,2 +4,4 @@")
    verification, _ = verify(shifted)
    ruff = verification.check("ruff")
    assert ruff.status == "passed" and ruff.details["baseline_violations"] == 1


def test_unavailable_ruff_is_not_a_pass(verify, monkeypatch):
    monkeypatch.setattr(RuffRunner, "available", staticmethod(lambda: False))
    verification, _ = verify(AVERAGE_FIX)
    assert verification.check("ruff").status == "skipped" and verification.status == "inconclusive"
    monkeypatch.setattr(test_execution.RuffRunner, "available", staticmethod(lambda: False))
    relaxed, _ = verify(AVERAGE_FIX, limits=PatchLimits(baseline_runs=1, require_ruff=False))
    assert relaxed.status == "verified" and not relaxed.check("ruff").required


def test_full_suite_regression_is_rejected(verify):
    other = "test_solution.py::test_average_with_negative_numbers"
    runner = ScriptedRunner(FAST, {"patched-full": outcome(other, TestOutcome.FAILED)})
    verification, _ = verify(AVERAGE_FIX, runner=runner)
    assert verification.status == "rejected"
    assert verification.check("full_test_suite").details["regressed"] == [other]
    assert verification.check("regression_tests").status == "passed"


def test_full_suite_not_requested_is_not_required(verify):
    verification, _ = verify(AVERAGE_FIX, full_suite=False)
    check = verification.check("full_test_suite")
    assert check.status == "skipped" and not check.required and verification.status == "verified"


def test_isolation_breach_fails_the_verification(verify):
    verification, _ = verify(AVERAGE_FIX, unchanged=lambda: False)
    assert verification.status == "failed"
    assert verification.check("original_repository_unchanged").status == "failed"


def test_patch_that_did_not_apply(verify):
    conflict = PatchApplyResult(applied=False, error_kind="conflict", errors=["does not apply"])
    verification, _ = verify(AVERAGE_FIX, application=conflict)
    assert verification.status == "rejected" and verification.check("patch_application").status == "failed"
    assert all(c.status == "skipped" for c in verification.checks if c.name not in
               ("patch_application", "original_repository_unchanged"))
    environment = PatchApplyResult(applied=False, error_kind="environment", errors=["git is not available"])
    verification, _ = verify(AVERAGE_FIX, application=environment)
    assert verification.status == "inconclusive"


def test_exhausted_time_budget_is_inconclusive(verify):
    verification, _ = verify(AVERAGE_FIX, deadline_seconds=0)
    assert verification.status == "inconclusive"
    assert "time budget" in verification.check("original_bug_test").reason


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([("syntax_check", "passed", True), ("ruff", "skipped", True)], "inconclusive"),
        ([("syntax_check", "passed", True), ("ruff", "skipped", False)], "verified"),
        ([("syntax_check", "failed", True), ("ruff", "skipped", True)], "rejected"),
        ([("original_bug_test", "timed_out", True)], "inconclusive"),
        ([("original_repository_unchanged", "failed", True), ("syntax_check", "failed", True)], "failed"),
        ([], "verified"),
    ],
)
def test_decide(checks, expected):
    built = [VerificationCheck(name=n, status=s, required=r) for n, s, r in checks]
    assert decide(built, FAST)[0] == expected


def test_missing_third_party_dependency_is_not_a_reproduced_bug(write_repo, workspaces):
    root = write_repo({"mod.py": "import not_installed_pkg\n\n\ndef f():\n    return 1 / 0\n",
                       "test_mod.py": "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n"}, name="dep")
    engine = VerificationEngine(FAST)
    workspace = workspaces.create(root, execution_id="e", label="baseline")
    analysis = make_analysis("i", affected_files=["mod.py"], evidence=[], tests_run=[
        __import__("app.agents.schemas", fromlist=["TestRunRecord"]).TestRunRecord(
            observation_id="o", test_path="test_mod.py", exit_code=1, timed_out=False, duration_seconds=0.1,
            outcome="failed")])
    selection = select_tests(workspace.repo, analysis, affected_files=["mod.py"], include_full_suite=False)
    baseline = engine.capture_baseline(workspace, selection, time.monotonic() + 60)
    assert not baseline.reproducible and baseline.bug_tests == []
    assert any("'not_installed_pkg'" in p and "environment problem" in p for p in baseline.problems)


def test_missing_repository_module_still_counts_as_the_bug(write_repo, workspaces):
    # The repository's own module is missing (renamed by the bug): that is a real, fixable failure.
    root = write_repo({"helpers/__init__.py": "", "mod.py": "from helpers.util import x\n",
                       "test_mod.py": "import mod\n\n\ndef test_x():\n    assert mod.x == 1\n"}, name="own")
    engine = VerificationEngine(FAST)
    workspace = workspaces.create(root, execution_id="e", label="baseline")
    analysis = make_analysis("i", affected_files=["mod.py"], evidence=[], tests_run=[
        __import__("app.agents.schemas", fromlist=["TestRunRecord"]).TestRunRecord(
            observation_id="o", test_path="test_mod.py", exit_code=1, timed_out=False, duration_seconds=0.1,
            outcome="failed")])
    selection = select_tests(workspace.repo, analysis, affected_files=["mod.py"], include_full_suite=False)
    baseline = engine.capture_baseline(workspace, selection, time.monotonic() + 60)
    assert baseline.reproducible and baseline.bug_tests
