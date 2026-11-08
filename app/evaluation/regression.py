"""Regression detection against a baseline (Task 14.8). Pure logic over case data; thresholds are inputs.

A finding is raised only when a metric moved in the bad direction by more than its threshold, and every
finding names the metric, both counts, the threshold it crossed and the cases that explain it.
Only cases present in both runs are compared; a case missing from the candidate is reported as
missing and is never a failure. Findings on small samples carry a low-confidence warning, because a
change of a few cases is not statistical evidence.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.comparisons import SMALL_SAMPLE
from app.evaluation.metrics import (
    CaseData,
    compute_grouped,
    compute_metrics,
    metrics_by_name,
)


class RegressionThresholds(BaseModel):
    """Every limit in one place. Percentage-point values are on a 0-100 scale, percent values relative."""

    model_config = ConfigDict(extra="forbid")

    verified_rate_drop_pp: float = Field(default=10.0, ge=0, le=100)
    heldout_rate_drop_pp: float = Field(default=10.0, ge=0, le=100)
    regression_rate_increase_pp: float = Field(default=5.0, ge=0, le=100)
    category_verified_drop_pp: float = Field(default=20.0, ge=0, le=100)
    cost_per_repair_increase_pct: float = Field(default=25.0, ge=0, le=10_000)
    latency_increase_pct: float = Field(default=25.0, ge=0, le=10_000)


class IncompatibleBenchmarksError(ValueError):
    pass


def _key(c: CaseData) -> tuple[str, str]:
    return (c.case_id, c.variant)


def _finding(metric: str, scope: str, base, cand, delta: float, unit: str, threshold: float, supporting: list,
             message: str) -> dict[str, Any]:
    small = min(base.denominator, cand.denominator) < SMALL_SAMPLE
    return {
        "metric": metric, "scope": scope, "unit": unit,
        "baseline": {"numerator": base.numerator, "denominator": base.denominator, "value": base.value},
        "candidate": {"numerator": cand.numerator, "denominator": cand.denominator, "value": cand.value},
        "delta": delta, "threshold": threshold, "message": message,
        "supporting_case_ids": [list(k) for k in sorted(supporting)],
        "low_confidence": small,
        "warning": f"Small sample (n={int(min(base.denominator, cand.denominator))}); treat as a signal to "
                   "investigate, not as statistical evidence." if small else None,
    }


def detect_regressions(baseline_version: str, candidate_version: str, baseline_cases: list[CaseData],
                       candidate_cases: list[CaseData], thresholds: RegressionThresholds) -> dict[str, Any]:
    if baseline_version != candidate_version:
        raise IncompatibleBenchmarksError(
            f"Benchmark versions differ (baseline {baseline_version}, candidate {candidate_version}); "
            "results of different benchmarks are not comparable.")
    base_map = {_key(c): c for c in baseline_cases}
    cand_map = {_key(c): c for c in candidate_cases}
    shared = set(base_map) & set(cand_map)
    base_cases = [base_map[k] for k in sorted(shared)]
    cand_cases = [cand_map[k] for k in sorted(shared)]
    base, cand = (metrics_by_name(compute_metrics(x)) for x in (base_cases, cand_cases))
    findings: list[dict[str, Any]] = []

    def drop(metric: str, limit: float, scope: str, b, c, cases_b, cases_c, verified_field: str):
        if b.value is None or c.value is None:
            return
        delta = (c.value - b.value) * 100
        if -delta > limit:
            lost = [_key(x) for x in cases_b if getattr(x, verified_field) is True
                    and getattr(cases_c_map(cases_c).get(_key(x)), verified_field, None) is not True]
            findings.append(_finding(
                metric, scope, b, c, delta, "percentage_points", limit, lost,
                f"{metric} fell {-delta:.1f} percentage points ({b.numerator:g}/{b.denominator:g} to "
                f"{c.numerator:g}/{c.denominator:g}); the limit is {limit:g}."))

    def cases_c_map(cases):
        return {_key(x): x for x in cases}

    drop("final_verified_rate", thresholds.verified_rate_drop_pp, "overall", base["final_verified_rate"],
         cand["final_verified_rate"], base_cases, cand_cases, "final_verified")
    drop("heldout_pass_rate", thresholds.heldout_rate_drop_pp, "overall", base["heldout_pass_rate"],
         cand["heldout_pass_rate"], base_cases, cand_cases, "heldout_passed")

    b, c = base["regression_rate"], cand["regression_rate"]
    if b.value is not None and c.value is not None and (c.value - b.value) * 100 > thresholds.regression_rate_increase_pp:
        newly = [_key(x) for x in cand_cases if x.regression_detected_any
                 and not base_map[_key(x)].regression_detected_any]
        findings.append(_finding(
            "regression_rate", "overall", b, c, (c.value - b.value) * 100, "percentage_points",
            thresholds.regression_rate_increase_pp, newly,
            f"regression_rate rose {(c.value - b.value) * 100:.1f} percentage points ({b.numerator:g}/"
            f"{b.denominator:g} to {c.numerator:g}/{c.denominator:g}); the limit is "
            f"{thresholds.regression_rate_increase_pp:g}."))

    for metric, limit in (("cost_per_successful_repair", thresholds.cost_per_repair_increase_pct),
                          ("average_latency_seconds", thresholds.latency_increase_pct)):
        b, c = base[metric], cand[metric]
        if b.value and c.value is not None and (c.value / b.value - 1) * 100 > limit:
            pct = (c.value / b.value - 1) * 100
            findings.append(_finding(metric, "overall", b, c, pct, "percent", limit, [],
                                     f"{metric} rose {pct:.1f}% ({b.value:.4g} to {c.value:.4g}); the limit is {limit:g}%."))

    gb, gc = compute_grouped(base_cases, "category"), compute_grouped(cand_cases, "category")
    for key in sorted(set(gb) & set(gc)):
        label = key.removeprefix("category:")
        b, c = metrics_by_name(gb[key])["final_verified_rate"], metrics_by_name(gc[key])["final_verified_rate"]
        cat_b = [x for x in base_cases if (x.category or "unknown") == label]
        cat_c = [x for x in cand_cases if (x.category or "unknown") == label]
        drop("final_verified_rate", thresholds.category_verified_drop_pp, f"category:{label}", b, c, cat_b, cat_c,
             "final_verified")

    return {
        "status": "regressed" if findings else "ok",
        "findings": findings,
        "summary": ([f["message"] for f in findings] or ["No metric crossed its threshold."]),
        "compared_cases": len(shared),
        "missing_in_candidate": [list(k) for k in sorted(set(base_map) - set(cand_map))],
        "not_in_baseline": [list(k) for k in sorted(set(cand_map) - set(base_map))],
        "thresholds": thresholds.model_dump(),
        "sample_note": f"With fewer than {SMALL_SAMPLE} cases a crossed threshold is a signal, not proof.",
    }
