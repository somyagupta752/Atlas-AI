"""Score patch executions against benchmark cases.

Expected answers (expected files, required tests, reference patches, held-out
tests) live only in ``benchmarks/patching/`` and here. The model receives the
diagnosis and the source, never these.

A green test suite is not the success metric. A patch that deletes or weakens a
failing test can make it green, and a patch that special-cases the reported
input passes every repository test. Success therefore needs all of:

- ``verified`` by the engine;
- the case's own ``required_tests`` passing in the patched run;
- only expected files modified;
- the held-out tests passing. They are kept outside the repository, so neither
  the model nor the verifier ever sees them. They run against the final patch
  in a fresh copy, after the execution. Held-out tests check only behaviour the
  specification defines (docstrings, the bug report, existing tests).

Exploratory tests check behaviour the specification leaves open. They run the
same way and are reported, but they never affect the held-out rate or success:
failing them means the patch made a different defensible choice, not a wrong one.

Per case: lifecycle stages (generated, validated, applied), bug fixed,
regression free, verified, attempts, the status of every attempt (so retries
can be measured), regressions per attempt, held-out result, diagnosis
correctness when Task 12 produced the diagnosis, latency and cost (patch and
diagnosis separately).
"""

import shutil
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.agents.evaluation import cost_usd
from app.patching.models import PatchExecution, PatchLimits


class ReferencePatch(BaseModel):
    """A fixed patch (or a sequence, one per attempt) with a known verdict. Tests the verifier, not a model."""

    name: str
    file: str | None = None
    files: list[str] = Field(default_factory=list)
    expected_status: str
    expected_attempts: int | None = None
    # None: not checked. True/False: the held-out tests must pass / must fail (an overfit patch).
    expected_holdout: bool | None = None
    description: str = ""

    @model_validator(mode="after")
    def _one_source(self) -> "ReferencePatch":
        if bool(self.file) == bool(self.files):
            raise ValueError("give exactly one of file or files")
        return self

    @property
    def diff_files(self) -> list[str]:
        return [self.file] if self.file else list(self.files)


class PatchingBenchmarkCase(BaseModel):
    case_id: str
    category: str = "single_file"
    difficulty: str = "baseline"
    repository_id: str
    investigation_case: str
    repository_path: str
    expected_files: list[str]
    expected_behaviour: str
    required_tests: list[str] = Field(min_length=1)
    holdout_tests: list[str] = Field(default_factory=list)
    # Unspecified behaviour: reported, never scored.
    exploratory_tests: list[str] = Field(default_factory=list)
    reference_patches: list[ReferencePatch] = Field(default_factory=list)


class PatchCaseScore(BaseModel):
    case_id: str
    category: str = "single_file"
    variant: str  # "live" or a reference patch name
    status: str
    expected_status: str | None = None
    matches_expectation: bool | None = None
    generated: bool
    validated: bool
    applied: bool
    bug_fixed: bool
    regression_free: bool
    verified: bool
    success: bool
    only_expected_files: bool
    attempts: int
    attempt_statuses: list[str] = Field(default_factory=list)
    attempts_with_regressions: int = 0
    attempts_applied: int = 0
    holdout_passed: bool | None = None
    holdout_results: dict[str, str] = Field(default_factory=dict)
    first_attempt_failed: bool = False
    # Evidence-based: see repair_evidence(). A first-attempt success can never count as a repair.
    repaired_by_retry: bool = False
    repair_evidence: dict[str, Any] = Field(default_factory=dict)
    exploratory_passed: bool | None = None  # informational only
    exploratory_results: dict[str, str] = Field(default_factory=dict)
    files_modified: list[str]
    lines_added: int
    lines_removed: int
    verification_failures: list[str]
    rejected_reasons: list[str]
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    diagnosis_status: str | None = None
    diagnosis_correct: bool | None = None
    diagnosis_cost_usd: float | None = None
    diagnosis_seconds: float | None = None
    failure_reason: str | None = None
    stop_reason: str | None = None

    @property
    def total_cost_usd(self) -> float | None:
        if self.cost_usd is None:
            return None
        return self.cost_usd + (self.diagnosis_cost_usd or 0.0)

    @property
    def total_seconds(self) -> float:
        return self.duration_seconds + (self.diagnosis_seconds or 0.0)


