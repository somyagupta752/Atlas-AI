"""The investigation timeline. The loop reports events; a sink stores them.

Event payloads hold what is needed to audit and replay a run: model text,
requested tool calls, tool results as returned to the model, and stop
reasons. They never hold credentials. Repository content appears only where a
tool returned it to the model.
"""

import threading
from typing import Any, Protocol


class EventType:
    INVESTIGATION_CREATED = "investigation_created"
    LLM_RESPONSE = "llm_response"
    LLM_ERROR = "llm_error"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    LOOP_WARNING = "loop_warning"
    LOOP_STOPPED = "loop_stopped"
    DIAGNOSIS_INVALID = "diagnosis_invalid"
    HYPOTHESIS = "hypothesis"
    EVIDENCE = "evidence"
    DIAGNOSIS_COMPLETED = "diagnosis_completed"
    INVESTIGATION_FAILED = "investigation_failed"
    STATUS_CHANGED = "status_changed"


class EventSink(Protocol):
    def record(self, event_type: str, payload: dict[str, Any]) -> None: ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


class NullEventSink:
    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        return None
