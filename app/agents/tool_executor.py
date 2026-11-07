"""Turn one model-requested tool call into a validated, bounded execution.

The model's tool name and arguments are untrusted input. For every call:

1. Check the tool is registered and permitted for this investigation.
2. Validate the arguments (types, required fields, no extra fields).
3. Run it with a time limit.
4. Capture the result or the error as a structured ``Observation``.
5. Bound the text returned to the model.

A failing tool becomes an observation the model can react to. It never raises
into the loop.
"""

import json
import logging
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from app.agents.llm import ToolCall
from app.agents.state import InvestigationState, Observation, ObservationStatus
from app.agents.tool_registry import (
    InvalidToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
)
from app.tools.base import (
    ToolContext,
    ToolExecutionError,
    ToolInputError,
    ToolOutput,
    ToolPermissionError,
    truncate_text,
)

logger = logging.getLogger(__name__)

# Worker threads for in-process tools. Python cannot kill a thread: on timeout the
# observation is returned at once and the worker finishes (or hits its own
# cooperative deadline) in the background. run_tests is a subprocess with its own
# hard timeout.
_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="atlas-tool")
TIMEOUT_GRACE_SECONDS = 2.0


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, allowed_tools: Iterable[str] | None = None) -> None:
        self._registry = registry
        names = set(registry.names)
        self.allowed_tools = frozenset(names if allowed_tools is None else set(allowed_tools) & names)

    def schemas(self) -> list[dict[str, Any]]:
        return self._registry.get_schemas(self.allowed_tools)

    def execute(self, call: ToolCall, context: ToolContext, state: InvestigationState) -> Observation:
        observation_id = state.next_observation_id()
        started = time.monotonic()

        def observe(status: ObservationStatus, *, output: ToolOutput | None = None, error: str | None = None) -> Observation:
            return self._observation(observation_id, state.iteration, call, status, output, error, started, context)

        if call.name not in self.allowed_tools:
            try:
                self._registry.get(call.name)
                return observe("permission_denied", error=f"Tool {call.name!r} is not permitted in this investigation")
            except UnknownToolError:
                available = ", ".join(sorted(self.allowed_tools))
                return observe("unknown_tool", error=f"Unknown tool {str(call.name)[:100]!r}. Available: {available}")
        try:
            tool, arguments = self._registry.validate(call.name, call.arguments)
        except InvalidToolArgumentsError as exc:
            return observe("invalid_arguments", error=str(exc))

        context.deadline = time.monotonic() + context.tool_timeout_seconds
        future = _WORKERS.submit(tool.execute, arguments, context)
        try:
            output = future.result(timeout=context.tool_timeout_seconds + TIMEOUT_GRACE_SECONDS)
        except FutureTimeout:
            return observe("timeout", error=f"{call.name} exceeded {context.tool_timeout_seconds:g}s")
        except ToolPermissionError as exc:
            return observe("permission_denied", error=str(exc))
        except ToolInputError as exc:
            return observe("tool_error", error=str(exc))
        except ToolExecutionError as exc:
            status: ObservationStatus = "timeout" if "time limit" in str(exc) else "tool_error"
            return observe(status, error=str(exc))
        except Exception as exc:
            # Only the type is logged and returned: messages may contain paths or data.
            logger.exception("Tool %s raised %s", call.name, type(exc).__name__)
            return observe("internal_error", error=f"{call.name} failed with an internal error ({type(exc).__name__})")
        # The deadline is left in place: a timed-out worker still sees it and stops.
        if not isinstance(output, ToolOutput):
            return observe("internal_error", error=f"{call.name} returned an invalid result")
        return observe("ok", output=output)

    def _observation(
        self,
        observation_id: str,
        iteration: int,
        call: ToolCall,
        status: ObservationStatus,
        output: ToolOutput | None,
        error: str | None,
        started: float,
        context: ToolContext,
    ) -> Observation:
        envelope: dict[str, Any] = {"observation_id": observation_id, "tool": call.name, "status": status}
        if output is not None:
            envelope["result"] = output.data
        else:
            envelope["error"] = error
        content = json.dumps(envelope, ensure_ascii=False, default=str)
        content, truncated = truncate_text(content, context.max_output_bytes)
        locations = output.locations if output is not None else []
        if truncated:
            # Evidence may only cite what the model actually saw.
            locations = [loc for loc in locations if loc.file_path in content]
        return Observation(
            id=observation_id,
            iteration=iteration,
            tool_call_id=call.id,
            tool_name=str(call.name)[:100],
            arguments=_bounded(call.arguments),
            status=status,
            data=output.data if output is not None and not truncated else None,
            error=error,
            content=content,
            truncated=truncated,
            duration_seconds=round(time.monotonic() - started, 4),
            locations=locations,
        )


def _bounded(arguments: Any, limit: int = 4000) -> Any:
    """Keep stored arguments small. Oversized ones are stored as truncated text."""
    try:
        text = json.dumps(arguments, default=str)
    except (TypeError, ValueError):
        return repr(arguments)[:limit]
    return arguments if len(text) <= limit else text[:limit] + "...[truncated]"
