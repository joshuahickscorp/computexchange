#!/usr/bin/env bash
# bootstrap-prod.sh · one-shot, safe production bring-up / upgrade for the droplet.
#
# Run this ON the droplet (192.241.134.31), from the repo root, after filling .env.
# It is a thin, LOUD wrapper around the pieces that already exist:
#   1. preflight  · refuse to run unless docker + compose are present and every
#                   REQUIRED secret in .env is set (BLACKHOLE: never half-deploy);
#   2. confirm    · this targets LIVE prod (Stripe live, real Postgres data), so it
#                   prints what it is about to do and waits for an explicit "yes"
#                   (skip with --yes for automation);
#   3. backup     · because prod already holds real data, take a verified OFFSITE
#                   backup BEFORE migrating, so a bad rollout is recoverable
#                   (scripts/backup.sh; skip only for a genuinely empty box with
#                   --skip-backup);
#   4. deploy     · scripts/deploy.sh validates the compose config, builds, applies
#                   the (idempotent) schema via the migrate service, rolls the stack
#                   with a health gate, and smoke-checks the public edge;
#   5. verify     · curl /healthz + /readyz on the public host.
#
# Idempotent: safe to re-run to ship a new build. It moves no money and fakes
# nothing; any failure exits nonzero and stops.
#
# Usage:
#   bash scripts/bootstrap-prod.sh                 # backup, then deploy core stack
#   bash scripts/bootstrap-prod.sh --monitoring    # also bring up Prometheus/Grafana
#   bash scripts/bootstrap-prod.sh --pull          # git pull --ff-only first
#   bash scripts/bootstrap-prod.sh --skip-backup   # ONLY for a fresh, empty box
#   bash scripts/bootstrap-prod.sh --yes           # do not prompt (CI/automation)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MONITORING=0 ; PULL=0 ; SKIP_BACKUP=0 ; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --monitoring) MONITORING=1 ;;
    --pull)       PULL=1 ;;
    --skip-backup) SKIP_BACKUP=1 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    *) echo "unknown flag: $a" >&2 ; exit 2 ;;
  esac
done

log()  { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. tooling ───────────────────────────────────────────────────────────────
command -v docker >/dev/null || die "docker not installed on this host."
docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is missing (need 'docker compose')."
[ -f .env ] || die "no .env found. Run: cp .env.example .env  and fill the prod secrets, then re-run."
[ -f docker-compose.prod.yml ] || die "docker-compose.prod.yml not found · are you in the repo root?"

# ── 1. preflight: required secrets must be set (and not the dev defaults) ─────
# shellcheck disable=SC1091
set -a ; . ./.env ; set +a

req() { # req VAR "human reason"
  local v="${!1:-}"
  [ -n "$v" ] || die "missing required .env var $1  ($2)"
}
forbid_default() { # forbid_default VAR baddefault
  [ "${!1:-}" != "$2" ] || die "$1 is still the insecure dev default ($2) · set a real value."
}

req POSTGRES_PASSWORD     "prod Postgres password"
req MINIO_ROOT_PASSWORD   "prod object-store password"
req ACME_EMAIL            "Caddy refuses to start without an ACME account email"
forbid_default POSTGRES_PASSWORD   "cx"
forbid_default MINIO_ROOT_PASSWORD "minioadmin"

if [ "$SKIP_BACKUP" -eq 0 ]; then
  req CX_BACKUP_OFFSITE     "offsite backup destination (needed for the pre-deploy backup)"
  req AWS_ACCESS_KEY_ID     "offsite bucket credentials"
  req AWS_SECRET_ACCESS_KEY "offsite bucket credentials"
fi
[ "$MONITORING" -eq 1 ] && req GRAFANA_ADMIN_PASSWORD "Grafana refuses to start without an admin password"

# Soft warnings · deploy proceeds, but say plainly what will be inert.
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
[ -n "${STRIPE_SECRET_KEY:-}" ] || warn "STRIPE_SECRET_KEY unset · buyer charging + supplier payouts will honestly 503 until set."
[ -n "${SLACK_WEBHOOK_URL:-}${PAGERDUTY_ROUTING_KEY:-}" ] || warn "no alert channel (SLACK_WEBHOOK_URL / PAGERDUTY_ROUTING_KEY) · Alertmanager will log delivery errors."
[ -n "${SITE_HOST:-}" ] || warn "SITE_HOST unset · Caddy will use the repo default host."

# ── 2. confirm (LIVE prod) ───────────────────────────────────────────────────
HOST="${SITE_HOST:-computexchange.net}"
log "About to deploy to LIVE prod"
echo "   host:        $HOST"
echo "   compose:     docker-compose.prod.yml$([ $MONITORING -eq 1 ] && echo ' (+ monitoring)')"
echo "   git ref:     $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') @ $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "   pre-backup:  $([ $SKIP_BACKUP -eq 0 ] && echo 'YES (offsite)' || echo 'SKIPPED (--skip-backup)')"
echo "   migrations:  applied by the compose 'migrate' service (schema is idempotent)"
if [ "$ASSUME_YES" -eq 0 ]; then
  printf 'Type "yes" to proceed: '
  read -r ans
  [ "$ans" = "yes" ] || die "aborted."
fi

# ── 3. (optional) pull latest code ───────────────────────────────────────────
if [ "$PULL" -eq 1 ]; then
  log "git pull --ff-only"
  git pull --ff-only || die "git pull failed · resolve before deploying."
fi

# ── 4. backup BEFORE migrating (recoverable rollout) ─────────────────────────
if [ "$SKIP_BACKUP" -eq 0 ]; then
  if docker compose -f docker-compose.prod.yml ps -q postgres 2>/dev/null | grep -q .; then
    log "pre-deploy offsite backup (scripts/backup.sh)"
    bash scripts/backup.sh || die "pre-deploy backup FAILED · not deploying over un-backed-up data. Fix offsite creds and retry."
  else
    warn "no running postgres found · treating as a fresh box, skipping the pre-deploy backup."
  fi
fi

# ── 5. deploy (validate · build · migrate · roll · smoke) ────────────────────
log "deploy (scripts/deploy.sh)"
DEPLOY_ARGS=()
[ "$MONITORING" -eq 1 ] && DEPLOY_ARGS+=(--monitoring)
bash scripts/deploy.sh "${DEPLOY_ARGS[@]}" || die "deploy failed · the stack was health-gated, so it stopped rather than ship broken. Check 'docker compose -f docker-compose.prod.yml logs'."

# ── 6. verify the public edge ────────────────────────────────────────────────
log "verify public edge"
for path in /healthz /readyz; do
  code="$(curl -fsS -o /dev/null -w '%{http_code}' "https://$HOST$path" 2>/dev/null || echo '000')"
  if [ "$code" = "200" ]; then echo "   $path → 200 OK"
  else warn "$path → $code (DNS not pointed yet, certs still issuing, or a real problem · check 'docker compose -f docker-compose.prod.yml logs caddy control')"; fi
done

log "done"
cat <<NEXT
Next, one-time, in the Stripe dashboard (live mode):
  • point a webhook at  https://$HOST/v1/stripe/webhook          (setup_intent.succeeded, payment_method.attached) → STRIPE_WEBHOOK_SECRET
  • point a webhook at  https://$HOST/v1/stripe/connect-webhook  (account.updated)                              → CX_CONNECT_WEBHOOK_SECRET
And once:
  • add the daily backup cron:   0 3 * * *  cd $ROOT && ./scripts/backup.sh >> /var/log/cx-backup.log 2>&1
  • run the restore drill:        scripts/restore.sh --latest --to cx_restore   (prove the backup restores)
NEXT