class PatchingSummary(BaseModel):
    cases: int
    diagnosis_accuracy: float | None = None
    patch_generation_rate: float
    patch_validation_rate: float
    patch_application_rate: float
    bug_fix_rate: float
    regression_free_rate: float
    verified_patch_rate: float
    first_attempt_verified_rate: float
    holdout_pass_rate: float | None = None
    holdout_cases: int = 0  # the denominator of holdout_pass_rate
    exploratory_pass_rate: float | None = None  # informational only, never part of success
    success_rate: float
    # Attempt level: share of applied patches that broke a previously passing test.
    regression_rate: float | None = None
    mean_attempts: float
    cases_with_retries: int
    retry_success_rate: float | None = None  # verified, among cases whose first attempt was not
    first_attempt_failure_rate: float = 0.0
    repaired_by_retry: int = 0
    cost_per_successful_repair: float | None = None
    cost_per_success: float | None = None
    mean_cost_usd: float | None
    total_cost_usd: float | None
    mean_latency_seconds: float
    expectation_match_rate: float | None = None
    by_category: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _required_passed(execution: PatchExecution, required: list[str]) -> bool:
    final = execution.final_attempt
    if final is None or final.verification is None:
        return False
    tests = (final.verification.after_results.get("targeted") or {}).get("tests") or {}
    for name in required:
        matches = [t for t in tests.values() if t["test_id"] == name or t["test_id"].endswith("::" + name)]
        if not matches or any(t["outcome"] != "passed" for t in matches):
            return False
    return True


def _had_regression(attempt) -> bool:
    verification = attempt.verification
    if verification is None:
        return False
    return any(c.name in ("regression_tests", "full_test_suite") and c.status == "failed" for c in verification.checks)


def _required_outcomes(attempt, required: list[str]) -> dict[str, str]:
    tests = ((attempt.verification.after_results.get("targeted") or {}).get("tests") or {}) if attempt.verification else {}
    out = {}
    for name in required:
        matches = [t for t in tests.values() if t["test_id"] == name or t["test_id"].endswith("::" + name)]
        out[name] = (matches[0]["outcome"] if len(matches) == 1 else
                     ("passed" if matches and all(t["outcome"] == "passed" for t in matches) else
                      ("failed" if matches else "not run")))
    return out


def _failure_reasons(attempt) -> list[str]:
    if attempt.verification is not None:
        return list(attempt.verification.failure_reasons)
    if attempt.validation is not None and not attempt.validation.valid:
        return list(attempt.validation.errors)
    return [attempt.summary] if attempt.summary else []


def repair_evidence(case: PatchingBenchmarkCase, execution: PatchExecution) -> dict[str, Any]:
    """What the record shows about a retry: the first failure, the feedback, and the final outcome."""
    if len(execution.attempts) < 2:
        return {}
    first, final = execution.attempts[0], execution.attempts[-1]
    first_reasons = _failure_reasons(first)
    received = final.feedback_reasons
    first_sha = first.validation.diff_sha256 if first.validation else None
    final_sha = final.validation.diff_sha256 if final.validation else None
    return {
        "first_attempt_status": str(first.status),
        "first_attempt_failed_checks": [c.name for c in (first.verification.checks if first.verification else [])
                                        if c.required and c.status != "passed"],
        "first_attempt_reasons": first_reasons[:5],
        "first_attempt_required_tests": _required_outcomes(first, case.required_tests),
        "final_attempt": final.attempt_number,
        "final_received_feedback_from": final.feedback_from,
        "feedback_delivered": 1 in final.feedback_from and any(r in received for r in first_reasons),
        "diff_changed": bool(first_sha) and bool(final_sha) and first_sha != final_sha
                        or (first_sha is None and final_sha is not None),
        "final_required_tests": _required_outcomes(final, case.required_tests),
    }


