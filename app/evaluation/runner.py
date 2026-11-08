"""Benchmark execution (Task 14.3): orchestration only.

The runner calls the existing services and records what they return:

* diagnosis: ``DebuggingAgent`` (Task 12) or the recorded diagnosis from ``diagnoses.json``;
* patching: ``PatchingService`` (Task 13) with the configured test backend;
* held-out and exploratory tests: ``run_holdout`` / ``run_exploratory``;
* money: one ``LLMBudget`` for the whole run, shared by diagnosis, patch and repair calls.

It owns no agent loop. Each case is saved on its own, each attempt as it finishes, so an
interrupted run keeps everything that completed. Execution produces evidence; scores are
calculated later by ``metrics`` from the stored rows.
"""

import logging
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.agents.agent import (
    DebuggingAgent,
    SessionScopedRetrieval,
    StaticRepositoryResolver,
)
from app.agents.evaluation import cost_usd, root_cause_matches
from app.agents.llm import (
    LLMConfigurationError,
    LLMError,
    LLMProvider,
    LLMResponse,
    create_llm_provider,
)
from app.agents.repository import InvestigationRepository
from app.agents.schemas import AgentLimits
from app.agents.tool_registry import create_default_registry
from app.config import Settings
from app.core import metrics
from app.core.logging import log_event
from app.core.observability import workflow_scope
from app.evaluation.benchmarks import BENCHMARKS_DIR, PATCHING_DIR, BenchmarkSuite
from app.evaluation.configuration import prompts_changed
from app.evaluation.environment import git_info, runtime_versions
from app.evaluation.models import (
    EvaluationAttempt,
    EvaluationCaseResult,
    EvaluationRun,
    RunStatus,
)
from app.evaluation.repository import RunRepository
from app.patching.budget import (
    LLMBudget,
    LLMBudgetExhaustedError,
    LLMPricingUnknownError,
    estimate_input_tokens,
    model_prices,
    usage_cost,
    worst_case_cost,
)
from app.patching.evaluation import (
    PatchCaseScore,
    PatchingBenchmarkCase,
    benchmark_investigation,
    had_regression,
    reference_llm,
    required_outcomes,
    run_case,
    run_exploratory,
    run_holdout,
    score_execution,
    score_without_patch,
)
from app.patching.models import PatchAttempt, PatchExecution, PatchLimits
from app.patching.service import (
    InvestigationNotEligibleError,
    LoadedInvestigation,
    TestBackendUnavailableError,
)
from app.patching.workspace import WorkspaceManager, snapshot_tree
from app.retrieval.models import RetrievalSource

logger = logging.getLogger(__name__)

NON_DETERMINISM_NOTE = (
    "Model output is not deterministic. This record fixes the experimental conditions (configuration, "
    "prompts, benchmark version, environment) and keeps every output, but a rerun can differ."
)


class EvaluationSetupError(Exception):
    """The run cannot start: bad configuration, unavailable model or backend. Raised before any record exists."""

    def __init__(self, message: str, kind: str = "setup") -> None:
        super().__init__(message)
        self.kind = kind


class FatalRunError(Exception):
    """A condition that makes every later case pointless (not a failure of one case)."""


