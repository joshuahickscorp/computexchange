#!/usr/bin/env bash
# stripe-webhooks.sh — register the two Stripe webhook endpoints CX needs and write
# their signing secrets into .env, so the whole webhook step is one command instead
# of a hand-click in the Stripe dashboard.
#
# It creates (idempotently — reuses an existing endpoint at the same URL):
#   1. https://$HOST/v1/stripe/webhook          → setup_intent.succeeded,
#                                                   payment_method.attached
#        → STRIPE_WEBHOOK_SECRET   (enables the auto-charge once a card is saved;
#          control/billing.go handleStripeWebhook)
#   2. https://$HOST/v1/stripe/connect-webhook   → account.updated
#        → CX_CONNECT_WEBHOOK_SECRET  (flips a supplier's payouts_enabled when Stripe
#          finishes KYC; control/suppliers.go handleConnectWebhook)
#
# The signing secret (whsec_…) is returned by the Stripe API ONLY at creation time,
# so for a NEWLY created endpoint we capture it and write it to .env. For an endpoint
# that already existed we cannot re-read its secret via the API — the script says so
# and points you at the dashboard "reveal" (it never invents a secret).
#
# HONESTY (BLACKHOLE): a live sk_live_ key creates LIVE endpoints on your real Stripe
# account. Nothing is faked. Read STRIPE_SECRET_KEY from .env or the environment.
#
# ── What you type ────────────────────────────────────────────────────────────
#   # after scripts/setup-keys.sh has put STRIPE_SECRET_KEY in .env:
#   HOST=computexchange.net bash scripts/stripe-webhooks.sh
#
# Env:
#   HOST                 public hostname the endpoints point at (no scheme).
#                        Default: SITE_HOST from .env, else computexchange.net.
#   STRIPE_SECRET_KEY    sk_live_… (or sk_test_… to rehearse on test data). Read from
#                        the environment first, then from .env.
#   WRITE_ENV=0          print the secrets instead of writing them into .env.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ENV=.env

die()  { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }
hr()   { echo >&2; echo "== $* ==" >&2; }

command -v jq   >/dev/null 2>&1 || die "jq not found (brew install jq)"
command -v curl >/dev/null 2>&1 || die "curl not found"

# envval KEY — the current uncommented value of KEY in .env ("" if unset/commented).
envval() { [ -f "$ENV" ] && grep -E "^[[:space:]]*$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# set_env KEY VALUE — replace KEY (commented or not) in .env, else append. Mirrors
# scripts/setup-keys.sh so the two scripts agree on .env formatting + perms.
set_env() {
  local key="$1" val="$2" tmp
  [ -f "$ENV" ] || { cp .env.example "$ENV" 2>/dev/null || touch "$ENV"; }
  chmod 600 "$ENV"
  tmp="$(mktemp)"
  grep -vE "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV" > "$tmp" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV"; chmod 600 "$ENV"
}

SK="${STRIPE_SECRET_KEY:-$(envval STRIPE_SECRET_KEY)}"
[ -n "$SK" ] || die "STRIPE_SECRET_KEY not set (run scripts/setup-keys.sh first, or export it). Never commit it."
case "$SK" in
  sk_live_*) info "using a LIVE key — endpoints will be created on your real Stripe account" ;;
  sk_test_*) info "using a TEST key — endpoints created against Stripe test data (safe rehearsal)" ;;
  rk_*)      die "that is a RESTRICTED key (rk_…). Webhook management needs the standard secret key (sk_…)." ;;
  *)         die "STRIPE_SECRET_KEY does not look like an sk_ key" ;;
esac

HOST="${HOST:-$(envval SITE_HOST)}"
HOST="${HOST:-computexchange.net}"
HOST="${HOST#http://}"; HOST="${HOST#https://}"; HOST="${HOST%%/*}"   # tolerate a pasted URL
info "target host: https://$HOST"

# stripe_api METHOD PATH [curl-data-args…] — call the Stripe REST API with the secret
# key as Basic-auth user, fail loudly on an API error.
stripe_api() {
  local method="$1" path="$2"; shift 2
  local resp
  resp="$(curl -fsS -X "$method" "https://api.stripe.com/v1/$path" -u "$SK:" "$@" 2>/dev/null)" \
    || die "Stripe API $method /$path failed (network, or the key lacks permission)"
  if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
    die "Stripe API error: $(echo "$resp" | jq -r '.error.message')"
  fi
  echo "$resp"
}

# find_endpoint_id URL — id of an existing enabled endpoint at URL, or "".
find_endpoint_id() {
  local url="$1"
  stripe_api GET "webhook_endpoints?limit=100" \
    | jq -r --arg u "$url" '.data[] | select(.url==$u) | .id' | head -1
}

# ensure_endpoint URL ENVKEY EVENT[,EVENT…] — create the endpoint if absent; on a
# fresh create, write its whsec_ secret to ENVKEY in .env. Returns via globals.
ensure_endpoint() {
  local url="$1" envkey="$2" events="$3"
  hr "$url"
  local existing
  existing="$(find_endpoint_id "$url")"
  if [ -n "$existing" ]; then
    info "endpoint already exists ($existing) — NOT recreating."
    if [ -n "$(envval "$envkey")" ]; then
      info "$envkey is already set in .env — leaving it."
    else
      info "Stripe will NOT re-reveal an existing endpoint's secret via API."
      info "  Get it from: Stripe dashboard → Developers → Webhooks → $url → Signing secret → Reveal,"
      info "  then: set $envkey=whsec_… in .env (or delete the endpoint and re-run to mint a fresh one)."
    fi
    return 0
  fi
  # Build the -d event args.
  local args=(-d "url=$url")
  local ev; IFS=',' read -ra EVS <<< "$events"
  for ev in "${EVS[@]}"; do args+=(-d "enabled_events[]=$ev"); done
  args+=(-d "description=computexchange $envkey")
  local resp secret id
  resp="$(stripe_api POST "webhook_endpoints" "${args[@]}")"
  id="$(echo "$resp" | jq -r '.id')"
  secret="$(echo "$resp" | jq -r '.secret // empty')"
  [ -n "$secret" ] || die "created endpoint $id but Stripe returned no signing secret (unexpected) — check the dashboard"
  info "created $id for events: $events"
  if [ "${WRITE_ENV:-1}" = "1" ]; then
    set_env "$envkey" "$secret"
    info "wrote $envkey → .env (whsec_…${secret: -4})"
  else
    echo "$envkey=$secret"
  fi
}

ensure_endpoint "https://$HOST/v1/stripe/webhook"         "STRIPE_WEBHOOK_SECRET"     "setup_intent.succeeded,payment_method.attached"
ensure_endpoint "https://$HOST/v1/stripe/connect-webhook" "CX_CONNECT_WEBHOOK_SECRET" "account.updated"

hr "done"
info "If secrets were written to .env, restart the control plane so it loads them:"
info "  local:  make control     ·     prod:  cx reload   (or docker compose -f docker-compose.prod.yml up -d control)"
