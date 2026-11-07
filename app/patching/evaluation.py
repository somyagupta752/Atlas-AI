"""Score patch executions against benchmark cases.

Expected answers (expected files, required tests, reference patches) live only
in ``benchmarks/patching/cases.json`` and here. The model receives the
diagnosis and the source, never these.

A green test suite is not the success metric: a patch that deletes or weakens
a failing test can make it green. Success is ``verified`` *and* the case's own
required tests passing in the patched run *and* only expected files modified.

Per case (final attempt):

- generated / validated / applied: the lifecycle stages reached;
- bug_fixed: every ``required_tests`` entry passed in the patched targeted run;
- regression_free: no previously passing test failed (targeted and full suite);
- verified: the engine's verdict;
- success: verified, bug fixed, and only expected files modified.
"""

from statistics import mean

from pydantic import BaseModel, Field

from app.agents.evaluation import cost_usd
from app.patching.models import PatchExecution


class ReferencePatch(BaseModel):
    """A fixed patch with a known verdict. Checks the verifier itself, with no model involved."""

    name: str
    file: str
    expected_status: str
    description: str = ""


class PatchingBenchmarkCase(BaseModel):
    case_id: str
    repository_id: str
    investigation_case: str
    repository_path: str
    expected_files: list[str]
    expected_behaviour: str
    required_tests: list[str] = Field(min_length=1)
    reference_patches: list[ReferencePatch] = Field(default_factory=list)


class PatchCaseScore(BaseModel):
    case_id: str
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
    files_modified: list[str]
    lines_added: int
    lines_removed: int
    verification_failures: list[str]
    rejected_reasons: list[str]
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    failure_reason: str | None = None


class PatchingSummary(BaseModel):
    cases: int
    patch_generation_rate: float
    patch_validation_rate: float
    patch_application_rate: float
    bug_fix_rate: float
    regression_free_rate: float
    verified_patch_rate: float
    success_rate: float
    mean_attempts: float
    mean_cost_usd: float | None
    total_cost_usd: float | None
    mean_latency_seconds: float
    expectation_match_rate: float | None = None


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


def score_execution(
    case: PatchingBenchmarkCase,
    execution: PatchExecution,
    *,
    variant: str = "live",
    expected_status: str | None = None,
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
    usage = execution.usage
    return PatchCaseScore(
        case_id=case.case_id,
        variant=variant,
        status=execution.status,
        expected_status=expected_status,
        matches_expectation=None if expected_status is None else execution.status == expected_status,
        generated=proposal is not None,
        validated=validated,
        applied=applied,
        bug_fixed=bug_fixed,
        regression_free=regression_free,
        verified=verified,
        success=verified and bug_fixed and only_expected,
        only_expected_files=only_expected,
        attempts=len(execution.attempts),
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
        failure_reason=execution.failure_reason,
    )


def summarize(scores: list[PatchCaseScore]) -> PatchingSummary:
    if not scores:
        raise ValueError("no scores to summarise")
    costs = [s.cost_usd for s in scores]
    known = [c for c in costs if c is not None]
    expectations = [s.matches_expectation for s in scores if s.matches_expectation is not None]

    def rate(values) -> float:
        return round(mean(bool(v) for v in values), 4)

    return PatchingSummary(
        cases=len(scores),
        patch_generation_rate=rate(s.generated for s in scores),
        patch_validation_rate=rate(s.validated for s in scores),
        patch_application_rate=rate(s.applied for s in scores),
        bug_fix_rate=rate(s.bug_fixed for s in scores),
        regression_free_rate=rate(s.regression_free for s in scores),
        verified_patch_rate=rate(s.verified for s in scores),
        success_rate=rate(s.success for s in scores),
        mean_attempts=round(mean(s.attempts for s in scores), 2),
        mean_cost_usd=round(mean(known), 6) if known and len(known) == len(costs) else None,
        total_cost_usd=round(sum(known), 6) if known and len(known) == len(costs) else None,
        mean_latency_seconds=round(mean(s.duration_seconds for s in scores), 3),
        expectation_match_rate=rate(expectations) if expectations else None,
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
             full_suite=True) -> PatchExecution:
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
    )
    return service.generate_and_verify_patch(investigation.investigation_id, max_attempts=max_attempts,
                                             run_full_test_suite=full_suite)


def reference_llm(diff: str):
    """A FakeLLM that proposes exactly ``diff``: the verifier is under test, not a model."""
    from app.agents.llm import FakeLLM, tool_call

    return FakeLLM([tool_call("submit_patch", {
        "description": "Reference patch", "rationale": "Benchmark reference", "unified_diff": diff,
        "affected_files": [], "assumptions": [], "risks": [],
    })])
