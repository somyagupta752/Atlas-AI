# Dashboard authentication and authorization

How a person signs in to the dashboard, what the browser keeps, and who may do what. Backend: `app/auth/`,
`app/core/security.py`. Frontend: `frontend/src/auth/`, `frontend/src/api.js`.

## The session

| Piece | Where it lives | Lifetime | Readable by page scripts |
| --- | --- | --- | --- |
| Access token (signed JWT, user id only) | JavaScript memory of the open tab (`auth/session.js`) | 15 minutes | yes, this tab only |
| Refresh token (random 256-bit) | `httpOnly` cookie, `SameSite=Strict`, `Secure`, `Path=/auth` | 14 days, single use | **no** |
| Refresh token at rest | SHA-256 hash in PostgreSQL | same | n/a |

Nothing is written to `localStorage`, `sessionStorage`, a readable cookie or the URL (a test asserts this after a sign-in).
Closing the tab forgets the access token. Reloading a page calls `POST /auth/refresh`, which turns the cookie into a new
access token, so the user stays signed in without a stored credential.

- **Rotation.** Every refresh spends the presented token and issues a new one in the same family.
- **Theft detection.** Presenting a token that was already spent revokes the whole family: every device from that login.
- **Concurrent tabs.** A token spent within the last 10 seconds, whose replacement has not been used, is treated as two tabs
  racing, not theft, and gets its own new token. After 10 seconds, or once the replacement has been used, it is theft.
  The trade-off: a thief who replays within 10 seconds of the real user, before the real user's next refresh, gets a live
  chain too. The next time either party refreshes after the window, the family is revoked.
- **Expiry.** A call that gets 401 triggers one renewal (shared by all simultaneous calls) and one retry. If renewal is
  refused the app returns to the sign-in page with "Your session has expired". If the server is merely unreachable the user
  stays signed in.
- **Sign out** revokes the family and clears the cookie. **Sign out everywhere** (`POST /auth/logout-all`) and **a password
  reset** also set `users.tokens_valid_after`: access tokens issued before that second stop working immediately instead of
  after up to 15 minutes. A plain sign-out does not invalidate the current access token before it expires; the browser
  discards it.
- **Rate limits** (per API process): registration, login (per client address and per account), refresh and reset.
  Failures answer 429 with `Retry-After`, which the sign-in page shows.
- **Passwords:** Argon2id. At least 12 characters. Login failures answer identically for unknown accounts and wrong passwords.
- **Reset:** the token travels in the URL **fragment** (`#token=`), which is never sent to a server or in a `Referer`
  header, and the page removes it from the address bar. The server stores only its hash; it is single-use and expires in 30
  minutes. Sending the message needs a mail sender, which does not exist yet (below).

## Redirects after sign-in

`/login?next=...` is attacker-controllable, and `react-router` 6.x has open-redirect advisories for untrusted targets.
`frontend/src/auth/safeRedirect.js` accepts only an in-app absolute path and rejects `//host`, `/\host`, encoded forms of
both, schemes, control characters and the sign-in pages themselves. 27 hostile inputs are tested.

## Roles

A repository exists in the registry once someone indexes it; the first user to index an identifier becomes its owner. A
user who is not a member gets **404**, the same answer as for a repository that does not exist. A member with too low a
role gets **403**.

| | Viewer | Editor | Owner |
| --- | --- | --- | --- |
| Index status (without server directory paths), search, graph, read diagnoses and patches | yes | yes | yes |
| Index and re-index, build the graph, start a diagnosis, generate and verify patches | no | yes | yes |
| Delete the index or graph, manage members | no | no | yes |

A repository always keeps at least one owner. An operator account (`is_admin`, created only with
`python -m app.auth.admin create-admin`) reaches every repository and can run the host benchmark endpoint when it is
enabled. Evaluation data (configurations, runs, baselines) belongs to the user who created it; imported history has no owner
and is readable by everyone signed in.

The dashboard shows or disables controls by role and says why a control is unavailable, but it is only a convenience: the
server checks the role on every request (`tests/auth/test_authorization.py` covers every repository endpoint for owner,
editor, viewer and outsider, and `test_every_operation_requires_authentication` walks the OpenAPI document so a new endpoint
cannot be forgotten).

## Tests

- Backend: `tests/auth/` (52 tests: registration, login, cookies, expired and forged tokens, rotation, concurrent refresh,
  theft detection, logout, logout-all, reset, rate limits, CORS, the role matrix, concealed 404s, evaluation isolation).
- Frontend: `frontend/src/**/*.test.*` (73 tests, jsdom): redirect guard, API client renewal and single-flight, route
  protection, sign-in and failures, storage hygiene, expiry, sign-out, registration, reset, role-based controls.
- Browser end-to-end (`frontend/e2e/`, 51 tests, `docs/e2e-testing.md`): the real frontend against the real API. Cookie flags
  and storage, protected routes, safe redirects, expiry and renewal, a lost cookie, a stolen token, two tabs, sign out
  everywhere across two devices, password reset ending sessions, owner/editor/viewer/outsider, and responsive layout.

## Known limits

- **No MFA**, no account lockout beyond the rate limit, no audit log of sign-ins yet.
- **No mail sender yet.** Reset tokens are only logged in `development` and `test` (the browser tests read them there). In staging and production the reset flow
  cannot deliver a message until a sender is implemented, so password reset is effectively unavailable there.
- **Rate limits are per process.** Run one API replica or enforce limits in the reverse proxy. Behind a TLS proxy, configure
  the client address (`docs/deployment.md`) or all users share one limit.
- **Registration is open by default.** Set `AUTH_REGISTRATION_OPEN=false` once your users exist.
- **Plain sign-out leaves the current access token valid** until it expires (at most 15 minutes) unless the user chooses
  "sign out everywhere".
- Two MODERATE `react-router` 6.x advisories remain until the v7 migration (`docs/ci-cd.md`).
