#!/usr/bin/env bash
# Computexchange — local backup: a timestamped pg_dump of the ledger/jobs DB plus
# a copy of the local object-store data. The internal double-entry ledger is the
# source of truth, so back it up at least as often as you settle payouts. Restore:
# create a fresh DB and `psql <url> -f <dump.sql>` (see docs/RUNBOOKS.md). The
# `disaster-recovery` check in `make prove-local` proves this dump is restorable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
DATABASE_URL="${DATABASE_URL:-postgres://cx:cx@localhost:5432/cx?sslmode=disable}"
OUT="${CX_BACKUP_DIR:-$ROOT/.artifacts/backups}"
DEST="$OUT/$(date +%Y%m%d-%H%M%S)"

command -v pg_dump >/dev/null 2>&1 || { echo "[backup] ERROR: pg_dump not found (install the postgres client)"; exit 1; }
mkdir -p "$DEST"

echo "[backup] pg_dump → $DEST/db.sql"
pg_dump "$DATABASE_URL" >"$DEST/db.sql"

# Local object store: copy the MinIO data dir when present. Managed S3/R2 use the
# provider's own snapshots/versioning instead.
for cand in "${MINIO_DATA:-}" "$ROOT/.artifacts/prove-local/minio-data"; do
  if [ -n "$cand" ] && [ -d "$cand" ]; then
    echo "[backup] object store → $DEST/objects"
    cp -R "$cand" "$DEST/objects"
    break
  fi
done

echo "[backup] done: $DEST ($(du -sh "$DEST" 2>/dev/null | cut -f1))"
echo "[backup] restore: create a fresh DB, then  psql <url> -f $DEST/db.sql   (docs/RUNBOOKS.md)"
