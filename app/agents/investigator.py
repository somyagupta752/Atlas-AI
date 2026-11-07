"""Run one investigation: agent loop, then diagnosis validation, repair and status.

Status rules (``schemas.InvestigationStatus``):

- final answer that validates: the draft's ``completed`` or ``inconclusive``;
- final answer after a limit forced the last turn: ``limit_reached``, keeping
  whatever the validated answer contains, and saying which limit;
- limit without a usable answer (including the duration limit): ``limit_reached``;
- cancelled: ``cancelled``;
- refusal, LLM error, or an answer still invalid after repair: ``failed``.

An invalid final answer gets ``limits.diagnosis_repair_attempts`` chances to
be corrected. Each is one extra model call, with tools forbidden.
"""

import logging

from app.agents.diagnosis import (
    LIMIT_DESCRIPTIONS,
    DiagnosisError,
    build_analysis,
    parse_draft,
    validate_draft,
)
from app.agents.events import EventSink, EventType, NullEventSink
from app.agents.llm import LLMError, LLMProvider
from app.agents.loop import AgentLoop, CancellationToken, LoopOutcome
from app.agents.prompts import diagnosis_repair_message
from app.agents.schemas import (
    LIMIT_STOP_REASONS,
    AgentLimits,
    DiagnosisDraft,
    Hypothesis,
    InvestigationStatus,
    RootCauseAnalysis,
    StopReason,
)
from app.agents.state import InvestigationState
from app.agents.tool_executor import ToolExecutor
from app.tools.base import ToolContext

logger = logging.getLogger(__name__)


class Investigator:
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
        self._cancel = cancellation

    def run(self, state: InvestigationState, context: ToolContext) -> RootCauseAnalysis:
        loop = AgentLoop(self._llm, self._executor, self._limits, self._events, self._cancel)
        outcome = loop.run(state, context)
        analysis = self._conclude(state, outcome)
        state.transition(InvestigationStatus(analysis.status))
        state.hypotheses = analysis.hypotheses
        state.evidence = analysis.evidence
        return analysis

    # --- conclusions ---------------------------------------------------------------

    def _conclude(self, state: InvestigationState, outcome: LoopOutcome) -> RootCauseAnalysis:
        reason = outcome.stop_reason
        if reason == StopReason.CANCELLED:
            return build_analysis(state, status="cancelled", summary="The investigation was cancelled.")
        if reason == StopReason.REFUSAL:
            return build_analysis(
                state,
                status="failed",
                summary="The model declined to continue the investigation.",
                failure_reason="refusal",
            )
        if reason == StopReason.LLM_ERROR:
            kind = outcome.error.kind if outcome.error else "provider_error"
            return build_analysis(
                state,
                status="failed",
                summary="The investigation failed because the language model could not be reached or answered invalidly.",
                failure_reason=f"llm_{kind}",
            )

        is_limit = reason in LIMIT_STOP_REASONS
        if outcome.final_text is None:
            # A limit stopped the loop before any answer. Observations are preserved.
            description = LIMIT_DESCRIPTIONS.get(reason, str(reason))
            return build_analysis(
                state,
                status="limit_reached",
                summary=f"The investigation reached {description} before producing a diagnosis.",
                extra_questions=["The investigation stopped at a limit; the root cause was not determined."],
            )

        draft, hypotheses, errors = self._validated(state, outcome.final_text)
        if draft is None or errors:
            for _ in range(self._limits.diagnosis_repair_attempts):
                if not self._can_repair(state):
                    break
                repaired_text = self._repair(state, errors)
                if repaired_text is None:
                    break
                draft, hypotheses, errors = self._validated(state, repaired_text)
                if draft is not None and not errors:
                    break

        if draft is None or errors:
            status = "limit_reached" if is_limit else "failed"
            return build_analysis(
                state,
                status=status,
                summary="The final diagnosis did not pass validation, so no root cause is reported.",
                failure_reason="invalid_diagnosis",
                extra_questions=[f"Validation: {e}" for e in errors[:10]],
            )

        self._record_diagnosis(draft, hypotheses)
        if is_limit:
            description = LIMIT_DESCRIPTIONS.get(reason, str(reason))
            return build_analysis(
                state,
                status="limit_reached",
                draft=draft,
                hypotheses=hypotheses,
                summary=f"Stopped at {description}. {draft.summary}",
                extra_questions=[f"The investigation stopped at {description}; the conclusion may be incomplete."],
            )
        return build_analysis(state, status=draft.status, draft=draft, hypotheses=hypotheses)

    def _validated(
        self, state: InvestigationState, text: str
    ) -> tuple[DiagnosisDraft | None, list[Hypothesis], list[str]]:
        try:
            draft = parse_draft(text)
        except DiagnosisError as exc:
            self._events.record(EventType.DIAGNOSIS_INVALID, {"stage": "parse", "errors": exc.errors})
            return None, [], exc.errors
        hypotheses, errors = validate_draft(draft, state)
        if errors:
            self._events.record(EventType.DIAGNOSIS_INVALID, {"stage": "validation", "errors": errors})
        return draft, hypotheses, errors

    def _can_repair(self, state: InvestigationState) -> bool:
        if self._cancel is not None and self._cancel.cancelled:
            return False
        return state.elapsed_seconds() < self._limits.max_duration_seconds

    def _repair(self, state: InvestigationState, errors: list[str]) -> str | None:
        state.messages.append({"role": "user", "content": diagnosis_repair_message(errors)})
        remaining = self._limits.max_duration_seconds - state.elapsed_seconds()
        try:
            response = self._llm.generate(
                state.messages, self._executor.schemas(), timeout=remaining, tool_choice="none"
            )
        except LLMError as exc:
            self._events.record(EventType.LLM_ERROR, {"stage": "repair", "kind": exc.kind, "message": str(exc)})
            return None
        state.iteration += 1
        state.usage = state.usage + response.usage
        self._events.record(
            EventType.LLM_RESPONSE,
            {
                "iteration": state.iteration,
                "stage": "repair",
                "text": (response.text or "")[:8000] or None,
                "tool_calls": [],
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(),
            },
        )
        if not response.text or not response.text.strip():
            return None
        state.messages.append({"role": "assistant", "content": response.text})
        return response.text

    def _record_diagnosis(self, draft: DiagnosisDraft, hypotheses: list[Hypothesis]) -> None:
        for hypothesis in hypotheses:
            self._events.record(EventType.HYPOTHESIS, hypothesis.model_dump(mode="json"))
        for item in draft.evidence:
            self._events.record(EventType.EVIDENCE, item.model_dump(mode="json"))
