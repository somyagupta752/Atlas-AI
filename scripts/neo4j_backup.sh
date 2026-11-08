#!/usr/bin/env bash
# Offline Neo4j (Community) backup and restore using neo4j-admin. Community Edition has no online backup, so the
# database must be stopped while it is dumped: expect a short outage, or rebuild the graph from source instead
# (python -m app.ops.rebuild_graphs), which needs no downtime and no backup.
#
#   scripts/neo4j_backup.sh dump    <container> <output-dir>
#   scripts/neo4j_backup.sh restore <container> <dump-file>   # replaces the database in that container's volume
#
# The dump contains customer source structure. Keep the output directory private (mode 700) and encrypted at rest.
set -euo pipefail

IMAGE="${NEO4J_IMAGE:-neo4j:5.15-community}"
DB="${NEO4J_DATABASE:-neo4j}"
cmd="${1:-}"; container="${2:-}"; target="${3:-}"
[ -n "$cmd" ] && [ -n "$container" ] && [ -n "$target" ] || { sed -n '2,10p' "$0"; exit 2; }

data_volume() { docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$1"; }

vol="$(data_volume "$container")"
[ -n "$vol" ] || { echo "the container has no named /data volume" >&2; exit 1; }
was_running="$(docker inspect -f '{{.State.Running}}' "$container")"

case "$cmd" in
  dump)
    umask 077; mkdir -p "$target"; chmod 700 "$target"
    [ "$was_running" = "true" ] && docker stop -t 120 "$container" >/dev/null
    trap '[ "$was_running" = "true" ] && docker start "$container" >/dev/null' EXIT
    docker run --rm -v "$vol":/data -v "$(cd "$target" && pwd)":/backups "$IMAGE" \
      neo4j-admin database dump "$DB" --to-path=/backups --overwrite-destination=true
    chmod 600 "$target"/*.dump
    echo "dumped to $target/$DB.dump"
    ;;
  restore)
    dumpdir="$(cd "$(dirname "$target")" && pwd)"
    [ "$was_running" = "true" ] && docker stop -t 120 "$container" >/dev/null
    trap '[ "$was_running" = "true" ] && docker start "$container" >/dev/null' EXIT
    docker run --rm -v "$vol":/data -v "$dumpdir":/backups "$IMAGE" \
      neo4j-admin database load "$DB" --from-path=/backups --overwrite-destination=true
    echo "restored $DB from $target"
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
