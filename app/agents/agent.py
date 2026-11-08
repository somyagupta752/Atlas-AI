"""The debugging agent service: ``investigate_bug(repository_id, bug_report)``.

Steps:

1. Validate the request and resolve the repository ID to an approved root.
2. Create the investigation ID and state; persist the row before anything runs.
3. Build the system prompt and the per-investigation tool context.
4. Run the investigator (agent loop, diagnosis validation, repair).
5. Persist the final result, whatever the outcome, and return it.

No HTTP here: the same service runs from FastAPI, scripts or a future worker.
"""

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from app.agents.diagnosis import build_analysis
from app.agents.events import EventSink, EventType, InMemoryEventSink
from app.agents.investigator import Investigator
from app.agents.llm import LLMProvider
from app.agents.loop import CancellationToken
from app.agents.prompts import SYSTEM_PROMPT, initial_user_message
from app.agents.repository import InvestigationRepository, PersistentEventSink
from app.agents.schemas import MAX_BUG_REPORT_LENGTH, AgentLimits, RootCauseAnalysis
from app.agents.state import InvestigationState
from app.agents.tool_executor import ToolExecutor
from app.agents.tool_registry import ToolRegistry
from app.core import metrics
from app.core.logging import log_event
from app.core.observability import workflow_scope
from app.knowledge_graph.models import is_valid_repository_id
from app.tools.base import RetrievalPort, ToolContext

logger = logging.getLogger(__name__)


class InvalidInvestigationRequestError(ValueError):
    pass


class UnknownRepositoryError(LookupError):
    pass


class RepositoryAccessDeniedError(PermissionError):
    pass


class RepositoryResolver(Protocol):
    def resolve(self, repository_id: str) -> Path:
        """Return the approved, resolved root. Raise Unknown/AccessDenied errors."""
        ...


def within_roots(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved.is_relative_to(root.expanduser().resolve()) for root in roots)


def _checked_root(repository_id: str, root_path: str | Path, allowed_roots: list[Path]) -> Path:
    if not allowed_roots:
        raise RepositoryAccessDeniedError("Repository access is disabled: ALLOWED_REPOSITORY_ROOTS is not configured")
    root = Path(root_path).expanduser().resolve()
    if not within_roots(root, allowed_roots):
        raise RepositoryAccessDeniedError(f"Repository {repository_id!r} is not in an allowed root")
    if not root.is_dir():
        raise UnknownRepositoryError(f"Repository {repository_id!r} is no longer on disk")
    return root


class IndexedRepositoryResolver:
    """Repository IDs are the ones indexed by Task 11. The index records the root path."""

    def __init__(self, chunks: Any, allowed_roots: list[Path]) -> None:
        self._chunks = chunks
        self._allowed = allowed_roots

    def resolve(self, repository_id: str) -> Path:
        state = self._chunks.get_index_state(repository_id)
        if state is None:
            raise UnknownRepositoryError(f"Unknown repository {repository_id!r}: index it first")
        return _checked_root(repository_id, state.root_path, self._allowed)


class StaticRepositoryResolver:
    """A fixed ``{repository_id: path}`` map, for tests and benchmarks."""

    def __init__(self, repositories: dict[str, Path], allowed_roots: list[Path] | None = None) -> None:
        self._repositories = repositories
        self._allowed = allowed_roots if allowed_roots is not None else list(repositories.values())

    def resolve(self, repository_id: str) -> Path:
        if repository_id not in self._repositories:
            raise UnknownRepositoryError(f"Unknown repository {repository_id!r}")
        return _checked_root(repository_id, self._repositories[repository_id], self._allowed)


class SessionScopedRetrieval:
    """Hybrid retrieval with a fresh database session per call.

    Tools run in worker threads, and a timed-out worker may still be running, so
    retrieval must never share the session the service uses for events.
    """

    def __init__(self, session_factory: Callable[[], Any], provider: Any = None, graph_source: Any = None,
                 strategies: Any = None) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._graph_source = graph_source
        # RetrievalSource members to use; None means all of them (hybrid).
        self._strategies = strategies

    def retrieve_code(self, repository_id: str, query: str, limit: int) -> Any:
        from app.retrieval.graph_search import GraphSearcher
        from app.retrieval.keyword_search import KeywordSearcher
        from app.retrieval.repository import ChunkRepository
        from app.retrieval.service import RetrievalService
        from app.retrieval.vector_search import VectorSearcher

        with self._session_factory() as session:
            chunks = ChunkRepository(session)
            service = RetrievalService(
                chunks,
                KeywordSearcher(session),
                VectorSearcher(session, self._provider) if self._provider is not None else None,
                GraphSearcher(self._graph_source, chunks) if self._graph_source is not None else None,
            )
            if self._strategies is None:
                return service.retrieve_code(repository_id, query, limit)
            return service.retrieve_code(repository_id, query, limit, strategies=self._strategies)


