// Thin client for the Atlas AI API. Every number the dashboard shows comes from here.
//
// Every request carries the in-memory access token. A 401 on a normal call means the token expired: the client renews it
// once (single-flight, see session.js) and repeats the request. If renewal is refused, the session is over, listeners are
// told, and the caller gets an error. Calls under /auth never trigger renewal: there a 401 means "wrong credentials".
import { getToken, refreshSession, sessionLost } from "./auth/session";

export class ApiError extends Error {
  constructor(status, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function describe(detail) {
  if (!detail) return "The request failed";
  if (typeof detail === "string") return detail;
  if (detail.message) return detail.message;
  if (Array.isArray(detail)) return detail.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ");
  return JSON.stringify(detail);
}

async function send(path, options) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  try {
    return await fetch(path, {
      ...options,
      headers,
      credentials: "same-origin",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError(0, "The API is not reachable");
  }
}

export async function request(path, options = {}) {
  let response = await send(path, options);
  if (response.status === 401 && !path.startsWith("/auth/")) {
    const renewed = await refreshSession();
    if (renewed.token) {
      response = await send(path, options);
    } else if (renewed.error === "unauthenticated") {
      sessionLost("expired");
      throw new ApiError(401, "Your session has expired. Sign in again.");
    } else {
      throw new ApiError(0, "The API is not reachable");
    }
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const retry = response.headers && response.headers.get ? response.headers.get("Retry-After") : null;
    throw new ApiError(response.status, describe(data && data.detail), retry ? Number(retry) : null);
  }
  return data;
}

const post = (path, body) => request(path, { method: "POST", body });
const enc = encodeURIComponent;

export const api = {
  // session
  register: (email, password) => post("/auth/register", { email, password }),
  login: (email, password) => post("/auth/login", { email, password }),
  logout: () => request("/auth/logout", { method: "POST" }),
  logoutEverywhere: () => request("/auth/logout-all", { method: "POST" }),
  requestReset: (email) => post("/auth/password-reset", { email }),
  confirmReset: (token, newPassword) => post("/auth/password-reset/confirm", { token, new_password: newPassword }),
  // repositories
  repositories: () => request("/repositories"),
  repository: (id) => request(`/repositories/${enc(id)}`),
  indexStatus: (id) => request(`/repositories/${enc(id)}/index/status`),
  indexRepository: (id, repositoryPath) => post(`/repositories/${enc(id)}/index`, { repository_path: repositoryPath }),
  search: (id, query, limit = 10) => post(`/repositories/${enc(id)}/search`, { query, limit }),
  members: (id) => request(`/repositories/${enc(id)}/members`),
  setMember: (id, email, role) => request(`/repositories/${enc(id)}/members`, { method: "PUT", body: { email, role } }),
  removeMember: (id, userId) => request(`/repositories/${enc(id)}/members/${enc(userId)}`, { method: "DELETE" }),
  // evaluations
  runs: (params = {}) => request(`/evaluations?${new URLSearchParams(params)}`),
  run: (id) => request(`/evaluations/${id}`),
  cases: (id, params = {}) => request(`/evaluations/${id}/cases?${new URLSearchParams({ limit: 100, ...params })}`),
  caseDetail: (id, caseId, variant) =>
    request(`/evaluations/${id}/cases/${enc(caseId)}${variant ? `?variant=${enc(variant)}` : ""}`),
  metrics: (id) => request(`/evaluations/${id}/metrics`),
  compare: (ids) => post("/evaluations/compare", { evaluation_run_ids: ids }),
  configurations: () => request("/evaluations/configurations?limit=100"),
  benchmarks: () => request("/evaluations/benchmarks"),
  start: (body) => post("/evaluations/run", body),
  cancel: (id) => request(`/evaluations/${id}/cancel`, { method: "POST" }),
  baselines: () => request("/evaluations/baselines?limit=100"),
  createBaseline: (id) => post("/evaluations/baselines", { evaluation_run_id: id }),
  regressionCheck: (id, baselineId) => post(`/evaluations/${id}/regression-check`, { baseline_id: baselineId }),
};
