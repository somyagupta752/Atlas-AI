// Thin client for the Atlas AI evaluation API. Every number the dashboard shows comes from here.
export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function describe(detail) {
  if (!detail) return "The request failed";
  if (typeof detail === "string") return detail;
  if (detail.message) return detail.message;
  if (Array.isArray(detail)) return detail.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ");
  return JSON.stringify(detail);
}

export async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError(0, "The API is not reachable");
  }
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new ApiError(response.status, describe(data && data.detail));
  return data;
}

export const api = {
  runs: (params = {}) => request(`/evaluations?${new URLSearchParams(params)}`),
  run: (id) => request(`/evaluations/${id}`),
  cases: (id, params = {}) => request(`/evaluations/${id}/cases?${new URLSearchParams({ limit: 100, ...params })}`),
  caseDetail: (id, caseId, variant) =>
    request(`/evaluations/${id}/cases/${encodeURIComponent(caseId)}${variant ? `?variant=${encodeURIComponent(variant)}` : ""}`),
  metrics: (id) => request(`/evaluations/${id}/metrics`),
  compare: (ids) => request("/evaluations/compare", { method: "POST", body: { evaluation_run_ids: ids } }),
  configurations: () => request("/evaluations/configurations?limit=100"),
  benchmarks: () => request("/evaluations/benchmarks"),
  start: (body) => request("/evaluations/run", { method: "POST", body }),
  cancel: (id) => request(`/evaluations/${id}/cancel`, { method: "POST" }),
  baselines: () => request("/evaluations/baselines?limit=100"),
  createBaseline: (id) => request("/evaluations/baselines", { method: "POST", body: { evaluation_run_id: id } }),
  regressionCheck: (id, baselineId) =>
    request(`/evaluations/${id}/regression-check`, { method: "POST", body: { baseline_id: baselineId } }),
};