class DebuggingAgent:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        resolver: RepositoryResolver,
        *,
        limits: AgentLimits | None = None,
        repository: InvestigationRepository | None = None,
        retrieval: RetrievalPort | None = None,
        graph: Any = None,
        trusted_test_roots: list[Path] | None = None,
        allowed_tools: Iterable[str] | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._resolver = resolver
        self._limits = limits or AgentLimits()
        self._repository = repository
        self._retrieval = retrieval
        self._graph = graph
        self._trusted_test_roots = trusted_test_roots or []
        self._allowed_tools = allowed_tools
        self.last_events: EventSink | None = None

    def investigate_bug(
        self,
        repository_id: str,
        bug_report: str,
        cancellation: CancellationToken | None = None,
    ) -> RootCauseAnalysis:
        """Raises InvalidInvestigationRequestError, UnknownRepositoryError or
        RepositoryAccessDeniedError before anything is stored. Every other
        failure is returned as a persisted ``failed`` result."""
        repository_id, bug_report = _validate_request(repository_id, bug_report)
        root = self._resolver.resolve(repository_id)

        state = InvestigationState(repository_id=repository_id, bug_report=bug_report)
        with workflow_scope(state.investigation_id):
            return self._investigate(state, root, repository_id, cancellation)

    def _investigate(self, state: InvestigationState, root: Path, repository_id: str,
                     cancellation: CancellationToken | None) -> RootCauseAnalysis:
        started = time.perf_counter()
        log_event(logger, "agent_workflow_started", repository_id=repository_id)
        state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": initial_user_message(repository_id, state.bug_report)},
        ]
        executor = ToolExecutor(self._registry, self._allowed_tools)
        events = self._start(state, executor)

        context = ToolContext(
            repository_id=repository_id,
            repository_root=root,
            retrieval=self._retrieval,
            graph=self._graph,
            test_execution_allowed=within_roots(root, self._trusted_test_roots),
            tool_timeout_seconds=self._limits.tool_timeout_seconds,
            max_output_bytes=self._limits.max_tool_output_bytes,
        )
        investigator = Investigator(self._llm, executor, self._limits, events, cancellation)
        try:
            result = investigator.run(state, context)
        except Exception as exc:
            logger.exception("Investigation %s failed", state.investigation_id)
            events.record(EventType.INVESTIGATION_FAILED, {"error_type": type(exc).__name__})
            result = build_analysis(
                state,
                status="failed",
                summary="The investigation failed because of an internal error.",
                failure_reason="internal_error",
            )
        events.record(
            EventType.DIAGNOSIS_COMPLETED,
            {"status": result.status, "stop_reason": result.stop_reason, "failure_reason": result.failure_reason},
        )
        if self._repository is not None:
            self._repository.finish(result)
        elapsed = time.perf_counter() - started
        metrics.WORKFLOW_DURATION.labels(result.status).observe(elapsed)
        log_event(logger, "agent_workflow_completed", repository_id=repository_id, status=result.status,
                  stop_reason=result.stop_reason, failure_reason=result.failure_reason,
                  duration_ms=round(elapsed * 1000, 1))
        return result

    def _start(self, state: InvestigationState, executor: ToolExecutor) -> EventSink:
        if self._repository is not None:
            self._repository.create(state)
            events: EventSink = PersistentEventSink(self._repository, state.investigation_id)
        else:
            events = InMemoryEventSink()
        self.last_events = events
        events.record(
            EventType.INVESTIGATION_CREATED,
            {
                "repository_id": state.repository_id,
                "bug_report": state.bug_report,
                "provider": getattr(self._llm, "name", None),
                "model": getattr(self._llm, "model", None),
                "tools": sorted(executor.allowed_tools),
                "limits": self._limits.model_dump(),
            },
        )
        return events


def _validate_request(repository_id: str, bug_report: str) -> tuple[str, str]:
    if not isinstance(repository_id, str) or not is_valid_repository_id(repository_id):
        raise InvalidInvestigationRequestError("Invalid repository id")
    if not isinstance(bug_report, str) or not bug_report.strip():
        raise InvalidInvestigationRequestError("The bug report must not be empty")
    if len(bug_report) > MAX_BUG_REPORT_LENGTH:
        raise InvalidInvestigationRequestError(f"The bug report must be at most {MAX_BUG_REPORT_LENGTH} characters")
    return repository_id, bug_report.strip()
