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
set -a
# shellcheck disable=SC1091
. ./.env
set +a

req() { # req VAR "human reason"
  local v="${!1:-}"
  [ -n "$v" ] || die "missing required .env var $1  ($2)"
}
forbid_default() { # forbid_default VAR baddefault
  [ "${!1:-}" != "$2" ] || die "$1 is still the insecure dev default ($2) · set a real value."
}

req POSTGRES_PASSWORD                 "prod Postgres password"
req MINIO_ROOT_USER                  "prod object-store user"
req MINIO_ROOT_PASSWORD              "prod object-store password"
req ACME_EMAIL                       "Caddy refuses to start without an ACME account email"
req SITE_HOST                       "canonical production hostname"
req CX_PUBLIC_CONTROL_ORIGIN        "canonical public HTTPS control-plane origin"
req STRIPE_SECRET_KEY               "live buyer-charge and supplier-payout rail"
req STRIPE_WEBHOOK_SECRET           "buyer billing/cash-event webhook verification"
req CX_CONNECT_WEBHOOK_SECRET       "connected-account webhook verification"
req CX_CONNECT_RETURN_URL           "Stripe Connect onboarding return URL"
req CX_CONNECT_REFRESH_URL          "Stripe Connect onboarding refresh URL"
req CX_TOKEN_KEY                    "OAuth token and customer-webhook secret encryption"
req CX_VERIFICATION_SAMPLE_SECRET   "unpredictable verification sampling"
req CX_ECON_SCHEDULE_VERSION        "versioned production economics"
req CX_PROCESSOR_PERCENT_BPS        "processor variable fee input"
req CX_PROCESSOR_FIXED_USD          "processor fixed fee input"
req CX_CONTROL_PLANE_PER_TASK_USD   "control-plane task cost input"
req CX_TARGET_MARGIN_BPS            "minimum target margin input"
forbid_default POSTGRES_PASSWORD   "cx"
forbid_default MINIO_ROOT_USER     "minioadmin"
forbid_default MINIO_ROOT_PASSWORD "minioadmin"

case "$STRIPE_SECRET_KEY" in
  sk_live_*) ;;
  *) die "STRIPE_SECRET_KEY must be an sk_live_ key for this LIVE production bootstrap." ;;
esac
case "$STRIPE_WEBHOOK_SECRET" in whsec_*) ;; *) die "STRIPE_WEBHOOK_SECRET must look like whsec_…." ;; esac
case "$CX_CONNECT_WEBHOOK_SECRET" in whsec_*) ;; *) die "CX_CONNECT_WEBHOOK_SECRET must look like whsec_…." ;; esac
[ "$STRIPE_WEBHOOK_SECRET" != "$CX_CONNECT_WEBHOOK_SECRET" ] || die "billing and Connect webhook secrets must be distinct endpoint secrets."
[ "${#CX_TOKEN_KEY}" -ge 32 ] || die "CX_TOKEN_KEY must contain at least 32 unpredictable bytes."
[ "${#CX_VERIFICATION_SAMPLE_SECRET}" -ge 32 ] || die "CX_VERIFICATION_SAMPLE_SECRET must contain at least 32 unpredictable bytes."

[ "$MONITORING" -eq 1 ] && req GRAFANA_ADMIN_PASSWORD "Grafana refuses to start without an admin password"

# Soft warnings · deploy proceeds, but say plainly what will be inert.
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
[ -n "${SLACK_WEBHOOK_URL:-}${PAGERDUTY_ROUTING_KEY:-}" ] || warn "no alert channel (SLACK_WEBHOOK_URL / PAGERDUTY_ROUTING_KEY) · Alertmanager will log delivery errors."

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

# Validate the fully interpolated production graph before touching backups or
# migrations. Re-run after an optional pull so newly-required variables fail here,
# not halfway through a deployment.
log "validate production compose configuration"
docker compose -f docker-compose.prod.yml config -q \
  || die "production compose configuration is incomplete or invalid · no backup/migration was attempted."

# ── 4. backup BEFORE migrating (recoverable rollout) ─────────────────────────
if [ "$SKIP_BACKUP" -eq 0 ]; then
  if docker compose -f docker-compose.prod.yml ps -q postgres 2>/dev/null | grep -q .; then
    if [ -n "${CX_BACKUP_OFFSITE:-}" ]; then
      log "pre-deploy OFFSITE backup (scripts/backup.sh)"
      bash scripts/backup.sh || die "pre-deploy backup FAILED · not deploying over un-backed-up data. Fix offsite creds or use --skip-backup (not recommended)."
    else
      log "pre-deploy LOCAL backup (CX_BACKUP_OFFSITE unset · dumping the live DB to disk)"
      mkdir -p .artifacts/backups
      dump=".artifacts/backups/predeploy-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
      docker compose -f docker-compose.prod.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$dump" \
        || die "local pre-deploy pg_dump FAILED · not migrating over un-backed-up data."
      [ -s "$dump" ] || die "local pre-deploy backup is empty · aborting."
      warn "live DB backed up LOCALLY to $dump ($(du -h "$dump" | cut -f1)). Same-host only · set CX_BACKUP_OFFSITE for a real offsite copy."
    fi
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
Stripe endpoints and their distinct secrets were required before this deploy.
Verify them now with live-mode test deliveries (no money movement):
  • https://$HOST/v1/stripe/webhook          → saved-card, collection, refund, and dispute events
  • https://$HOST/v1/stripe/connect-webhook  → account.updated, Connect scope enabled
And once:
  • add the daily backup cron:   0 3 * * *  cd $ROOT && ./scripts/backup.sh >> /var/log/cx-backup.log 2>&1
  • run the restore drill:        scripts/restore.sh --latest --to cx_restore   (prove the backup restores)
NEXT
