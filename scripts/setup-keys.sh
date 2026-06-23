#!/usr/bin/env bash
# setup-keys.sh — paste provider keys in (hidden) and save them straight to .env.
#
# Secrets are read with `read -s`, so they never appear on screen or in shell
# scrollback. .env is created from .env.example if absent and chmod'd to 600. The
# script is re-runnable: a blank answer keeps the current value. The two hardening
# secrets (CX_TOKEN_KEY / CX_STATE_SECRET) are auto-generated if unset. Each prompt
# prints exactly where to find the value in the provider's dashboard.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV=.env
EXAMPLE=.env.example

if [ ! -f "$ENV" ]; then
  cp "$EXAMPLE" "$ENV"
  echo "created $ENV from $EXAMPLE"
fi
chmod 600 "$ENV"

# set_env KEY VALUE — replace KEY (commented or not) in .env, else append it. We
# strip any existing line and append a fresh one (robust against / & $ in values).
set_env() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  grep -vE "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV" > "$tmp" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV"
  chmod 600 "$ENV"
}

# current KEY — the current UNCOMMENTED value ("" if unset or only a commented stub).
current() {
  grep -E "^[[:space:]]*$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# mask VALUE — a hint only, never the secret itself.
mask() {
  local v="$1"
  if [ -z "$v" ]; then echo "(unset)"; elif [ "${#v}" -le 8 ]; then echo "••••"; else echo "${v:0:7}…${v: -4}"; fi
}

prompt_secret() {
  local key="$1" label="$2" where="$3" cur val
  cur="$(current "$key")"
  echo
  echo "▸ $label"
  echo "  where: $where"
  echo "  current: $(mask "$cur")"
  printf "  paste value (hidden — Enter to keep current): "
  read -rs val; echo
  if [ -n "$val" ]; then set_env "$key" "$val"; echo "  ✓ saved $key"; else echo "  – kept current"; fi
}

prompt_plain() {
  local key="$1" label="$2" where="$3" def="${4:-}" cur val
  cur="$(current "$key")"
  echo
  echo "▸ $label"
  echo "  where: $where"
  echo "  current: ${cur:-(unset)}"
  if [ -z "$cur" ] && [ -n "$def" ]; then
    printf "  value (Enter for default: %s): " "$def"
    read -r val
    val="${val:-$def}"
  else
    printf "  value (Enter to keep current): "
    read -r val
  fi
  if [ -n "$val" ]; then set_env "$key" "$val"; echo "  ✓ saved $key"; else echo "  – kept current"; fi
}

echo "Computexchange — key setup."
echo "Secrets stay hidden as you paste and are written to $ENV (chmod 600). Blank = skip."
echo "Tip: do this in Stripe TEST mode first (toggle top-right of the dashboard)."

# ── Stripe ───────────────────────────────────────────────────────────────────
prompt_secret STRIPE_SECRET_KEY \
  "Stripe SECRET key (sk_test_… / sk_live_…)" \
  "Dashboard → Developers → API keys → 'Secret key' → Reveal/Copy"

prompt_plain STRIPE_PUBLISHABLE_KEY \
  "Stripe publishable key (pk_… — front-end use, not secret)" \
  "same API keys page → 'Publishable key'"

prompt_secret STRIPE_WEBHOOK_SECRET \
  "Stripe WEBHOOK signing secret (whsec_…)" \
  "Developers → Webhooks → your endpoint (URL …/v1/stripe/webhook) → 'Signing secret' → Reveal. Local: 'stripe listen --print-secret'"

# ── GitHub OAuth app (OPTIONAL — only powers "connect a repo from GitHub") ────
# Derive the local callback default from LISTEN_ADDR (the browser always hits
# localhost; only the port varies). Default :8080 to match .env.example.
gh_addr="$(current LISTEN_ADDR)"; gh_addr="${gh_addr:-:8080}"
gh_port="${gh_addr##*:}"; [ -z "$gh_port" ] && gh_port="8080"
gh_home="http://localhost:${gh_port}"
gh_cb="http://localhost:${gh_port}/v1/connect/github/callback"

cat <<EOF

────────────────────────────────────────────────────────────────────────────
GitHub OAuth App — OPTIONAL. Only needed for "Connect a repo" (buyers importing
code straight from GitHub). Skip all three (blank) and everything else runs fine.

Like the Stripe webhook, you CREATE the app once before these values exist:

  1. Open  https://github.com/settings/developers
     (github.com → your avatar → Settings → Developer settings → OAuth Apps)
  2. Click "New OAuth App"
  3. Fill in:
       Application name            Computexchange      (anything)
       Homepage URL                ${gh_home}
       Authorization callback URL  ${gh_cb}
                                   ^ must match GITHUB_REDIRECT_URL below EXACTLY
  4. Click "Register application"
  5. Copy the "Client ID"                                  -> prompt 1 below
  6. "Generate a new client secret", copy it NOW (shown once) -> prompt 2 below

Production later: edit the app's URLs (or create a second app) for your real
domain, e.g. https://compute.exchange/v1/connect/github/callback, then update
these three values on the server.
────────────────────────────────────────────────────────────────────────────
EOF

prompt_plain GITHUB_CLIENT_ID \
  "GitHub OAuth App client id" \
  "the app you created above -> 'Client ID' (https://github.com/settings/developers)"

prompt_secret GITHUB_CLIENT_SECRET \
  "GitHub OAuth App client secret" \
  "same app page -> 'Generate a new client secret' -> copy immediately (shown once)"

prompt_plain GITHUB_REDIRECT_URL \
  "GitHub OAuth callback URL" \
  "must EXACTLY match the app's 'Authorization callback URL'" \
  "${gh_cb}"

# ── Hardening: auto-generate if unset ────────────────────────────────────────
gen() { openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p -c 64; }
for k in CX_TOKEN_KEY CX_STATE_SECRET; do
  if [ -z "$(current "$k")" ]; then
    set_env "$k" "$(gen)"
    echo
    echo "▸ $k — generated a random secret. ✓ saved"
  fi
done

echo
echo "Done — $ENV updated (chmod 600, gitignored)."
echo "Restart the control plane to load it:   make control"
echo "Then billing/status reports configured:true and worker/connect returns a real onboarding link."
