"""Compare evaluation runs (Task 14.5). Pure functions over stored case data.

Rules that keep a comparison honest:

* Only cases present in every run are compared case by case. A case missing from one run is
  listed as missing, never counted as a failure there.
* A case with no verdict (``success is None``) is *unjudged*: it is neither solved nor unsolved.
* Every difference shows both raw counts and the sample size. Below ``SMALL_SAMPLE`` cases the
  result carries a warning. This module never reports statistical significance: a handful of
  constructed benchmark cases cannot support it.
"""

from dataclasses import dataclass
from typing import Any

from app.evaluation.metrics import (
    CaseData,
    MetricResult,
    compute_grouped,
    compute_metrics,
    metrics_by_name,
)

SMALL_SAMPLE = 30

COMPARED_METRICS = (
    "diagnosis_accuracy", "patch_validity_rate", "first_attempt_verified_rate", "final_verified_rate",
    "heldout_pass_rate", "regression_rate", "retry_success_rate", "success_rate", "completion_rate",
    "average_attempts", "average_latency_seconds", "average_cost_per_case", "total_cost_usd",
    "cost_per_successful_repair",
)
# True: a bigger value is better. False: a smaller one is. Missing: no direction is claimed.
HIGHER_IS_BETTER = {
    "diagnosis_accuracy": True, "patch_validity_rate": True, "first_attempt_verified_rate": True,
    "final_verified_rate": True, "heldout_pass_rate": True, "regression_rate": False,
    "retry_success_rate": True, "success_rate": True, "completion_rate": True, "average_latency_seconds": False,
    "average_cost_per_case": False, "total_cost_usd": False, "cost_per_successful_repair": False,
}
CATEGORY_METRICS = ("final_verified_rate", "success_rate", "regression_rate", "heldout_pass_rate")


@dataclass(frozen=True)
class RunInput:
    run_id: str
    name: str
    status: str
    benchmark_version: str
    mode: str
    cases: list[CaseData]
    selected_case_ids: list[str]


def _cell(m: MetricResult | None) -> dict[str, Any]:
    if m is None:
        return {"numerator": 0, "denominator": 0, "value": None}
    cell = {"numerator": m.numerator, "denominator": m.denominator, "value": m.value}
    if m.notes:
        cell["notes"] = m.notes
    return cell


def metric_difference(name: str, base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Absolute and relative change from ``base`` to ``other``, with both sample sizes."""
    a, b = base["value"], other["value"]
    out: dict[str, Any] = {"sample_sizes": [base["denominator"], other["denominator"]]}
    if a is None or b is None:
        out.update(absolute=None, relative=None, direction=None,
                   note="not comparable: one side has no applicable cases")
        return out
    absolute = b - a
    out["absolute"] = absolute
    out["relative"] = None if a == 0 else absolute / a
    better = HIGHER_IS_BETTER.get(name)
    out["direction"] = None if better is None or absolute == 0 else (
        "better" if (absolute > 0) == better else "worse")
    if min(base["denominator"], other["denominator"]) < SMALL_SAMPLE:
        out["warning"] = (f"Small sample (n={int(min(base['denominator'], other['denominator']))}); "
                          "this difference is not statistically meaningful.")
    return out


def _key(c: CaseData) -> tuple[str, str]:
    return (c.case_id, c.variant)


def case_comparison(base: RunInput, other: RunInput) -> dict[str, Any]:
    a = {_key(c): c for c in base.cases}
    b = {_key(c): c for c in other.cases}
    shared = sorted(set(a) & set(b))
    out: dict[str, Any] = {
        "base_run_id": base.run_id, "other_run_id": other.run_id, "compared": len(shared),
        "both_solved": [], "only_base_solved": [], "only_other_solved": [], "neither_solved": [],
        "unjudged": [], "regression_in_base_only": [], "regression_in_other_only": [],
        "retries_in_base_only": [], "retries_in_other_only": [],
        "missing_in_other": [list(k) for k in sorted(set(a) - set(b))],
        "missing_in_base": [list(k) for k in sorted(set(b) - set(a))],
    }
    for k in shared:
        x, y = a[k], b[k]
        label = list(k)
        if x.success is None or y.success is None:
            out["unjudged"].append(label)
        elif x.success and y.success:
            out["both_solved"].append(label)
        elif x.success:
            out["only_base_solved"].append(label)
        elif y.success:
            out["only_other_solved"].append(label)
        else:
            out["neither_solved"].append(label)
        if x.regression_detected_any and not y.regression_detected_any:
            out["regression_in_base_only"].append(label)
        elif y.regression_detected_any and not x.regression_detected_any:
            out["regression_in_other_only"].append(label)
        if x.attempts_count > 1 and y.attempts_count == 1:
            out["retries_in_base_only"].append(label)
        elif y.attempts_count > 1 and x.attempts_count == 1:
            out["retries_in_other_only"].append(label)
    return out


def compare(runs: list[RunInput]) -> dict[str, Any]:
    if len(runs) < 2:
        raise ValueError("a comparison needs at least two runs")
    versions = sorted({r.benchmark_version for r in runs})
    case_sets = [{_key(c) for c in r.cases} for r in runs]
    shared = set.intersection(*case_sets)
    union = set.union(*case_sets)
    warnings: list[str] = []
    if len(versions) > 1:
        warnings.append("The runs used different benchmark versions (" + ", ".join(versions)
                        + "); results measure different things.")
    if len({r.mode for r in runs}) > 1:
        warnings.append("The runs mix reference and live modes; reference runs test the verifier, not a model.")
    if shared != union:
        warnings.append(f"The runs do not cover the same cases: {len(shared)} shared of {len(union)} in total. "
                        "Aggregate metrics cover each run's own cases; case-by-case comparison covers the shared ones.")
    if any(r.status != "completed" for r in runs):
        warnings.append("Some runs are not 'completed'; their metrics cover only the cases that ran.")
    per_run = [metrics_by_name(compute_metrics(r.cases)) for r in runs]
    metrics: dict[str, Any] = {}
    for name in COMPARED_METRICS:
        cells = [_cell(m.get(name)) for m in per_run]
        entry: dict[str, Any] = {"runs": cells,
                                 "differences_from_first": [metric_difference(name, cells[0], c) for c in cells[1:]]}
        metrics[name] = entry
    grouped = [compute_grouped(r.cases, "category") for r in runs]
    categories = sorted({k for g in grouped for k in g})
    by_category: dict[str, Any] = {}
    for key in categories:
        label = key.removeprefix("category:")
        by_category[label] = {}
        for name in CATEGORY_METRICS:
            cells = [_cell(metrics_by_name(g[key]).get(name)) if key in g else _cell(None) for g in grouped]
            by_category[label][name] = {
                "runs": cells, "differences_from_first": [metric_difference(name, cells[0], c) for c in cells[1:]]}
    return {
        "runs": [{"id": r.run_id, "name": r.name, "status": r.status, "benchmark_version": r.benchmark_version,
                  "mode": r.mode, "cases": len(r.cases), "link": f"/evaluations/{r.run_id}"} for r in runs],
        "compatible": len(versions) == 1,
        "benchmark_versions": versions,
        "warnings": warnings,
        "sample_note": f"Fewer than {SMALL_SAMPLE} cases cannot support a claim that one configuration is better; "
                       "read these as observations.",
        "metrics": metrics,
        "by_category": by_category,
        "cases": [case_comparison(runs[0], r) for r in runs[1:]],
    }
