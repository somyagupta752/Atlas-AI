"""The patching service: ``generate_and_verify_patch(investigation_id)``.

It orchestrates; the work happens in the other modules:

    load investigation -> check eligibility and access -> baseline workspace
    -> select tests -> baseline run -> [generate -> validate -> fresh workspace
    -> apply -> verify -> persist] x up to max_attempts -> final report

Invariants:

- the original repository is only read (copied and hashed), never written; its
  hash is re-checked after every attempt;
- every attempt gets a fresh copy. A failed attempt's workspace is discarded,
  never reused or patched again;
- every attempt is persisted as it happens and never overwritten;
- retries stop at ``max_attempts``, on a verified, inconclusive or failed
  result, or when the verification time budget is spent. Only rejected and
  validation-failed attempts are retried, with their failure evidence;
- a patch identical to an earlier attempt is rejected without re-running tests.

No HTTP here: the same service runs from FastAPI, the benchmark or a worker.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.agents.agent import (
    RepositoryAccessDeniedError,
    RepositoryResolver,
    within_roots,
)
from app.agents.repository import InvestigationNotFoundError, InvestigationRepository
from app.agents.schemas import RootCauseAnalysis
from app.patching.applier import ApplierError, apply_patch
from app.patching.budget import LLMBudget, LLMPricingUnknownError, model_prices
from app.patching.generator import (
    PROMPT_VERSION,
    AttemptFeedback,
    PatchGenerationError,
    PatchGenerator,
    RepositoryContext,
)
from app.patching.graph_tests import GraphDiscovery, discover_tests
from app.patching.models import (
    ExecutionStatus,
    PatchApplyResult,
    PatchAttempt,
    PatchExecution,
    PatchLimits,
    PatchStatus,
    PatchVerificationResult,
    VerificationCheck,
    check_patch_transition,
)
from app.patching.repository import PatchPersistenceError, PatchRepository
from app.patching.test_selector import select_tests
from app.patching.validator import PatchScope, derive_allowed_files, validate_patch
from app.patching.verification import Baseline, VerificationEngine
from app.patching.workspace import (
    Workspace,
    WorkspaceError,
    WorkspaceManager,
    snapshot_tree,
)
from app.tools.base import truncate_text

logger = logging.getLogger(__name__)

MIN_ATTEMPT_SECONDS = 5.0


class PatchRequestError(ValueError):
    """The request itself is invalid (for example, too many attempts)."""


class TestBackendUnavailableError(RuntimeError):
    """The configured test runner (for example the container image) cannot run."""

    __test__ = False


class InvestigationNotEligibleError(Exception):
    """The investigation cannot be patched (not completed, no root cause, no modifiable files)."""


@dataclass
class LoadedInvestigation:
    investigation_id: str
    repository_id: str
    status: str
    bug_report: str
    analysis: RootCauseAnalysis | None


class InvestigationSource(Protocol):
    def load(self, investigation_id: str) -> LoadedInvestigation:
        """Raise ``InvestigationNotFoundError`` when it does not exist."""
        ...


class DatabaseInvestigationSource:
    def __init__(self, repository: InvestigationRepository) -> None:
        self._repository = repository

    def load(self, investigation_id: str) -> LoadedInvestigation:
        record = self._repository.get(investigation_id)
        analysis = None
        if record.result and record.status == "completed":
            try:
                analysis = RootCauseAnalysis.model_validate(record.result)
            except ValueError:
                analysis = None
        return LoadedInvestigation(str(record.id), record.repository_id, record.status, record.bug_report, analysis)


class InMemoryInvestigationSource:
    """For the benchmark and tests: ``{investigation_id: LoadedInvestigation}``."""

    def __init__(self, investigations: dict[str, LoadedInvestigation]) -> None:
        self._investigations = investigations

    def load(self, investigation_id: str) -> LoadedInvestigation:
        if investigation_id not in self._investigations:
            raise InvestigationNotFoundError(investigation_id)
        return self._investigations[investigation_id]


class _Budget:
    """Verification time: baseline and checks. LLM latency is bounded separately."""

    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self.seconds = seconds
        self.spent = 0.0
        self._clock = clock

    def deadline(self) -> float:
        return self._clock() + max(0.0, self.seconds - self.spent)

    def remaining(self) -> float:
        return self.seconds - self.spent

    def track(self, started: float) -> None:
        self.spent += self._clock() - started


class PatchingService:
    def __init__(
        self,
        llm: Any,
        investigations: InvestigationSource,
        resolver: RepositoryResolver,
        *,
        workspaces: WorkspaceManager,
        patches: PatchRepository | None = None,
        limits: PatchLimits | None = None,
        engine: VerificationEngine | None = None,
        trusted_test_roots: list[Path] | None = None,
        clock: Callable[[], float] = time.monotonic,
        llm_budget: LLMBudget | None = None,
        graph: Any = None,
        allow_untrusted_repositories: bool = False,
        on_attempt: Callable[[PatchExecution, PatchAttempt], None] | None = None,
    ) -> None:
        self._llm = llm
        # Called right after each attempt is recorded, so a caller can persist it immediately.
        self._on_attempt = on_attempt
        # Optional shared parent: every execution's budget must also fit in it (e.g. one benchmark run).
        self._shared_budget = llm_budget
        # Task 10 graph store (Neo4j), optional. Only ever suggests tests; see graph_tests.py.
        self._graph = graph
        # Honoured only when the test backend is an isolation boundary (the container runner).
        self._allow_untrusted = allow_untrusted_repositories
        self._investigations = investigations
        self._resolver = resolver
        self._workspaces = workspaces
        self._patches = patches
        self._limits = limits or PatchLimits()
        self._engine = engine or VerificationEngine(self._limits)
        self._trusted = trusted_test_roots or []
        self._clock = clock
        self._generator = PatchGenerator(
            llm,
            timeout_seconds=self._limits.llm_timeout_seconds,
            max_source_bytes=self._limits.max_context_source_bytes,
            max_feedback_bytes=self._limits.max_feedback_bytes,
            max_output_tokens=self._limits.llm_max_output_tokens,
        )

    # --- entry point -----------------------------------------------------------------

    def generate_and_verify_patch(
        self,
        investigation_id: str,
        *,
        repository_id: str | None = None,
        max_attempts: int | None = None,
        run_full_test_suite: bool = True,
    ) -> PatchExecution:
        """Raises before any work for: not found, not eligible, access denied, bad request.
        Every later problem is part of the returned execution."""
        investigation = self._investigations.load(investigation_id)
        if repository_id is not None and investigation.repository_id != repository_id:
            raise InvestigationNotFoundError(investigation_id)  # never reveal other repositories
        analysis = self._eligible(investigation)
        attempts_allowed = self._limits.max_attempts if max_attempts is None else max_attempts
        if not 1 <= attempts_allowed <= self._limits.max_attempts:
            raise PatchRequestError(f"max_attempts must be between 1 and {self._limits.max_attempts}")
        if self._limits.max_llm_cost_usd is not None and model_prices(self._llm) is None:
            raise LLMPricingUnknownError(
                f"A cost cap is set, but the price of model {getattr(self._llm, 'model', None)!r} is unknown"
            )
        root = self._resolver.resolve(investigation.repository_id)
        runner = self._engine.runner
        isolated = bool(getattr(runner, "isolated", False))
        if not within_roots(root, self._trusted) and not (isolated and self._allow_untrusted):
            raise RepositoryAccessDeniedError(
                "Patch verification runs the repository's tests, and this repository is not a trusted test root"
            )
        if not runner.available():
            # Never fall back to running the tests some other, less isolated way.
            raise TestBackendUnavailableError("The configured test execution backend is not available")
        candidates = [*analysis.affected_files] + [
            e.file_path for e in analysis.evidence if e.file_path and e.source_type == "source_code"
        ]
        allowed, excluded = derive_allowed_files(root, candidates, self._limits)
        if not allowed:
            raise InvestigationNotEligibleError(
                "The diagnosis names no modifiable source file" + (f" ({'; '.join(excluded[:3])})" if excluded else "")
            )

        execution = PatchExecution(
            investigation_id=investigation.investigation_id,
            repository_id=investigation.repository_id,
            status="inconclusive",
            summary="",
        )
        llm_budget = LLMBudget.from_limits(self._limits, parent=self._shared_budget)
        started = self._clock()
        try:
            self._run(execution, investigation, analysis, root, allowed, attempts_allowed, run_full_test_suite,
                      llm_budget)
        except PatchPersistenceError:
            logger.exception("Patch execution %s could not be persisted", execution.execution_id)
            self._finish(execution, "failed", "Patch data could not be saved.", "persistence_error")
        except Exception:
            logger.exception("Patch execution %s failed", execution.execution_id)
            self._finish(execution, "failed", "Patch generation failed because of an internal error.", "internal_error")
        execution.duration_seconds = round(self._clock() - started, 3)
        execution.llm_budget = llm_budget.report()
        try:
            execution.original_repository_modified = (
                execution.source_snapshot is not None
                and snapshot_tree(root).root_hash != execution.source_snapshot.root_hash
            )
        except Exception:
            logger.exception("Could not re-check the original repository")
        if execution.original_repository_modified:
            logger.error("Original repository %s changed during execution %s", root, execution.execution_id)
        return execution

    # --- workflow --------------------------------------------------------------------

    def _run(self, execution, investigation, analysis, root, allowed, max_attempts, full_suite, llm_budget) -> None:
        budget = _Budget(self._limits.max_total_seconds, self._clock)
        try:
            baseline_ws = self._workspaces.create(root, execution_id=execution.execution_id, label="baseline")
        except WorkspaceError as exc:
            self._finish(execution, "failed", f"The baseline workspace could not be created: {exc}", "workspace_error")
            return
        success = False
        try:
            execution.source_snapshot = baseline_ws.snapshot
            symbols = [e.qualified_name for e in analysis.evidence if e.qualified_name]
            suggestions, graph_report = self._graph_suggestions(baseline_ws, investigation, allowed, symbols)
            selection = select_tests(
                baseline_ws.repo, analysis, affected_files=allowed, affected_symbols=symbols,
                max_test_files=self._limits.max_selected_test_files, include_full_suite=full_suite,
                suggested=suggestions,
            )
            selection.graph = graph_report.model_dump(mode="json")
            execution.test_selection = selection
            if not selection.items:
                self._finish(execution, "inconclusive", "No relevant tests were found, so a fix cannot be verified.",
                             "no_relevant_tests", stop_reason="no_relevant_tests")
                return
            started = self._clock()
            baseline = self._engine.capture_baseline(baseline_ws, selection, budget.deadline())
            budget.track(started)
            execution.baseline = {
                "reproducible": baseline.reproducible,
                "bug_tests": baseline.bug_tests,
                "flaky": sorted(baseline.flaky),
                "problems": baseline.problems,
                "tests": len(baseline.outcomes),
            }
            if not baseline.reproducible and not self._limits.generate_when_baseline_unreproducible:
                self._finish(
                    execution, "inconclusive",
                    "The original bug could not be reproduced in the baseline, so no fix can be verified: "
                    + "; ".join(baseline.problems[:3]),
                    "baseline_not_reproducible", stop_reason="baseline_not_reproducible",
                )
                return
            context = self._context(investigation, analysis, baseline_ws, allowed, selection, baseline)
            scope = PatchScope(root=baseline_ws.repo, allowed_files=frozenset(allowed))
            unchanged = self._original_check(root, baseline_ws.snapshot.root_hash)

            history: list[AttemptFeedback] = []
            hashes: dict[str, int] = {}
            stop_reason = "max_attempts"
            budget_limit = None
            for number in range(1, max_attempts + 1):
                if budget.remaining() < MIN_ATTEMPT_SECONDS:
                    stop_reason = "verification_budget_exhausted"
                    break
                attempt_started = self._clock()
                try:
                    attempt = self._attempt(
                        number, execution, analysis, context, history, scope, root, baseline_ws, baseline,
                        selection, budget, hashes, unchanged, llm_budget,
                    )
                except PatchGenerationError as exc:
                    execution.usage = execution.usage + exc.usage
                    if exc.kind == "budget_exhausted":
                        # Not a model failure: Atlas declined to make the next call.
                        stop_reason, budget_limit = "llm_budget_exhausted", exc.budget_limit
                        break
                    self._finish(execution, "failed", str(exc), f"llm_{exc.kind}", stop_reason="generation_failed")
                    return
                attempt.duration_seconds = round(self._clock() - attempt_started, 3)
                execution.attempts.append(attempt)
                if attempt.proposal is not None:
                    execution.usage = execution.usage + attempt.proposal.usage
                    execution.model = attempt.proposal.model
                if self._on_attempt is not None:
                    self._on_attempt(execution, attempt)
                if attempt.status in ("verified", "inconclusive", "failed"):
                    stop_reason = str(attempt.status)
                    break
                history.append(_feedback(attempt, self._limits.max_feedback_bytes))

            final = execution.final_attempt
            if final is None and stop_reason == "llm_budget_exhausted":
                self._finish(execution, "inconclusive",
                             f"The LLM budget ({budget_limit}) did not allow a single model call, so no patch was "
                             "generated.", "llm_budget_exhausted", stop_reason=stop_reason)
                return
            if final is None:
                self._finish(execution, "inconclusive", "The verification time budget ran out before any attempt.",
                             "verification_budget_exhausted", stop_reason=stop_reason)
                return
            execution.final_patch_id = final.proposal.patch_id if final.proposal else None
            success = final.status == "verified"
            status: ExecutionStatus = final.status  # type: ignore[assignment]
            count = len(execution.attempts)
            prefix = f"Attempt {final.attempt_number} of {count} attempt(s): "
            if stop_reason == "max_attempts" and not success:
                prefix = f"Stopped after the maximum of {count} attempt(s). Final attempt: "
            elif stop_reason == "llm_budget_exhausted":
                prefix = f"Stopped: the LLM budget ({budget_limit}) was exhausted. Final attempt: "
            elif stop_reason == "verification_budget_exhausted":
                prefix = "Stopped: the verification time budget ran out. Final attempt: "
            self._finish(execution, status, prefix + final.summary, final.failure_reason, stop_reason=stop_reason)
        finally:
            self._workspaces.release(baseline_ws, success=success)

    def _attempt(self, number, execution, analysis, context, history, scope, root, baseline_ws, baseline,
                 selection, budget, hashes, unchanged, llm_budget) -> PatchAttempt:
        generated = self._generator.generate_patch(
            analysis, context, execution_id=execution.execution_id, attempt_number=number, history=history,
            budget=llm_budget,
        )
        proposal = generated.proposal
        attempt = PatchAttempt(attempt_number=number, status="generated", proposal=proposal,
                               feedback_from=[h.attempt_number for h in history],
                               feedback_reasons=[r for h in history for r in h.reasons][:20])
        provenance = {**self._provenance(generated, baseline_ws), "llm_budget": llm_budget.report(),
                      "feedback_from": attempt.feedback_from, "feedback_reasons": attempt.feedback_reasons}
        self._save(lambda p: p.create_proposal(proposal, provenance))
        try:
            return self._validate_apply_verify(attempt, scope, root, baseline_ws, baseline, selection, budget,
                                               hashes, unchanged)
        except Exception:
            proposal.status = PatchStatus.FAILED
            if self._patches is not None:
                self._patches.mark_failed(proposal.patch_id, "internal error")
            raise

    def _validate_apply_verify(self, attempt, scope, root, baseline_ws, baseline, selection, budget, hashes,
                               unchanged) -> PatchAttempt:
        proposal = attempt.proposal
        validation, validated = validate_patch(
            proposal.unified_diff, scope, self._limits, claimed_files=proposal.affected_files
        )
        attempt.validation = validation
        if validated is None:
            self._move(attempt, PatchStatus.VALIDATION_FAILED, validation=validation)
            attempt.failure_reason = "validation_failed"
            attempt.summary = "The patch failed validation: " + "; ".join(validation.errors[:3])
            return attempt
        proposal.affected_files = validation.affected_files
        self._move(attempt, PatchStatus.VALIDATED, validation=validation, affected_files=validation.affected_files)

        if validated.sha256 in hashes:
            earlier = hashes[validated.sha256]
            reason = f"identical to attempt {earlier}, which already failed"
            result = PatchVerificationResult(
                patch_id=proposal.patch_id, status="rejected",
                checks=[VerificationCheck(name="duplicate_patch", status="failed", reason=reason)],
                summary=f"Rejected: the patch is {reason}.", failure_reasons=[f"duplicate_patch: {reason}"],
            )
            return self._conclude(attempt, result, PatchStatus.REJECTED, "duplicate_patch")
        hashes[validated.sha256] = attempt.attempt_number

        try:
            workspace = self._workspaces.create(root, execution_id=proposal.execution_id,
                                                label=f"patched-{attempt.attempt_number}")
        except WorkspaceError as exc:
            self._move(attempt, PatchStatus.FAILED, note=str(exc))
            attempt.failure_reason = "workspace_error"
            attempt.summary = f"The isolated workspace could not be created: {exc}"
            return attempt
        try:
            attempt.workspace = workspace.describe()
            if workspace.snapshot.root_hash != baseline_ws.snapshot.root_hash:
                result = PatchVerificationResult(
                    patch_id=proposal.patch_id, status="inconclusive",
                    checks=[VerificationCheck(name="original_repository_unchanged", status="skipped",
                                              reason="the source changed after the baseline was captured")],
                    summary="Inconclusive: the repository changed after the baseline was captured.",
                    failure_reasons=["the source changed after the baseline was captured"],
                )
                return self._conclude(attempt, result, PatchStatus.INCONCLUSIVE, "source_changed")
            started = self._clock()
            try:
                application = apply_patch(workspace, validated, timeout=30.0)
            except ApplierError as exc:
                application = PatchApplyResult(applied=False, error_kind="environment", errors=[str(exc)])
            attempt.application = application
            if application.applied:
                self._move(attempt, PatchStatus.APPLIED)
                self._move(attempt, PatchStatus.VERIFYING)
            result = self._engine.verify(
                patch_id=proposal.patch_id, patch=validated, application=application, workspace=workspace,
                baseline_workspace=baseline_ws, baseline=baseline, selection=selection,
                deadline=budget.deadline(), original_unchanged=unchanged,
            )
            budget.track(started)
            return self._conclude(attempt, result, PatchStatus(result.status), None)
        finally:
            retained = self._workspaces.release(workspace, success=attempt.status == "verified")
            attempt.workspace = workspace.describe(retained)

    # --- helpers ---------------------------------------------------------------------

    def _eligible(self, investigation: LoadedInvestigation) -> RootCauseAnalysis:
        analysis = investigation.analysis
        if investigation.status != "completed" or analysis is None or analysis.status != "completed":
            raise InvestigationNotEligibleError(
                f"Only completed investigations can be patched (this one is {investigation.status})"
            )
        if not analysis.root_cause:
            raise InvestigationNotEligibleError("The investigation has no root cause")
        if analysis.repository_id != investigation.repository_id:
            raise InvestigationNotEligibleError("The stored diagnosis does not match its investigation")
        return analysis

    def _graph_suggestions(self, baseline_ws: Workspace, investigation, allowed, symbols):
        """Graph test suggestions from the baseline copy. Any failure is a warning, never a blocker."""
        if not self._limits.graph_test_discovery:
            return [], GraphDiscovery(source="disabled")
        try:
            return discover_tests(
                baseline_ws.repo, investigation.repository_id, allowed, symbols, graph=self._graph,
                max_depth=self._limits.graph_max_depth, max_suggestions=self._limits.max_graph_test_files,
                allow_in_memory=self._limits.graph_in_memory_fallback,
            )
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            logger.warning("Graph test discovery failed: %s", type(exc).__name__)
            return [], GraphDiscovery(source="unavailable",
                                      warnings=[f"Graph test discovery failed ({type(exc).__name__})"])

    def _context(self, investigation, analysis, baseline_ws: Workspace, allowed, selection, baseline: Baseline):
        def read(path: str) -> str:
            return (baseline_ws.repo / path).read_text(encoding="utf-8", errors="replace")

        shown = selection.items[: self._limits.max_context_test_files]
        tests = {item.target: read(item.target) for item in shown}
        return RepositoryContext(
            repository_id=investigation.repository_id,
            bug_report=investigation.bug_report,
            modifiable_files={path: read(path) for path in allowed},
            test_files=tests,
            withheld_test_files=len(selection.items) - len(shown),
            failing_tests=baseline.bug_test_details(),
        )

    @staticmethod
    def _original_check(root: Path, expected: str) -> Callable[[], bool]:
        return lambda: snapshot_tree(root).root_hash == expected

    def _provenance(self, generated, baseline_ws: Workspace) -> dict[str, Any]:
        proposal = generated.proposal
        return {
            "provider": proposal.provider,
            "model": proposal.model,
            "prompt_version": PROMPT_VERSION,
            "usage": proposal.usage.model_dump(),
            "llm_calls": generated.llm_calls,
            "source_snapshot_hash": baseline_ws.snapshot.root_hash,
            "source_revision": baseline_ws.snapshot.revision,
            "claimed_files": generated.draft.affected_files,
            "limits": self._limits.model_dump(),
            "context_test_files": self._limits.max_context_test_files,
        }

    def _save(self, action: Callable[[PatchRepository], Any]) -> None:
        if self._patches is not None:
            action(self._patches)

    def _move(self, attempt: PatchAttempt, status: PatchStatus, **kwargs) -> None:
        check_patch_transition(attempt.proposal.status, status)
        self._save(lambda p: p.transition(attempt.proposal.patch_id, status, **kwargs))
        attempt.proposal.status = status
        attempt.status = str(status)

    def _conclude(self, attempt, result: PatchVerificationResult, status: PatchStatus, reason: str | None):
        check_patch_transition(attempt.proposal.status, status)
        self._save(lambda p: p.record_verification(result, status))
        attempt.proposal.status = status
        attempt.status = str(status)
        attempt.verification = result
        attempt.failure_reason = reason or (None if status == PatchStatus.VERIFIED else str(status))
        attempt.summary = result.summary
        return attempt

    @staticmethod
    def _finish(execution: PatchExecution, status, summary: str, failure_reason: str | None,
                stop_reason: str | None = None) -> None:
        execution.status = status
        execution.summary = summary
        execution.failure_reason = None if status == "verified" else failure_reason
        execution.stop_reason = stop_reason or execution.stop_reason


def _feedback(attempt: PatchAttempt, max_bytes: int) -> AttemptFeedback:
    """Only the evidence that explains the failure, size-limited and already redacted."""
    proposal = attempt.proposal
    if attempt.validation is not None and not attempt.validation.valid:
        return AttemptFeedback(attempt.attempt_number, attempt.status, proposal.unified_diff,
                               attempt.validation.errors[:10])
    verification = attempt.verification
    reasons = verification.failure_reasons if verification else [attempt.summary]
    details: list[str] = []
    if verification is not None:
        for check in verification.checks:
            if check.status in ("failed", "timed_out"):
                output = (check.stdout or "") + ("\n" + check.stderr if check.stderr else "")
                details.append(f"[{check.name}] {check.reason}\n" + truncate_text(output, 2_500, keep="tail")[0])
    if attempt.application is not None and not attempt.application.applied and attempt.application.stderr:
        details.append("[git apply] " + attempt.application.stderr[:1_500])
    text = truncate_text("\n".join(details), max_bytes // 2, keep="head")[0]
    return AttemptFeedback(attempt.attempt_number, attempt.status, proposal.unified_diff, reasons[:8], text)
