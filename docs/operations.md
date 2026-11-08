# Operations

Commands assume `docker compose -f docker-compose.prod.yml --env-file deploy.env`, written below as `dc`. First deployment,
upgrade and roll back are in `docs/deployment.md`. Backup and restore are in `docs/backup-recovery.md`.

## Daily checks

1. `dc ps`: every service healthy, `migrate` exited (0).
2. `curl -fsS http://127.0.0.1:8080/ready`: `{"status":"ready",...}`. A 503 names the failing check in `checks`.
3. Newest file in the backup volume is younger than 26 hours.
4. `dc logs --since 24h api | grep -E '"level": ?"(ERROR|CRITICAL)"'` shows nothing unexpected.

## Health endpoints

| Path | Meaning |
| --- | --- |
| `/health` | The process answers. No dependencies checked. |
| `/ready` | Database, graph store and worker backend. Fixed words only, no host names. 503 when not ready. |
| `/metrics` | Prometheus text. Needs `Authorization: Bearer <metrics_token>`. Without a token the path returns 404. |

## Metrics worth alerting on

| Metric | Alert when |
| --- | --- |
| `atlas_http_requests_total{status_class="5xx"}` | Rate above your baseline for 5 minutes |
| `atlas_http_request_duration_seconds` | p95 above your target |
| `atlas_emails_total{outcome!="sent"}` | Any failure: password reset is not arriving |
| `atlas_llm_requests_total{outcome!="ok"}`, `atlas_llm_cost_usd_total` | Failures rise, or daily cost exceeds budget |
| `atlas_container_cleanup_failures_total` | Any increase: worker containers are leaking |
| `atlas_worker_timeouts_total` | Sudden rise |
| `atlas_evaluation_runs_total{status="failed"}` | Any increase |

Label values of `outcome` and `status` come from `app/core/metrics.py`: check the exact words there before writing rules.
Starting rules are in `deploy/alerts/atlas.rules.yml` (load with `rule_files:` in Prometheus, route in Alertmanager). Tune the
thresholds, especially `AtlasModelSpendHigh` (25 USD is only an example). Prometheus must scrape the api with the
`metrics_token` and job name `atlas-api`, or change the `up{job=...}` rule. A test checks every metric name in the file exists,
but the rules have **not** been loaded into a running Prometheus.

## Logs

JSON on stdout, one object per line, with `request_id` and `workflow_id`. Useful events: `http_request`,
`agent_workflow_started/completed`, `llm_request_completed/failed`, `test_run_completed`, `patch_verification_completed`,
`evaluation_started/finished`, `evaluation_case_completed`, `password_reset_email_sent/failed`. Logs never contain tokens,
reset links, passwords or email addresses (a one-way label appears for recipients). Docker keeps them until you rotate them: set
a log driver limit on the host.

## Users and access

- Create an operator: `dc run --rm api python -m app.auth.admin create-admin EMAIL` (prompts for a password).
- Give a repository an owner: `dc run --rm api python -m app.auth.admin claim SLUG EMAIL`.
- Close registration: `AUTH_REGISTRATION_OPEN=false` in `deploy.env`, then `dc up -d`.
- Deactivate a user: set `users.is_active = false`, then end their sessions (see `incident-response.md`).

## Secrets rotation

Secrets are files in `SECRETS_DIR`. Replace the file, then `dc up -d --force-recreate <services that mount it>`.

| Secret | Effect of rotating |
| --- | --- |
| `auth_secret_key` | Every access token becomes invalid at once. Users renew from their refresh cookie. Keep 32+ random characters. |
| `smtp_password` | Takes effect on restart. Check `atlas_emails_total` afterwards. |
| `metrics_token` | Update the scraper. |
| `anthropic_api_key` | Takes effect on restart. |
| `backup_encryption_key` | Old backups still need the old key. Keep it until those backups are pruned. |
| `database_url`, `postgres_password` | Change the database role first, then the files, then restart api, migrate, backup. |

## Evaluations and workers

- Stop cleanly: `dc stop api` (45 s grace). Active evaluations are cancelled and their workers removed.
- Leftover workers after a crash are removed at the next start. Only containers labelled `atlas.instance=<INSTANCE_ID>` are touched.
- Inspect workers: `docker ps --filter label=atlas.patching=1`.
- Only one evaluation can run at a time. A second start request is refused until the first ends.

## Capacity and limits

- Run one `api` replica. Rate limits live in process memory and reset on restart.
- `api` has `mem_limit: 2g` and `pids_limit: 512`. Raise them in the compose file if workflows are killed (exit code 137).
- Disk: watch `pgdata`, `backups` and `WORKSPACE_HOST_DIR`. Workspaces are temporary.

## Off-host backups

The backup volume is on the same host as the database. Copy it elsewhere with `scripts/offsite_backup.sh` on the Docker host:

1. Make the volume readable on the host, for example `docker volume inspect <project>_backups` for its mount point (root needed),
   or copy files out with a one-shot container.
2. `scripts/offsite_backup.sh push <backup-dir> <user@host:/path or mounted bucket path>` from cron after the daily backup.
   It copies only finished, encrypted backups, verifies by checksum, refuses to run when no backup is under 26 hours old, and never
   deletes at the destination (apply retention there).
3. `scripts/offsite_backup.sh check <backup-dir>` exits 1 when the newest backup is older than 26 hours. Run it from cron and send
   a failure to your pager or a dead-man's-switch service. This covers the backup-age alert that Prometheus cannot.
4. Store `backup_encryption_key` somewhere other than the destination.

The script is tested against local directories only. It has not been tried over SSH or against a real bucket.

## Maintenance

- Backups: `dc exec backup python -m app.ops.backup backup`, then `verify`. See `docs/backup-recovery.md`.
- Rebuild the graph after a Neo4j loss: `dc run --rm api python -m app.ops.rebuild_graphs`.
- Re-run a restore drill on the schedule in `docs/backup-recovery.md`.

## Known gaps

Off-host copy and alert rules exist but are untried on a real host; no mail retry queue, one api replica, and the whole stack has not yet run on a
deployment host.
