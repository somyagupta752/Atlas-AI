# Troubleshooting

Start with `dc ps`, `/ready` and `dc logs --since 15m api`. Find one request by its `request_id`. `dc` means
`docker compose -f docker-compose.prod.yml --env-file deploy.env`.

## The stack will not start

| Symptom | Cause | Fix |
| --- | --- | --- |
| `api` exits at once, log starts with a configuration error | Start-up validation refused the settings. It lists each problem and never prints secret values. | Fix each listed setting. Common: weak `auth_secret_key`, `SMTP_HOST`, `SMTP_FROM` or `PUBLIC_BASE_URL` missing, `PUBLIC_BASE_URL` not `https://`, remote database with `ENVIRONMENT=development`. |
| Need to run without email | Reset is on by default in staging and production | Set `AUTH_PASSWORD_RESET_ENABLED=false`. |
| `api` waits, `migrate` failed | A migration error | `dc logs migrate`. Do not edit the database by hand. Restore the pre-upgrade backup if the schema is half changed. |
| `config -q` fails on a variable | Required value missing in `deploy.env` | Set `REGISTRY`, `ATLAS_VERSION`, `WORKSPACE_HOST_DIR`, `REPOSITORIES_DIR`. |
| `postgres` unhealthy | Wrong volume permissions or password | `dc logs postgres`. Do not delete the volume. |

## `/ready` returns 503

Read `checks` in the response. It uses fixed words. `database`: check `postgres` and `database_url`. Graph store: check
`neo4j` (it is optional; empty `NEO4J_URI` turns it off). Worker backend: check `docker-proxy` and that the worker image exists
on the host.

## Sign-in and sessions

| Symptom | Cause | Fix |
| --- | --- | --- |
| Cannot sign in over http | The cookie is `Secure` | Serve the dashboard through the TLS proxy. |
| Everyone shares one login lockout | The api sees the proxy's address | Set `set_real_ip_from` in `frontend/nginx.conf` and make the proxy send `X-Forwarded-For`. |
| "Too many attempts" | Rate limit | Wait the seconds shown. Limits reset on api restart. |
| Signed out after every deploy | `auth_secret_key` changed | Expected once. Users sign in again. |
| Signed out right after sign-in | Clock skew or a proxy stripping the cookie | Check that `Path=/auth` cookies reach `/auth/*` and the host clock is correct. |
| Registration refused | `AUTH_REGISTRATION_OPEN=false` | Create the user with an operator account or reopen registration. |
| Page reload shows raw JSON | A page path collides with an API path | Page routes must not share a prefix with `/auth`, `/evaluations`, `/repositories`, `/debug`. A test guards this. |

## Password-reset email

1. `dc logs api | grep password_reset_email`. No line at all: reset is off, the account does not exist, or the rate limit hit.
2. `kind` in `password_reset_email_failed` says why:

| `kind` | Meaning | Fix |
| --- | --- | --- |
| `not_configured` | No `SMTP_HOST` or `SMTP_FROM` | Set them. |
| `connect` | Cannot reach the server | Port, firewall, DNS, `egress` network. |
| `timeout` | Server did not answer in `SMTP_TIMEOUT_SECONDS` | Check the server, raise the timeout. |
| `tls` | Bad certificate, wrong host name, or STARTTLS not offered | Use the right host name. Set `SMTP_CA_FILE` for a private CA. Match `SMTP_SECURITY` to the port (587 starttls, 465 ssl). |
| `auth` | Login refused | Check `SMTP_USERNAME` and the `smtp_password` file. |
| `rejected` | Sender or recipient refused (`smtp_code` has the code) | Verify the sender or domain with the provider. |
| `unexpected` | Programming or library error | Read `error_type`, then the api log around it. |

3. The link opens a form only with `#token=` in the URL. Links expire after 30 minutes and work once. A newer request voids older links.

## Repositories, indexing, investigations

| Symptom | Fix |
| --- | --- |
| 404 on a repository you expect to see | You are not a member. Ask an owner, or use `claim` as an operator. |
| 403 on an action | Your role is too low (viewer cannot edit, editor cannot manage members). |
| Index rejects the path | The path is outside `ALLOWED_REPOSITORY_ROOTS`. In Docker, repositories must be under `/srv/repositories`. |
| Investigation fails at once | `LLM_PROVIDER=disabled`, or `LLM_ALLOW_EXTERNAL=false`, or no `anthropic_api_key`. Check `llm_request_failed` in the log. |
| Results lack graph context | Neo4j is off or empty. Run `rebuild_graphs`. |

## Patching and evaluations

| Symptom | Fix |
| --- | --- |
| Worker backend not ready | Worker image missing on the host, or `docker-proxy` down. Build the image (see `docs/deployment.md`). |
| Patch test always fails with no output | Workspace directory not owned by uid 10001, or not mounted at the same path in api. |
| Worker killed, exit 137 | Memory limit. Raise the worker limits or reduce the test scope. |
| Containers pile up | `atlas_container_cleanup_failures_total` rising. Restart `api`: it removes labelled leftovers. Remove by hand only containers labelled for this instance. |
| Start evaluation refused | Another evaluation is active. Wait, or stop `api` to cancel it. |

## Backups

| Symptom | Fix |
| --- | --- |
| "backup or verification FAILED" in the backup log | `dc logs backup`. Check disk space, `backup_encryption_key`, `database_url`. |
| No backup newer than 26 hours | Treat as an incident (`incident-response.md`). Run a manual backup and verify. |
| Verify fails | Do not delete older backups. Keep them and investigate. |

## Collect for a bug report

Version (`dc images`), `/ready` output, `request_id`, the relevant log lines (check there are no secrets), and what you did.
