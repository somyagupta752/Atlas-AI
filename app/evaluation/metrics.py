"""Metrics calculated from stored evidence (Task 14.4).

Pure functions over plain records. No database, no HTTP, no model calls. Every
metric is ``numerator / denominator`` over an *explicit population*:

====================== ================================================== =======================================
metric                 numerator                                          denominator
====================== ================================================== =======================================
diagnosis_accuracy     cases with a correct diagnosis                     cases whose diagnosis was scored
patch_validity_rate    attempts whose patch was valid                     attempts that produced a patch
patch_application_rate valid patches that applied                         valid patches whose application is known
first_attempt_verified cases verified on attempt 1                        cases with an attempted patch
final_verified_rate    cases finally verified                             cases with an attempted patch
heldout_pass_rate      cases passing held-out tests                       cases with applicable held-out tests
exploratory_pass_rate  cases passing exploratory tests                    cases with applicable exploratory tests
regression_rate        applied attempts that broke a passing test         applied attempts with a known result
retry_success_rate     cases repaired after a failed first attempt        cases whose first attempt failed and
                                                                          that were retried
average_attempts       attempts of cases with an attempt                  cases with an attempted patch
average_latency        summed latency                                     cases with recorded latency
average_cost_per_case  summed cost                                        cases with recorded cost
cost_per_successful_.. summed cost                                        successful repairs
success_rate           cases verified, right files, held-out not failing  cases with an attempted patch
completion_rate        cases whose pipeline completed                     all selected cases
====================== ================================================== =======================================

A case or attempt field that is ``None`` is *unknown or not applicable* and is left out of
that metric's denominator. It is never counted as a failure and never as a success. A zero
denominator gives ``value = None``, not 0%. Cases that never ran stay in ``completion_rate``
and nowhere else.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.evaluation.environment import sha256_json

METRICS_VERSION = "1"

RATE_METRICS = (
    "diagnosis_accuracy", "patch_validity_rate", "patch_application_rate", "first_attempt_verified_rate",
    "final_verified_rate", "heldout_pass_rate", "exploratory_pass_rate", "regression_rate",
    "retry_success_rate", "success_rate", "completion_rate",
)
MEAN_METRICS = ("average_attempts", "average_latency_seconds", "average_cost_per_case")
COST_METRICS = ("total_cost_usd", "cost_per_successful_repair")
METRIC_NAMES = RATE_METRICS + MEAN_METRICS + COST_METRICS

GROUP_BY = ("category", "model", "retrieval_strategy", "test_visibility", "configuration", "run")


@dataclass(frozen=True)
class AttemptData:
    attempt_number: int
    status: str
    patch_valid: bool | None = None
    patch_applied: bool | None = None
    regression_detected: bool | None = None
    cost_usd: Decimal | float | None = None


@dataclass(frozen=True)
class CaseData:
    case_id: str
    status: str
    category: str | None = None
    variant: str = "live"
    diagnosis_correct: bool | None = None
    first_attempt_verified: bool | None = None
    final_verified: bool | None = None
    repaired_by_retry: bool | None = None
    success: bool | None = None
    heldout_passed: bool | None = None
    exploratory_passed: bool | None = None
    attempts_count: int = 0
    total_cost_usd: Decimal | float | None = None
    cost_kind: str | None = None
    total_latency_seconds: float | None = None
    attempts: Sequence[AttemptData] = ()
    regression_detected: bool | None = None
    # Run-level attributes, used only for grouping across runs.
    run_id: str | None = None
    configuration_id: str | None = None
    model_id: str | None = None
    retrieval_strategy: str | None = None
    test_visibility: str | None = None

    @property
    def regression_detected_any(self) -> bool:
        return bool(self.regression_detected) or any(a.regression_detected for a in self.attempts)


@dataclass(frozen=True)
class MetricResult:
    name: str
    numerator: float
    denominator: float
    value: float | None
    grouping_key: str | None = None
    # Why a number should be read with care: mixed or missing costs, for example.
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "numerator": self.numerator, "denominator": self.denominator,
               "value": self.value}
        if self.grouping_key is not None:
            out["grouping_key"] = self.grouping_key
        if self.notes:
            out["notes"] = self.notes
        return out


def _dec(value: Decimal | float) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _ratio(name: str, numerator: float, denominator: float, key: str | None, notes: dict | None = None
           ) -> MetricResult:
    value = None if denominator == 0 else numerator / denominator
    return MetricResult(name, numerator, denominator, value, key, notes or {})


def _money(total: Decimal) -> float:
    return float(round(total, 6))


def compute_metrics(cases: Iterable[CaseData], grouping_key: str | None = None) -> list[MetricResult]:
    """Every metric over ``cases``, one result each, in the order of ``METRIC_NAMES``."""
    cases = list(cases)
    attempts = [a for c in cases for a in c.attempts]
    r = lambda name, num, den, notes=None: _ratio(name, num, den, grouping_key, notes)

    scored = [c for c in cases if c.diagnosis_correct is not None]
    produced = [a for a in attempts if a.patch_valid is not None]
    valid = [a for a in attempts if a.patch_valid is True and a.patch_applied is not None]
    attempted = [c for c in cases if c.final_verified is not None]
    first = [c for c in cases if c.first_attempt_verified is not None]
    heldout = [c for c in cases if c.heldout_passed is not None]
    exploratory = [c for c in cases if c.exploratory_passed is not None]
    applied = [a for a in attempts if a.patch_applied is True and a.regression_detected is not None]
    retried = [c for c in cases if c.first_attempt_verified is False and c.attempts_count >= 2
               and c.repaired_by_retry is not None]
    succeeded = [c for c in cases if c.success is True]
    judged = [c for c in cases if c.success is not None]
    with_attempts = [c for c in cases if c.attempts_count >= 1]
    with_latency = [c for c in cases if c.total_latency_seconds is not None]
    with_cost = [c for c in cases if c.total_cost_usd is not None]

    total_cost = sum((_dec(c.total_cost_usd) for c in with_cost), Decimal(0))
    kinds = sorted({c.cost_kind or "unknown" for c in with_cost})
    cost_notes: dict[str, Any] = {}
    if kinds and kinds != ["actual"]:
        cost_notes["cost_kind"] = kinds[0] if len(kinds) == 1 else "mixed"
    missing_cost = [c.case_id for c in cases if c.total_cost_usd is None and (c.attempts_count or 0) > 0]
    if missing_cost:
        cost_notes["cases_missing_cost"] = len(missing_cost)

    results = [
        r("diagnosis_accuracy", sum(c.diagnosis_correct is True for c in scored), len(scored)),
        r("patch_validity_rate", sum(a.patch_valid is True for a in produced), len(produced)),
        r("patch_application_rate", sum(a.patch_applied is True for a in valid), len(valid)),
        r("first_attempt_verified_rate", sum(c.first_attempt_verified is True for c in first), len(first)),
        r("final_verified_rate", sum(c.final_verified is True for c in attempted), len(attempted)),
        r("heldout_pass_rate", sum(c.heldout_passed is True for c in heldout), len(heldout)),
        r("exploratory_pass_rate", sum(c.exploratory_passed is True for c in exploratory), len(exploratory)),
        r("regression_rate", sum(a.regression_detected is True for a in applied), len(applied)),
        r("retry_success_rate", sum(c.repaired_by_retry is True for c in retried), len(retried)),
        r("success_rate", len(succeeded), len(judged)),
        r("completion_rate", sum(c.status == "completed" for c in cases), len(cases)),
        r("average_attempts", sum(c.attempts_count for c in with_attempts), len(with_attempts)),
        r("average_latency_seconds", sum(c.total_latency_seconds for c in with_latency), len(with_latency)),
        r("average_cost_per_case", _money(total_cost), len(with_cost), cost_notes),
    ]
    results.append(MetricResult("total_cost_usd", _money(total_cost), len(with_cost),
                                _money(total_cost) if with_cost else None, grouping_key, cost_notes))
    results.append(r("cost_per_successful_repair", _money(total_cost), len(succeeded), cost_notes))
    return results


def metrics_by_name(results: Iterable[MetricResult]) -> dict[str, MetricResult]:
    return {m.name: m for m in results}


def group_key(case: CaseData, group_by: str) -> str:
    if group_by not in GROUP_BY:
        raise ValueError(f"unknown grouping {group_by!r}; use one of {GROUP_BY}")
    value = {
        "category": case.category, "model": case.model_id, "retrieval_strategy": case.retrieval_strategy,
        "test_visibility": case.test_visibility, "configuration": case.configuration_id, "run": case.run_id,
    }[group_by]
    return f"{group_by}:{value if value is not None else 'unknown'}"


def compute_grouped(cases: Iterable[CaseData], group_by: str) -> dict[str, list[MetricResult]]:
    """Metrics per group. A group's population is exactly its own cases, never the whole run's."""
    buckets: dict[str, list[CaseData]] = defaultdict(list)
    for case in cases:
        buckets[group_key(case, group_by)].append(case)
    return {key: compute_metrics(items, key) for key, items in sorted(buckets.items())}


def evidence_digest(cases: Iterable[CaseData]) -> str:
    """Fingerprint of the raw evidence that was scored. Equal digests mean the evidence did not change."""
    def attempt_row(a: AttemptData):
        return [a.attempt_number, a.status, a.patch_valid, a.patch_applied, a.regression_detected,
                None if a.cost_usd is None else str(_dec(a.cost_usd))]

    rows = [
        [c.case_id, c.variant, c.status, c.category, c.diagnosis_correct, c.first_attempt_verified,
         c.final_verified, c.repaired_by_retry, c.success, c.heldout_passed, c.exploratory_passed,
         c.attempts_count, None if c.total_cost_usd is None else str(_dec(c.total_cost_usd)), c.cost_kind,
         c.total_latency_seconds, [attempt_row(a) for a in sorted(c.attempts, key=lambda a: a.attempt_number)]]
        for c in sorted(cases, key=lambda c: (c.case_id, c.variant))
    ]
    return sha256_json(rows)


def solved(case: CaseData) -> bool | None:
    """Did the case end in a success? ``None``: not judged (never reached a verdict)."""
    return case.success


def summarize_cases(cases: Sequence[CaseData]) -> dict[str, int]:
    """Population counts shown next to the metrics so denominators are never hidden."""
    return {
        "selected": len(cases),
        "completed": sum(c.status == "completed" for c in cases),
        "failed": sum(c.status == "failed" for c in cases),
        "incomplete": sum(c.status in ("incomplete", "pending", "running") for c in cases),
        "with_attempted_patch": sum(c.final_verified is not None for c in cases),
    }


Selector = Callable[[CaseData], bool]
