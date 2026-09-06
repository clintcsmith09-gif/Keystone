#!/usr/bin/env bash
# Veritas automated full backup (architecture §13 Q2 — managed cloud with
# automated full backups). Config-driven; runnable on the MVP host via cron/systemd
# timer. Backs up the Postgres database (logical dump) AND the encrypted object
# store (the blobs are already encrypted at rest, so they ship as-is).
#
# Config (environment):
#   VERITAS_DATABASE_URL  connection string (psycopg v3 / pg_dump URI)
#   VERITAS_STORAGE_ROOT  object-store directory (default ./data/objects)
#   VERITAS_BACKUP_DIR    where backups are written (default ./data/backups)
#   VERITAS_KEEP_BACKUPS  number of backups to retain (default 7)
#
# Prints the backup directory path on success (so schedulers/tests can read it).
# A restore procedure lives in restore.sh; see ops/README.md for a full DR walk.
set -euo pipefail

DB_URL="${VERITAS_DATABASE_URL:-postgresql://localhost/veritas}"
STORAGE="${VERITAS_STORAGE_ROOT:-./data/objects}"
BACKUP_ROOT="${VERITAS_BACKUP_DIR:-./data/backups}"
KEEP="${VERITAS_KEEP_BACKUPS:-7}"

if ! command -v pg_dump >/dev/null 2>&1; then
  echo "backup.sh: pg_dump not found (install postgresql-client-16)" >&2
  exit 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST"

# 1) Logical database dump (no plaintext; the master key is NOT stored in the DB).
pg_dump --dbname="$DB_URL" -Fc -f "$DEST/db.dump"

# 2) Encrypted object store (blobs already encrypted-at-rest via the envelope).
if [ -d "$STORAGE" ]; then
  tar -czf "$DEST/storage.tar.gz" -C "$(dirname "$STORAGE")" "$(basename "$STORAGE")"
fi

echo "$DEST"

# 3) Retention: keep only the newest $KEEP backups (delete older ones we can name).
mapfile -t OLD < <(ls -1t "$BACKUP_ROOT" | tail -n +$((KEEP + 1)))
for d in "${OLD[@]}"; do
  [ -n "$d" ] && rm -rf "$BACKUP_ROOT/$d"
done
