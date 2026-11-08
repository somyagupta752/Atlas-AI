import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, request } from "./api";
import { getToken, onSessionLost, setToken } from "./auth/session";
import { fakeNetwork, json } from "./testing";

afterEach(() => vi.unstubAllGlobals());

describe("the API client and the session", () => {
  it("sends the in-memory access token as a bearer token", async () => {
    const net = fakeNetwork({ "GET /repositories": json(200, { items: [] }) });
    setToken("tok-abc");
    await api.repositories();
    expect(net.calls[0].headers.Authorization).toBe("Bearer tok-abc");
  });

  it("sends no Authorization header when there is no session", async () => {
    const net = fakeNetwork();
    await api.repositories();
    expect(net.calls[0].headers.Authorization).toBeUndefined();
  });

  it("renews an expired token once and repeats the request", async () => {
    let first = true;
    const net = fakeNetwork({
      "GET /repositories": (call) => {
        if (first) { first = false; return json(401, { detail: "Not authenticated" }); }
        return json(200, { items: [{ repository_id: "p", role: "owner" }], seen: call.headers.Authorization });
      },
      "POST /auth/refresh": json(200, { access_token: "tok-new", user: {} }),
    });
    setToken("tok-old");
    const result = await api.repositories();
    expect(result.seen).toBe("Bearer tok-new");
    expect(net.count("POST /auth/refresh")).toBe(1);
    expect(net.count("GET /repositories")).toBe(2);
  });

  it("ends the session when renewal is refused, without retrying in a loop", async () => {
    const net = fakeNetwork({
      "GET /repositories": json(401, { detail: "Not authenticated" }),
      "POST /auth/refresh": json(401, { detail: "Not signed in" }),
    });
    const lost = vi.fn();
    const stop = onSessionLost(lost);
    setToken("tok-old");
    await expect(api.repositories()).rejects.toMatchObject({ status: 401, message: expect.stringContaining("expired") });
    stop();
    expect(lost).toHaveBeenCalledWith("expired");
    expect(getToken()).toBeNull();
    expect(net.count("GET /repositories")).toBe(1); // not repeated
    expect(net.count("POST /auth/refresh")).toBe(1);
  });

  it("shares one renewal between simultaneous requests: two refreshes would look like token theft", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let renewed = false;
    const net = fakeNetwork({
      "GET /repositories": () => json(renewed ? 200 : 401, renewed ? { items: [] } : { detail: "expired" }),
      "GET /evaluations": () => json(renewed ? 200 : 401, renewed ? { items: [] } : { detail: "expired" }),
      "POST /auth/refresh": async () => { await gate; renewed = true; return json(200, { access_token: "tok-new", user: {} }); },
    });
    setToken("tok-old");
    const both = Promise.all([api.repositories(), api.runs(), api.repositories()]);
    await new Promise((r) => setTimeout(r, 20));
    release();
    await both;
    expect(net.count("POST /auth/refresh")).toBe(1);
  });

  it("does not treat a failed sign-in as an expired session", async () => {
    const net = fakeNetwork({ "POST /auth/login": json(401, { detail: "Incorrect email or password" }) });
    const lost = vi.fn();
    const stop = onSessionLost(lost);
    await expect(api.login("a@example.com", "wrong")).rejects.toMatchObject({ status: 401, message: "Incorrect email or password" });
    stop();
    expect(net.count("POST /auth/refresh")).toBe(0);
    expect(lost).not.toHaveBeenCalled();
  });

  it("does not end the session when the server is merely unreachable during renewal", async () => {
    fakeNetwork({
      "GET /repositories": json(401, { detail: "expired" }),
      "POST /auth/refresh": () => { throw new TypeError("network down"); },
    });
    const lost = vi.fn();
    const stop = onSessionLost(lost);
    setToken("tok");
    await expect(api.repositories()).rejects.toMatchObject({ status: 0 });
    stop();
    expect(lost).not.toHaveBeenCalled();
  });

  it("reports how long to wait when the server rate limits", async () => {
    fakeNetwork({ "POST /auth/login": json(429, { detail: "Too many attempts; try again later" }, { "Retry-After": "42" }) });
    await expect(api.login("a@example.com", "x")).rejects.toMatchObject({ status: 429, retryAfter: 42 });
  });

  it("turns a network failure into a readable error", async () => {
    vi.stubGlobal("fetch", async () => { throw new TypeError("offline"); });
    await expect(request("/repositories")).rejects.toBeInstanceOf(ApiError);
  });

  it("encodes identifiers placed in a path", async () => {
    const net = fakeNetwork({ "GET /repositories/a%2Fb/index/status": json(200, {}) });
    await api.indexStatus("a/b");
    expect(net.calls[0].url).toBe("/repositories/a%2Fb/index/status");
  });
});
