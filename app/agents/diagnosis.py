"""Parse the model's final answer and validate it into a ``RootCauseAnalysis``.

Nothing the model says is accepted on its word:

- malformed JSON or unknown fields fail parsing;
- evidence must be grounded in real observations (``evidence.py``);
- hypothesis statuses must agree with their evidence (``hypotheses.py``);
- a ``completed`` diagnosis needs a root cause backed by a supported
  hypothesis with source-code, test or execution evidence;
- ``tests_run`` is built from run_tests observations, never from the answer.

A draft that fails is never turned into a successful diagnosis.
"""

import json
import re
from typing import Any

from pydantic import ValidationError

from app.agents.evidence import evidence_files, validate_all
from app.agents.hypotheses import reconcile
from app.agents.schemas import (
    DiagnosisDraft,
    Evidence,
    Hypothesis,
    RootCauseAnalysis,
    StopReason,
    TestRunRecord,
)
from app.agents.state import InvestigationState

# Evidence types strong enough to back a completed diagnosis on their own.
STRONG_EVIDENCE = frozenset({"source_code", "test", "execution"})
_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class DiagnosisError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def extract_json(text: str | None) -> dict[str, Any]:
    if not text or not text.strip():
        raise DiagnosisError(["The final answer is empty"])
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise DiagnosisError(["The final answer contains no JSON object"])
        candidate = candidate[start : end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise DiagnosisError([f"The final answer is not valid JSON: {exc.msg} at position {exc.pos}"]) from None
    if not isinstance(value, dict):
        raise DiagnosisError(["The final answer must be a JSON object"])
    return value


def parse_draft(text: str | None) -> DiagnosisDraft:
    data = extract_json(text)
    try:
        return DiagnosisDraft.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc']) or 'diagnosis'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise DiagnosisError(errors[:20]) from None


def validate_draft(draft: DiagnosisDraft, state: InvestigationState) -> tuple[list[Hypothesis], list[str]]:
    """Return the reconciled hypotheses and every validation error."""
    errors = validate_all(draft.evidence, state)
    hypotheses, hypothesis_errors = reconcile(draft.hypotheses, draft.evidence)
    errors.extend(hypothesis_errors)

    cited_files = evidence_files(draft.evidence)
    for path in draft.affected_files:
        if path not in cited_files:
            errors.append(f"Affected file {path} is not backed by any evidence item")

    if draft.status == "completed":
        if not draft.root_cause or not draft.root_cause.strip():
            errors.append("A completed diagnosis needs a root_cause")
        by_id = {item.id: item for item in draft.evidence}
        backed = [
            h
            for h in hypotheses
            if h.status == "supported"
            and any(by_id.get(e) and by_id[e].source_type in STRONG_EVIDENCE for e in h.supporting_evidence)
        ]
        if not backed:
            errors.append(
                "A completed diagnosis needs a supported hypothesis backed by source_code, test or execution evidence"
            )
    elif draft.root_cause is not None:
        errors.append("An inconclusive diagnosis must have root_cause null")
    return hypotheses, errors


def tests_run_from(state: InvestigationState) -> list[TestRunRecord]:
    records = []
    for observation in state.observations:
        if observation.tool_name != "run_tests" or not observation.ok or observation.data is None:
            continue
        data = observation.data
        records.append(
            TestRunRecord(
                observation_id=observation.id,
                test_path=data["test_path"],
                exit_code=data["exit_code"],
                timed_out=data["timed_out"],
                duration_seconds=data["duration_seconds"],
                outcome=data["outcome"],
                summary=data.get("summary"),
            )
        )
    return records


LIMIT_DESCRIPTIONS = {
    StopReason.MAX_ITERATIONS: "the maximum number of reasoning iterations",
    StopReason.MAX_TOOL_CALLS: "the maximum number of tool calls",
    StopReason.TIMEOUT: "the maximum investigation duration",
    StopReason.REPEATED_TOOL_CALLS: "too many repeated tool calls",
    StopReason.INVALID_TOOL_CALLS: "too many rejected tool calls",
    StopReason.EMPTY_RESPONSES: "too many empty model responses",
}


def build_analysis(
    state: InvestigationState,
    *,
    status: str,
    draft: DiagnosisDraft | None = None,
    hypotheses: list[Hypothesis] | None = None,
    summary: str | None = None,
    failure_reason: str | None = None,
    extra_questions: list[str] | None = None,
) -> RootCauseAnalysis:
    evidence: list[Evidence] = list(draft.evidence) if draft else []
    questions = list(draft.unresolved_questions) if draft else []
    questions.extend(extra_questions or [])
    root_cause = draft.root_cause if draft and status in ("completed", "limit_reached") else None
    return RootCauseAnalysis(
        investigation_id=state.investigation_id,
        repository_id=state.repository_id,
        status=status,  # type: ignore[arg-type]
        summary=summary or (draft.summary if draft else "No diagnosis was produced"),
        root_cause=root_cause,
        confidence=draft.confidence if draft and status != "failed" else 0.0,
        hypotheses=hypotheses if hypotheses is not None else (list(draft.hypotheses) if draft else []),
        evidence=evidence,
        affected_files=list(draft.affected_files) if draft else [],
        suggested_fix=draft.suggested_fix if draft else None,
        tests_run=tests_run_from(state),
        unresolved_questions=questions,
        stop_reason=state.stop_reason,
        failure_reason=failure_reason,
        iterations=state.iteration,
        tool_calls=state.tool_calls,
        duration_seconds=round(state.elapsed_seconds(), 3),
        model=state.model,
        usage=state.usage,
    )
