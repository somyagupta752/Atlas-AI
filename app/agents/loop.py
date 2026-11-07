"""The agent loop: model turn, tool execution, observation, repeat.

One iteration is one LLM call. The loop sends the conversation and the
permitted tool schemas, then:

- tool calls: execute each one in order, append the assistant turn and one
  tool result per call (matched by call ID), and call the model again;
- text without tool calls: that is the final answer;
- anything else: handled explicitly (see below). The loop never runs forever.

Stopping conditions, all configurable through ``AgentLimits``:

- iterations: the last permitted iteration is a *final turn*: tools are
  forbidden and the model is told to answer now;
- tool calls: when the budget is spent, the next turn is a final turn;
- repeated identical calls: answered from the earlier observation without
  re-running, and after too many the loop moves to a final turn;
- consecutive responses whose calls were all rejected or repeated: final turn;
- empty responses: one nudge, then a final turn;
- total duration, cancellation, refusals and LLM errors: stop at once.

A final turn reached through a limit returns ``forced_final=True``, so the
caller can report ``limit_reached`` instead of pretending all went well.
"""

import json
import threading
from dataclasses import dataclass
from typing import Any

from app.agents.events import EventSink, EventType, NullEventSink
from app.agents.llm import LLMError, LLMProvider, LLMResponse, LLMTimeoutError, ToolCall
from app.agents.prompts import EMPTY_RESPONSE_NUDGE, final_turn_message
from app.agents.schemas import AgentLimits, StopReason
from app.agents.state import InvestigationState, Observation
from app.agents.tool_executor import ToolExecutor
from app.tools.base import ToolContext

# Calls with these outcomes cannot give a different answer when repeated verbatim.
_DEDUPLICATED = frozenset({"ok", "tool_error", "permission_denied", "invalid_arguments", "unknown_tool"})
_UNPRODUCTIVE = frozenset({"unknown_tool", "invalid_arguments", "permission_denied", "duplicate", "skipped"})
MAX_EVENT_TEXT = 8000


