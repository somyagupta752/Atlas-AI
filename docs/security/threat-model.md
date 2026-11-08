# Atlas AI threat model

Status: 15.1, written against the code at the time of the audit. Update it when a boundary changes.

## What Atlas does that makes it risky

Atlas reads repositories it does not control, sends their source to an external language model, runs the
repository's tests, applies model-written patches, and stores the results. Each of those steps handles
data that an attacker can influence.

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| LLM API key | Process environment (`ANTHROPIC_API_KEY`) | Spend, and abuse of the provider account |
| User source code | Allowed repository roots, `code_chunks`, Neo4j, LLM requests | Confidentiality of customer code |
| Evaluation history | PostgreSQL (`evaluation_*`, immutable by trigger) | Trustworthy record of what was measured |
| Debugging and patch records | PostgreSQL (`investigations`, `patch_proposals`, `patch_verifications`) | Evidence behind every verdict |
| Database and Neo4j credentials | Environment | Full read and write access to all of the above |
| The host (files, network, Docker daemon) | The machine running the API | Everything else |
| LLM budget | `LLMBudget`, configuration | Direct money |

## Actors

- **Anonymous network client.** Can reach any port the deployment exposes.
- **Authenticated low-privilege user.** After 15.2: a viewer or editor of some repositories.
- **Author of a malicious repository.** Controls every file Atlas reads, including tests, comments and docstrings.
- **Compromised or misbehaving model.** Returns hostile diffs or tool calls.
- **Insider or operator.** Out of scope except for secret handling.

## Trust boundaries

```
 Browser / API client --(B1)--> FastAPI --(B2)--> PostgreSQL / Neo4j
                                   |
                                   +--(B3)--> LLM provider (external, sees source code)
                                   |
                                   +--(B4)--> Docker daemon --(B5)--> worker container --> repository code
                                   |
                                   +--(B6)--> allowed repository roots on disk (untrusted content)
```

| Boundary | What crosses it | Control today |
| --- | --- | --- |
| B1 client to API | JSON bodies, path and query parameters | Pydantic validation, size limit, patterns on IDs. **No authentication yet (15.2).** |
| B2 API to databases | SQL, Cypher | SQLAlchemy parameters, parameterised Cypher in `queries.py` |
| B3 API to LLM | Repository source, bug reports, tool results | `LLM_ALLOW_EXTERNAL` opt-in, secret redaction in tool output, budget and timeouts |
| B4 API to Docker | `docker run` argument lists built in code | No shell, fixed argument list, labelled containers |
| B5 container to repository code | The untrusted code runs here | Network none, read-only root, caps dropped, non-root, limits |
| B6 disk to API | Files of the analysed repository | Allowed roots, canonical paths, symlink rejection |

## Attack surfaces

All 36 HTTP endpoints (listed in the audit), the files of any analysed repository, the model's responses,
the Docker CLI invocation, environment variables and the dashboard's API calls.

## Threat scenarios

| # | Threat | Required protection | Existing mitigation | Residual risk |
| --- | --- | --- | --- | --- |
| T1 | Malicious Python repository runs code | Container isolation | Container backend with the flags in `security-boundaries.md`. Local backend only for `AGENT_TRUSTED_TEST_ROOTS`. | Local backend is the default. A container shares the host kernel. |
| T2 | Path traversal | Canonical path validation | Allowed roots resolved before comparison; patch validator rejects `..`, absolute and Windows paths; `read_file` tool rejects them | Low |
| T3 | Symlink escape | Reject unsafe symlinks | Scanner skips escaping links; workspace copy skips them; validator and applier reject paths through links | Low |
| T4 | Test execution on the host for untrusted code | No host execution | `run_tests` and the local runner refuse paths outside trusted roots. `POST /benchmarks/run` runs a committed benchmark on the host. | See finding H1 |
| T5 | Prompt injection in source files | Treat content as evidence | System prompt marks repository text as data; the model can only call read-only tools; patches are validated and verified | Cannot be eliminated. A human reviews every patch. |
| T6 | Destructive LLM patch | Validate diff and boundaries | Allowed-file scope, size limits, no test edits, no new or deleted files by default, applied only in a copy | Low |
| T7 | API key leakage | Secrets never in repository or logs | `SecretStr`, redaction, config validation (15.3 extends) | Logs not yet structured (15.5) |
| T8 | Unauthorized repository access | Repository-level authorization | **None** | **Critical, see C1 and C2** |
| T9 | SQL or Cypher injection | Parameterised queries | SQLAlchemy and parameterised Cypher; ID patterns | Low |
| T10 | Resource exhaustion | CPU, memory, disk, time limits | Container limits, workspace size caps, LLM budget, per-request size limit | No rate limiting; unbounded concurrent requests |
| T11 | Docker socket abuse | No socket in workers | Workers get no socket. The API process itself talks to the daemon, which is root-equivalent. | See R1 |
| T12 | Sensitive data in logs | Redaction | Tool output redaction; no request bodies logged | Not systematically tested until 15.5 |
| T13 | Cross-tenant data exposure | Per-repository authorization | None yet. `repository_id` is a global namespace. | Critical until 15.2 |
| T14 | Cost abuse | Spending limits | Budget caps per run | Anyone can start a paid run until 15.2 |

## Remaining risks that cannot be removed

- A container is not a hard boundary. For hostile multi-tenant code use gVisor, Kata or one VM per run.
- Source code is sent to the model provider when `LLM_ALLOW_EXTERNAL=true`.
- Prompt injection can mislead a diagnosis. Patches are still verified and reviewed.
- The API process holds access to the Docker daemon.

## Severity and remediation priority

See `security-audit.md` for the findings, their severity and status. Rule for deployment: no unresolved
critical or high finding may be deployed.