class BudgetedLLM:
    """Wraps a provider so every call is reserved against the run budget first and settled afterwards.

    The patch generator already reserves its own calls. The Task 12 agent does not, so this
    wrapper is what keeps diagnosis spending inside the same hard cap.
    """

    def __init__(self, inner: LLMProvider, budget: LLMBudget, label: str) -> None:
        self._inner = inner
        self._budget = budget
        self._label = label
        self.exhausted_by: str | None = None
        self.max_tokens = getattr(inner, "max_tokens", None) or 16_000

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def pricing(self):
        return getattr(self._inner, "pricing", None)

    def generate(self, messages, tools, *, timeout=None, tool_choice="auto") -> LLMResponse:
        prices = model_prices(self._inner)
        estimate = None
        if prices is not None:
            estimate = worst_case_cost(prices, estimate_input_tokens(messages, tools), self.max_tokens)
        try:
            reservation = self._budget.reserve(estimated_cost=estimate, timeout=timeout or 120.0,
                                               label=f"{self._label} diagnosis")
        except LLMBudgetExhaustedError as exc:
            self.exhausted_by = exc.limit
            raise LLMError(f"The evaluation budget is exhausted ({exc.limit})") from None
        except LLMPricingUnknownError:
            raise LLMConfigurationError("The model's price is unknown, so its cost cannot be capped") from None
        try:
            response = self._inner.generate(messages, tools, timeout=reservation.seconds, tool_choice=tool_choice)
        except BaseException:
            self._budget.settle(reservation, actual_cost=None, outcome="error", model=self._inner.model)
            raise
        actual_prices = model_prices(self._inner, response.model) or prices
        self._budget.settle(reservation, actual_cost=usage_cost(actual_prices, response.usage) if actual_prices else None,
                            outcome="ok", model=response.model or self._inner.model)
        return response


def default_llm_factory(settings: Settings, model: dict[str, Any]) -> LLMProvider:
    """The configured model through the normal provider factory. Keys come from runtime settings only."""
    chosen = settings.model_copy(update={"llm_model": model["model_id"], "llm_effort": model["effort"],
                                         "llm_max_tokens": model["max_tokens"]})
    return create_llm_provider(chosen)


def default_engine_factory(limits: PatchLimits, settings: Settings, backend: str):
    """None means the local runner. The container runner is checked now, so a missing image fails the setup."""
    if backend == "local":
        return None
    from app.patching.container import ContainerLimits, ContainerTestRunner
    from app.patching.verification import VerificationEngine

    runner = ContainerTestRunner(limits, ContainerLimits(
        image=settings.patch_container_image, cpus=settings.patch_container_cpus,
        memory_bytes=settings.patch_container_memory_mb * 1024 * 1024, pids_limit=settings.patch_container_pids_limit),
        instance_id=settings.instance_id)
    if not runner.available():
        raise TestBackendUnavailableError(f"The container image {settings.patch_container_image} is not available")
    return VerificationEngine(limits, runner=runner)


def patch_limits_for(config: dict[str, Any], settings: Settings, run_budget_usd: float) -> PatchLimits:
    limits = PatchLimits.from_settings(settings)
    visible = config["test_execution"]["test_visibility"] == "visible"
    return limits.model_copy(update={
        "max_attempts": config["patching"]["max_attempts"],
        "max_llm_requests": config["budget"]["max_requests"],
        "max_llm_seconds": config["budget"]["max_seconds"],
        "max_llm_cost_usd": run_budget_usd,
        "llm_max_output_tokens": config["model"]["max_tokens"],
        "graph_test_discovery": bool(config["retrieval"]["graph_enabled"]),
        "max_context_test_files": limits.max_context_test_files if visible else 0,
    })


def agent_limits_for(config: dict[str, Any], settings: Settings) -> AgentLimits:
    base = AgentLimits.from_settings(settings)
    return base.model_copy(update={
        "max_iterations": config["agent"]["max_iterations"], "max_tool_calls": config["agent"]["max_tool_calls"],
        "max_duration_seconds": config["agent"]["max_duration_seconds"],
    })


def environment_record(config: dict[str, Any], settings: Settings, suite: BenchmarkSuite,
                       engine: Any, mode: str, diagnosis_mode: str) -> dict[str, Any]:
    runner = getattr(engine, "runner", None)
    identity = runner.identity() if runner is not None and hasattr(runner, "identity") else {
        "backend": "local", "isolated": False}
    return {
        "application": git_info(), "benchmark_version": suite.version, "versions": runtime_versions(),
        "test_backend": identity, "evaluation_mode": mode, "diagnosis_mode": diagnosis_mode,
        "embedding": {"provider": settings.embedding_provider, "model": settings.embedding_model,
                      "dimensions": settings.embedding_dimensions},
        "retrieval_applied": diagnosis_mode == "agent" and config["retrieval"]["strategy"] != "none",
        "prompts_changed_since_configuration": prompts_changed(config),
        "determinism": NON_DETERMINISM_NOTE,
        "cost_basis": "tokens reported by the provider multiplied by the list prices in app.agents.evaluation.PRICING",
    }


