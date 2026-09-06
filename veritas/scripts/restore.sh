#!/usr/bin/env bash
# Veritas restore procedure (architecture §13 Q2). Restores a full backup
# (created by backup.sh) into a database and object-store location.
#
# Usage:
#   restore.sh <backup_dir> [target_db_url]
#
# <backup_dir> must contain db.dump (and optionally storage.tar.gz) as written by
# backup.sh. The database is restored with --clean --if-exists (the restored dump
# is the authoritative snapshot); encrypted blobs are unpacked into the store.
#
# Config (environment): VERITAS_DATABASE_URL (used when <target_db_url> omitted),
#   VERITAS_STORAGE_ROOT.
set -euo pipefail

BACKUP_DIR="${1:?usage: restore.sh <backup_dir> [target_db_url]}"
DB_URL="${2:-${VERITAS_DATABASE_URL:-postgresql://localhost/veritas}}"
STORAGE="${VERITAS_STORAGE_ROOT:-./data/objects}"

if ! command -v pg_restore >/dev/null 2>&1; then
  echo "restore.sh: pg_restore not found (install postgresql-client-16)" >&2
  exit 1
fi
[ -f "$BACKUP_DIR/db.dump" ] || { echo "restore.sh: no db.dump in $BACKUP_DIR" >&2; exit 1; }

# Database — drop existing objects and recreate from the backup snapshot.
pg_restore --dbname="$DB_URL" --clean --if-exists --no-owner "$BACKUP_DIR/db.dump"

# Object store — unpack retained encrypted blobs (destructive replace on restore).
if [ -f "$BACKUP_DIR/storage.tar.gz" ]; then
  mkdir -p "$STORAGE"
  tar -xzf "$BACKUP_DIR/storage.tar.gz" -C "$(dirname "$STORAGE")"
fi

echo "restore complete from $BACKUP_DIR"
