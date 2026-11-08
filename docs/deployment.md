# Deployment

This describes `docker-compose.prod.yml` (staging and production). `docker-compose.yml` is the development file and is
not used here. Read `docs/security/security-boundaries.md` first: one decision below (Docker access for the test
workers) is a real security trade-off.

## What runs

| Service | Image | Network | Published |
| --- | --- | --- | --- |
| `frontend` | `atlas-ai-frontend` (nginx, static build, proxies the API paths) | edge, app | `127.0.0.1:8080` |
| `api` | `atlas-ai-api` (non-root, read-only filesystem) | app, backend, workers, egress | no |
| `migrate` | same image, runs `alembic upgrade head` once | backend | no |
| `postgres` | `pgvector/pgvector:0.8.0-pg16` | backend | **no** |
| `neo4j` | `neo4j:5.15-community` | backend | **no** |
| `docker-proxy` | `tecnativa/docker-socket-proxy` | workers | no |
| `backup` | `atlas-ai-backup` (API image plus PostgreSQL 16 client tools) | backend | no |

`backend`, `app` and `workers` are internal Docker networks with no route to the internet. Only `api` joins `egress`
(for the model provider); remove it from the compose file to run fully offline.

> **Worker isolation is not validated.** The compose file as it stands gives the API access to a rootful daemon through a
> filtering proxy, which a compromised API can abuse. Do not run untrusted repositories until the rootless worker design
> (`docs/security/rootless-worker-design.md`) is implemented and `docs/security/worker-validation-plan.md` is signed off.

## Prerequisites on the Docker host

1. A Linux host or VM that holds nothing else of value. Docker Engine with Compose v2.
2. The worker image, built once on that host (the API image does not contain it):
   `docker build -f docker/test-runner.Dockerfile -t atlas-ai-test-runner:py3.12.13-pytest9.2.0 docker`
3. Directories, owned by the container user (uid 10001):
   ```
   sudo install -d -o 10001 -g 10001 -m 700 /var/lib/atlas/workspaces
   ```
   `REPOSITORIES_DIR` holds the repositories to analyse; it is mounted read-only.
4. A TLS-terminating reverse proxy in front of `127.0.0.1:8080`. The session cookie is `Secure`, so the dashboard
   does not work over plain http. The proxy must set `X-Forwarded-Proto`. Configure nginx's `set_real_ip_from` for that
   proxy's address in `frontend/nginx.conf`, or every user shares one login rate limit.

## Secrets

Secrets are files, one value per file, in a directory that is not in Git (`secrets/` is ignored). No secret is an
environment variable, a build argument or part of an image.

| File | Content | Used by |
| --- | --- | --- |
| `database_url` | `postgresql+psycopg://atlas:<password>@postgres:5432/atlas` (URL-encode special characters) | api, migrate, backup |
| `postgres_password` | the same password | postgres |
| `auth_secret_key` | random, 32+ characters: `openssl rand -base64 48` | api, migrate, backup |
| `neo4j_password` | 12+ characters | api, neo4j |
| `backup_encryption_key` | `python -m app.ops.backup keygen` | backup only |
| `anthropic_api_key` | provider key, or an empty file when `LLM_ALLOW_EXTERNAL=false` | api only |
| `smtp_password` | your SMTP password, or an empty file for a relay without login (see [password-reset-email.md](password-reset-email.md)) | api, migrate, backup |
| `metrics_token` | scrape token, or an empty file to disable `/metrics` | api only |

```
umask 077
mkdir -p secrets
openssl rand -base64 36 | tr -d '\n' > secrets/postgres_password
printf 'postgresql+psycopg://atlas:%s@postgres:5432/atlas' "$(cat secrets/postgres_password)" > secrets/database_url
openssl rand -base64 48 | tr -d '\n' > secrets/auth_secret_key
openssl rand -base64 36 | tr -d '\n' > secrets/neo4j_password
: > secrets/anthropic_api_key ; : > secrets/metrics_token ; : > secrets/smtp_password        # fill in, or leave empty
sudo chown 10001:10001 secrets/* && sudo chmod 400 secrets/*      # readable by the container user only
```

The `backup_encryption_key` and `postgres_password` files are read as root or by their own service; keep the backup key
somewhere other than the backups themselves (a password manager or your secret manager).

