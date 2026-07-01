#!/usr/bin/env bash
# mint-admin-key.sh — mint a break-glass ADMIN api key for the /admin panel.
#
# Run this ON the droplet (it talks to the compose Postgres). It generates a strong
# random key, stores ONLY its SHA-256 hash (matching the control plane's hashKey), and
# prints the raw key ONCE. Save it in your password manager: it is
#   (a) the bootstrap credential you paste at https://<host>/admin to register your
#       first passkey, and
#   (b) your permanent BREAK-GLASS — if a passkey is ever lost, this key still opens
#       /admin (authAdmin accepts it directly).
#
# The raw key is shown once and is unrecoverable from the DB. Re-run any time to mint
# another (e.g. to rotate); revoke old ones in the api_keys table.
#
# Usage (on the droplet):  cd /opt/computexchange && bash scripts/mint-admin-key.sh
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

COMPOSE="${CX_COMPOSE_FILE:-docker-compose.prod.yml}"
command -v openssl >/dev/null 2>&1 || { echo "ERROR · openssl not found" >&2; exit 1; }

# A high-entropy key with an obvious prefix. sha256sum (or shasum -a 256) → the same
# hex hashKey() stores, so it authenticates.
KEY="cx_admin_$(openssl rand -hex 24)"
if command -v sha256sum >/dev/null 2>&1; then
  HASH="$(printf '%s' "$KEY" | sha256sum | cut -d' ' -f1)"
else
  HASH="$(printf '%s' "$KEY" | shasum -a 256 | cut -d' ' -f1)"
fi

# Insert as an admin key. buyer_id is a fresh uuid (admin reads never dereference it,
# but the column scans into a non-nullable uuid, so it must be non-null).
docker compose -f "$COMPOSE" exec -T postgres \
  psql -U cx -d cx -v ON_ERROR_STOP=1 -c \
  "INSERT INTO api_keys (buyer_id, key_hash, is_admin, revoked, name)
   VALUES (gen_random_uuid(), '$HASH', true, false, 'break-glass admin');" >/dev/null

echo
echo "  ADMIN KEY (save it now — shown once, unrecoverable):"
echo
echo "      $KEY"
echo
echo "  Use it once at  https://\${SITE_HOST:-computexchange.net}/admin  to register your"
echo "  passkey, then keep it as break-glass. Revoke with:"
echo "    UPDATE api_keys SET revoked=true WHERE name='break-glass admin';"
