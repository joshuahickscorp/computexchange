#!/usr/bin/env bash
# assemble-app.sh — build the SwiftPM executable and assemble a runnable
# ComputeExchangeAgent.app bundle, so sign-notarize.sh has something to sign.
#
# This closes the one manual gap README.md called "owner's choice": it builds
# Contents/{MacOS/ComputeExchangeAgent, Info.plist, Resources/cx-agent, Resources/
# AppIcon.icns, Frameworks/Sparkle.framework}. After this, distribution is exactly:
#
#     macapp/assemble-app.sh           # build + assemble → build/ComputeExchangeAgent.app
#     APP_PATH=build/ComputeExchangeAgent.app macapp/sign-notarize.sh   # sign+notarize
#
# HONESTY (BLACKHOLE): this assembles an UNSIGNED bundle only. It never signs or
# notarizes (that is sign-notarize.sh, which needs your Developer ID). An unsigned
# bundle runs locally for YOU but Gatekeeper blocks it for others — which is exactly
# why the signing step is separate and mandatory before distribution.
#
# Env:
#   APP_PATH        output bundle path (default build/ComputeExchangeAgent.app)
#   CONFIG          swift build config: release | debug (default release)
#   SKIP_AGENT=1    do NOT embed a cx-agent binary (dev-only; the app will show its
#                   honest "cx-agent binary not found" state instead of running one)
#   CX_AGENT_BIN    path to a prebuilt cx-agent to embed (default: build ../agent)
set -euo pipefail
cd "$(dirname "$0")"                     # macapp/
ROOT="$(cd .. && pwd)"

APP_PATH="${APP_PATH:-$ROOT/build/ComputeExchangeAgent.app}"
CONFIG="${CONFIG:-release}"

fail() { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }

command -v swift >/dev/null 2>&1 || fail "swift not found — install Xcode / the Command Line Tools"
case "$CONFIG" in release|debug) ;; *) fail "CONFIG must be release or debug, got '$CONFIG'";; esac

# ── 1. build the SwiftPM executable ──────────────────────────────────────────
info "swift build ($CONFIG)"
swift build --package-path . -c "$CONFIG"
EXE="$(swift build --package-path . -c "$CONFIG" --show-bin-path)/ComputeExchangeAgent"
[ -x "$EXE" ] || fail "built executable not found at '$EXE'"

# ── 2. (re)create the bundle skeleton ────────────────────────────────────────
info "assembling bundle at $APP_PATH"
rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources" "$APP_PATH/Contents/Frameworks"

cp "$EXE" "$APP_PATH/Contents/MacOS/ComputeExchangeAgent"
cp "ComputeExchangeAgent/Info.plist" "$APP_PATH/Contents/Info.plist"

# ── 3. embed the cx-agent Rust binary (AgentPaths.bundledBinary resolves it) ──
if [ "${SKIP_AGENT:-0}" = "1" ]; then
  info "SKIP_AGENT=1 — not embedding cx-agent (the app will show its honest not-found state)"
else
  AGENT_BIN="${CX_AGENT_BIN:-}"
  if [ -z "$AGENT_BIN" ]; then
    info "building cx-agent (release, default features = Metal) to embed"
    ( cd "$ROOT/agent" && cargo build --release ) || fail "cx-agent build failed (set CX_AGENT_BIN to a prebuilt binary, or SKIP_AGENT=1)"
    AGENT_BIN="$ROOT/agent/target/release/cx-agent"
  fi
  [ -x "$AGENT_BIN" ] || fail "cx-agent binary not found/executable at '$AGENT_BIN'"
  cp "$AGENT_BIN" "$APP_PATH/Contents/Resources/cx-agent"
  info "embedded cx-agent from $AGENT_BIN"
fi

# ── 4. app icon (.icns) from the PNG, if the tools + source exist ────────────
ICON_SRC="$ROOT/logo/cx-app-icon.png"
if [ -f "$ICON_SRC" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  info "rendering AppIcon.icns from $ICON_SRC"
  ICONSET="$(mktemp -d)/AppIcon.iconset"; mkdir -p "$ICONSET"
  for sz in 16 32 128 256 512; do
    sips -z "$sz" "$sz"       "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}.png"     >/dev/null 2>&1
    sips -z $((sz*2)) $((sz*2)) "$ICON_SRC" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
  done
  if iconutil -c icns "$ICONSET" -o "$APP_PATH/Contents/Resources/AppIcon.icns" 2>/dev/null; then
    # Register the icon file with the bundle so Finder/menu use it.
    /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP_PATH/Contents/Info.plist" 2>/dev/null \
      || /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$APP_PATH/Contents/Info.plist" 2>/dev/null || true
    info "AppIcon.icns embedded"
  else
    info "iconutil failed — shipping without a custom icon (cosmetic, not a blocker)"
  fi
else
  info "icon source or sips/iconutil missing — shipping without a custom icon (cosmetic)"
fi

# ── 5. embed Sparkle.framework from the resolved SwiftPM artifact ─────────────
# Sparkle 2 ships its runtime + helper tools (Autoupdate, Updater.app, XPC) inside
# Sparkle.framework. The app links it, so the bundle must carry it in Frameworks/
# (sign-notarize.sh signs the nested helpers with --deep). Locate it in the SPM
# build/checkout tree; if not found, warn — the app still launches, but auto-update
# won't work until the framework is embedded.
SPARKLE_FW=""
for cand in \
  "$(swift build --package-path . -c "$CONFIG" --show-bin-path)/Sparkle.framework" \
  "$ROOT/build/artifacts"/*/Sparkle/Sparkle.framework \
  ".build/artifacts"/*/Sparkle/Sparkle.framework \
  ".build/checkouts/Sparkle/Sparkle.framework"; do
  if [ -d "$cand" ]; then SPARKLE_FW="$cand"; break; fi
done
if [ -n "$SPARKLE_FW" ]; then
  info "embedding Sparkle.framework from $SPARKLE_FW"
  cp -R "$SPARKLE_FW" "$APP_PATH/Contents/Frameworks/"
else
  info "Sparkle.framework not located in the build tree — bundle assembled WITHOUT it."
  info "  Auto-update stays inert until it is embedded. Find it with:"
  info "    find .build \$HOME/Library/Developer -name Sparkle.framework -type d 2>/dev/null | head"
  info "  then: cp -R <path> '$APP_PATH/Contents/Frameworks/'"
fi

# ── 6. sanity: the bundle is structurally an app ─────────────────────────────
/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$APP_PATH/Contents/Info.plist" >/dev/null 2>&1 \
  || fail "assembled Info.plist is missing CFBundleExecutable — bundle is malformed"

echo >&2
echo "OK · assembled UNSIGNED bundle: $APP_PATH" >&2
echo "Next (needs your Apple Developer ID · see macapp/README.md):" >&2
echo "  APP_PATH='$APP_PATH' macapp/sign-notarize.sh" >&2
