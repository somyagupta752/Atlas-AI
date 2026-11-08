# Architecture

Atlas AI indexes a code repository, answers debugging questions about it with a language model, proposes patches, checks them
by running tests, and measures how well that works. This page shows the parts and how data moves. Security limits are in
`docs/security/security-boundaries.md`.

## Components

| Part | What it does | Stores |
| --- | --- | --- |
| Dashboard (`frontend/`, nginx in production) | React app. Serves static files and proxies API paths to the api. | Access token in tab memory only |
| API (`app/`, FastAPI) | Auth, repository registry, indexing, retrieval, debugging agent, patching, evaluation lab, metrics. | None itself |
| PostgreSQL 16 + pgvector | Users, sessions, repositories, code chunks and embeddings, investigations, patches, evaluation history. | All durable data except the graph |
| Neo4j 5.15 (optional) | Code knowledge graph. Rebuilt from source, so it is not backed up. | Graph |
| Docker proxy + worker containers | Run tests for patch checks, one container per run. | Temporary workspaces |
| Backup job | Daily encrypted PostgreSQL dump, verified by restoring it into a scratch database. | Backup volume |
| Language model provider | Diagnosis and patch generation. Off unless `LLM_ALLOW_EXTERNAL=true`. | Nothing |
| SMTP server | Password-reset email only. | Nothing |

## Packages in `app/`

`auth` (users, tokens, roles, mail) · `core` (config, logging, metrics, security helpers, middleware) · `repository`,
`code_intelligence`, `retrieval`, `knowledge_graph` (indexing and search) · `agents` (debugging agent, tool executor) ·
`patching` (workspace, container runner, verification) · `evaluation` (immutable run history, baselines) · `ops` (backup,
retention, graph rebuild).

## Networks (production)

```
internet -> TLS proxy (yours) -> frontend [edge, app] -> api [app, backend, workers, egress]
                                                          |-> postgres, neo4j [backend]
                                                          |-> docker-proxy [workers] -> Docker daemon -> worker containers
                                                          '-> model provider, SMTP server [egress]
```

`app`, `backend` and `workers` are internal networks with no route out. Only `api` joins `egress`. PostgreSQL and Neo4j
publish no port.

## Request path

1. The browser loads static files. It calls the API through the same origin, so there is no CORS in the default setup.
2. `POST /auth/login` returns a 15-minute access token (kept in memory) and sets the refresh cookie (`httpOnly`, `Path=/auth`).
3. Each data call sends `Authorization: Bearer`. The API checks the signature, expiry, `tokens_valid_after` and the user's
   role on the repository. Non-members get 404. Members without enough role get 403.
4. When a call returns 401, the dashboard calls `POST /auth/refresh` once, then repeats the call. See `docs/security/dashboard-auth.md`.

## Main workflows

- **Index:** read files, split into chunks, embed, write to PostgreSQL, build the graph in Neo4j.
- **Investigate:** retrieve chunks and graph context, ask the model, run read-only tools, store the result and events.
- **Patch:** copy the repository into a workspace, apply the model's diff, run tests in a worker container, retry on failure, store every attempt.
- **Evaluate:** run a fixed set of cases through a diagnosis mode and a patch mode, record metrics. Records are immutable.
  Only one evaluation can be active at a time across the whole system.

## Data ownership

Repositories have one owner at least and optional editors and viewers. Evaluation rows carry `owner_id`. Rows with no owner
are system or historical records and every signed-in user can read them. An `is_admin` operator bypasses membership checks.

## Version and traceability

Images carry `VERSION` and `REVISION` build arguments. Every evaluation records the code revision that produced it.

## Not validated

Worker isolation. The API reaches a rootful Docker daemon through a filtering proxy, which a compromised API can abuse.
Rootless workers on a dedicated VM are designed (`docs/security/rootless-worker-design.md`) and not validated. Do not run
untrusted repositories until `docs/security/worker-validation-plan.md` is signed off.
