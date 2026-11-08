// The browser side of the session.
//
// * The ACCESS token lives only in this module's memory. It is never written to localStorage, sessionStorage, a cookie
//   or the URL, so a script injected into the page cannot read a long-lived credential from storage, and closing the tab
//   forgets it.
// * The REFRESH token is an httpOnly cookie the server sets for /auth only. JavaScript cannot see it. On a page load the
//   app calls POST /auth/refresh to turn that cookie into a fresh access token.
// * Refreshing is single-flight: any number of callers that need a new token (React's double effect in development, a
//   burst of 401s) share one request. Two refreshes with the same cookie would look like token theft to the server.

let accessToken = null;
let inflight = null;
const listeners = new Set();

export const getToken = () => accessToken;
export const setToken = (token) => {
  accessToken = token || null;
};

// Called when the server says the session is over and cannot be renewed.
export function onSessionLost(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function sessionLost(reason = "expired") {
  accessToken = null;
  listeners.forEach((listener) => listener(reason));
}

// Resolves to { token, user } on success, { error: "unauthenticated" } when the server refuses (no cookie, expired,
// revoked), or { error: "unavailable" } when the server cannot be reached. Only the first means the user is signed out.
export function refreshSession() {
  if (!inflight) {
    inflight = (async () => {
      try {
        const response = await fetch("/auth/refresh", { method: "POST", credentials: "same-origin" });
        if (response.status === 401 || response.status === 403) {
          accessToken = null;
          return { error: "unauthenticated" };
        }
        if (!response.ok) return { error: "unavailable" };
        const body = await response.json();
        accessToken = body.access_token;
        return { token: body.access_token, user: body.user };
      } catch {
        return { error: "unavailable" };
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}