class CancellationToken:
    """Thread-safe cancellation flag, checked between steps."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass
class LoopOutcome:
    stop_reason: StopReason
    final_text: str | None = None
    # True when the answer came from a final turn forced by a limit.
    forced_final: bool = False
    error: LLMError | None = None


def call_key(call: ToolCall) -> str:
    try:
        arguments = json.dumps(call.arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        arguments = repr(call.arguments)
    return f"{call.name}\x00{arguments}"


def tool_message(observation: Observation) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": observation.tool_call_id,
        "name": observation.tool_name,
        "content": observation.content,
        "is_error": not observation.ok,
    }


class AgentLoop:
    def __init__(
        self,
        llm: LLMProvider,
        executor: ToolExecutor,
        limits: AgentLimits,
        events: EventSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._limits = limits
        self._events = events or NullEventSink()
        self._cancel = cancellation or CancellationToken()
        self._seen: dict[str, str] = {}  # call key -> observation ID
        self._repeats = 0

    # --- public ------------------------------------------------------------------

    def run(self, state: InvestigationState, context: ToolContext) -> LoopOutcome:
        limits = self._limits
        final_turn: StopReason | None = None
        unproductive_streak = 0
        empty_responses = 0

        while True:
            if self._cancel.cancelled:
                return self._stop(state, StopReason.CANCELLED)
            remaining = limits.max_duration_seconds - state.elapsed_seconds()
            if remaining <= 0:
                return self._stop(state, StopReason.TIMEOUT)
            if state.iteration >= limits.max_iterations:
                return self._stop(state, final_turn or StopReason.MAX_ITERATIONS)

            if final_turn is None:
                if state.iteration == limits.max_iterations - 1:
                    final_turn = self._begin_final_turn(state, StopReason.MAX_ITERATIONS)
                elif state.tool_calls >= limits.max_tool_calls:
                    final_turn = self._begin_final_turn(state, StopReason.MAX_TOOL_CALLS)

            try:
                response = self._llm.generate(
                    state.messages,
                    self._executor.schemas(),
                    timeout=remaining,
                    tool_choice="none" if final_turn else "auto",
                )
            except LLMError as exc:
                self._events.record(EventType.LLM_ERROR, {"iteration": state.iteration + 1, "kind": exc.kind, "message": str(exc)})
                if isinstance(exc, LLMTimeoutError) and state.elapsed_seconds() >= limits.max_duration_seconds:
                    return self._stop(state, StopReason.TIMEOUT, error=exc)
                return self._stop(state, StopReason.LLM_ERROR, error=exc)

            state.iteration += 1
            state.usage = state.usage + response.usage
            state.model = response.model or state.model
            self._record_response(state, response)

            if response.finish_reason == "refusal":
                return self._stop(state, StopReason.REFUSAL)

            if final_turn is not None:
                # Tool calls are ignored here; they are not appended, so no call is left unanswered.
                text = response.text if response.text and response.text.strip() else None
                if text:
                    state.messages.append({"role": "assistant", "content": text})
                return self._stop(state, final_turn, final_text=text, forced_final=text is not None)

            if response.is_empty:
                empty_responses += 1
                if empty_responses >= limits.max_empty_responses:
                    final_turn = self._limit(state, StopReason.EMPTY_RESPONSES)
                    if final_turn is None:
                        return self._stop(state, StopReason.EMPTY_RESPONSES)
                else:
                    self._warn(state, "empty_response")
                    state.messages.append({"role": "user", "content": EMPTY_RESPONSE_NUDGE})
                continue

            state.messages.append(response.to_message())
            if not response.has_tool_calls:
                return self._stop(state, StopReason.FINAL_ANSWER, final_text=response.text)

            productive = self._run_tool_calls(state, context, response.tool_calls)
            unproductive_streak = 0 if productive else unproductive_streak + 1

            unhealthy: StopReason | None = None
            if self._repeats > limits.max_repeated_tool_calls:
                unhealthy = StopReason.REPEATED_TOOL_CALLS
            elif unproductive_streak >= limits.max_consecutive_invalid_responses:
                unhealthy = StopReason.INVALID_TOOL_CALLS
            if unhealthy is not None:
                final_turn = self._limit(state, unhealthy)
                if final_turn is None:
                    return self._stop(state, unhealthy)

    # --- tool calls ------------------------------------------------------------------

    def _run_tool_calls(self, state: InvestigationState, context: ToolContext, calls: list[ToolCall]) -> bool:
        """Execute sequentially. Every call gets a result message. Returns whether any was productive."""
        productive = False
        for call in calls:
            observation = self._execute_one(state, context, call)
            state.add_observation(observation)
            if observation.status not in _UNPRODUCTIVE:
                productive = True
            self._events.record(
                EventType.TOOL_CALL,
                {"iteration": state.iteration, "tool_call_id": call.id, "tool": observation.tool_name, "arguments": observation.arguments},
            )
            self._events.record(EventType.OBSERVATION, observation.model_dump(mode="json", exclude={"data"}))
            state.messages.append(tool_message(observation))
        return productive

    def _execute_one(self, state: InvestigationState, context: ToolContext, call: ToolCall) -> Observation:
        if self._cancel.cancelled:
            return self._synthetic(state, call, "skipped", "Not executed: the investigation was cancelled")
        if state.elapsed_seconds() >= self._limits.max_duration_seconds:
            return self._synthetic(state, call, "skipped", "Not executed: the investigation time limit was reached")
        if state.tool_calls >= self._limits.max_tool_calls:
            return self._synthetic(state, call, "skipped", "Not executed: the tool call budget is spent")
        key = call_key(call)
        earlier = self._seen.get(key)
        if earlier is not None:
            self._repeats += 1
            return self._synthetic(
                state,
                call,
                "duplicate",
                f"Identical call already made; its result is unchanged. See observation {earlier}.",
                duplicate_of=earlier,
            )
        observation = self._executor.execute(call, context, state)
        if observation.status in _DEDUPLICATED:
            self._seen[key] = observation.id
        return observation

    @staticmethod
    def _synthetic(
        state: InvestigationState, call: ToolCall, status: str, message: str, duplicate_of: str | None = None
    ) -> Observation:
        observation_id = state.next_observation_id()
        content = json.dumps({"observation_id": observation_id, "tool": call.name, "status": status, "error": message})
        return Observation(
            id=observation_id,
            iteration=state.iteration,
            tool_call_id=call.id,
            tool_name=str(call.name)[:100],
            arguments=call.arguments if isinstance(call.arguments, dict) else str(call.arguments)[:1000],
            status=status,  # type: ignore[arg-type]
            error=message,
            content=content,
            duplicate_of=duplicate_of,
        )

    # --- helpers -------------------------------------------------------------------------

    def _begin_final_turn(self, state: InvestigationState, reason: StopReason) -> StopReason:
        self._warn(state, f"final_turn:{reason}")
        state.messages.append({"role": "user", "content": final_turn_message(reason)})
        return reason

    def _limit(self, state: InvestigationState, reason: StopReason) -> StopReason | None:
        """A loop-health limit was hit: give the model one final turn if allowed."""
        if not self._limits.finalize_on_limit or state.iteration >= self._limits.max_iterations:
            return None
        return self._begin_final_turn(state, reason)

    def _warn(self, state: InvestigationState, warning: str) -> None:
        self._events.record(EventType.LOOP_WARNING, {"iteration": state.iteration, "warning": warning})

    def _record_response(self, state: InvestigationState, response: LLMResponse) -> None:
        self._events.record(
            EventType.LLM_RESPONSE,
            {
                "iteration": state.iteration,
                "text": (response.text or "")[:MAX_EVENT_TEXT] or None,
                "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls],
                "finish_reason": response.finish_reason,
                "provider": response.provider,
                "model": response.model,
                "request_id": response.request_id,
                "usage": response.usage.model_dump(),
            },
        )

    def _stop(
        self,
        state: InvestigationState,
        reason: StopReason,
        *,
        final_text: str | None = None,
        forced_final: bool = False,
        error: LLMError | None = None,
    ) -> LoopOutcome:
        state.stop_reason = reason
        self._events.record(
            EventType.LOOP_STOPPED,
            {
                "stop_reason": str(reason),
                "iterations": state.iteration,
                "tool_calls": state.tool_calls,
                "forced_final": forced_final,
                "elapsed_seconds": round(state.elapsed_seconds(), 3),
            },
        )
        return LoopOutcome(stop_reason=reason, final_text=final_text, forced_final=forced_final, error=error)
