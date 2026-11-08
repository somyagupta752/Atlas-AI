# Browser end-to-end tests

`frontend/e2e/` holds the Playwright suite (Chromium). It drives the **production build** of the dashboard against the
**real API** and a **real PostgreSQL database**. Sign-in, sessions, cookies, roles and password reset are never mocked.

```bash
cd frontend
npm ci
npx playwright install chromium        # once; CI uses --with-deps
npm run e2e                            # all specs; the harness starts and stops everything
npx playwright test e2e/tests/roles.spec.ts --workers=1 --headed     # one file, visibly
npm run e2e:report                     # open the last HTML report (traces and screenshots of failures)
```

It needs a reachable PostgreSQL server (the project's `DATABASE_URL`, locally from `.env`) and `uv` on the PATH.

## What runs

```
Playwright --> vite preview (production bundle, port 4173) --proxy--> real API (uvicorn, port 8765) --> atlas_e2e_<random>
```

`frontend/e2e/support/run_api.py` is the web server Playwright starts. It creates a database named `atlas_e2e_<random>` on the
server `DATABASE_URL` points to (same credentials, never the same database), migrates it, runs the API, and **drops the
database when it is stopped**. The API runs with: test environment, a known signing key, plain-http cookies, five attempts per
rate-limit window, **no Neo4j, no model provider, no key**. Nothing in a run can reach the development database, the development
Neo4j or a paid service. Every statement that creates or drops a database passes `checked_name`, which refuses any name not
starting with `atlas_e2e_` (a CI test asserts it exists).

## Cleanup, in three layers

1. Tests use unique accounts (`...@e2e.example.test`) and repositories.
2. After the last test, `global-teardown.ts` deletes everything the tests created and **fails the run** if any table still holds
   rows (it once caught a cascade it had disabled).
3. When the API server stops, the wrapper drops the whole database. If the process that started it is killed without any chance
   to clean up, a watchdog notices the parent has gone and tears down by itself; a later run also sweeps abandoned databases.

## What is covered

| Spec | Journeys |
| --- | --- |
| `auth.spec.ts` (20) | Registration; cookie flags (`httpOnly`, `SameSite=Strict`, `Path=/auth`, ~14 days) and no readable credential in storage, `document.cookie` or the URL; the cookie is never sent with data requests; duplicate and weak passwords; wrong password equals unknown account; rate limit message with the wait time; protected pages and `?next=`; reload keeps the session; deep links survive a reload; 8 hostile post-sign-in targets; sign out (cookie gone, Back button, old cookie dead on the server) |
| `session.spec.ts` (8) | Expired token renewed once and the request repeated; wrong-key and garbage tokens refused; lost cookie gives "session expired" and recovery to the same page; unreachable server is not reported as expiry; stolen spent token rejected and ends the login; two tabs refreshing at once; sign out everywhere across two devices, immediately |
| `reset.spec.ts` (7) | Same answer for known and unknown address; the whole journey; existing sessions end; single use; expired and made-up links give one message; server password policy; rate limit |
| `roles.spec.ts` (7) | Owner, editor, viewer and outsider each in their own browser: controls, concealed 404, every sensitive endpoint refused by the server, role changes without re-sign-in, last-owner rule, no takeover by indexing an existing name, evaluation data private to its creator |
| `evaluation.spec.ts` (3) | Empty states; a real reference run from the form to its metrics and case evidence; another account cannot open the run |
| `responsive.spec.ts` (6) | 375, 768 and 1440 px: no sideways scrolling, controls reachable |

A fixture fails any test whose page logs a console error or raises a page error (the browser's own report of an HTTP error status
is ignored: several tests provoke 401s on purpose).

## How the tests stay independent

- Each test gets its own client address (`X-Forwarded-For`, trusted by the test API from the local proxy), so the per-address
  rate limits of one test cannot affect another.
- The API allows **one active evaluation at a time**, so `evaluation.spec.ts` runs its tests serially.
- A token timing out is simulated by swapping the request's `Authorization` header for a genuinely signed, already expired token;
  the server verifies it for real. Waiting out a 10-second window is replaced by ageing the token rows in the throwaway database.

## Does the suite catch regressions? Checked.

Five deliberate breakages, each detected: a script-readable refresh cookie, access tokens surviving "sign out everywhere", a
replayed refresh token no longer ending the login, missing role checks, and an open redirect. Stable across 1, 3 and 6 workers and
repeated runs (51 tests, about 20 seconds).

## Bugs the suite found while it was being written

- Page `/repositories` shared its path with the API, so a reload showed JSON. Pages moved to `/repos`; a test now forbids any page
  route from sharing a prefix with a proxied API path.
- A reset link pasted into a tab already on the reset page did nothing (only the fragment changes, nothing remounted).
- The header's user menu overflowed a phone-width screen.
- The `Sign out everywhere` control did not exist in the UI.

## Limits

- Chromium only. Firefox and WebKit are not run.
- **The reset token is read from the API's log** (written only in the test environment) until the mail sender exists. Then
  `support/mail.ts` will read the message from a local mail-capture service and the API will stop logging tokens.
- The suite proves the dashboard works with the API as built; it does not exercise the container workers, Neo4j or a model.
- Run on GitHub Actions only through `ci.yml`'s `e2e` job, which has not run there yet.

## Reset email

The API sends reset email over SMTP to `frontend/e2e/support/mail_server.py`, a capture service Playwright starts (SMTP on
127.0.0.1:2525, JSON on :8026). `e2e/support/mail.ts` reads messages from it. The API logs no token, so the capture service is the
only source. Delivery runs after the HTTP response; the helpers poll, and "no message" checks wait a short settle period.
