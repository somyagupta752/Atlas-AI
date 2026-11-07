"""HTTP endpoints for the debugging agent. Thin: validation, wiring, error mapping.

Investigations run synchronously inside the request (bounded by
``AGENT_MAX_DURATION_SECONDS``). FastAPI runs these sync handlers in a worker
thread. Background jobs and streaming come later.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.agent import (
    DebuggingAgent,
    IndexedRepositoryResolver,
    InvalidInvestigationRequestError,
    RepositoryAccessDeniedError,
    SessionScopedRetrieval,
    UnknownRepositoryError,
)
from app.agents.events import EventType
from app.agents.llm import LLMProvider
from app.agents.repository import InvestigationNotFoundError, InvestigationRepository
from app.agents.schemas import AgentLimits, InvestigateRequest, RootCauseAnalysis
from app.agents.tool_registry import ToolRegistry, create_default_registry
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.dependencies import (
    get_allowed_repository_roots,
    get_embedding_provider_optional,
    get_graph_context_source,
)
from app.retrieval.repository import ChunkRepository
from app.tools.base import RetrievalPort

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debugging"])

MAX_REQUEST_BYTES = 64 * 1024

# failure_reason -> (HTTP status, client message). Bodies never contain internals.
FAILURE_RESPONSES: dict[str, tuple[int, str]] = {
    "llm_timeout": (504, "The language model did not respond in time"),
    "llm_rate_limited": (503, "The language model is rate limited; retry later"),
    "llm_configuration_error": (503, "The language model is not available"),
    "llm_provider_error": (502, "The language model provider failed"),
    "llm_invalid_response": (502, "The language model returned an invalid response"),
    "invalid_diagnosis": (502, "The model's diagnosis failed validation"),
    "refusal": (502, "The language model declined the request"),
    "internal_error": (500, "The investigation failed because of an internal error"),
}

INVESTIGATION_ID = PathParam(pattern=r"^[0-9a-fA-F-]{36}$")
OBSERVATION_EVENT_TYPES = [
    EventType.LLM_RESPONSE,
    EventType.TOOL_CALL,
    EventType.OBSERVATION,
    EventType.LOOP_WARNING,
    EventType.LOOP_STOPPED,
]


class InvestigationRecordOut(BaseModel):
    investigation_id: str
    repository_id: str
    status: str
    summary: str | None
    root_cause: str | None
    confidence: float | None
    created_at: datetime
    completed_at: datetime | None
    result: dict[str, Any] | None


class InvestigationEventOut(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class InvestigationEventsOut(BaseModel):
    investigation_id: str
    repository_id: str
    events: list[InvestigationEventOut]


# --- dependencies ---------------------------------------------------------------------


def get_llm_provider(request: Request) -> LLMProvider:
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:
        raise HTTPException(status_code=503, detail="The debugging agent's language model is not configured")
    return provider


def get_tool_registry(request: Request) -> ToolRegistry:
    registry = getattr(request.app.state, "tool_registry", None)
    if registry is None:
        registry = request.app.state.tool_registry = create_default_registry()
    return registry


def get_agent_limits() -> AgentLimits:
    return AgentLimits.from_settings(get_settings())


def get_trusted_test_roots() -> list[Path]:
    return get_settings().agent_trusted_test_roots


def get_agent_retrieval(
    provider: Any = Depends(get_embedding_provider_optional),
    graph_source: Any = Depends(get_graph_context_source),
) -> RetrievalPort | None:
    return SessionScopedRetrieval(SessionLocal, provider, graph_source)


def get_debugging_agent(
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    registry: ToolRegistry = Depends(get_tool_registry),
    limits: AgentLimits = Depends(get_agent_limits),
    allowed_roots: list[Path] = Depends(get_allowed_repository_roots),
    trusted_test_roots: list[Path] = Depends(get_trusted_test_roots),
    retrieval: RetrievalPort | None = Depends(get_agent_retrieval),
    graph_source: Any = Depends(get_graph_context_source),
) -> DebuggingAgent:
    return DebuggingAgent(
        llm,
        registry,
        IndexedRepositoryResolver(ChunkRepository(db), allowed_roots),
        limits=limits,
        repository=InvestigationRepository(db),
        retrieval=retrieval,
        graph=graph_source,
        trusted_test_roots=trusted_test_roots,
    )


def get_investigation_repository(db: Session = Depends(get_db)) -> InvestigationRepository:
    return InvestigationRepository(db)


# --- endpoints ------------------------------------------------------------------------


@router.post("/investigate", response_model=RootCauseAnalysis)
def investigate(
    body: InvestigateRequest, agent: DebuggingAgent = Depends(get_debugging_agent)
) -> RootCauseAnalysis | JSONResponse:
    try:
        result = agent.investigate_bug(body.repository_id, body.bug_report)
    except InvalidInvestigationRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownRepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if result.status == "failed":
        status, message = FAILURE_RESPONSES.get(result.failure_reason or "", (500, "The investigation failed"))
        return JSONResponse(
            status_code=status,
            content={
                "detail": message,
                "investigation_id": result.investigation_id,
                "status": result.status,
                "failure_reason": result.failure_reason,
            },
        )
    return result


@router.get("/investigations/{investigation_id}")
def get_investigation(
    investigation_id: str = INVESTIGATION_ID,
    repository_id: str | None = Query(default=None, max_length=100),
    repository: InvestigationRepository = Depends(get_investigation_repository),
) -> InvestigationRecordOut:
    try:
        record = repository.get(investigation_id, repository_id)
    except InvestigationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Investigation not found") from exc
    return InvestigationRecordOut(
        investigation_id=str(record.id),
        repository_id=record.repository_id,
        status=record.status,
        summary=record.summary,
        root_cause=record.root_cause,
        confidence=record.confidence,
        created_at=record.created_at,
        completed_at=record.completed_at,
        result=record.result,
    )


@router.get("/investigations/{investigation_id}/observations")
def get_investigation_observations(
    investigation_id: str = INVESTIGATION_ID,
    repository_id: str | None = Query(default=None, max_length=100),
    timeline: bool = Query(default=False, description="Return every event, not only tool activity."),
    repository: InvestigationRepository = Depends(get_investigation_repository),
) -> InvestigationEventsOut:
    try:
        record = repository.get(investigation_id, repository_id)
    except InvestigationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Investigation not found") from exc
    events = repository.list_events(str(record.id), None if timeline else OBSERVATION_EVENT_TYPES)
    return InvestigationEventsOut(
        investigation_id=str(record.id),
        repository_id=record.repository_id,
        events=[
            InvestigationEventOut(
                sequence=e.sequence, event_type=e.event_type, payload=e.payload, created_at=e.created_at
            )
            for e in events
        ],
    )


async def limit_debug_request_size(request: Request, call_next: Any) -> Any:
    """Reject oversized bodies on /debug before they are parsed."""
    if request.url.path.startswith("/debug") and request.method == "POST":
        length = request.headers.get("content-length")
        if length is None and request.headers.get("transfer-encoding"):
            return JSONResponse(status_code=411, content={"detail": "Content-Length is required"})
        if length is not None and (not length.isdigit() or int(length) > MAX_REQUEST_BYTES):
            return JSONResponse(status_code=413, content={"detail": "Request body is too large"})
    return await call_next(request)