@dataclass
class CaseOutcome:
    stop_run: str | None = None  # "budget" | "fatal" | None


@dataclass
class RunContext:
    config: dict[str, Any]
    mode: str
    diagnosis_mode: str
    limits: PatchLimits
    engine: Any
    llm: Any
    shared: LLMBudget | None
    run_budget_usd: float | None


@dataclass
class EvaluationRunner:
    session_factory: Callable[[], Session]
    settings: Settings
    suite: BenchmarkSuite
    llm_factory: Callable[[Settings, dict[str, Any]], LLMProvider] = default_llm_factory
    engine_factory: Callable[[PatchLimits, Settings, str], Any] = default_engine_factory
    embedding_provider_factory: Callable[[Settings], Any] | None = None
    workspace_dir: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    clock: Callable[[], float] = time.monotonic
    _context: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Worker containers bind-mount their workspace through the Docker daemon, which resolves the path on the
        # host. Scratch space must therefore be the configured workspace directory (a shared volume in a
        # deployment), never the API container's private /tmp.
        if self.workspace_dir is None and self.settings.patch_workspace_dir is not None:
            self.workspace_dir = Path(self.settings.patch_workspace_dir)
            self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def abort(self) -> list[str]:
        """Cancel: stop between cases and remove the containers running now. Returns the removed names."""
        self.cancel_event.set()
        runner = getattr(getattr(self._context, "engine", None), "runner", None)
        return runner.kill_active() if runner is not None and hasattr(runner, "kill_active") else []

    # --- setup (before a record exists) ------------------------------------------------

    def prepare(self, config: dict[str, Any], mode: str, diagnosis_mode: str, run_budget_usd: float | None
                ) -> RunContext:
        """Everything that can be checked without spending money. Raises ``EvaluationSetupError``."""
        changed = prompts_changed(config)
        if changed and mode == "live":
            raise EvaluationSetupError(
                "The prompts changed since this configuration was created (" + ", ".join(changed)
                + "). Clone the configuration to pin the current prompts.", "prompts_changed")
        limits = patch_limits_for(config, self.settings, run_budget_usd or config["budget"]["max_cost_usd"])
        try:
            engine = self.engine_factory(limits, self.settings, config["test_execution"]["backend"])
        except TestBackendUnavailableError as exc:
            raise EvaluationSetupError(str(exc), "backend_unavailable") from None
        llm = shared = None
        if mode == "live":
            if run_budget_usd is None or run_budget_usd <= 0:
                raise EvaluationSetupError("A live evaluation needs an explicit positive budget", "budget_required")
            if diagnosis_mode == "agent" and config["retrieval"]["strategy"] != "none" and (
                    config["retrieval"]["embedding_model"] != self.settings.embedding_model):
                raise EvaluationSetupError(
                    f"The configuration uses embedding model {config['retrieval']['embedding_model']!r} but the "
                    f"runtime uses {self.settings.embedding_model!r}. Clone the configuration or change EMBEDDING_MODEL.",
                    "embedding_mismatch")
            try:
                llm = self.llm_factory(self.settings, config["model"])
            except LLMConfigurationError as exc:
                raise EvaluationSetupError(f"The language model is not available: {exc}", "llm_unavailable") from None
            if model_prices(llm) is None:
                raise EvaluationSetupError("The model's price is unknown, so its cost cannot be capped", "pricing")
            shared = LLMBudget(max_requests=100_000, max_seconds=86_400, max_cost_usd=run_budget_usd)
        return RunContext(config, mode, diagnosis_mode, limits, engine, llm, shared, run_budget_usd)

    # --- execution -----------------------------------------------------------------------

    def execute(self, run_id: uuid.UUID, context: RunContext) -> None:
        """Run every selected case, saving as it goes. Always leaves the run in a terminal status."""
        self._context = context
        with workflow_scope(str(run_id)):
            self._execute(run_id, context)

    def _execute(self, run_id: uuid.UUID, context: RunContext) -> None:
        session = self.session_factory()
        repo = RunRepository(session)
        started = self.clock()
        stop: str | None = None
        reason: str | None = None
        try:
            run = repo.get_run(run_id)
            run.status = RunStatus.RUNNING.value
            run.started_at = datetime.now(UTC)
            session.commit()
            log_event(logger, "evaluation_started", run_id=str(run_id), mode=context.mode,
                      diagnosis_mode=context.diagnosis_mode, cases=run.total_cases,
                      budget_usd=context.run_budget_usd, benchmark_version=run.benchmark_version)
            selected = self.suite.select(run.selected_case_ids)
            with tempfile.TemporaryDirectory(prefix="atlas-eval-", dir=self.workspace_dir) as base:
                workspaces = WorkspaceManager(Path(base))
                for index, case in enumerate(selected):
                    if self.cancel_event.is_set():
                        stop, reason = "cancelled", "cancelled by request"
                    elif context.shared is not None and (context.shared.remaining_cost() or 0) <= 0:
                        stop, reason = "budget", "the run budget was exhausted before this case started"
                    if stop:
                        self._mark_unstarted(session, run, selected[index:], context, reason)
                        break
                    try:
                        outcome = self._run_case(session, run, case, context, workspaces)
                    except FatalRunError as exc:
                        session.rollback()
                        stop, reason = "fatal", str(exc)
                        self._mark_unstarted(session, run, selected[index + 1:], context, reason)
                        break
                    if outcome.stop_run:
                        stop = outcome.stop_run
                        reason = "the run budget was exhausted" if stop == "budget" else "a fatal condition"
                        self._mark_unstarted(session, run, selected[index + 1:], context, reason)
                        break
            self._finish(session, repo, run, context, started, stop, reason)
        except Exception as exc:  # last resort: never leave a run "running"
            logger.exception("Evaluation run %s crashed", run_id)
            session.rollback()
            self._fail(session, repo, run_id, f"internal error: {type(exc).__name__}", started)
        finally:
            session.close()

    # --- one case --------------------------------------------------------------------------

    def _run_case(self, session: Session, run: EvaluationRun, case: PatchingBenchmarkCase, ctx: RunContext,
                  workspaces: WorkspaceManager) -> CaseOutcome:
        root = self.suite.case_root(case)
        entry = self.suite.diagnoses[case.investigation_case]
        before = snapshot_tree(root).root_hash
        outcome = CaseOutcome()
        variants: list[tuple[str, Any]]
        if ctx.mode == "reference":
            variants = [(ref.name, ref) for ref in case.reference_patches]
        else:
            variants = [("live", None)]
        for name, ref in variants:
            if self.cancel_event.is_set():
                self._mark_unstarted_variant(session, run, case, name, "cancelled by request")
                continue
            if ctx.shared is not None and (ctx.shared.remaining_cost() or 0) <= 0:
                self._mark_unstarted_variant(session, run, case, name, "the run budget was exhausted")
                outcome.stop_run = "budget"
                continue
            row = EvaluationCaseResult(
                evaluation_run_id=run.id, case_id=case.case_id, variant=name, category=case.category,
                status="running", evidence={}, attempts_count=0)
            session.add(row)
            session.commit()
            try:
                stop = self._run_variant(session, run, case, row, name, ref, entry, root, ctx, workspaces)
            except FatalRunError:
                raise
            except Exception as exc:  # one broken case must not erase the others
                logger.exception("Case %s (%s) failed", case.case_id, name)
                session.rollback()
                row = session.get(EvaluationCaseResult, row.id)
                row.status = "failed"
                row.failure_reason = f"internal_error: {type(exc).__name__}"
                session.commit()
                stop = None
            if stop:
                outcome.stop_run = stop
        if snapshot_tree(root).root_hash != before:
            raise FatalRunError(f"{case.case_id}: the original benchmark repository changed")
        return outcome

    def _run_variant(self, session, run, case, row, name, ref, entry, root, ctx, workspaces) -> str | None:
        case_started = self.clock()
        diagnosis: dict[str, Any] | None = None
        investigation = benchmark_investigation(case, entry)
        diagnosis_evidence: dict[str, Any] = {"source": "recorded"}
        model = ctx.llm
        if ctx.mode == "reference":
            model = reference_llm([(PATCHING_DIR / f).read_text() for f in ref.diff_files])
        elif ctx.diagnosis_mode == "agent":
            investigation, diagnosis, diagnosis_evidence, exhausted = self._diagnose(session, case, entry, root, ctx)
            row.diagnosis_status = diagnosis["status"]
            row.diagnosis_correct = diagnosis["correct"] if diagnosis["status"] == "completed" else None
            row.diagnosis_record_id = uuid.UUID(diagnosis_evidence["investigation_id"])
            if diagnosis["status"] != "completed":
                self._finish_case(session, row, score_without_patch(case, f"diagnosis_{diagnosis['status']}",
                                                                    diagnosis=diagnosis, variant=name),
                                  None, diagnosis_evidence, case_started, ctx, status="incomplete" if exhausted
                                  else "failed", reason=f"diagnosis {diagnosis['status']}"
                                  + (" (run budget exhausted)" if exhausted else ""))
                return "budget" if exhausted else None
            session.commit()

        persisted: list[int] = []

        def save_attempt(execution: PatchExecution, attempt: PatchAttempt) -> None:
            session.add(attempt_row(row.id, attempt, case, execution))
            row.attempts_count = len(execution.attempts)
            session.commit()
            persisted.append(attempt.attempt_number)

        try:
            execution = run_case(
                case, root, model, investigation, workspaces=workspaces, limits=ctx.limits,
                trusted_roots=[BENCHMARKS_DIR],
                max_attempts=(len(ref.diff_files) if ref else ctx.config["patching"]["max_attempts"]),
                full_suite=ctx.config["patching"]["run_full_test_suite"], llm_budget=ctx.shared, engine=ctx.engine,
                on_attempt=save_attempt,
            )
        except InvestigationNotEligibleError as exc:
            self._finish_case(session, row, score_without_patch(case, "not_eligible", variant=name,
                                                                diagnosis=diagnosis, reason=str(exc)),
                              None, diagnosis_evidence, case_started, ctx, status="failed", reason=str(exc))
            return None
        except (LLMPricingUnknownError, LLMConfigurationError, TestBackendUnavailableError) as exc:
            raise FatalRunError(f"{type(exc).__name__}: {exc}") from None
        runner = ctx.engine.runner if ctx.engine else None
        holdout = run_holdout(case, root, execution, PATCHING_DIR, workspaces=workspaces, limits=ctx.limits,
                              runner=runner)
        exploratory = run_exploratory(case, root, execution, PATCHING_DIR, workspaces=workspaces,
                                      limits=ctx.limits, runner=runner)
        score = score_execution(
            case, execution, variant=name,
            expected_status=ref.expected_status if ref else None,
            expected_attempts=ref.expected_attempts if ref else None,
            expected_holdout=ref.expected_holdout if ref else None,
            holdout=holdout, exploratory=exploratory, diagnosis=diagnosis)
        budget_stop = execution.stop_reason == "llm_budget_exhausted"
        if execution.status == "failed" and execution.failure_reason in (
                "llm_pricing_unknown", "llm_configuration_error"):
            raise FatalRunError(execution.failure_reason)
        status = ("incomplete" if budget_stop else "failed" if execution.status == "failed" else "completed")
        self._finish_case(session, row, score, execution, diagnosis_evidence, case_started, ctx, status=status,
                          holdout=holdout, exploratory=exploratory,
                          reason=execution.failure_reason or (execution.stop_reason if budget_stop else None))
        return "budget" if budget_stop else None

    def _diagnose(self, session: Session, case, entry, root, ctx: RunContext):
        """Run the Task 12 agent under the shared budget. Returns the investigation, its score and evidence."""
        from app.code_intelligence import analyze_repository
        from app.knowledge_graph.builder import build_code_graph
        from app.retrieval.embeddings import create_embedding_provider
        from app.retrieval.graph_search import InMemoryGraphContextSource
        from app.retrieval.indexing import RepositoryIndexer
        from app.retrieval.repository import ChunkRepository

        config = ctx.config
        sources = [RetrievalSource(s) for s in _retrieval_sources(config)]
        provider = None
        if sources:
            provider = (self.embedding_provider_factory or create_embedding_provider)(self.settings)
            RepositoryIndexer(ChunkRepository(session), provider).index_repository(case.repository_id, root)
            session.commit()
        graph = (InMemoryGraphContextSource(build_code_graph(analyze_repository(root), case.repository_id))
                 if RetrievalSource.GRAPH in sources else None)
        llm = BudgetedLLM(ctx.llm, ctx.shared, case.case_id)
        tools = [t for t in config["agent"]["tools"] if t != "retrieve_evidence" or sources]
        agent = DebuggingAgent(
            llm, create_default_registry(),
            StaticRepositoryResolver({case.repository_id: root}, allowed_roots=[BENCHMARKS_DIR]),
            limits=agent_limits_for(config, self.settings),
            repository=InvestigationRepository(session),
            retrieval=SessionScopedRetrieval(self.session_factory, provider, graph, strategies=sources)
            if sources else None,
            trusted_test_roots=[BENCHMARKS_DIR], allowed_tools=tools)
        started = self.clock()
        result = agent.investigate_bug(case.repository_id, entry["bug_report"])
        expected = self.suite.agent_cases.get(case.investigation_case)
        diagnosis = {
            "status": result.status,
            "correct": (result.status == "completed" and expected is not None
                        and root_cause_matches(result.root_cause, expected.root_cause_keywords)),
            "cost_usd": cost_usd(result.model, result.usage),
            "seconds": round(self.clock() - started, 3),
        }
        loaded = LoadedInvestigation(result.investigation_id, case.repository_id, result.status,
                                     entry["bug_report"], result)
        evidence = {"source": "agent", "investigation_id": result.investigation_id,
                    "analysis": result.model_dump(mode="json"), "retrieval_sources": [s.value for s in sources],
                    "tools": tools}
        return loaded, diagnosis, evidence, llm.exhausted_by is not None

    def _log_case(self, row: EvaluationCaseResult) -> None:
        log_event(logger, "evaluation_case_completed", case_id=row.case_id, variant=row.variant, status=row.status,
                  execution_status=row.execution_status, attempts=row.attempts_count,
                  final_verified=row.final_verified, heldout_passed=row.heldout_passed,
                  cost_usd=None if row.total_cost_usd is None else float(row.total_cost_usd),
                  duration_s=row.total_latency_seconds, failure_reason=row.failure_reason,
                  level=logging.WARNING if row.status in ("failed", "incomplete") else logging.INFO)

    def _finish_case(self, session: Session, row: EvaluationCaseResult, score: PatchCaseScore,
                     execution: PatchExecution | None, diagnosis_evidence: dict[str, Any], case_started: float,
                     ctx: RunContext, *, status: str, reason: str | None, holdout=None, exploratory=None) -> None:
        attempted = bool(score.attempts)
        row = session.merge(row)
        row.status = status
        row.execution_status = score.status
        row.diagnosis_status = score.diagnosis_status
        row.diagnosis_correct = score.diagnosis_correct
        row.patch_generated = score.generated if attempted else None
        row.patch_valid = score.validated if score.generated else None
        row.patch_applied = score.applied if score.validated else None
        row.first_attempt_verified = (score.attempt_statuses[0] == "verified") if attempted else None
        row.final_verified = score.verified if attempted else None
        row.repaired_by_retry = score.repaired_by_retry if attempted else None
        row.success = score.success if attempted else None
        row.heldout_passed = score.holdout_passed
        row.exploratory_passed = score.exploratory_passed
        row.regression_detected = (score.attempts_with_regressions > 0) if score.attempts_applied else None
        row.attempts_count = score.attempts
        row.failure_reason = reason
        if ctx.mode == "live":
            total = score.total_cost_usd
            row.total_cost_usd = None if total is None else Decimal(str(total))
            row.cost_kind = "actual" if total is not None else None
            row.input_tokens, row.output_tokens = score.input_tokens or None, score.output_tokens or None
        row.total_latency_seconds = round(score.total_seconds if attempted or ctx.mode == "live"
                                          else self.clock() - case_started, 3)
        row.evidence = {
            "score": score.model_dump(mode="json"), "diagnosis": diagnosis_evidence,
            "holdout": holdout, "exploratory": exploratory,
            "execution": execution_evidence(execution) if execution else None,
        }
        if holdout is not None and attempted:
            final = session.query(EvaluationAttempt).filter_by(
                evaluation_case_result_id=row.id, attempt_number=score.attempts).one_or_none()
            if final is not None:
                final.heldout_tests_passed = holdout.get("passed")
        session.commit()
        self._log_case(row)

    # --- bookkeeping ------------------------------------------------------------------------

    def _mark_unstarted(self, session, run, cases, ctx, reason) -> None:
        for case in cases:
            names = [r.name for r in case.reference_patches] if ctx.mode == "reference" else ["live"]
            for name in names:
                self._mark_unstarted_variant(session, run, case, name, reason)

    @staticmethod
    def _mark_unstarted_variant(session, run, case, name, reason) -> None:
        log_event(logger, "evaluation_case_not_started", case_id=case.case_id, variant=name, reason=reason,
                  level=logging.WARNING)
        session.add(EvaluationCaseResult(
            evaluation_run_id=run.id, case_id=case.case_id, variant=name, category=case.category,
            status="incomplete", failure_reason=reason, evidence={}, attempts_count=0))
        session.commit()

    def _finish(self, session: Session, repo: RunRepository, run: EvaluationRun, ctx: RunContext, started: float,
                stop: str | None, reason: str | None) -> None:
        session.expire_all()
        run = repo.get_run(run.id)
        cases = repo.list_cases(run.id, limit=10_000, offset=0)[0]
        unfinished = [c for c in cases if c.status in ("incomplete", "running", "pending")]
        failed = [c for c in cases if c.status == "failed"]
        for c in cases:
            if c.status == "running":  # cannot happen after a normal case; do not leave it dangling
                c.status, c.failure_reason = "incomplete", "the case did not finish"
        if stop == "cancelled":
            status = RunStatus.CANCELLED
        elif stop == "fatal" and not any(c.status == "completed" for c in cases):
            status = RunStatus.FAILED
        elif unfinished or stop:
            status = RunStatus.PARTIAL
        elif cases and len(failed) == len(cases):
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETED
        expected_units = run.total_cases
        if status == RunStatus.COMPLETED and len(cases) != expected_units:
            status = RunStatus.PARTIAL  # never "completed" with silently skipped cases
            reason = reason or f"{len(cases)} of {expected_units} case records exist"
        costs = [c.total_cost_usd for c in cases if c.total_cost_usd is not None]
        run.total_cost_usd = sum(costs, Decimal(0)) if costs else None
        run.cost_kind = "actual" if costs else None
        run.total_duration_seconds = round(self.clock() - started, 3)
        run.completed_at = datetime.now(UTC)
        run.failure_reason = reason if status != RunStatus.COMPLETED else (
            f"{len(failed)} case(s) failed" if failed else None)
        if ctx.shared is not None:
            run.environment = {**run.environment, "budget_ledger": ctx.shared.report()}
        # Metrics first (allowed while the run is open), then the run becomes immutable.
        repo.calculate_metrics(run.id, f"initial calculation when the run {status.value}")
        run.status = status.value
        session.commit()
        metrics.EVALUATION_RUNS.labels(status.value).inc()
        log_event(logger, "evaluation_finished", run_id=str(run.id), status=status.value, stop=stop,
                  cases=len(cases), failed=len(failed), unfinished=len(unfinished),
                  cost_usd=None if run.total_cost_usd is None else float(run.total_cost_usd),
                  duration_s=run.total_duration_seconds, reason=reason,
                  level=logging.INFO if status == RunStatus.COMPLETED else logging.WARNING)

    def _fail(self, session: Session, repo: RunRepository, run_id: uuid.UUID, reason: str, started: float) -> None:
        try:
            run = repo.get_run(run_id)
            if run.status in ("pending", "running"):
                run.status = RunStatus.FAILED.value
                run.failure_reason = reason
                run.completed_at = datetime.now(UTC)
                run.total_duration_seconds = round(self.clock() - started, 3)
                session.commit()
        except Exception:  # the database itself is the problem; recovery at the next start handles it
            logger.exception("Could not mark run %s failed", run_id)
            session.rollback()


