// Display helpers. A missing value is "n/a", never 0: unknown and zero are different facts.
export const RATE_METRICS = new Set([
  "diagnosis_accuracy", "patch_validity_rate", "patch_application_rate", "first_attempt_verified_rate",
  "final_verified_rate", "heldout_pass_rate", "exploratory_pass_rate", "regression_rate", "retry_success_rate",
  "success_rate", "completion_rate",
]);

export function pct(value) {
  return value === null || value === undefined ? "n/a" : `${(value * 100).toFixed(1)}%`;
}

export function money(value) {
  if (value === null || value === undefined) return "n/a";
  const n = Number(value);
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}

export function seconds(value) {
  return value === null || value === undefined ? "n/a" : `${Number(value).toFixed(1)} s`;
}

export function metricValue(m) {
  if (!m || m.value === null || m.value === undefined) return "n/a";
  if (RATE_METRICS.has(m.name)) return pct(m.value);
  if (m.name.includes("cost")) return money(m.value);
  if (m.name.includes("latency")) return seconds(m.value);
  return Number(m.value).toFixed(2);
}

// "3 / 8": the counts behind a number, always shown beside it.
export function counts(m) {
  return m ? `${m.numerator} / ${m.denominator}` : "";
}

export function tri(value) {
  if (value === null || value === undefined) return "unknown";
  return value ? "yes" : "no";
}

export function metricMap(list) {
  return Object.fromEntries((list || []).map((m) => [m.name, m]));
}

// The run with the best value among those that have one; null when nothing can be compared.
export function bestIndex(cells, higherIsBetter) {
  let best = null;
  cells.forEach((c, i) => {
    if (c.value === null || c.value === undefined) return;
    if (best === null || (higherIsBetter ? c.value > cells[best].value : c.value < cells[best].value)) best = i;
  });
  if (best === null) return null;
  const same = cells.filter((c) => c.value === cells[best].value).length;
  return same === cells.length ? null : best;
}
