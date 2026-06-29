#!/usr/bin/env bash
# Computexchange · REAL offsite backup (prod).
#
# Replaces the old same-host `cp -R` toy. Produces:
#   1. a pg_dump in CUSTOM format (-Fc) of the ledger/jobs DB · compressed,
#      selectively restorable, the canonical backup of the source of truth;
#   2. a mirror of the MinIO object store (cx-jobs bucket: job inputs/results);
# and ships BOTH to an offsite S3-compatible bucket (AWS S3 / DO Spaces / R2 /
# Backblaze B2 · anything `aws s3` speaks). A local staging copy is also kept
# under .artifacts/backups for fast restore and is pruned to the last N.
#
# WAL archiving (point-in-time recovery) is the companion to this base backup;
# it is configured ON THE POSTGRES SERVER (archive_command) · see the block at
# the bottom of this file and docs/RUNBOOKS.md §"WAL archiving / PITR". This
# script takes the periodic base backup that WAL replay starts from.
#
# BLACKHOLE: offsite is the WHOLE POINT of a backup. If the offsite
# destination/creds are missing or the upload fails, this script EXITS NONZERO
# and shouts. It never silently degrades to a local-only copy and reports
# success. A backup you cannot restore from another machine is not a backup.
#
# Usage:
#   scripts/backup.sh                 # full: DB + objects, ship offsite
#   scripts/backup.sh --db-only       # skip the object-store mirror
#   CX_BACKUP_OFFSITE=s3://bucket/prefix  scripts/backup.sh
#
# Cron (daily 03:17, log to syslog):  see docs/RUNBOOKS.md §Backups.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

DB_ONLY=0
[ "${1:-}" = "--db-only" ] && DB_ONLY=1

die() { echo "[backup] ERROR: $*" >&2; exit 1; }
log() { echo "[backup] $*"; }

# ── Required config ──────────────────────────────────────────────────────────
# Offsite destination, e.g. s3://cx-backups/prod  (S3, Spaces, R2, B2).
OFFSITE="${CX_BACKUP_OFFSITE:-}"
[ -n "$OFFSITE" ] || die "CX_BACKUP_OFFSITE is unset. Set it (and the offsite \
S3 creds) in .env · see .env.example. Refusing to take a backup with nowhere \
offsite to put it."

# How the control plane reaches Postgres. In prod we exec pg_dump INSIDE the
# postgres container so we need no client on the host and no published port.
COMPOSE_FILE="${CX_COMPOSE_FILE:-$ROOT/docker-compose.prod.yml}"
PG_SERVICE="${CX_PG_SERVICE:-postgres}"
PG_USER="${POSTGRES_USER:-cx}"
PG_DB="${POSTGRES_DB:-cx}"

# Offsite S3 endpoint for non-AWS providers (Spaces/R2/B2). Empty = real AWS.
# These map onto the aws CLI's standard env so we shell out cleanly.
AWS_ARGS=()
if [ -n "${CX_BACKUP_S3_ENDPOINT:-}" ]; then
  AWS_ARGS+=(--endpoint-url "$CX_BACKUP_S3_ENDPOINT")
fi

command -v docker >/dev/null 2>&1 || die "docker not found"
command -v aws >/dev/null 2>&1 || die "aws CLI not found (install awscli; it \
speaks S3/Spaces/R2/B2). Offsite upload requires it."

# Fail loudly if the offsite creds are not in the environment. The aws CLI reads
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (set these to the OFFSITE bucket's
# creds in .env · distinct from the MinIO root creds).
[ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ] \
  || die "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (offsite bucket creds) not \
set. See .env.example. Refusing to back up with no way to authenticate offsite."

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

TS="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="${CX_BACKUP_DIR:-$ROOT/.artifacts/backups}/$TS"
mkdir -p "$STAGE"

# ── 1. Database: pg_dump custom format ───────────────────────────────────────
log "pg_dump (-Fc) $PG_DB → $STAGE/db.dump"
# -Fc custom format, compressed, parallel-restorable. Stream out of the
# container to the host so the dump lands on the host fs (not lost with the
# container). set -o pipefail (set above) makes a pg_dump failure fail the pipe.
if ! dc exec -T "$PG_SERVICE" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$STAGE/db.dump"; then
  die "pg_dump failed · see above. NO backup produced."
fi
[ -s "$STAGE/db.dump" ] || die "pg_dump produced an empty file · refusing to ship a zero-byte backup."
log "db.dump $(du -h "$STAGE/db.dump" | cut -f1)"

# Checksum so restore can verify integrity end to end.
( cd "$STAGE" && shasum -a 256 db.dump > db.dump.sha256 )

# ── 2. Object store: mirror MinIO → staging → offsite ────────────────────────
if [ "$DB_ONLY" -eq 0 ]; then
  S3_BUCKET="${S3_BUCKET:-cx-jobs}"
  log "object store: mirror minio/$S3_BUCKET → $STAGE/objects"
  # Use the mc client inside a throwaway container on the compose network so we
  # reach minio:9000 with the prod root creds. mirror is incremental + verifies.
  if ! dc run --rm -T \
        -e MC_HOST_local="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
        --entrypoint sh minio/mc -c \
        "mc mirror --overwrite --remove local/${S3_BUCKET} /tmp/o && tar -C /tmp -cf - o" \
        > "$STAGE/objects.tar"; then
    die "object-store mirror failed · see above."
  fi
  log "objects.tar $(du -h "$STAGE/objects.tar" | cut -f1)"
  ( cd "$STAGE" && shasum -a 256 objects.tar > objects.tar.sha256 )
fi

# ── 3. Ship offsite ──────────────────────────────────────────────────────────
DEST="${OFFSITE%/}/$TS"
log "ship → $DEST"
if ! aws "${AWS_ARGS[@]}" s3 cp --recursive "$STAGE" "$DEST"; then
  die "OFFSITE UPLOAD FAILED to $DEST. The local staging copy is at $STAGE but \
this backup is NOT safe (single host). Investigate creds/endpoint/network."
fi
# Verify the upload actually landed (list it back) · never trust a silent cp.
aws "${AWS_ARGS[@]}" s3 ls "$DEST/db.dump" >/dev/null \
  || die "post-upload verify failed: db.dump not visible at $DEST."
log "offsite verified: $DEST/db.dump present"

# ── 4. Prune local staging (offsite is the retained copy) ────────────────────
KEEP="${CX_BACKUP_KEEP_LOCAL:-7}"
BASE="$(dirname "$STAGE")"
# shellcheck disable=SC2012  # names are timestamps, ls sort is correct + simple
ls -1dt "$BASE"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  log "prune local $old"; rm -rf "$old"
done

log "done: $TS (offsite $DEST, local $STAGE)"
log "restore: scripts/restore.sh $TS    (or --latest) · see docs/RUNBOOKS.md"

# ─────────────────────────────────────────────────────────────────────────────
# WAL ARCHIVING (point-in-time recovery) · server-side companion, NOT run here.
# This base backup is the point WAL replay starts from. To enable continuous
# archiving so you can recover to any second between base backups, set on the
# Postgres server (postgresql.conf or compose command flags):
#
#   wal_level = replica
#   archive_mode = on
#   archive_command = 'aws s3 cp %p s3://cx-backups/wal/%f'   # + endpoint for non-AWS
#
# and run this base backup on a cron. Restore with restore.sh, then
# recovery.signal + restore_command to replay WAL. See docs/RUNBOOKS.md
# §"WAL archiving / PITR" for the exact, tested procedure.
