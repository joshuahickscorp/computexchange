#!/usr/bin/env bash
# Computexchange — production deploy / redeploy.
#
# Idempotent: run it to bring the prod stack up the first time, and again to
# ship a new build. It pulls the latest code (optional), validates the compose
# config, builds, rolls the stack with health gating, and runs a post-deploy
# smoke check against the public edge. Safe to re-run.
#
#   scripts/deploy.sh                 # build + up the core stack
#   scripts/deploy.sh --monitoring    # also bring up the monitoring profile
#   scripts/deploy.sh --pull          # git pull --ff-only first, then deploy
#
# BLACKHOLE: if compose config is invalid, a build fails, a service is
# unhealthy after the timeout, or the post-deploy smoke check fails, this exits
# nonzero and shouts. It never reports a green deploy over a broken stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${CX_COMPOSE_FILE:-$ROOT/docker-compose.prod.yml}"

die() { echo "[deploy] ERROR: $*" >&2; exit 1; }
log() { echo "[deploy] $*"; }

PULL=0; PROFILE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --pull)       PULL=1 ;;
    --monitoring) PROFILE_ARGS+=(--profile monitoring) ;;
    *)            die "unknown flag $1" ;;
  esac
  shift
done

# A production image is attributable to one committed source tree. Refuse a dirty
# checkout: labeling uncommitted bytes with HEAD would make /version confidently
# wrong, which is worse than having no provenance endpoint at all.
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  die "worktree is dirty — commit or stash changes before a provenance-bearing deploy"
fi

CX_BUILD_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
export CX_BUILD_VERSION="${CX_BUILD_VERSION:-$(git -C "$ROOT" describe --tags --always --dirty=+dirty)}"
CX_BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export CX_BUILD_COMMIT CX_BUILD_DATE

command -v docker >/dev/null 2>&1 || die "docker not found"
[ -f "$ROOT/.env" ] || die ".env not found at $ROOT/.env — copy .env.example and fill it in."

dc() { docker compose -f "$COMPOSE_FILE" "${PROFILE_ARGS[@]}" "$@"; }

if [ "$PULL" -eq 1 ]; then
  log "git pull --ff-only"
  git -C "$ROOT" pull --ff-only || die "git pull failed (not fast-forward?). Reconcile manually."
fi

log "validate compose config"
dc config -q || die "compose config invalid — fix before deploying."

log "build images"
log "build identity: version=$CX_BUILD_VERSION commit=$CX_BUILD_COMMIT date=$CX_BUILD_DATE"
dc build || die "build failed."

log "roll the stack (up -d, recreate changed)"
dc up -d --remove-orphans || die "compose up failed."

# ── Health gate: wait for both control instances + caddy to be healthy ───────
log "waiting for services to report healthy (timeout 180s)"
deadline=$(( $(date +%s) + 180 ))
need="control control-2 caddy postgres minio"
while :; do
  unhealthy=""
  for svc in $need; do
    cid="$(dc ps -q "$svc" 2>/dev/null || true)"
    [ -n "$cid" ] || { unhealthy="$unhealthy $svc(missing)"; continue; }
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
    case "$state" in
      healthy|running) ;;
      *) unhealthy="$unhealthy $svc($state)" ;;
    esac
  done
  [ -z "$unhealthy" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && die "services not healthy after 180s:$unhealthy. Check  dc logs."
  sleep 5
done
log "all core services healthy"

# ── Post-deploy smoke: the public edge answers /healthz 200 ──────────────────
SITE_HOST="${SITE_HOST:-computexchange.net}"
log "smoke: https://$SITE_HOST/healthz"
if command -v curl >/dev/null 2>&1; then
  code="$(curl -fsS -o /dev/null -w '%{http_code}' "https://$SITE_HOST/healthz" || true)"
  [ "$code" = "200" ] || die "post-deploy smoke FAILED: https://$SITE_HOST/healthz returned '$code' (expected 200). Stack is up but the edge is not serving — check Caddy + DNS + certs."
  log "smoke OK (200)"
else
  log "curl not present; skipping external smoke (verify https://$SITE_HOST/healthz manually)"
fi

# Provenance gate: health is not enough if Caddy is still serving an older image.
# Require the public build endpoint to report the exact commit we just built.
if command -v curl >/dev/null 2>&1; then
  version_json="$(curl -fsS "https://$SITE_HOST/version" || true)"
  reported_commit="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit", ""))' <<<"$version_json" 2>/dev/null || true)"
  [ "$reported_commit" = "$CX_BUILD_COMMIT" ] || die "provenance smoke FAILED: /version commit '$reported_commit' != deployed '$CX_BUILD_COMMIT'"
  log "provenance OK ($reported_commit)"
fi

log "deploy complete."
dc ps
