"""The verification engine: independent checks, then one explicit verdict.

Pipeline for one applied patch (each step is a ``VerificationCheck``):

1. ``patch_application``  did the diff apply cleanly to the isolated copy?
2. ``syntax_check``       do the modified Python files parse? (``ast``, nothing executes)
3. ``original_bug_test``  do the tests that failed in the baseline now pass?
4. ``regression_tests``   do the targeted tests that passed before still pass?
5. ``ruff``               does the patch introduce new lint violations?
6. ``full_test_suite``    does the whole suite show no new failures? (when requested)
7. ``original_repository_unchanged``  is the original tree byte-for-byte the same?

Verdict, in this order:

- ``failed``: isolation was breached (the original changed). Nothing else can be trusted;
- ``rejected``: a required check definitely failed;
- ``rejected`` / ``inconclusive``: a required test run timed out. With
  ``timeout_policy="reject"`` a timeout where the baseline completed is a
  failure (a likely hang); anything else is inconclusive;
- ``inconclusive``: a required check could not run (bug not reproduced in the
  baseline, tool unavailable, time budget exhausted, results not comparable);
- ``verified``: every required check passed.

An unavailable or skipped check is never counted as a pass.
"""

import ast
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.patching.comparison import FAILING, compare_results
from app.patching.models import (
    PatchApplyResult,
    PatchLimits,
    PatchVerificationResult,
    TestOutcome,
    TestSelection,
    TestSuiteRun,
    VerificationCheck,
)
from app.patching.test_execution import (
    PytestRunner,
    RuffReport,
    RuffRunner,
    environment_identity,
)
from app.patching.validator import ValidatedPatch
from app.patching.workspace import Workspace
from app.tools.base import truncate_text

MIN_CHECK_SECONDS = 1.0


@dataclass
class Baseline:
    """Test results on the unpatched copy. The reference every patch is judged against."""

    runs: list[TestSuiteRun]
    full_suite: TestSuiteRun | None
    outcomes: dict[str, TestOutcome]
    flaky: set[str]
    bug_tests: list[str]
    reproducible: bool
    problems: list[str] = field(default_factory=list)
    ruff: dict[str, RuffReport] = field(default_factory=dict)  # by file set, computed on demand

    @property
    def full_outcomes(self) -> dict[str, TestOutcome]:
        if self.full_suite is None:
            return {}
        return {k: v.outcome for k, v in self.full_suite.tests.items()}

    def bug_test_details(self) -> list[dict[str, Any]]:
        first = self.runs[0] if self.runs else None
        return [
            {"test_id": t, "outcome": str(self.outcomes[t]), "message": first.tests[t].message if first else None}
            for t in self.bug_tests
            if first and t in first.tests
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "targeted_runs": [run.model_dump(mode="json") for run in self.runs],
            "full_suite": self.full_suite.model_dump(mode="json") if self.full_suite else None,
            "bug_tests": self.bug_tests,
            "flaky": sorted(self.flaky),
            "reproducible": self.reproducible,
            "problems": self.problems,
        }


def _remaining(deadline: float, limits: PatchLimits) -> float | None:
    remaining = deadline - time.monotonic()
    if remaining < MIN_CHECK_SECONDS:
        return None
    return min(limits.check_timeout_seconds, remaining)


def _short(text: str, limit: int = 4_000) -> str:
    return truncate_text(text, limit, keep="tail")[0]


def _run_check_fields(run: TestSuiteRun) -> dict[str, Any]:
    return {
        "exit_code": run.exit_code,
        "stdout": _short(run.stdout),
        "stderr": _short(run.stderr, 2_000),
        "duration_seconds": run.duration_seconds,
    }


def _matches(test_id: str, target: str, names: list[str]) -> bool:
    if not test_id.startswith(target + "::"):
        return False
    if not names:
        return True
    rest = test_id[len(target) + 2 :]
    return any(rest == n or rest.startswith(n + "[") for n in names)