def score_execution(
    case: PatchingBenchmarkCase,
    execution: PatchExecution,
    *,
    variant: str = "live",
    expected_status: str | None = None,
    expected_attempts: int | None = None,
    expected_holdout: bool | None = None,
    holdout: dict[str, Any] | None = None,
    exploratory: dict[str, Any] | None = None,
    diagnosis: dict[str, Any] | None = None,
) -> PatchCaseScore:
    final = execution.final_attempt
    proposal = final.proposal if final else None
    validation = final.validation if final else None
    verification = final.verification if final else None
    validated = bool(validation and validation.valid)
    applied = bool(final and final.application and final.application.applied)
    files = validation.affected_files if validation else []
    regression_free = False
    failures: list[str] = []
    if verification is not None:
        checks = {c.name: c for c in verification.checks}
        regression_free = all(
            checks[name].status == "passed" for name in ("regression_tests", "full_test_suite")
            if name in checks and checks[name].required
        ) and "regression_tests" in checks
        failures = [f"{c.name}: {c.status}" for c in verification.checks if c.required and c.status != "passed"]
    elif validation is not None and not validation.valid:
        failures = ["patch_validation: failed"]
    verified = execution.status == "verified"
    bug_fixed = applied and _required_passed(execution, case.required_tests)
    only_expected = bool(files) and set(files) <= set(case.expected_files)
    holdout_passed = None if holdout is None else holdout.get("passed")
    evidence = repair_evidence(case, execution)
    first_failed = bool(execution.attempts) and str(execution.attempts[0].status) != "verified"
    repaired = (first_failed and len(execution.attempts) > 1 and verified and bug_fixed
                and bool(evidence.get("feedback_delivered")) and bool(evidence.get("diff_changed"))
                and holdout_passed is not False)
    usage = execution.usage
    diagnosis = diagnosis or {}
    matches = None
    if expected_status is not None:
        matches = (execution.status == expected_status
                   and (expected_attempts is None or len(execution.attempts) == expected_attempts)
                   and (expected_holdout is None or holdout_passed is expected_holdout))
    return PatchCaseScore(
        case_id=case.case_id,
        category=case.category,
        variant=variant,
        status=execution.status,
        expected_status=expected_status,
        matches_expectation=matches,
        generated=proposal is not None,
        validated=validated,
        applied=applied,
        bug_fixed=bug_fixed,
        regression_free=regression_free,
        verified=verified,
        success=verified and bug_fixed and only_expected and holdout_passed is not False,
        only_expected_files=only_expected,
        attempts=len(execution.attempts),
        attempt_statuses=[str(a.status) for a in execution.attempts],
        first_attempt_failed=first_failed,
        repaired_by_retry=repaired,
        repair_evidence=evidence,
        attempts_with_regressions=sum(_had_regression(a) for a in execution.attempts),
        attempts_applied=sum(bool(a.application and a.application.applied) for a in execution.attempts),
        holdout_passed=holdout_passed,
        holdout_results=(holdout or {}).get("tests", {}),
        exploratory_passed=None if exploratory is None else exploratory.get("passed"),
        exploratory_results=(exploratory or {}).get("tests", {}),
        files_modified=files,
        lines_added=validation.added_lines if validation else 0,
        lines_removed=validation.removed_lines if validation else 0,
        verification_failures=failures,
        rejected_reasons=[r for a in execution.attempts for r in
                          (a.verification.failure_reasons if a.verification else
                           (a.validation.errors[:3] if a.validation and not a.validation.valid else []))],
        duration_seconds=execution.duration_seconds,
        input_tokens=usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd(execution.model, usage) if execution.model else None,
        diagnosis_status=diagnosis.get("status"),
        diagnosis_correct=diagnosis.get("correct"),
        diagnosis_cost_usd=diagnosis.get("cost_usd"),
        diagnosis_seconds=diagnosis.get("seconds"),
        failure_reason=execution.failure_reason,
        stop_reason=execution.stop_reason,
    )


def _cost_per(scores: list[PatchCaseScore], successes: int | None = None) -> float | None:
    """Total cost (including failed attempts) divided by the number of successes."""
    costs = [s.total_cost_usd for s in scores]
    count = len(scores) if successes is None else successes
    if not scores or not count or any(c is None for c in costs):
        return None
    return round(sum(costs) / count, 6)


def score_without_patch(case: PatchingBenchmarkCase, status: str, *, variant: str = "live",
                        diagnosis: dict[str, Any] | None = None, reason: str | None = None) -> PatchCaseScore:
    """A case that never reached patch generation (diagnosis failed, not eligible). Counted, never hidden."""
    diagnosis = diagnosis or {}
    return PatchCaseScore(
        case_id=case.case_id, category=case.category, variant=variant, status=status, generated=False,
        validated=False, applied=False, bug_fixed=False, regression_free=False, verified=False, success=False,
        only_expected_files=False, attempts=0, files_modified=[], lines_added=0, lines_removed=0,
        verification_failures=[], rejected_reasons=[reason] if reason else [], duration_seconds=0.0,
        input_tokens=0, output_tokens=0, cost_usd=0.0, diagnosis_status=diagnosis.get("status"),
        diagnosis_correct=diagnosis.get("correct"), diagnosis_cost_usd=diagnosis.get("cost_usd"),
        diagnosis_seconds=diagnosis.get("seconds"), failure_reason=status,
    )


