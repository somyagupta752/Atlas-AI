// Test helpers: a fake network and a way to render the real app at a URL.
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";

export const USER = { id: "u-1", email: "ann@example.com", is_active: true, is_admin: false };

export function json(status, body, headers = {}) {
  return { status, ok: status >= 200 && status < 300, headers: { get: (n) => headers[n] ?? headers[n.toLowerCase()] ?? null },
    text: async () => (body === undefined ? "" : JSON.stringify(body)), json: async () => body };
}

// routes: { "POST /auth/login": (call) => json(...) | static response }. Anything unmatched is a 404 so a test notices.
export function fakeNetwork(routes = {}) {
  const calls = [];
  const defaults = {
    "GET /evaluations": json(200, { items: [], page: { limit: 25, offset: 0, total: 0 } }),
    "GET /repositories": json(200, { items: [] }),
  };
  const table = { ...defaults, ...routes };
  const fetchMock = vi.fn(async (path, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    const url = String(path).split("?")[0];
    const call = { method, path: String(path), url, headers: options.headers || {}, body: options.body ? JSON.parse(options.body) : undefined };
    calls.push(call);
    const handler = table[`${method} ${url}`];
    if (handler === undefined) return json(404, { detail: `no fake for ${method} ${url}` });
    return typeof handler === "function" ? handler(call, calls) : handler;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock, count: (key) => calls.filter((c) => `${c.method} ${c.url}` === key).length };
}

export function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

export const signedIn = (extra = {}) => ({
  "POST /auth/refresh": json(200, { access_token: "tok-1", user: USER }),
  ...extra,
});
export const signedOut = (extra = {}) => ({ "POST /auth/refresh": json(401, { detail: "Not signed in" }), ...extra });
