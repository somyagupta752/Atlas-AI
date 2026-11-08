# Incident response

For a suspected security incident or a data-loss event. Rules first, then a playbook per case. `dc` means
`docker compose -f docker-compose.prod.yml --env-file deploy.env`.

## Rules

1. **Contain first, investigate second.** Stop the damage, then find the cause.
2. **Keep evidence.** Do not delete containers, volumes, logs or database rows until someone has copied what is needed.
   Never run `dc down -v`: it deletes the volumes.
3. **Never paste secrets, tokens or reset links** into chat, tickets or logs. Atlas logs contain none. Do not add them.
4. **Write a timeline** as you go: time, what you saw, what you did, who did it.
5. **Rotate before you restore trust.** Any secret that may have leaked is rotated (see `docs/operations.md`).

## Severity

| Level | Examples | First response |
| --- | --- | --- |
| SEV1 | Host or worker escape, data leaked, attacker has operator rights | Isolate now, wake the owner |
| SEV2 | One account taken over, secret leaked, data lost but restorable | Within the hour |
| SEV3 | Outage, failed backups, email not sending | Same day |

## Playbooks

### Stolen or leaked user credentials, or one account taken over

1. Deactivate and end all sessions of the user. Run in the database (replace the address):
   ```sql
   UPDATE users SET is_active = false, tokens_valid_after = date_trunc('second', now()) WHERE email = 'user@example.com';
   UPDATE refresh_tokens SET revoked_at = now()
     WHERE revoked_at IS NULL AND user_id = (SELECT id FROM users WHERE email = 'user@example.com');
   ```
   Open it with `dc exec postgres psql -U atlas -d atlas`.
2. Access tokens issued before that second stop working at once. A refresh token cannot renew.
3. Review what the account did: memberships, repositories it owns or changed, evaluation and investigation rows by `owner_id`,
   and `http_request` log lines for its sessions.
4. Reactivate only after the owner has reset the password through the email link. A reset ends every session again.

Signals: the `token reuse` revocation of a whole login family means someone replayed a spent refresh token. Treat repeated
cases for one user as a possible theft.

### Leaked secret

| Secret | Do |
| --- | --- |
| `auth_secret_key` | Replace the file, recreate api, migrate, backup. All access tokens die. Check for forged-token activity before the change. |
| `database_url` / `postgres_password` | Change the role password in PostgreSQL, then the files, then restart. Review database logs. |
| `anthropic_api_key` | Revoke it at the provider first, then replace the file. Check provider usage for spend you did not make. |
| `smtp_password` | Rotate at the provider, replace the file, restart. |
| `metrics_token` | Replace and update the scraper. |
| `backup_encryption_key` | Treat all backups as readable by the key holder. Make new backups with a new key. Keep the old key for old files until pruned. |

If a secret was committed to Git: rotate it first. Removing it from history does not make it safe. The gitleaks scan in CI
exists to catch this before a push.

### Suspected compromise of the API or a worker (SEV1)

Worker isolation is not validated (`docs/security/security-boundaries.md`). The API can reach a rootful Docker daemon through a
filtering proxy, so a compromised API may control the host.

1. Cut the network at the host or firewall. Stop the TLS proxy so no new requests arrive.
2. Do **not** reboot or delete anything yet. Take a disk and memory snapshot if your platform allows it.
3. Treat the whole host and every secret on it as exposed. Rotate all secrets, from a clean machine.
4. Rebuild the host from a clean image. Restore data from a backup older than the compromise
   (`docs/backup-recovery.md`), into a new database.
5. List containers by label to find attacker-started ones: `docker ps -a --filter label=atlas.patching=1`. Containers without
   the label are not Atlas workers.
6. Do not run untrusted repositories again until the rootless design is validated.

### Hostile repository or malicious patch result

1. Stop the evaluation or workflow: `dc stop api` cancels active runs and removes their workers.
2. Keep the workspace under `WORKSPACE_HOST_DIR` for study. Do not open files from it on a normal machine.
3. Remove the repository from the registry and revoke the submitting user's access (see account playbook).
4. Treat it as a possible SEV1 if the worker showed any network, mount or host access.

### Data exposure between users

Symptom: a user sees another user's repository or evaluation. Take the API out of service, find the route in the
`http_request` logs, and write a regression test in `tests/security/` before the fix. Tell the affected owners.

### Data loss or corruption

1. Stop writes: `dc stop api`.
2. Copy the newest good backup somewhere safe.
3. Restore into a **new** database and compare against the damaged one (`docs/backup-recovery.md`, restore procedure).
4. Point `database_url` at the restored database, start `api`, check `/ready`.
5. Rebuild the graph: `dc run --rm api python -m app.ops.rebuild_graphs`. RPO is 24 hours: work after the last backup is lost.

### Backups not running or failing

1. `dc logs backup`. Look for "backup or verification FAILED".
2. Fix the cause (disk, key, database). Run `dc exec backup python -m app.ops.backup backup` then `verify`.
3. Until a verified backup exists, avoid risky changes and upgrades.
4. Copy the encrypted files off the host. A host loss otherwise loses database and backups together.

### Runaway model spend

1. Set `LLM_ALLOW_EXTERNAL=false` (or `LLM_PROVIDER=disabled`) in `deploy.env`, then `dc up -d`.
2. Find the caller in `llm_request_completed` logs and `atlas_llm_cost_usd_total`.
3. Revoke or rotate the provider key if misuse is possible.

### Email problems

Failures show as `password_reset_email_failed` with a `kind`. See `docs/troubleshooting.md`. A sudden flood of reset requests
for one address is rate limited. Sudden floods across many addresses suggest account enumeration: check `http_request` volume.

## After the incident

Within a week, write down: timeline, cause, impact (which users and data), what worked, what failed, fixes with owners and
dates. Add a regression test for any code bug. Update these docs if a step was wrong or missing.

## Known limits of this plan

There is no alerting, so many incidents will be found by users. There is no audit log of administrative actions beyond
request logs. Sessions can be ended per user only through SQL until an admin command exists.
