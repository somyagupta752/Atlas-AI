# Backup, recovery and retention

## Targets

| Objective | Initial target | Status |
| --- | --- | --- |
| Recovery Point Objective (RPO) | 24 hours | Met by a daily backup. Data written after the last backup is lost. |
| Recovery Time Objective (RTO) | 4 hours | Measured restore of the development database (a few MB, 36 tables) took under 20 s for backup + verify. **Re-measure with production data volume before relying on 4 hours.** |
| Frequency | Daily | `app.ops.backup backup` from a scheduler |
| Retention | 7 daily + 4 weekly | `app/ops/retention.py`, tested |

These are engineering targets, not guarantees. They hold only while the daily job succeeds: alert when no backup file is newer than 26 hours.

## What is stored where

| Data | Store | Recoverable from source? | Backup |
| --- | --- | --- | --- |
| Users, sessions, memberships | PostgreSQL | No | Daily dump |
| Repository registry, indexes, code chunks, embeddings | PostgreSQL | Yes: re-index (cost: embedding time) | Daily dump (restores faster than re-embedding) |
| Investigations, events, patches, verifications | PostgreSQL | No (they record model output and test runs) | Daily dump |
| Evaluation configurations, runs, cases, attempts, metrics, baselines | PostgreSQL | No. Immutable history. | Daily dump |
| Code knowledge graph | Neo4j | **Yes**: rebuilt from source | Rebuild; optional offline dump |
| Worker workspaces, containers | Local disk, Docker | Temporary | None |

## PostgreSQL

Create a key once and keep it in your secret manager, separate from the backups:

```bash
python -m app.ops.backup keygen          # prints a 256-bit key
```

Environment of the backup job: `DATABASE_URL`, `BACKUP_DIR` (mode 700), `BACKUP_ENCRYPTION_KEY`, and
`BACKUP_PG_DOCKER_CONTAINER` when `pg_dump` runs inside the database container (otherwise the host needs
`pg_dump`/`pg_restore` of the same major version as the server).

```bash
python -m app.ops.backup backup          # dump -> AES-256-GCM encrypt -> manifest -> retention
python -m app.ops.backup verify          # restore the newest backup into a scratch database, compare, drop it
python -m app.ops.backup prune           # retention only
```

A backup is `atlas-pg-<UTC timestamp>.dump.enc` plus `...manifest.json` (file hash, Alembic revision, PostgreSQL
version, table row counts). The dump is a custom-format `pg_dump` and is encrypted in 1 MiB authenticated chunks, so a
wrong key, a flipped bit and a truncated file are all detected. The directory is created 0700, files 0600, and the job
refuses a directory other users can read.

### Integrity checks

`verify` does four things: decrypts (fails on any corruption), checks the SHA-256 against the manifest, restores into
`atlas_verify_<random>`, and compares the Alembic revision and every table's row count with the manifest. The scratch
database is always dropped. Run it after every backup, and treat a failure as an incident.

### Restore procedure

1. Stop the API and any evaluation (`docker compose stop api`). Writes during a restore are lost.
2. Choose the backup. `ls -1 $BACKUP_DIR/*.dump.enc`.
3. Restore into a **new** database first and inspect it:
   `python -m app.ops.backup restore FILE --target-db atlas_restored --confirm`
4. Check it: `python -m app.ops.backup verify FILE` (restores again into a scratch database and compares).
5. Switch over: point `DATABASE_URL` at `atlas_restored`, or rename the databases while nothing is connected.
6. Run migrations: `alembic upgrade head`. A backup from an older revision upgrades; one from a newer revision than the
   code must not be restored under older code.
7. Start the API and check `/ready`.
8. Rebuild the graph if Neo4j was also lost (below). Re-index only repositories whose chunks are missing.

Restoring into the live database name requires `--existing` and the same `--confirm`; there is no automatic overwrite.

## Neo4j

The graph is derived from source. **The primary recovery is a rebuild**, which needs no backup and no downtime:

```bash
python -m app.ops.rebuild_graphs              # every repository in repository_indexes
python -m app.ops.rebuild_graphs my-repo      # one repository
```

It uses the root path recorded at indexing, and only for roots still inside `ALLOWED_REPOSITORY_ROOTS`. Anything it
cannot rebuild is listed with the reason. What cannot be reconstructed from source: nothing of value. The graph holds
the structure of code that exists on disk; PostgreSQL holds every record that depends on it.

Neo4j Community has no online backup. If you want a dump (to skip a long rebuild), take it offline:

```bash
scripts/neo4j_backup.sh dump    atlas-neo4j /var/backups/atlas/neo4j     # stops the container, dumps, restarts it
scripts/neo4j_backup.sh restore atlas-neo4j /var/backups/atlas/neo4j/neo4j.dump
```

The script waits up to 120 s for Neo4j to stop cleanly (a shorter wait can leave a stale PID file that blocks the next
start). The dump is private (0600) but **not encrypted**: encrypt it at rest like any other backup. The round trip is
tested against throwaway containers (`tests/ops/test_neo4j_recovery.py`).

## Retention

| Data | Rule | Where enforced |
| --- | --- | --- |
| Database backups | 7 daily + 4 weekly (Sundays); the newest is never deleted; files that do not match the name pattern are never touched | `app/ops/retention.py`, `prune` |
| Temporary workspaces | Deleted after every run. Kept workspaces (`PATCH_WORKSPACE_RETENTION`) expire after `PATCH_WORKSPACE_MAX_AGE_HOURS` (24). All inactive workspaces are removed at startup. | `WorkspaceManager` |
| Worker containers | Removed after every run, and at startup and shutdown (only this deployment's, only `atlas-test-*`) | `cleanup_orphans` |
| Worker and application logs | 30 days in the log platform. Atlas writes to stdout only; it holds no log files. | Your log pipeline |
| LLM request metadata (model, tokens, cost, duration) | Logged (30 days) and stored with the evaluation attempt or patch verification (kept with that record) | Logs; PostgreSQL |
| Patch artifacts (diffs, verification evidence) | Kept with their investigation | PostgreSQL |
| Evaluation evidence and historical benchmark records | **Never deleted by cleanup.** Database triggers reject changes to finished runs, their cases and attempts, metric snapshots, baselines and configurations. | PostgreSQL triggers |
| Uploaded repository copies | Atlas stores none: it reads repositories under the allowed roots. Indexed chunks are deleted with `DELETE /repositories/{id}/index`. | API |
| Refresh and reset tokens | Expired tokens are inert. Deleting rows older than 90 days is safe (not automated yet). | Manual SQL |

Deleting a user's data on request is a manual operation today (indexes and graphs through the API; investigations and
patches by SQL). Evaluation history cannot be edited by design.

## Backups are not public

- Files are created 0600 in a 0700 directory, and the job refuses a more open directory (tested).
- Backups are encrypted, so a leaked file is unreadable without the key. Store the key elsewhere.
- The production compose file mounts the backup volume only into the backup job, publishes no ports for it, and the
  API container cannot read it. For object storage, use a private bucket with server-side encryption and no public ACLs.

## Drills

Run a restore drill (`verify`) after every backup, and a full restore into a new database each quarter. Record the
elapsed time to keep the RTO figure honest.
