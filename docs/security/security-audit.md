# Security audit

Scope: the repository at the start of Task 15. Method: reading the code, enumerating routes from the
OpenAPI document, and writing regression tests (`tests/security/`). This is not a penetration test.

## Verdict

**Not deployable yet.** C1, C2 and H1 were fixed in 15.2. H2 (local test backend default) is fixed by 15.3.

## Findings

Severity: Critical = anonymous user can spend money, read or destroy data, or run code. High = serious
under realistic conditions. Medium = needs another weakness. Low = hardening.

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| C1 | Critical | None of the 36 endpoints authenticates. Anyone who can reach the port can start paid model calls (`POST /debug/investigate`, `/debug/investigations/{id}/patches`, `POST /evaluations/run`), delete indexes and graphs, and read all results. | **Fixed in 15.2.** Every operation except the public auth routes and `/health` requires a bearer token; `tests/auth/test_authorization.py::test_every_operation_requires_authentication` iterates the OpenAPI document. |
| C2 | Critical | No repository ownership. `repository_id` is one global namespace, so any caller can read, overwrite or delete another user's index, graph, diagnoses and patches. | **Fixed in 15.2.** Repositories are registered with owner/editor/viewer memberships; non-members get 404, low roles 403; evaluation data is scoped to its creator. Tested for every repository endpoint and role. |
| H1 | High | `POST /benchmarks/run` runs pytest on the host. The case list is a fixed, committed allowlist, but the endpoint is reachable anonymously and executes in the API's own process context. | **Fixed in 15.2.** Requires an operator account and `BENCHMARKS_ENDPOINT_ENABLED=true`; always 404 in production. |
| H2 | High | `PATCH_TEST_BACKEND` defaults to `local`, a child process of the API with the API's privileges. A deployment that adds a repository to `AGENT_TRUSTED_TEST_ROOTS` runs its code on the host. | **Open. Fix: 15.3 (production requires the container backend and rejects trusted roots)** |
| H3 | High | The API process can reach the Docker daemon, which is root-equivalent on the host. A bug in the API gives full host control. | **Open until validated.** The socket is not mounted in the API; a filtering proxy sits in between, which narrows verbs but not request contents. Decision: rootless daemon, dedicated account, dedicated VM (`rootless-worker-design.md`); acceptance is the staging plan (`worker-validation-plan.md`). Blocks hostile repositories. |
| M1 | Medium | The request size limit covered only `/debug`. Other write endpoints accepted bodies of any size. | **Fixed in 15.1.** Applies to every write method; tested. |
| M2 | Medium | No rate limiting on any endpoint. | Open. Authentication rate limiting in 15.2; general limits at the reverse proxy in 15.7. |
| M3 | Medium | `/docs`, `/redoc` and `/openapi.json` are public. | Open. Disabled in production by 15.3. |
| M4 | Medium | No security headers. | **Fixed in 15.1.** `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store` on data endpoints. |
| M5 | Medium | `docker-compose.yml` publishes PostgreSQL and Neo4j on the host and sets fixed development passwords. | Open for production. 15.7 adds a production compose file with no published database ports and secrets from the environment. The development file stays development-only. |
| M6 | Medium | Application logs are unstructured and not tested for secret leakage. | Open. 15.5. |
| M7 | Medium | An evaluation or investigation runs in a thread of the API process, so a runaway request shares memory with the API. | Open. Bounded by existing limits; revisit in 15.4. |
| L1 | Low | The worker `--user` is the host UID, so the bind mount is writable. It has no privileges inside the container. | Accepted. Documented. |
| L2 | Low | Embedded Docker `--tmpfs` and file limits are fixed in code, not per environment. | Accepted. |
| L3 | Low | A redirect from graph and index endpoints checks the Neo4j or embedding dependency before the path, so the error code differs (503 instead of 403) when a dependency is down. No data leaks. | Accepted. |

## What was checked and found sound

| Area | Result | Evidence |
| --- | --- | --- |
| Allowed repository roots | Paths are resolved before the containment check; `..`, absolute paths and symlinked roots cannot escape; the check runs before the existence check | `tests/test_api.py`, `tests/security/test_boundaries.py` |
| Scanner and parser | Symlinks leaving the root are skipped; files are read with a size limit; code is parsed with `ast`, never imported | `tests/test_scanner.py`, `tests/test_parser.py` |
| Patch validation | Rejects traversal, absolute and Windows paths, `.git`, `.env`, test files, new and deleted files, paths through symlinks, oversize diffs | `tests/patching/test_validator.py` |
| Patch application | Applies only in a workspace copy; re-checks that no file outside the patch changed; the original repository is hashed before and after | `tests/patching/test_applier.py`, `test_workspace.py` |
| Worker container | See `security-boundaries.md`. A hostile test in the real image sees no host secrets, no network, no Docker socket, no root, and a read-only root filesystem | `tests/security/test_boundaries.py::test_a_real_worker_cannot_see_host_secrets_or_the_network` |
| Database access | SQLAlchemy bound parameters; Cypher is parameterised; identifiers validated by pattern; metacharacters in filters are treated as data | `tests/security/test_boundaries.py` |
| Secrets in configuration | `SecretStr` for keys; evaluation configurations reject unknown fields and key-shaped strings; error responses do not echo input | `tests/evaluation/test_configuration.py` |
| Secrets in Git | No credential-shaped string in tracked non-test files; `.env*` ignored | `tests/security/test_boundaries.py` |
| LLM budget | Every call is reserved before it is made; a call that may not fit is refused | `tests/patching/test_budget.py` |

