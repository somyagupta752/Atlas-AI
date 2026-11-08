#!/usr/bin/env bash
# Copy the encrypted PostgreSQL backups off the host, and check that a recent one exists.
#
#   scripts/offsite_backup.sh check <backup-dir> [max-age-minutes]   exit 1 if no backup is younger than that (default 1560 = 26 h)
#   scripts/offsite_backup.sh push  <backup-dir> <destination>       copy new backups; never deletes anything at the destination
#
# <destination> is anything rsync accepts: user@host:/path (over SSH), or a local path such as a mounted object-storage bucket
# (s3fs, rclone mount, NFS). Run it on the Docker HOST from cron or a systemd timer, after the daily backup. Example:
#   0 3 * * *  /opt/atlas/scripts/offsite_backup.sh push /var/lib/atlas-backups backup@vault.example.com:/srv/atlas-backups
#
# Backups are written with mode 0600 and rsync -a keeps it, so copies stay private. Only finished backups are copied (*.dump.enc and their manifests). Backups are already encrypted with backup_encryption_key,
# so the destination never sees data in the clear. Keep that key somewhere else: a backup next to its key protects nothing.
# The destination keeps everything it receives: apply your own retention there. Nothing here removes remote files.
set -euo pipefail

usage() { sed -n '2,12p' "$0"; exit 2; }
cmd="${1:-}"; src="${2:-}"
if [ -z "$cmd" ] || [ -z "$src" ]; then usage; fi
[ -d "$src" ] || { echo "backup directory not found: $src" >&2; exit 1; }

newest_ok() { # any finished backup younger than $1 minutes
  [ -n "$(find "$src" -maxdepth 1 -type f -name 'atlas-pg-*.dump.enc' -mmin "-$1" -print -quit)" ]
}

case "$cmd" in
  check)
    max="${3:-1560}"
    if newest_ok "$max"; then echo "ok: a backup younger than ${max} minutes exists"; else
      echo "STALE: no backup younger than ${max} minutes in $src" >&2; exit 1; fi
    ;;
  push)
    dest="${3:-}"; [ -n "$dest" ] || usage
    command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }
    newest_ok 1560 || { echo "refusing to copy: no backup from the last 26 hours (the backup job may be failing)" >&2; exit 1; }
    # Dump files first, manifests second: a manifest at the destination always has its dump beside it.
    rsync -a --ignore-existing --include='atlas-pg-*.dump.enc' --exclude='*' "$src"/ "$dest"/
    rsync -a --include='atlas-pg-*.manifest.json' --exclude='*' "$src"/ "$dest"/
    # Verify: nothing left to transfer, by checksum.
    left="$(rsync -a --checksum --dry-run --itemize-changes --include='atlas-pg-*.dump.enc' --include='atlas-pg-*.manifest.json' \
            --exclude='*' "$src"/ "$dest"/ | grep -c '^>f' || true)"
    [ "$left" = 0 ] || { echo "verification failed: $left file(s) differ at the destination" >&2; exit 1; }
    echo "ok: backups copied and verified"
    ;;
  *) usage ;;
esac