class VerificationEngine:
    def __init__(
        self,
        limits: PatchLimits | None = None,
        *,
        runner: PytestRunner | None = None,
        ruff: RuffRunner | None = None,
    ) -> None:
        self.limits = limits or PatchLimits()
        self.runner = runner or PytestRunner(self.limits)
        self.ruff = ruff or RuffRunner(self.limits)

    # --- baseline ------------------------------------------------------------------------

    def capture_baseline(self, workspace: Workspace, selection: TestSelection, deadline: float) -> Baseline:
        problems: list[str] = []
        runs: list[TestSuiteRun] = []
        for index in range(self.limits.baseline_runs):
            timeout = _remaining(deadline, self.limits)
            if timeout is None:
                problems.append("The time budget ran out during the baseline")
                break
            runs.append(self.runner.run(workspace, selection.targets, label=f"baseline-{index + 1}",
                                        kind="targeted", timeout=timeout))
        completed = [r for r in runs if r.status == "completed"]
        for run in runs:
            if run.status != "completed":
                problems.append(f"Baseline run {run.label} did not complete: {run.status} ({run.error or 'no detail'})")

        outcomes = {k: v.outcome for k, v in completed[0].tests.items()} if completed else {}
        flaky: set[str] = set()
        for run in completed[1:]:
            for test_id in outcomes.keys() | run.tests.keys():
                if run.outcome(test_id) != outcomes.get(test_id):
                    flaky.add(test_id)
        if flaky:
            problems.append(f"{len(flaky)} test(s) changed outcome between baseline runs (flaky): {sorted(flaky)[:5]}")

        bug_tests: list[str] = []
        environment_failures = self._environment_failures(workspace, completed[0]) if completed else {}
        if environment_failures:
            modules = sorted(set(environment_failures.values()))
            problems.append(
                f"{len(environment_failures)} test(s) fail only because the test environment lacks "
                f"{', '.join(repr(m) for m in modules)}, which is not part of the repository. That is an "
                "environment problem, not the bug"
            )
        if not selection.bug_items:
            problems.append("The investigation recorded no failing test, so the bug cannot be reproduced")
        for item in selection.bug_items:
            matching = [t for t in outcomes if _matches(t, item.target, item.bug_test_names)]
            failing = [t for t in matching if outcomes[t] in FAILING and t not in environment_failures]
            flaky_bug = [t for t in failing if t in flaky]
            if flaky_bug:
                problems.append(f"Bug test(s) are flaky in the baseline: {flaky_bug[:5]}")
            bug_tests.extend(t for t in failing if t not in flaky)
            if not failing:
                problems.append(f"No test in {item.target} fails in the baseline: the bug was not reproduced")

        full = None
        if selection.full_suite:
            timeout = _remaining(deadline, self.limits)
            if timeout is None:
                problems.append("The time budget ran out before the baseline full suite")
            else:
                full = self.runner.run(workspace, [], label="baseline-full", kind="full_regression", timeout=timeout)
                if full.status != "completed":
                    problems.append(f"The baseline full suite did not complete: {full.status}")

        reproducible = bool(completed) and len(completed) == len(runs) and bool(bug_tests) and not any(
            t in flaky for t in bug_tests
        )
        return Baseline(
            runs=runs,
            full_suite=full,
            outcomes=outcomes,
            flaky=flaky,
            bug_tests=sorted(set(bug_tests)),
            reproducible=reproducible and bool(runs),
            problems=problems,
        )

    @staticmethod
    def _environment_failures(workspace: Workspace, run: TestSuiteRun) -> dict[str, str]:
        """Failing tests whose only problem is a missing third-party module: test id -> module."""
        out = {}
        for test_id, result in run.tests.items():
            if result.outcome not in FAILING or not result.missing_module:
                continue
            top = result.missing_module.split(".")[0]
            if not ((workspace.repo / f"{top}.py").exists() or (workspace.repo / top).is_dir()):
                out[test_id] = result.missing_module
        return out

    # --- verification --------------------------------------------------------------------

    def verify(
        self,
        *,
        patch_id: str,
        patch: ValidatedPatch | None,
        application: PatchApplyResult,
        workspace: Workspace | None,
        baseline_workspace: Workspace,
        baseline: Baseline,
        selection: TestSelection,
        deadline: float,
        original_unchanged: Callable[[], bool],
    ) -> PatchVerificationResult:
        start = time.monotonic()
        checks: list[VerificationCheck] = []
        after: dict[str, Any] = {}
        comparison = None

        def skip_rest(reason: str, names: list[str]) -> None:
            for name in names:
                required = not (name == "full_test_suite" and not selection.full_suite) and not (
                    name == "ruff" and not self.limits.require_ruff
                )
                checks.append(VerificationCheck(name=name, status="skipped", required=required, reason=reason))

        later = ["syntax_check", "original_bug_test", "regression_tests", "ruff", "full_test_suite"]

        # 1. application
        if application.applied:
            checks.append(VerificationCheck(name="patch_application", status="passed",
                                            duration_seconds=application.duration_seconds,
                                            details={"files_changed": application.files_changed}))
        else:
            environment = application.error_kind == "environment"
            checks.append(VerificationCheck(
                name="patch_application",
                status="skipped" if environment else "failed",
                reason="; ".join(application.errors)[:1000] or "the patch did not apply",
                stdout=application.stdout, stderr=application.stderr,
                duration_seconds=application.duration_seconds,
                details={"error_kind": application.error_kind, "unexpected_changes": application.unexpected_changes},
            ))
            skip_rest("not run: the patch did not apply", later)
        if application.applied and workspace is not None and patch is not None:
            self._run_checks(checks, after, patch, workspace, baseline_workspace, baseline, selection, deadline,
                             skip_rest)
            comparison = after.pop("_comparison", None)

        # 7. isolation, always
        try:
            unchanged = original_unchanged()
            checks.append(VerificationCheck(
                name="original_repository_unchanged", status="passed" if unchanged else "failed",
                reason=None if unchanged else "The original repository changed during verification",
            ))
        except Exception as exc:  # noqa: BLE001 - an unverifiable original is reported, not raised
            checks.append(VerificationCheck(name="original_repository_unchanged", status="skipped",
                                            reason=f"could not be checked: {type(exc).__name__}"))

        status, reasons = decide(checks, self.limits)
        return PatchVerificationResult(
            patch_id=patch_id,
            status=status,
            checks=checks,
            before_results=baseline.to_dict(),
            after_results=after,
            comparison=comparison,
            summary=summarize(status, reasons, baseline),
            failure_reasons=reasons,
            duration_seconds=round(time.monotonic() - start, 3),
            environment={
                **environment_identity(),
                "test_backend": self.runner.identity() if hasattr(self.runner, "identity") else {"backend": "custom"},
                "test_command": PytestRunner.command_identity(selection.targets),
                "snapshot_hash": baseline_workspace.snapshot.root_hash,
                "source_revision": baseline_workspace.snapshot.revision,
                "baseline_runs": self.limits.baseline_runs,
                "timeout_policy": self.limits.timeout_policy,
                "check_timeout_seconds": self.limits.check_timeout_seconds,
                "canonical_diff_sha256": patch.sha256 if patch else None,
            },
        )

    def _run_checks(self, checks, after, patch, workspace, baseline_workspace, baseline, selection, deadline,
                    skip_rest) -> None:
        # 2. syntax
        syntax_errors = []
        for file_patch in patch.parsed.files:
            if file_patch.operation == "delete" or not file_patch.path.endswith((".py", ".pyi")):
                continue
            path = workspace.repo / file_patch.path
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=file_patch.path)
            except SyntaxError as exc:
                syntax_errors.append({"file": file_patch.path, "line": exc.lineno, "message": exc.msg})
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                syntax_errors.append({"file": file_patch.path, "line": None, "message": type(exc).__name__})
        after["syntax"] = syntax_errors
        if syntax_errors:
            first = syntax_errors[0]
            checks.append(VerificationCheck(
                name="syntax_check", status="failed", details={"errors": syntax_errors},
                reason=f"{first['file']}:{first['line']}: {first['message']}",
            ))
            skip_rest("not run: the patched code does not parse", ["original_bug_test", "regression_tests", "ruff",
                                                                   "full_test_suite"])
            return
        checks.append(VerificationCheck(name="syntax_check", status="passed"))

        # 3 + 4. targeted tests
        timeout = _remaining(deadline, self.limits)
        if timeout is None:
            skip_rest("not run: the verification time budget was exhausted",
                      ["original_bug_test", "regression_tests", "ruff", "full_test_suite"])
            return
        run = self.runner.run(workspace, selection.targets, label="patched", kind="targeted", timeout=timeout)
        after["targeted"] = run.model_dump(mode="json")
        checks.extend(self._targeted_checks(run, baseline, after))

        # 5. ruff
        checks.append(self._ruff_check(patch, workspace, baseline_workspace, baseline, deadline, after))

        # 6. full suite
        checks.append(self._full_suite_check(workspace, baseline, selection, deadline, after))

    def _targeted_checks(self, run: TestSuiteRun, baseline: Baseline, after: dict) -> list[VerificationCheck]:
        fields = _run_check_fields(run)
        baseline_completed = bool(baseline.runs) and all(r.status == "completed" for r in baseline.runs)
        if run.status == "timed_out":
            details = {"baseline_completed": baseline_completed}
            return [
                VerificationCheck(name=name, status="timed_out", reason="the patched test run timed out",
                                  details=details, **fields)
                for name in ("original_bug_test", "regression_tests")
            ]
        if run.status != "completed":
            reason = f"the patched test run did not complete: {run.status} ({run.error or 'no detail'})"
            return [VerificationCheck(name=n, status="skipped", reason=reason, **fields)
                    for n in ("original_bug_test", "regression_tests")]

        patched = {k: v.outcome for k, v in run.tests.items()}
        comparison = compare_results(baseline.outcomes, patched, bug_tests=baseline.bug_tests, flaky=baseline.flaky)
        after["_comparison"] = comparison
        checks = []

        if not baseline.reproducible:
            checks.append(VerificationCheck(
                name="original_bug_test", status="skipped",
                reason="the bug was not reproduced in the baseline, so a fix cannot be confirmed",
                details={"problems": baseline.problems}, **fields,
            ))
        else:
            still = [t for t in baseline.bug_tests if patched.get(t) != TestOutcome.PASSED]
            details = {"bug_tests": [
                {"test_id": t, "before": str(baseline.outcomes.get(t)), "after": str(patched.get(t)) if t in patched else None,
                 "message": run.tests[t].message if t in run.tests else None}
                for t in baseline.bug_tests
            ]}
            if still:
                first = still[0]
                after_outcome = patched.get(first)
                message = run.tests[first].message if first in run.tests else "the test did not run"
                checks.append(VerificationCheck(
                    name="original_bug_test", status="failed", details=details, **fields,
                    reason=f"{first} still {after_outcome or 'missing'} after the patch: {message}",
                ))
            else:
                checks.append(VerificationCheck(name="original_bug_test", status="passed", details=details, **fields))

        regressions = [t for t in comparison.regressed if t not in comparison.bug_tests]
        missing = [e.test_id for e in comparison.entries if e.classification == "missing_after"]
        details = {"regressed": regressions, "missing_after": missing, "flaky_excluded": sorted(baseline.flaky),
                   "summary": comparison.summary}
        if not baseline.outcomes:
            checks.append(VerificationCheck(name="regression_tests", status="skipped", details=details, **fields,
                                            reason="no baseline results to compare against"))
        elif regressions:
            first = regressions[0]
            entry = next(e for e in comparison.entries if e.test_id == first)
            message = run.tests[first].message if first in run.tests else None
            checks.append(VerificationCheck(
                name="regression_tests", status="failed", details=details, **fields,
                reason=f"{first} {entry.before or 'absent'} -> {entry.after}: {message or 'no message'}",
            ))
        elif missing:
            checks.append(VerificationCheck(name="regression_tests", status="skipped", details=details, **fields,
                                            reason=f"{len(missing)} baseline test(s) did not run after the patch"))
        else:
            checks.append(VerificationCheck(name="regression_tests", status="passed", details=details, **fields))
        return checks

    def _ruff_check(self, patch, workspace, baseline_workspace, baseline, deadline, after) -> VerificationCheck:
        required = self.limits.require_ruff
        if not required:
            return VerificationCheck(name="ruff", status="skipped", required=False, reason="disabled by policy")
        if not self.ruff.available():
            return VerificationCheck(name="ruff", status="skipped", reason="ruff is not installed")
        files = [f.path for f in patch.parsed.files if f.operation != "delete" and f.path.endswith((".py", ".pyi"))]
        timeout = _remaining(deadline, self.limits)
        if timeout is None:
            return VerificationCheck(name="ruff", status="skipped", reason="the verification time budget was exhausted")
        key = "\0".join(sorted(files))
        before = baseline.ruff.get(key)
        if before is None:
            before = baseline.ruff[key] = self.ruff.run(baseline_workspace, files, timeout=timeout)
        timeout = _remaining(deadline, self.limits)
        if timeout is None:
            return VerificationCheck(name="ruff", status="skipped", reason="the verification time budget was exhausted")
        report = self.ruff.run(workspace, files, timeout=timeout)
        after["ruff"] = report.model_dump(mode="json")
        after["ruff_baseline"] = before.model_dump(mode="json")
        if report.timed_out or before.timed_out:
            return VerificationCheck(name="ruff", status="timed_out", reason="ruff timed out",
                                     details={"baseline_completed": False})
        if report.error or before.error:
            return VerificationCheck(name="ruff", status="skipped", reason=f"ruff failed: {report.error or before.error}")
        new = report.keys() - before.keys()
        details = {"new_violations": [v for v in report.violations if (v["file"], v["code"], v["message"]) in new],
                   "baseline_violations": len(before.violations), "patched_violations": len(report.violations)}
        if new:
            first = details["new_violations"][0]
            return VerificationCheck(name="ruff", status="failed", exit_code=report.exit_code, details=details,
                                     reason=f"new lint violation {first['code']} at {first['file']}:{first['line']}: "
                                            f"{first['message']}")
        return VerificationCheck(name="ruff", status="passed", exit_code=report.exit_code, details=details,
                                 duration_seconds=report.duration_seconds)

    def _full_suite_check(self, workspace, baseline: Baseline, selection, deadline, after) -> VerificationCheck:
        if not selection.full_suite:
            return VerificationCheck(name="full_test_suite", status="skipped", required=False, reason="not requested")
        if baseline.full_suite is None or baseline.full_suite.status != "completed":
            status = baseline.full_suite.status if baseline.full_suite else "not run"
            return VerificationCheck(name="full_test_suite", status="skipped",
                                     reason=f"the baseline full suite is unavailable ({status})")
        timeout = _remaining(deadline, self.limits)
        if timeout is None:
            return VerificationCheck(name="full_test_suite", status="skipped",
                                     reason="the verification time budget was exhausted")
        run = self.runner.run(workspace, [], label="patched-full", kind="full_regression", timeout=timeout)
        after["full_suite"] = run.model_dump(mode="json")
        fields = _run_check_fields(run)
        if run.status == "timed_out":
            return VerificationCheck(name="full_test_suite", status="timed_out", reason="the full suite timed out",
                                     details={"baseline_completed": True}, **fields)
        if run.status != "completed":
            return VerificationCheck(name="full_test_suite", status="skipped",
                                     reason=f"the patched full suite did not complete: {run.status}", **fields)
        comparison = compare_results(
            baseline.full_outcomes, {k: v.outcome for k, v in run.tests.items()},
            bug_tests=baseline.bug_tests, flaky=baseline.flaky,
        )
        after["full_suite_comparison"] = comparison.model_dump(mode="json")
        regressions = [t for t in comparison.regressed if t not in comparison.bug_tests]
        details = {"regressed": regressions, "still_failing": comparison.still_failing, "summary": comparison.summary,
                   "tests": len(run.tests)}
        if regressions:
            return VerificationCheck(name="full_test_suite", status="failed", details=details, **fields,
                                     reason=f"{len(regressions)} test(s) newly fail in the full suite: {regressions[:3]}")
        missing = [e.test_id for e in comparison.entries if e.classification == "missing_after"]
        if missing:
            return VerificationCheck(name="full_test_suite", status="skipped", details=details, **fields,
                                     reason=f"{len(missing)} test(s) did not run after the patch")
        pre_existing = [t for t in comparison.still_failing if t not in comparison.bug_tests]
        reason = f"{len(pre_existing)} pre-existing failure(s) unchanged" if pre_existing else None
        return VerificationCheck(name="full_test_suite", status="passed", details=details, reason=reason, **fields)


