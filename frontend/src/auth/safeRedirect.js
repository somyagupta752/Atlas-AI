// The login page sends the user back to where they were headed (?next=...). That value comes from a URL anyone can craft,
// so it is only ever used if it is an in-app path. Anything else - another site, a protocol-relative URL, a backslash trick
// that browsers treat as a slash, control characters - falls back to the default.
//
// This matters beyond hygiene: react-router 6.x has open-redirect advisories for untrusted targets, and this is the only
// place the app passes a URL-derived target to navigate().
const ALLOWED = /^\/[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#[\]]*$/;

function decodeOnce(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

export function safeRedirect(next, fallback = "/") {
  if (typeof next !== "string" || next.length === 0 || next.length > 2000) return fallback;
  if (!ALLOWED.test(next)) return fallback;
  // "//host" and "/\host" are protocol-relative to a browser; so are their percent-encoded forms.
  for (const candidate of [next, decodeOnce(next)]) {
    if (candidate === null) return fallback;
    if (/^[/\\]{2}/.test(candidate) || candidate.includes("\\") || /^\/[/\\]/.test(candidate)) return fallback;
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f\u007f]/.test(candidate)) return fallback;
  }
  // Never bounce between the auth pages.
  if (/^\/(login|register|reset-password)(\/|\?|#|$)/.test(next)) return fallback;
  return next;
}
