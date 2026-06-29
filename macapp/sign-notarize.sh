#!/usr/bin/env bash
#
# sign-notarize.sh · codesign (Developer ID + Hardened Runtime), notarize
# (notarytool), and staple the ComputeExchangeAgent.app bundle, then produce a
# Sparkle-ready zip and (optionally) the EdDSA-signed appcast entry.
#
# HONESTY (BLACKHOLE): every required Apple credential is read from the environment.
# If any is unset this script FAILS LOUDLY with the exact missing variable and
# exits non-zero. It never pretends to sign or notarize. There is no fallback that
# emits a "signed" artifact without a real Developer ID and a real notarytool
# round-trip.
#
# ---------------------------------------------------------------------------------
# Required environment (the OWNER supplies these · see macapp/README.md):
#
#   DEVELOPER_ID   "Developer ID Application: Your Name (TEAMID)"
#                  The codesigning identity. Must be present in your login keychain
#                  (an Apple Developer Program membership issues it).
#   TEAM_ID        Your 10-char Apple Developer Team ID (e.g. AB12CD34EF).
#
# Notarization credentials · provide EITHER a keychain profile OR the Apple-ID trio:
#
#   (A) NOTARY_PROFILE   name of a stored notarytool keychain profile, created once:
#                          xcrun notarytool store-credentials "$NOTARY_PROFILE" \
#                            --apple-id "$APPLE_ID" --team-id "$TEAM_ID" \
#                            --password "<app-specific-password>"
#   (B) APPLE_ID         your Apple ID email, AND
#       APPLE_PASSWORD   an app-specific password (appleid.apple.com → Security),
#       TEAM_ID          (already required above).
#
# Optional:
#   APP_PATH       path to the built .app (default: build/ComputeExchangeAgent.app).
#   SPARKLE_KEY    path to Sparkle's `sign_update` tool. If set, the script signs
#                  the output zip and prints the appcast <enclosure> attributes
#                  (edSignature + length) for you to paste into appcast.xml.
# ---------------------------------------------------------------------------------

set -euo pipefail

# Use the middot, never an em/en dash, in any message this script prints.
fail() { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }

APP_PATH="${APP_PATH:-build/ComputeExchangeAgent.app}"

# --- credential preflight: fail loudly, list exactly what is missing -------------
missing=()
[ -n "${DEVELOPER_ID:-}" ] || missing+=("DEVELOPER_ID")
[ -n "${TEAM_ID:-}" ]      || missing+=("TEAM_ID")

# Notarization: keychain profile OR the Apple-ID trio.
use_profile=0
if [ -n "${NOTARY_PROFILE:-}" ]; then
  use_profile=1
else
  [ -n "${APPLE_ID:-}" ]       || missing+=("APPLE_ID (or set NOTARY_PROFILE)")
  [ -n "${APPLE_PASSWORD:-}" ] || missing+=("APPLE_PASSWORD (or set NOTARY_PROFILE)")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  fail "missing required credential(s): ${missing[*]}
This script does NOT fake signing or notarization. Supply the variable(s) above and re-run.
See macapp/README.md (section: Signing and notarization) for how to obtain each one."
fi

[ -d "$APP_PATH" ] || fail "app bundle not found at '$APP_PATH'. Build it first (see README), or set APP_PATH."

# Verify the codesigning identity actually exists in a keychain before we start.
if ! security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
  fail "no 'Developer ID Application' codesigning identity found in your keychains.
Install your Developer ID certificate (Apple Developer Program) before signing."
fi

# --- 1. codesign inside-out (bundled cx-agent first, then the .app) --------------
AGENT_BIN="$APP_PATH/Contents/Resources/cx-agent"
ENTITLEMENTS="ComputeExchangeAgent/ComputeExchangeAgent.entitlements"
[ -f "$ENTITLEMENTS" ] || fail "entitlements file not found at '$ENTITLEMENTS'."

if [ -f "$AGENT_BIN" ]; then
  info "codesigning bundled cx-agent"
  codesign --force --options runtime --timestamp \
    --sign "$DEVELOPER_ID" "$AGENT_BIN"
else
  info "no bundled cx-agent at '$AGENT_BIN' · skipping (agent ships separately?)"
fi

# Sparkle ships helper tools (Autoupdate, Updater.app, XPC services) inside its
# framework that must each be signed with Hardened Runtime. --deep handles the
# nested framework; we sign the whole bundle with the app entitlements.
info "codesigning ComputeExchangeAgent.app (Hardened Runtime + entitlements)"
codesign --force --options runtime --timestamp --deep \
  --entitlements "$ENTITLEMENTS" \
  --sign "$DEVELOPER_ID" "$APP_PATH"

info "verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

# --- 2. zip for notarization (preserve bundle structure) -------------------------
ZIP_PATH="${APP_PATH%.app}.zip"
info "packing $ZIP_PATH for notarization"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

# --- 3. notarize (notarytool, --wait blocks until Apple returns a verdict) -------
info "submitting to Apple notary service (this can take a few minutes)"
if [ "$use_profile" -eq 1 ]; then
  xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
else
  xcrun notarytool submit "$ZIP_PATH" \
    --apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" --team-id "$TEAM_ID" --wait
fi

# --- 4. staple the ticket onto the .app, then re-zip the stapled bundle ----------
info "stapling notarization ticket"
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"

info "Gatekeeper assessment"
spctl --assess --type execute --verbose=4 "$APP_PATH" || \
  fail "spctl assessment failed · the app would be blocked by Gatekeeper."

# Re-zip the STAPLED bundle so the artifact Sparkle ships carries the ticket
# (offline installs then verify without a network round-trip).
info "re-packing stapled bundle into $ZIP_PATH"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

# --- 5. (optional) EdDSA-sign the zip for the Sparkle appcast ---------------------
if [ -n "${SPARKLE_KEY:-}" ]; then
  [ -x "$SPARKLE_KEY" ] || fail "SPARKLE_KEY '$SPARKLE_KEY' is not an executable (point it at Sparkle's sign_update)."
  info "EdDSA-signing $ZIP_PATH for the appcast"
  echo "Paste these into the appcast <enclosure> for this build:" >&2
  "$SPARKLE_KEY" "$ZIP_PATH"
fi

echo "OK · signed + notarized + stapled: $APP_PATH" >&2
echo "OK · Sparkle artifact: $ZIP_PATH" >&2