An empty secret file means "not set". The application refuses to start in `staging` or `production` with a missing or
weak `AUTH_SECRET_KEY`, a weak database or Neo4j password, `PATCH_TEST_BACKEND=local`, any `AGENT_TRUSTED_TEST_ROOTS`,
an insecure cookie, the host benchmark endpoint, or a `*` or non-https CORS origin. The message names the setting and
never prints a value.

## Configuration

Copy `deploy.env.example` to `deploy.env` and set `REGISTRY`, `ATLAS_VERSION`, `WORKSPACE_HOST_DIR`,
`REPOSITORIES_DIR`. These have no defaults on purpose. `WORKSPACE_HOST_DIR` is mounted at the **same path** inside the
API: worker containers bind-mount their workspace through the Docker daemon, which resolves the path on the host.

## Images

Images are tagged with a version (never `latest`). Build with the commit recorded, so every evaluation names the code
that produced it:

```
docker build --build-arg VERSION=$V --build-arg REVISION=$(git rev-parse HEAD) -t $REGISTRY/atlas-ai-api:$V .
docker build --target backup --build-arg VERSION=$V --build-arg REVISION=$(git rev-parse HEAD) \
             -t $REGISTRY/atlas-ai-backup:$V .
docker build --build-arg VERSION=$V -t $REGISTRY/atlas-ai-frontend:$V frontend
```

## First deployment

```
docker compose -f docker-compose.prod.yml --env-file deploy.env config -q     # validates; prints nothing
docker compose -f docker-compose.prod.yml --env-file deploy.env pull
docker compose -f docker-compose.prod.yml --env-file deploy.env up -d
docker compose -f docker-compose.prod.yml --env-file deploy.env ps            # all healthy; migrate: exited (0)
curl -fsS http://127.0.0.1:8080/ready                                         # {"status":"ready",...}
docker compose -f docker-compose.prod.yml --env-file deploy.env run --rm api \
    python -m app.auth.admin create-admin you@example.com                     # prompts for a password
```

Then set `AUTH_REGISTRATION_OPEN=false` in `deploy.env` once your users exist, and `up -d` again.

## Upgrade and roll back

1. Take a backup and verify it: `docker compose ... exec backup python -m app.ops.backup backup` then `verify`.
2. Note the running tag (`docker compose ... images`). Set the new `ATLAS_VERSION`; run `pull` then `up -d`.
   `migrate` runs first; the API starts only if it succeeds.
3. Check `/ready` and the logs (`docker compose logs --since 5m api`, JSON lines).
4. **Roll back:** set `ATLAS_VERSION` back and `up -d`. Migrations are additive and not reverted automatically. If a
   release changed the schema in a way the old code cannot use, restore the pre-upgrade backup into a new database
   (`docs/backup-recovery.md`) instead of running `alembic downgrade` against data.

## Operating

- Logs: JSON on stdout, one object per line, with `request_id` and `workflow_id`. Metrics: `GET /metrics` with
  `Authorization: Bearer <metrics_token>` from a scraper on the `app` network or through the proxy you control.
- Backups run daily inside `backup` and are verified by restoring them into a scratch database. The backup volume lives on
  the same host as the database: **copy the encrypted files off the host** (object storage with a private bucket) or a
  host loss loses both. A failed run only writes to the container log; alert on that line or on the age of the newest file.
- Stop with `docker compose ... stop api` (45 s grace): active evaluations are cancelled and their workers removed.
- Workers left over after a crash are removed at the next start, only those labelled for this `INSTANCE_ID`.

## Known limits of this stack

- The Docker proxy narrows the API calls, not their content. See `security-boundaries.md` and the comment in the
  compose file before putting untrusted code on this host.
- The `backup` job does not alert by itself and does not copy files off the host. `scripts/offsite_backup.sh` and `deploy/alerts/atlas.rules.yml` cover both; set them up (`docs/operations.md`).
- Neo4j Community has no online backup; recover the graph with `python -m app.ops.rebuild_graphs`.
- Rate limiting is per API process and per client address; run one `api` replica, or enforce limits in the proxy.
- None of this has been run end to end on a deployment host. Treat the first staging deployment as the test.
