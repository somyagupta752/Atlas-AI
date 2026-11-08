# Staging deployment plan (Task 15.9)

**Status: plan only. Nothing has been deployed.** Do not treat anything here as done until a person has run it and recorded
the result. Staging is the first end-to-end run of this stack on a real host (`docs/deployment.md` says so). It is also not a
production-readiness claim.

## Preconditions (gates)

| Gate | Needed for | State |
| --- | --- | --- |
| GitHub CI green on `main` (lint, backend, frontend, docker, e2e, security, CodeQL) | Anything | Not run yet |
| Images built from that commit and pushed to your registry with a version tag | Anything | Not done |
| Dedicated staging host or VM with nothing of value, Docker Engine + Compose v2 | Anything | Not provisioned |
| TLS proxy and a DNS name for staging | Sign-in (cookie is `Secure`) | Not done |
| SMTP account (provider sandbox is fine) and a verified sender | Password reset | Not done |
| Rootless worker validation V1-V10 passed on a Linux VM (`docs/security/worker-validation-plan.md`) | **Running any untrusted repository** | Not done |

Without the last gate, staging may run only repositories you wrote or trust fully. Do not point it at third-party code.

## Staging rules

- Use a separate registry tag, secrets, database and domain from production. Never share secrets.
- Set `ATLAS_ENVIRONMENT=staging`, `AUTH_REGISTRATION_OPEN=false` after the first operator exists, `LLM_ALLOW_EXTERNAL` as needed
  with a spend-capped provider key.
- Use synthetic data only.

## Steps

1. **Host.** Create the workspace and repository directories (`docs/deployment.md`, prerequisites). Build the worker image on the host.
2. **Secrets.** Create each file in `SECRETS_DIR` with mode 600 or stricter. Create `smtp_password` (empty if the relay needs no login).
3. **Configure.** Copy `deploy.env.example` to `deploy.env`. Set `REGISTRY`, `ATLAS_VERSION`, `WORKSPACE_HOST_DIR`, `REPOSITORIES_DIR`,
   `SMTP_HOST`, `SMTP_FROM`, `PUBLIC_BASE_URL`.
4. **Validate.** `dc config -q` prints nothing on success. The API refuses to start with an incomplete mail or auth setting, which is intended.
5. **Start.** `dc pull`, then `dc up -d`, then `dc ps` and `curl -fsS http://127.0.0.1:8080/ready`.
6. **Operator.** Create the first operator with `python -m app.auth.admin create-admin`.
7. **Smoke test** (below).
8. **Backup and restore drill.** Take a backup, verify it, restore into a scratch database, compare table counts.
9. **Upgrade and roll back drill.** Deploy a second tag, check `/ready`, roll back to the first.
10. **Record** results in the table at the end.

## Smoke test

| Check | Expected |
| --- | --- |
| `/health`, `/ready` through the TLS proxy | 200 and `ready` |
| Register or create a user, sign in, reload the page | Stays signed in |
| Sign out, press Back | Locked, no data shown |
| Password reset to a real inbox | Email arrives, link works once, old sessions end |
| Unknown address on reset | Same message, no email |
| Create a repository, index a small trusted one | Indexed, appears in the list |
| Two users: member vs outsider | Member sees it, outsider gets 404 |
| Investigate (provider key set) | Result stored, `llm_request_completed` logged |
| Patch with test run on a trusted repository | Verification result stored, worker container removed |
| `/metrics` with and without the token | 200 with token, 404 without |
| Logs | JSON, no tokens, reset links or passwords (grep for `#token=`) |
| Stop and start `api` | No orphan worker containers |

## Verification from outside

From a machine that is not the host, confirm that only the TLS proxy port is open. PostgreSQL, Neo4j, the Docker proxy and
port 8080 must not be reachable from the network. Confirm that `/docs` and `/openapi.json` return 404.

## Exit criteria for staging

All smoke checks pass, backup/restore and upgrade/roll back drills pass with recorded timings, no unexplained errors in seven
days of logs, alerts exist for the metrics in `docs/operations.md`, backups are copied off the host. Even then, production
requires the rootless validation gate and a separate decision.

## Results log

| Date | Tag | Who | Result | Notes |
| --- | --- | --- | --- | --- |
| | | | | |
