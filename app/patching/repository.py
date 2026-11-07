"""PostgreSQL persistence for patch attempts and their verifications.

Transaction rules:

- each proposal row is committed as soon as it is generated, so a later crash
  still leaves the attempt on record;
- each status transition is checked (``check_patch_transition``), appended to
  ``status_history`` and committed on its own;
- a verification row and the proposal's final status are written in one
  transaction: either both exist or neither does;
- a failed commit is rolled back and raised as ``PatchPersistenceError``.
  Earlier commits are unaffected.

Every read can be scoped by repository ID. A patch from another repository is
reported as not found, never as forbidden, so IDs cannot be probed.

Nothing here stores credentials: diffs and test output were redacted upstream,
and only the model name, usage and prompt version are kept as provenance.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.repository import json_safe
from app.models import PatchProposalRecord, PatchVerificationRecord
from app.patching.models import (
    PatchProposal,
    PatchStatus,
    PatchValidationResult,
    PatchVerificationResult,
    check_patch_transition,
    utcnow,
)


class PatchNotFoundError(LookupError):
    pass


class PatchPersistenceError(RuntimeError):
    pass


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise PatchNotFoundError(value) from None


def _history_entry(status: str, note: str | None = None) -> dict[str, Any]:
    entry = {"status": status, "at": utcnow().isoformat()}
    if note:
        entry["note"] = note[:500]
    return entry


class PatchRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes ----------------------------------------------------------------------

    def create_proposal(self, proposal: PatchProposal, provenance: dict[str, Any]) -> PatchProposalRecord:
        record = PatchProposalRecord(
            id=uuid.UUID(proposal.patch_id),
            investigation_id=uuid.UUID(proposal.investigation_id),
            execution_id=uuid.UUID(proposal.execution_id),
            repository_id=proposal.repository_id,
            attempt_number=proposal.attempt_number,
            description=proposal.description,
            rationale=proposal.rationale,
            unified_diff=proposal.unified_diff.replace("\x00", ""),
            affected_files=json_safe(proposal.affected_files),
            assumptions=json_safe(proposal.assumptions),
            risks=json_safe(proposal.risks),
            status=str(PatchStatus.GENERATED),
            status_history=[_history_entry(PatchStatus.GENERATED)],
            provenance=json_safe(provenance),
        )
        self._session.add(record)
        self._commit()
        return record

    def transition(
        self,
        patch_id: str,
        new_status: PatchStatus,
        *,
        validation: PatchValidationResult | None = None,
        affected_files: list[str] | None = None,
        note: str | None = None,
    ) -> PatchProposalRecord:
        record = self._get(patch_id)
        check_patch_transition(record.status, new_status)
        self._apply_status(record, new_status, note)
        if validation is not None:
            record.validation_result = json_safe(validation.model_dump(mode="json"))
            record.diff_sha256 = validation.diff_sha256
        if affected_files is not None:
            record.affected_files = list(affected_files)
        self._commit()
        return record

    def record_verification(
        self, result: PatchVerificationResult, final_status: PatchStatus
    ) -> PatchVerificationRecord:
        """Insert the verification and set the proposal's final status atomically."""
        proposal = self._get(result.patch_id)
        check_patch_transition(proposal.status, final_status)
        self._apply_status(proposal, final_status, result.summary)
        record = PatchVerificationRecord(
            id=uuid.UUID(result.verification_id),
            patch_id=proposal.id,
            status=result.status,
            checks=json_safe([c.model_dump(mode="json") for c in result.checks]),
            before_results=json_safe(result.before_results),
            after_results=json_safe(result.after_results),
            comparison=json_safe(result.comparison.model_dump(mode="json")) if result.comparison else None,
            summary=result.summary,
            failure_reasons=json_safe(result.failure_reasons),
            environment=json_safe(result.environment),
            duration_seconds=result.duration_seconds,
        )
        self._session.add(record)
        self._commit()
        return record

    def mark_failed(self, patch_id: str, note: str) -> None:
        """Last resort after an internal error. Never raises."""
        try:
            record = self._get(patch_id)
            if PatchStatus.FAILED in _allowed(record.status):
                self._apply_status(record, PatchStatus.FAILED, note)
                self._commit()
        except Exception:  # noqa: BLE001 - the original error matters more
            self._session.rollback()

    # --- reads -----------------------------------------------------------------------

    def get_proposal(self, patch_id: str, repository_id: str | None = None) -> PatchProposalRecord:
        record = self._get(patch_id)
        if repository_id is not None and record.repository_id != repository_id:
            raise PatchNotFoundError(patch_id)
        return record

    def get_verification(self, patch_id: str) -> PatchVerificationRecord | None:
        return self._session.scalar(
            select(PatchVerificationRecord).where(PatchVerificationRecord.patch_id == _uuid(patch_id))
        )

    def list_for_investigation(
        self, investigation_id: str, repository_id: str | None = None, limit: int = 200
    ) -> list[PatchProposalRecord]:
        statement = (
            select(PatchProposalRecord)
            .where(PatchProposalRecord.investigation_id == _uuid(investigation_id))
            .order_by(PatchProposalRecord.created_at, PatchProposalRecord.execution_id,
                      PatchProposalRecord.attempt_number)
            .limit(limit)
        )
        if repository_id is not None:
            statement = statement.where(PatchProposalRecord.repository_id == repository_id)
        return list(self._session.scalars(statement))

    # --- internals -------------------------------------------------------------------

    def _get(self, patch_id: str) -> PatchProposalRecord:
        record = self._session.get(PatchProposalRecord, _uuid(patch_id))
        if record is None:
            raise PatchNotFoundError(patch_id)
        return record

    @staticmethod
    def _apply_status(record: PatchProposalRecord, status: PatchStatus, note: str | None) -> None:
        record.status = str(status)
        record.status_history = [*record.status_history, _history_entry(status, note)]
        record.updated_at = utcnow()

    def _commit(self) -> None:
        try:
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            raise PatchPersistenceError(f"Could not save patch data: {type(exc).__name__}") from exc


def _allowed(status: str) -> frozenset[PatchStatus]:
    from app.patching.models import PATCH_TRANSITIONS

    return PATCH_TRANSITIONS[PatchStatus(status)]
