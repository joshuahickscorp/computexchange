#!/usr/bin/env bash
# Computexchange — supplier agent installer (macOS, Apple Silicon).
#
# One command for a Mac owner to go from a checkout to an earning agent, unaided:
# it builds the release `cx-agent` from source, installs the binary, writes a
# starter config, and installs a login-time LaunchAgent so the agent runs in the
# background. Signing + notarization of a distributable `.app` is the external
# step (Apple Developer ID); this is the local build-from-source path.
#
#   scripts/install.sh            build + install + write config + LaunchAgent
#   scripts/install.sh --start    the above, then load the LaunchAgent now
#   scripts/install.sh --check    dry run: verify prerequisites, print the plan
#   scripts/install.sh --uninstall   remove everything (delegates to uninstall.sh)
#
# Config comes from the environment when present (so the install is scriptable):
#   CX_CONTROL_URL   control-plane URL (default http://localhost:8080)
#   CX_WORKER_TOKEN  worker token from `make seed` (required to actually earn)
#   CX_PREFIX        install dir for the binary (default ~/.local/bin)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${CX_PREFIX:-$HOME/.local/bin}"
BIN="$PREFIX/cx-agent"
HOMEDIR="$HOME/.compute-exchange"
CONFIG="$HOMEDIR/agent.toml"
PLIST="$HOME/Library/LaunchAgents/dev.computeexchange.agent.plist"
LABEL="dev.computeexchange.agent"

say()  { printf '\033[36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

MODE="install"
case "${1:-}" in
  --check)     MODE="check" ;;
  --start)     MODE="start" ;;
  --uninstall) exec "$ROOT/scripts/uninstall.sh" "${@:2}" ;;
  -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "")          : ;;
  *)           die "unknown flag ${1} (try --help)" ;;
esac

# ── Prerequisites (the same for check + install) ─────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "macOS only (Apple Silicon is the supported supply target)"
command -v cargo >/dev/null 2>&1 || die "Rust toolchain (cargo) required — install from https://rustup.rs"
ARCH="$(uname -m)"
[ "$ARCH" = "arm64" ] || warn "arch is $ARCH, not arm64 — the agent runs but Metal acceleration needs Apple Silicon"

if [ "$MODE" = "check" ]; then
  say "dry run — prerequisites OK (macOS + cargo). This install would:"
  say "  1. build the release agent:  (cd $ROOT/agent && cargo build --release)"
  say "  2. install the binary to:    $BIN"
  say "  3. write a starter config:   $CONFIG  (CX_CONTROL_URL / CX_WORKER_TOKEN)"
  say "  4. install a LaunchAgent:     $PLIST  (runs the agent at login)"
  say "Run without --check to perform the install; --uninstall to remove it."
  exit 0
fi

# ── 1. Build ─────────────────────────────────────────────────────────────────
say "building the release agent (first build downloads + compiles deps; a few minutes)…"
( cd "$ROOT/agent" && cargo build --release ) || die "agent build failed"
SRC_BIN="$ROOT/agent/target/release/cx-agent"
[ -x "$SRC_BIN" ] || die "built binary not found at $SRC_BIN"

# ── 2. Install the binary ────────────────────────────────────────────────────
mkdir -p "$PREFIX"
install -m 0755 "$SRC_BIN" "$BIN"
say "installed $BIN ($("$BIN" version 2>/dev/null || echo cx-agent))"

# ── 3. Starter config (never clobber an existing one) ────────────────────────
mkdir -p "$HOMEDIR"
if [ -f "$CONFIG" ]; then
  say "keeping existing config $CONFIG"
else
  umask 077
  cat >"$CONFIG" <<TOML
# Computexchange supplier agent config. Keep this file private (holds the token).
control_url = "${CX_CONTROL_URL:-http://localhost:8080}"
worker_token = "${CX_WORKER_TOKEN:-PASTE_WORKER_TOKEN_FROM_make_seed}"
supplier_id = "00000000-0000-0000-0000-000000000000"
max_cpu_pct = 80.0
power_only = true            # don't run on battery
min_payout_usd_per_hr = 0.05 # reservation price floor
data_dir = "${HOMEDIR}/data"
# quiet_hours = [22, 7]      # uncomment to refuse work 22:00–06:59 (local≈UTC)
TOML
  say "wrote starter config $CONFIG"
  [ -n "${CX_WORKER_TOKEN:-}" ] || warn "set worker_token in $CONFIG (from 'make seed') before earning"
fi

# ── 4. LaunchAgent (runs the agent at login; logs to ~/.compute-exchange) ────
cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${BIN}</string><string>run</string><string>--config</string><string>${CONFIG}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOMEDIR}/agent.log</string>
  <key>StandardErrorPath</key><string>${HOMEDIR}/agent.log</string>
</dict></plist>
PLIST_EOF
say "installed LaunchAgent $PLIST"

if [ "$MODE" = "start" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST" && say "agent started (launchctl). Logs: $HOMEDIR/agent.log"
else
  say "to start now:  launchctl load $PLIST   (or re-run with --start)"
fi
say "done. Menu-bar app: see macapp/ (build with: swift build --package-path macapp)."