def summarize(scores: list[PatchCaseScore]) -> PatchingSummary:
    if not scores:
        raise ValueError("no scores to summarise")
    costs = [s.total_cost_usd for s in scores]
    known = [c for c in costs if c is not None]
    expectations = [s.matches_expectation for s in scores if s.matches_expectation is not None]
    diagnoses = [s.diagnosis_correct for s in scores if s.diagnosis_correct is not None]
    holdouts = [s.holdout_passed for s in scores if s.holdout_passed is not None]
    exploratory = [s.exploratory_passed for s in scores if s.exploratory_passed is not None]
    applied = sum(s.attempts_applied for s in scores)
    first_failed = [s for s in scores if s.attempt_statuses and s.attempt_statuses[0] != "verified"
                    and len(s.attempt_statuses) > 1]

    def rate(values) -> float:
        values = list(values)
        return round(mean(bool(v) for v in values), 4) if values else 0.0

    categories: dict[str, list[PatchCaseScore]] = {}
    for score in scores:
        categories.setdefault(score.category, []).append(score)

    return PatchingSummary(
        cases=len(scores),
        diagnosis_accuracy=rate(diagnoses) if diagnoses else None,
        patch_generation_rate=rate(s.generated for s in scores),
        patch_validation_rate=rate(s.validated for s in scores),
        patch_application_rate=rate(s.applied for s in scores),
        bug_fix_rate=rate(s.bug_fixed for s in scores),
        regression_free_rate=rate(s.regression_free for s in scores),
        verified_patch_rate=rate(s.verified for s in scores),
        first_attempt_verified_rate=rate(bool(s.attempt_statuses) and s.attempt_statuses[0] == "verified"
                                         for s in scores),
        holdout_pass_rate=rate(holdouts) if holdouts else None,
        holdout_cases=len(holdouts),
        exploratory_pass_rate=rate(exploratory) if exploratory else None,
        success_rate=rate(s.success for s in scores),
        regression_rate=round(sum(s.attempts_with_regressions for s in scores) / applied, 4) if applied else None,
        mean_attempts=round(mean(s.attempts for s in scores), 2),
        cases_with_retries=sum(1 for s in scores if s.attempts > 1),
        retry_success_rate=rate(s.verified for s in first_failed) if first_failed else None,
        first_attempt_failure_rate=rate(s.first_attempt_failed for s in scores if s.attempts),
        repaired_by_retry=sum(s.repaired_by_retry for s in scores),
        cost_per_successful_repair=_cost_per([s for s in scores if s.repaired_by_retry]),
        cost_per_success=_cost_per(scores, successes=sum(s.success for s in scores)),
        mean_cost_usd=round(mean(known), 6) if known and len(known) == len(costs) else None,
        total_cost_usd=round(sum(known), 6) if known and len(known) == len(costs) else None,
        mean_latency_seconds=round(mean(s.total_seconds for s in scores), 3),
        expectation_match_rate=rate(expectations) if expectations else None,
        by_category={
            name: {"cases": len(items), "verified_rate": rate(s.verified for s in items),
                   "success_rate": rate(s.success for s in items),
                   "mean_attempts": round(mean(s.attempts for s in items), 2)}
            for name, items in sorted(categories.items())
        },
    )


# --- running cases ---------------------------------------------------------------------


def benchmark_investigation(case: PatchingBenchmarkCase, entry: dict, investigation_id: str | None = None):
    """A ``LoadedInvestigation`` from a recorded diagnosis (``benchmarks/patching/diagnoses.json``)."""
    import uuid

    from app.agents.schemas import RootCauseAnalysis
    from app.patching.service import LoadedInvestigation

    iid = investigation_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas-ai/patching/{case.case_id}"))
    analysis = RootCauseAnalysis(investigation_id=iid, repository_id=case.repository_id, status="completed",
                                 **entry["diagnosis"])
    return LoadedInvestigation(iid, case.repository_id, "completed", entry["bug_report"], analysis)


