"""Score debugging investigations against benchmark cases.

Expected answers live only here and in ``benchmarks/agent/cases.json``. The
agent receives the bug report and nothing else.

Metrics per case:

- root-cause accuracy: the reported root cause matches every keyword group
  (each group lists accepted synonyms). Deterministic, and deliberately
  coarse: an LLM judge can replace it later;
- evidence precision: share of cited evidence that points at expected files
  or symbols;
- evidence coverage: share of ``required_evidence`` the diagnosis contains;
- tool efficiency: tool calls and iterations;
- success: a validated ``completed`` diagnosis with evidence, a correct root
  cause and full coverage. A right sentence without evidence is not a success;
- latency and cost: wall-clock seconds and token cost.
"""

from statistics import mean

from pydantic import BaseModel, Field

from app.agents.llm import LLMUsage
from app.agents.schemas import RootCauseAnalysis

# USD per million tokens, Anthropic first-party list prices (checked 2026-10).
# Cache writes use the 5-minute rate (1.25x input).
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}


class DebuggingBenchmarkCase(BaseModel):
    case_id: str
    category: str | None = None
    repository_id: str
    repository_path: str
    bug_report: str
    expected_root_cause: str
    # All groups must match; any keyword inside a group matches that group.
    root_cause_keywords: list[list[str]] = Field(min_length=1)
    expected_files: list[str]
    expected_symbols: list[str]
    # "source_code:<file>" or "test:<file>": evidence of that type citing the file.
    # "execution:<text>": execution evidence citing a run_tests output containing the text.
    # "graph:<symbol>": graph evidence naming the symbol.
    required_evidence: list[str]


class CaseScore(BaseModel):
    case_id: str
    status: str
    root_cause_correct: bool
    evidence_precision: float | None
    evidence_coverage: float
    missing_evidence: list[str]
    valid_diagnosis: bool
    success: bool
    tool_calls: int
    iterations: int
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    stop_reason: str | None = None
    failure_reason: str | None = None


class BenchmarkSummary(BaseModel):
    cases: int
    success_rate: float
    root_cause_accuracy: float
    valid_diagnosis_rate: float
    mean_evidence_precision: float | None
    mean_evidence_coverage: float
    mean_tool_calls: float
    mean_iterations: float
    mean_duration_seconds: float
    total_cost_usd: float | None


def cost_usd(model: str | None, usage: LLMUsage) -> float | None:
    prices = PRICING.get(model or "")
    if prices is None:
        return None
    total = (
        usage.input_tokens * prices["input"]
        + usage.output_tokens * prices["output"]
        + usage.cache_read_input_tokens * prices["cache_read"]
        + usage.cache_creation_input_tokens * prices["cache_write"]
    )
    return round(total / 1_000_000, 6)


def root_cause_matches(root_cause: str | None, keyword_groups: list[list[str]]) -> bool:
    if not root_cause:
        return False
    text = root_cause.lower()
    return all(any(keyword.lower() in text for keyword in group) for group in keyword_groups)


def _symbol_expected(name: str | None, expected: list[str]) -> bool:
    if not name:
        return False
    return any(name == s or name.endswith("." + s) or s.endswith("." + name) for s in expected)


def evidence_precision(result: RootCauseAnalysis, case: DebuggingBenchmarkCase) -> float | None:
    if not result.evidence:
        return None
    relevant = sum(
        1
        for item in result.evidence
        if (item.file_path in case.expected_files) or _symbol_expected(item.qualified_name, case.expected_symbols)
    )
    return round(relevant / len(result.evidence), 4)


def missing_evidence(
    result: RootCauseAnalysis, case: DebuggingBenchmarkCase, observation_contents: dict[str, str]
) -> list[str]:
    missing = []
    for requirement in case.required_evidence:
        kind, _, target = requirement.partition(":")
        if kind in ("source_code", "test"):
            ok = any(e.source_type in ("source_code", "test") and e.file_path == target for e in result.evidence)
        elif kind == "execution":
            ok = any(
                e.source_type == "execution"
                and any(target in observation_contents.get(o, "") for o in e.observation_ids)
                for e in result.evidence
            )
        elif kind == "graph":
            ok = any(e.source_type == "graph" and _symbol_expected(e.qualified_name, [target]) for e in result.evidence)
        else:
            ok = False
        if not ok:
            missing.append(requirement)
    return missing


def score_case(
    case: DebuggingBenchmarkCase, result: RootCauseAnalysis, observation_contents: dict[str, str]
) -> CaseScore:
    missing = missing_evidence(result, case, observation_contents)
    coverage = round(1 - len(missing) / len(case.required_evidence), 4) if case.required_evidence else 1.0
    correct = result.status in ("completed", "limit_reached") and root_cause_matches(
        result.root_cause, case.root_cause_keywords
    )
    valid = result.status == "completed" and bool(result.evidence)
    return CaseScore(
        case_id=case.case_id,
        status=result.status,
        root_cause_correct=correct,
        evidence_precision=evidence_precision(result, case),
        evidence_coverage=coverage,
        missing_evidence=missing,
        valid_diagnosis=valid,
        success=valid and correct and not missing,
        tool_calls=result.tool_calls,
        iterations=result.iterations,
        duration_seconds=result.duration_seconds,
        input_tokens=result.usage.input_tokens + result.usage.cache_read_input_tokens + result.usage.cache_creation_input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=cost_usd(result.model, result.usage),
        stop_reason=str(result.stop_reason) if result.stop_reason else None,
        failure_reason=result.failure_reason,
    )


def summarize(scores: list[CaseScore]) -> BenchmarkSummary:
    if not scores:
        raise ValueError("no scores to summarise")
    precisions = [s.evidence_precision for s in scores if s.evidence_precision is not None]
    costs = [s.cost_usd for s in scores]
    return BenchmarkSummary(
        cases=len(scores),
        success_rate=round(mean(s.success for s in scores), 4),
        root_cause_accuracy=round(mean(s.root_cause_correct for s in scores), 4),
        valid_diagnosis_rate=round(mean(s.valid_diagnosis for s in scores), 4),
        mean_evidence_precision=round(mean(precisions), 4) if precisions else None,
        mean_evidence_coverage=round(mean(s.evidence_coverage for s in scores), 4),
        mean_tool_calls=round(mean(s.tool_calls for s in scores), 2),
        mean_iterations=round(mean(s.iterations for s in scores), 2),
        mean_duration_seconds=round(mean(s.duration_seconds for s in scores), 3),
        total_cost_usd=round(sum(c for c in costs if c is not None), 6) if all(c is not None for c in costs) else None,
    )