## Endpoint review

All 36 operations, from the OpenAPI document. "Auth" is the state at the end of 15.1.

| Endpoint | Executes code? | Spends money? | Writes data? | Auth |
| --- | --- | --- | --- | --- |
| `GET /health` | no | no | no | none needed |
| `POST /benchmarks/run` | **yes, host** | no | yes | none (H1) |
| `GET /benchmarks/{case_id}/runs` | no | no | no | none |
| `POST /repositories/analyze` | no (static parse) | no | no | none |
| `POST /repositories/graph/build`, `GET/DELETE /repositories/{id}/graph`, `GET .../graph/stats` | no | no | yes / deletes | none |
| `POST/DELETE /repositories/{id}/index`, `GET .../index/status`, `POST .../search` | no | embeddings if external | yes / deletes | none |
| `POST /debug/investigate` | read-only tools, `run_tests` on trusted roots | **yes** | yes | none |
| `GET /debug/investigations/{id}`, `.../observations` | no | no | no | none |
| `POST /debug/investigations/{id}/patches` | tests in the configured backend | **yes** | yes | none |
| `GET /debug/investigations/{id}/patches`, `/debug/patches/{id}`, `.../verification` | no | no | no | none |
| `POST /evaluations/configurations`, `/{id}/clone` | no | no | yes | none |
| `GET /evaluations/configurations`, `/{id}`, `/evaluations`, `/{id}`, `/cases`, `/cases/{case}`, `/metrics`, `/benchmarks`, `/baselines`, `/baselines/{id}` | no | no | no | none |
| `POST /evaluations/run` | tests in the configured backend | **yes (live)** | yes | none |
| `POST /evaluations/{id}/cancel`, `/recalculate`, `/baselines`, `/compare`, `/{id}/regression-check` | no | no | yes / no | none |

All rows marked "none" above gained authentication in 15.2; the table shows the state at the end of 15.1. Applying a patch to
the user's original repository is not an endpoint: Atlas only ever proposes patches.

## Remaining limitations after 15.1

- C1, C2, H1 and H2 are open until 15.2 and 15.3 are complete.
- The audit did not include dependency vulnerability scanning; CI adds it in 15.7.
- Container isolation on Docker Desktop for macOS is not the production Linux boundary and must be
  re-validated on the deployment host (15.4, 15.9).

## Findings from the CI/CD security pass (15.7)

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| D1 | High | `react-router-dom` 6.26.2: open-redirect and XSS advisories (3 HIGH) in a production dependency | **Fixed.** Upgraded to 6.30.6. Two MODERATE advisories remain (fix is a breaking v7); mitigated and documented in `ci-cd.md` |
| D2 | High | Eight HIGH Debian package vulnerabilities in the API and backup images (apt, pcre2, openssl...) because the base image was stale | **Fixed.** `apt-get upgrade` at build; Trivy reports 0 |
| D3 | High | 65 HIGH and 3 CRITICAL Alpine package vulnerabilities in the nginx image | **Fixed.** nginx 1.29 on Alpine 3.22 and `apk upgrade`; Trivy reports 0 |
| D4 | High | The Docker CLI copied into the API image (27.5.1, Go 1.22.11) carried 26 Go standard-library vulnerabilities, 1 CRITICAL | **Fixed.** CLI 29.9.3; Trivy reports 0. Client/daemon version skew is untested (unverified) |
| D5 | Medium | JUnit XML written by an untrusted repository's test run was parsed with the standard XML parser (entity expansion) | **Fixed.** `defusedxml` with DTDs forbidden; tested against billion-laughs and external-entity documents |
| D6 | Medium | nginx resolved `api` once at start-up: after any API redeploy it would proxy to a dead address | **Fixed.** Resolved at request time |
| D7 | Medium | The worker image's default user was root (always overridden at run time) | **Fixed.** `USER 65534:65534`; all container tests pass |
| D8 | Low | Ruff's security rules were not enabled | **Fixed.** `S` rules on application code; 4 audited ignores explained in `pyproject.toml` |
| D9 | Info | Frontend lockfile written by npm 11 could not be installed by the pinned Node 22 toolchain, then by a regeneration that had only one platform's native packages | **Fixed.** Regenerated from `package.json` alone in the pinned image; installs on macOS (Node 24) and in Docker (Node 22) |
| D10 | Medium | The dashboard's `/repositories` page shared its path with the API endpoint, so a reload (or a bookmark) would have shown JSON, in development and in the nginx image | **Fixed.** Pages are under `/repos`; `tests/deploy` forbids any page route sharing a prefix with a proxied API path |
| D11 | Low | A password-reset link opened in a tab already on the reset page did nothing | **Fixed.** Found by the browser tests |
| D12 | Low | The header user menu overflowed a phone-width screen; the UI had no "sign out everywhere" control | **Fixed.** Found by the browser tests |