def run_case(case, root, llm, investigation, *, workspaces, limits, trusted_roots, max_attempts=None,
             full_suite=True, llm_budget=None, engine=None) -> PatchExecution:
    """One generate-and-verify execution, in memory (no database)."""
    from app.agents.agent import StaticRepositoryResolver
    from app.patching.service import InMemoryInvestigationSource, PatchingService

    service = PatchingService(
        llm,
        InMemoryInvestigationSource({investigation.investigation_id: investigation}),
        StaticRepositoryResolver({case.repository_id: root}, allowed_roots=trusted_roots),
        workspaces=workspaces,
        limits=limits,
        trusted_test_roots=trusted_roots,
        llm_budget=llm_budget,
        engine=engine,
    )
    return service.generate_and_verify_patch(investigation.investigation_id, max_attempts=max_attempts,
                                             run_full_test_suite=full_suite)


def reference_llm(diffs: str | list[str]):
    """A FakeLLM that proposes exactly these diffs, one per attempt: the verifier is under test, not a model."""
    from app.agents.llm import FakeLLM, tool_call

    diffs = [diffs] if isinstance(diffs, str) else diffs
    return FakeLLM([tool_call("submit_patch", {
        "description": "Reference patch", "rationale": "Benchmark reference", "unified_diff": diff,
        "affected_files": [], "assumptions": [], "risks": [],
    }) for diff in diffs])


def run_holdout(case: PatchingBenchmarkCase, root: Path, execution: PatchExecution, benchmark_dir: Path, *,
                workspaces, limits: PatchLimits | None = None, runner=None, timeout: float = 60.0
                ) -> dict[str, Any] | None:
    """Held-out tests (scored) against the final patch. None when not applicable."""
    return _run_extra_tests(case.holdout_tests, "test_holdout_", root, execution, benchmark_dir,
                            workspaces=workspaces, limits=limits, runner=runner, timeout=timeout)


def run_exploratory(case: PatchingBenchmarkCase, root: Path, execution: PatchExecution, benchmark_dir: Path, *,
                    workspaces, limits: PatchLimits | None = None, runner=None, timeout: float = 60.0
                    ) -> dict[str, Any] | None:
    """Exploratory tests (informational, never scored) against the final patch."""
    return _run_extra_tests(case.exploratory_tests, "test_exploratory_", root, execution, benchmark_dir,
                            workspaces=workspaces, limits=limits, runner=runner, timeout=timeout)


def _run_extra_tests(test_files: list[str], prefix: str, root: Path, execution: PatchExecution,
                     benchmark_dir: Path, *, workspaces, limits: PatchLimits | None = None, runner=None,
                     timeout: float = 60.0) -> dict[str, Any] | None:
    """Apply the final patch to a fresh copy, add ``test_files``, run them."""
    from app.patching.applier import apply_patch
    from app.patching.test_execution import PytestRunner
    from app.patching.validator import PatchScope, validate_patch

    final = execution.final_attempt
    if not test_files or final is None or final.proposal is None:
        return None
    if final.validation is None or not final.validation.valid:
        return None
    limits = limits or PatchLimits()
    workspace = workspaces.create(root, execution_id=execution.execution_id, label=prefix.strip("_"))
    try:
        scope = PatchScope(workspace.repo, frozenset(final.validation.affected_files))
        _, validated = validate_patch(final.proposal.unified_diff, scope, limits)
        if validated is None or not apply_patch(workspace, validated).applied:
            return {"passed": None, "tests": {}, "error": "the final patch could not be re-applied"}
        names = []
        for relative in test_files:
            name = PurePosixPath(relative).name
            if not name.startswith(prefix) or (workspace.repo / name).exists():
                raise ValueError(f"extra test {relative} must be named {prefix}*.py and not collide")
            shutil.copyfile(benchmark_dir / relative, workspace.repo / name)
            names.append(name)
        run = (runner or PytestRunner(limits)).run(workspace, names, label=prefix.strip("_"), kind="targeted",
                                                   timeout=timeout)
        tests = {k: v.outcome.value for k, v in run.tests.items()}
        passed = run.status == "completed" and bool(tests) and all(v == "passed" for v in tests.values())
        return {"passed": passed, "tests": tests, "error": run.error}
    finally:
        workspaces.cleanup(workspace)