# --- translating pipeline records into stored evidence ---------------------------------------


def _retrieval_sources(config: dict[str, Any]) -> list[str]:
    from app.evaluation.configuration import RetrievalSpec

    return RetrievalSpec.model_validate(config["retrieval"]).sources


def attempt_row(case_row_id: uuid.UUID, attempt: PatchAttempt, case: PatchingBenchmarkCase,
                execution: PatchExecution) -> EvaluationAttempt:
    validation, application, verification = attempt.validation, attempt.application, attempt.verification
    applied = application.applied if application is not None else None
    required = None
    if verification is not None:
        required = all(v == "passed" for v in required_outcomes(attempt, case.required_tests).values())
    usage_cost_value = cost_usd(attempt.proposal.model, attempt.proposal.usage) if (
        attempt.proposal and attempt.proposal.model) else None
    status = str(attempt.status)
    return EvaluationAttempt(
        evaluation_case_result_id=case_row_id, attempt_number=attempt.attempt_number,
        status=status if status in ("verified", "rejected", "inconclusive", "failed", "validation_failed")
        else "incomplete",
        patch_valid=validation.valid if validation is not None else None,
        patch_applied=applied,
        required_tests_passed=required,
        regression_detected=had_regression(attempt) if (applied and verification is not None) else None,
        feedback_from_attempts=list(attempt.feedback_from), feedback_reasons=list(attempt.feedback_reasons),
        failure_reason=attempt.failure_reason,
        unified_diff=attempt.proposal.unified_diff if attempt.proposal else None,
        diff_sha256=validation.diff_sha256 if validation else None,
        patch_proposal_id=uuid.UUID(attempt.proposal.patch_id) if attempt.proposal else None,
        cost_usd=None if usage_cost_value is None else Decimal(str(usage_cost_value)),
        cost_kind="actual" if usage_cost_value is not None else None,
        latency_seconds=attempt.duration_seconds,
        evidence={
            "summary": attempt.summary,
            "description": attempt.proposal.description if attempt.proposal else None,
            "rationale": attempt.proposal.rationale if attempt.proposal else None,
            "validation": validation.model_dump(mode="json") if validation else None,
            "application": application.model_dump(mode="json") if application else None,
            "verification": verification.model_dump(mode="json") if verification else None,
            "required_test_outcomes": required_outcomes(attempt, case.required_tests) if verification else None,
            "usage": attempt.proposal.usage.model_dump(mode="json") if attempt.proposal else None,
        },
    )


def execution_evidence(execution: PatchExecution) -> dict[str, Any]:
    """The execution minus per-attempt bodies, which live in the attempt rows."""
    data = execution.model_dump(mode="json")
    data["attempts"] = [{"attempt_number": a["attempt_number"], "status": a["status"]} for a in data["attempts"]]
    return data
