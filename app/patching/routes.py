"""HTTP endpoints for patch generation and verification. Thin: wiring and error mapping.

    POST /debug/investigations/{investigation_id}/patches   generate and verify
    GET  /debug/investigations/{investigation_id}/patches   every attempt
    GET  /debug/patches/{patch_id}                          one proposal
    GET  /debug/patches/{patch_id}/verification             its checks and comparison

The diagnosis always comes from the stored investigation. Clients cannot send
one. Execution is synchronous and bounded (LLM timeout per attempt, a
verification time budget, attempt limits); background jobs come later.
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
    IndexedRepositoryResolver,
    RepositoryAccessDeniedError,
    UnknownRepositoryError,
)
from app.agents.llm import LLMProvider
from app.agents.repository import InvestigationNotFoundError, InvestigationRepository
from app.agents.routes import get_llm_provider, get_trusted_test_roots
from app.config import get_settings
from app.database import get_db
from app.dependencies import get_allowed_repository_roots
from app.models import PatchProposalRecord
from app.patching.models import GeneratePatchRequest, PatchExecution, PatchLimits
from app.patching.repository import PatchNotFoundError, PatchRepository
from app.patching.service import (
    DatabaseInvestigationSource,
    InvestigationNotEligibleError,
    PatchingService,
    PatchRequestError,
)
from app.patching.workspace import WorkspaceManager
from app.retrieval.repository import ChunkRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["patching"])

# One Path() per parameter name: FastAPI binds the name to the FieldInfo instance.
INVESTIGATION_ID = PathParam(pattern=r"^[0-9a-fA-F-]{36}$")
PATCH_ID = PathParam(pattern=r"^[0-9a-fA-F-]{36}$")
REPOSITORY_QUERY = Query(default=None, max_length=100)

# failure_reason -> (HTTP status, client message). Bodies never contain internals.
FAILURE_RESPONSES: dict[str, tuple[int, str]] = {
    "llm_timeout": (504, "The language model did not respond in time"),
    "llm_rate_limited": (503, "The language model is rate limited; retry later"),
    "llm_configuration_error": (503, "The language model is not available"),
    "llm_provider_error": (502, "The language model provider failed"),
    "llm_invalid_response": (502, "The language model returned an invalid patch proposal"),
    "llm_refusal": (502, "The language model declined the request"),
    "workspace_error": (503, "An isolated workspace could not be created"),
    "persistence_error": (500, "Patch data could not be saved"),
    "internal_error": (500, "Patch generation failed because of an internal error"),
}
# Fields of the stored provenance that are safe to return.
PUBLIC_PROVENANCE = ("provider", "model", "prompt_version", "usage", "llm_calls", "source_snapshot_hash",
                     "source_revision", "claimed_files")


class AttemptOut(BaseModel):
    attempt_number: int
    patch_id: str | None
    status: str
    summary: str
    failure_reason: str | None
    validation_errors: list[str]


class PatchExecutionOut(BaseModel):
    execution_id: str
    investigation_id: str
    repository_id: str
    patch_id: str | None
    status: str
    description: str | None
    affected_files: list[str]
    unified_diff: str | None
    verification: dict[str, str]
    attempts: list[AttemptOut]
    summary: str
    failure_reason: str | None
    stop_reason: str | None
    duration_seconds: float
    model: str | None
    original_repository_modified: bool


class PatchSummaryOut(BaseModel):
    patch_id: str
    execution_id: str
    attempt_number: int
    status: str
    description: str
    affected_files: list[Any]
    created_at: datetime


class InvestigationPatchesOut(BaseModel):
    investigation_id: str
    repository_id: str
    patches: list[PatchSummaryOut]


class PatchDetailOut(BaseModel):
    patch_id: str
    investigation_id: str
    execution_id: str
    repository_id: str
    attempt_number: int
    status: str
    status_history: list[dict[str, Any]]
    description: str
    rationale: str
    assumptions: list[Any]
    risks: list[Any]
    unified_diff: str
    affected_files: list[Any]
    validation_result: dict[str, Any] | None
    provenance: dict[str, Any]
    verification: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class VerificationOut(BaseModel):
    verification_id: str
    patch_id: str
    status: str
    summary: str
    failure_reasons: list[Any]
    checks: list[dict[str, Any]]
    before_results: dict[str, Any]
    after_results: dict[str, Any]
    comparison: dict[str, Any] | None
    environment: dict[str, Any]
    duration_seconds: float
    created_at: datetime


# --- dependencies ---------------------------------------------------------------------


def get_patch_limits() -> PatchLimits:
    return PatchLimits.from_settings(get_settings())


def get_workspace_manager(request: Request) -> WorkspaceManager:
    manager = getattr(request.app.state, "workspace_manager", None)
    if manager is None:
        settings = get_settings()
        manager = request.app.state.workspace_manager = WorkspaceManager(
            settings.patch_workspace_dir,
            retention=settings.patch_workspace_retention,
            max_age_seconds=settings.patch_workspace_max_age_hours * 3600,
        )
        try:
            removed = manager.purge_expired()  # retained workspaces past their age, once per process
            if removed:
                logger.info("Removed %d expired patch workspaces", removed)
        except OSError:
            logger.warning("Could not purge expired patch workspaces")
    return manager


def get_patch_repository(db: Session = Depends(get_db)) -> PatchRepository:
    return PatchRepository(db)


def get_patching_service(
    db: Session = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    allowed_roots: list[Path] = Depends(get_allowed_repository_roots),
    trusted_test_roots: list[Path] = Depends(get_trusted_test_roots),
    limits: PatchLimits = Depends(get_patch_limits),
    workspaces: WorkspaceManager = Depends(get_workspace_manager),
) -> PatchingService:
    return PatchingService(
        llm,
        DatabaseInvestigationSource(InvestigationRepository(db)),
        IndexedRepositoryResolver(ChunkRepository(db), allowed_roots),
        workspaces=workspaces,
        patches=PatchRepository(db),
        limits=limits,
        trusted_test_roots=trusted_test_roots,
    )


# --- endpoints ------------------------------------------------------------------------


def _execution_out(execution: PatchExecution) -> PatchExecutionOut:
    final = execution.final_attempt
    proposal = final.proposal if final else None
    verification = final.verification if final else None
    checks = {c.name: c.status for c in verification.checks} if verification else {}
    if final and final.validation is not None:
        checks = {"patch_validation": "passed" if final.validation.valid else "failed", **checks}
    return PatchExecutionOut(
        execution_id=execution.execution_id,
        investigation_id=execution.investigation_id,
        repository_id=execution.repository_id,
        patch_id=execution.final_patch_id,
        status=execution.status,
        description=proposal.description if proposal else None,
        affected_files=proposal.affected_files if proposal else [],
        unified_diff=proposal.unified_diff if proposal else None,
        verification=checks,
        attempts=[
            AttemptOut(
                attempt_number=a.attempt_number,
                patch_id=a.proposal.patch_id if a.proposal else None,
                status=a.status,
                summary=a.summary,
                failure_reason=a.failure_reason,
                validation_errors=a.validation.errors if a.validation else [],
            )
            for a in execution.attempts
        ],
        summary=execution.summary,
        failure_reason=execution.failure_reason,
        stop_reason=execution.stop_reason,
        duration_seconds=execution.duration_seconds,
        model=execution.model,
        original_repository_modified=execution.original_repository_modified,
    )


@router.post("/investigations/{investigation_id}/patches", response_model=PatchExecutionOut)
def generate_patch(
    body: GeneratePatchRequest,
    investigation_id: str = INVESTIGATION_ID,
    service: PatchingService = Depends(get_patching_service),
) -> PatchExecutionOut | JSONResponse:
    try:
        execution = service.generate_and_verify_patch(
            investigation_id,
            repository_id=body.repository_id,
            max_attempts=body.max_attempts,
            run_full_test_suite=body.run_full_test_suite,
        )
    except InvestigationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Investigation not found") from exc
    except InvestigationNotEligibleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownRepositoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PatchRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = _execution_out(execution)
    if execution.status == "failed":
        status, message = FAILURE_RESPONSES.get(execution.failure_reason or "", (500, "Patch generation failed"))
        return JSONResponse(status_code=status, content={"detail": message, **out.model_dump(mode="json")})
    return out


@router.get("/investigations/{investigation_id}/patches")
def list_patches(
    investigation_id: str = INVESTIGATION_ID,
    repository_id: str | None = REPOSITORY_QUERY,
    db: Session = Depends(get_db),
    patches: PatchRepository = Depends(get_patch_repository),
) -> InvestigationPatchesOut:
    try:
        record = InvestigationRepository(db).get(investigation_id, repository_id)
    except InvestigationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Investigation not found") from exc
    rows = patches.list_for_investigation(str(record.id), record.repository_id)
    return InvestigationPatchesOut(
        investigation_id=str(record.id),
        repository_id=record.repository_id,
        patches=[
            PatchSummaryOut(
                patch_id=str(r.id), execution_id=str(r.execution_id), attempt_number=r.attempt_number,
                status=r.status, description=r.description, affected_files=r.affected_files, created_at=r.created_at,
            )
            for r in rows
        ],
    )


def _proposal(patches: PatchRepository, patch_id: str, repository_id: str | None) -> PatchProposalRecord:
    try:
        return patches.get_proposal(patch_id, repository_id)
    except PatchNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Patch not found") from exc


@router.get("/patches/{patch_id}")
def get_patch(
    patch_id: str = PATCH_ID,
    repository_id: str | None = REPOSITORY_QUERY,
    patches: PatchRepository = Depends(get_patch_repository),
) -> PatchDetailOut:
    record = _proposal(patches, patch_id, repository_id)
    verification = patches.get_verification(str(record.id))
    return PatchDetailOut(
        patch_id=str(record.id),
        investigation_id=str(record.investigation_id),
        execution_id=str(record.execution_id),
        repository_id=record.repository_id,
        attempt_number=record.attempt_number,
        status=record.status,
        status_history=record.status_history,
        description=record.description,
        rationale=record.rationale,
        assumptions=record.assumptions,
        risks=record.risks,
        unified_diff=record.unified_diff,
        affected_files=record.affected_files,
        validation_result=record.validation_result,
        provenance={k: v for k, v in (record.provenance or {}).items() if k in PUBLIC_PROVENANCE},
        verification=None if verification is None else {
            "status": verification.status,
            "summary": verification.summary,
            "checks": {c["name"]: c["status"] for c in verification.checks},
        },
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get("/patches/{patch_id}/verification")
def get_patch_verification(
    patch_id: str = PATCH_ID,
    repository_id: str | None = REPOSITORY_QUERY,
    patches: PatchRepository = Depends(get_patch_repository),
) -> VerificationOut:
    record = _proposal(patches, patch_id, repository_id)
    verification = patches.get_verification(str(record.id))
    if verification is None:
        raise HTTPException(status_code=404, detail="This patch has no verification (it may have failed validation)")
    return VerificationOut(
        verification_id=str(verification.id),
        patch_id=str(record.id),
        status=verification.status,
        summary=verification.summary,
        failure_reasons=verification.failure_reasons,
        checks=verification.checks,
        before_results=verification.before_results,
        after_results=verification.after_results,
        comparison=verification.comparison,
        environment=verification.environment,
        duration_seconds=verification.duration_seconds,
        created_at=verification.created_at,
    )
