"""PostgreSQL persistence for investigations and their event timelines.

Transaction rules:

- ``create`` commits the investigation row before the agent loop starts, so a
  crash later still leaves a record.
- Each event is committed on its own. Earlier events survive a later failure.
- ``finish`` writes the final status and result in one transaction.

Every read is scoped by investigation ID (and, when given, repository ID). No
query can return another investigation's events.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.schemas import InvestigationStatus, RootCauseAnalysis, check_transition
from app.agents.state import InvestigationState
from app.models import InvestigationEventRecord, InvestigationRecord

logger = logging.getLogger(__name__)


class InvestigationNotFoundError(Exception):
    pass


def json_safe(value: Any) -> Any:
    """JSON-serialisable copy. PostgreSQL JSONB rejects NUL characters, so they are dropped."""
    return json.loads(json.dumps(value, default=str).replace("\\u0000", ""))


class InvestigationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes ----------------------------------------------------------------------

    def create(self, state: InvestigationState) -> InvestigationRecord:
        record = InvestigationRecord(
            id=uuid.UUID(state.investigation_id),
            repository_id=state.repository_id,
            bug_report=state.bug_report,
            status=str(InvestigationStatus.RUNNING),
        )
        self._session.add(record)
        self._commit()
        return record

    def append_event(self, investigation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        key = uuid.UUID(investigation_id)
        # One writer per investigation: the next sequence is the current count + 1.
        sequence = self._session.scalar(
            select(func.coalesce(func.max(InvestigationEventRecord.sequence), 0)).where(
                InvestigationEventRecord.investigation_id == key
            )
        )
        self._session.add(
            InvestigationEventRecord(
                investigation_id=key,
                sequence=int(sequence or 0) + 1,
                event_type=event_type,
                payload=json_safe(payload),
            )
        )
        self._commit()

    def finish(self, result: RootCauseAnalysis) -> InvestigationRecord:
        record = self._get(result.investigation_id)
        check_transition(InvestigationStatus(record.status), InvestigationStatus(result.status))
        record.status = result.status
        record.summary = result.summary
        record.root_cause = result.root_cause
        record.confidence = result.confidence
        record.result = json_safe(result.model_dump(mode="json"))
        record.completed_at = datetime.now(UTC)
        self._commit()
        return record

    def mark_failed(self, investigation_id: str, summary: str, failure_reason: str) -> None:
        """Last-resort update when the service itself failed. Keeps every event."""
        record = self._get(investigation_id)
        if InvestigationStatus(record.status) != InvestigationStatus.RUNNING:
            return
        record.status = str(InvestigationStatus.FAILED)
        record.summary = summary
        record.result = {"failure_reason": failure_reason}
        record.completed_at = datetime.now(UTC)
        self._commit()

    # --- reads -----------------------------------------------------------------------

    def get(self, investigation_id: str, repository_id: str | None = None) -> InvestigationRecord:
        record = self._get(investigation_id)
        if repository_id is not None and record.repository_id != repository_id:
            raise InvestigationNotFoundError(investigation_id)
        return record

    def list_events(
        self, investigation_id: str, event_types: list[str] | None = None, limit: int = 500
    ) -> list[InvestigationEventRecord]:
        statement = (
            select(InvestigationEventRecord)
            .where(InvestigationEventRecord.investigation_id == uuid.UUID(investigation_id))
            .order_by(InvestigationEventRecord.sequence)
            .limit(limit)
        )
        if event_types:
            statement = statement.where(InvestigationEventRecord.event_type.in_(event_types))
        return list(self._session.scalars(statement))

    # --- internals -------------------------------------------------------------------

    def _get(self, investigation_id: str) -> InvestigationRecord:
        try:
            key = uuid.UUID(str(investigation_id))
        except ValueError:
            raise InvestigationNotFoundError(investigation_id) from None
        record = self._session.get(InvestigationRecord, key)
        if record is None:
            raise InvestigationNotFoundError(investigation_id)
        return record

    def _commit(self) -> None:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise


class PersistentEventSink:
    """An ``EventSink`` that commits each event. A failed write is logged, not fatal."""

    def __init__(self, repository: InvestigationRepository, investigation_id: str) -> None:
        self._repository = repository
        self._investigation_id = investigation_id
        self.dropped = 0

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._repository.append_event(self._investigation_id, event_type, payload)
        except Exception as exc:  # noqa: BLE001 - losing an audit event must not kill the run
            self.dropped += 1
            logger.error("Could not persist %s event: %s", event_type, type(exc).__name__)