def decide(checks: list[VerificationCheck], limits: PatchLimits) -> tuple[str, list[str]]:
    """The verdict and its reasons. See the module docstring for the order."""
    required = [c for c in checks if c.required]
    isolation = next((c for c in checks if c.name == "original_repository_unchanged"), None)
    if isolation is not None and isolation.status == "failed":
        return "failed", [isolation.reason or "isolation breached"]
    failed = [c for c in required if c.status == "failed"]
    if failed:
        return "rejected", [f"{c.name}: {c.reason}" for c in failed]
    timed_out = [c for c in required if c.status == "timed_out"]
    if timed_out:
        reasons = [f"{c.name}: {c.reason}" for c in timed_out]
        if limits.timeout_policy == "reject" and any(c.details.get("baseline_completed") for c in timed_out):
            return "rejected", [r + " (the baseline completed, so the patch likely hangs)" for r in reasons]
        return "inconclusive", reasons
    skipped = [c for c in required if c.status == "skipped"]
    if skipped:
        return "inconclusive", [f"{c.name}: {c.reason}" for c in skipped]
    return "verified", []


def summarize(status: str, reasons: list[str], baseline: Baseline) -> str:
    if status == "verified":
        tests = ", ".join(baseline.bug_tests[:3])
        return (
            f"The patch fixes the original failure ({tests}) and every required check passed in an isolated "
            "workspace. The original repository was not modified."
        )
    label = {"rejected": "Rejected", "inconclusive": "Inconclusive", "failed": "Failed"}[status]
    return f"{label}: " + "; ".join(reasons[:4])
