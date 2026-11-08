"""Structured JSON logging with correlation IDs and redaction (Task 15.5).

Every line is one JSON object. ``request_id`` and ``workflow_id`` come from context variables, so any log call
made while handling a request, or inside a debugging workflow or an evaluation, carries them without the caller
passing them along. Threads started for a workflow must copy the context (``run_in_context``).

Redaction is applied to every record, whoever wrote it:
* credential-shaped strings and the live values of configured secrets are replaced;
* fields that hold source code, diffs or free text from users are dropped to a length marker;
* long strings are truncated.
"""

import contextvars
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.tools.base import REDACTED, redact_secrets

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
workflow_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("workflow_id", default=None)

T = TypeVar("T")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")
# Field names whose values may contain a customer's code or free text: never logged, only their size.
SENSITIVE_FIELDS = frozenset({
    "source", "code", "content", "diff", "unified_diff", "patch", "bug_report", "prompt", "messages", "response_text",
    "stdout", "stderr", "output", "body", "authorization", "cookie", "password", "token", "api_key",
    "refresh_token", "access_token", "secret",
})
MAX_STRING = 500
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
_secret_values: list[str] = []


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def new_workflow_id() -> str:
    return "wf_" + uuid.uuid4().hex[:16]


def accept_request_id(candidate: str | None) -> str:
    """A caller-supplied ID is kept only if it is a short safe token (it ends up in logs and headers)."""
    return candidate if candidate and SAFE_ID.match(candidate) else new_request_id()


def set_workflow(workflow_id: str | None) -> contextvars.Token:
    return workflow_id_var.set(workflow_id)


def run_in_context[T](fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap ``fn`` so a thread runs it with the caller's request and workflow IDs."""
    context = contextvars.copy_context()

    def wrapper(*args: Any, **kwargs: Any) -> T:
        return context.run(fn, *args, **kwargs)

    return wrapper


def register_secret_values(*values: str | None) -> None:
    """Exact secret strings to scrub from every log line (the API key, the signing key, passwords)."""
    for v in values:
        if v and len(v) >= 8 and v not in _secret_values:
            _secret_values.append(v)


def scrub(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_FIELDS:
        size = len(value) if isinstance(value, (str, bytes, list, tuple, dict)) else None
        return f"{REDACTED} (size={size})" if size is not None else REDACTED
    if isinstance(value, str):
        text = redact_secrets(value)
        for secret in _secret_values:
            text = text.replace(secret, REDACTED)
        return text if len(text) <= MAX_STRING else text[:MAX_STRING] + f"...[{len(text) - MAX_STRING} more]"
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in list(value)[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or scrub(record.getMessage()),
            "request_id": request_id_var.get(),
            "workflow_id": workflow_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in ("event", "request_id", "workflow_id") and not key.startswith("_"):
                payload[key] = scrub(value, key)
        if getattr(record, "event", None):
            message = scrub(record.getMessage())
            if message and message != payload["event"]:
                payload["message"] = message
        if record.exc_info and record.exc_info[0] is not None:
            # The exception type and where it happened; the message can contain data, so it is scrubbed.
            payload["exception"] = {"type": record.exc_info[0].__name__, "message": scrub(str(record.exc_info[1]))}
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get() or "-"
        return f"{record.levelname:<7} {record.name} [{rid}] {scrub(record.getMessage())}"


def configure_logging(*, json_logs: bool, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Access logs would repeat the request line without IDs; the request middleware logs it with them.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """One structured event: ``log_event(log, "patch_verification_completed", outcome="verified", duration_ms=42)``."""
    logger.log(level, event, extra={"event": event, **fields})
