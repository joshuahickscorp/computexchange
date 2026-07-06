#!/usr/bin/env bash
#
# sandbox-profile-test.sh — PROVE macapp/ComputeExchangeAgent/cx-agent.sb actually
# contains a hostile payload's filesystem blast radius while still permitting the
# reads/writes real Candle inference needs (Security Posture 8->9,
# docs/internal/CREED_AND_PATH_TO_TEN.md; the "known, named gap" in docs/SECURITY.md).
#
# This is a REAL adversarial proof, not an assertion from reading the profile: it
# stands up a throwaway fake HOME, launches STANDALONE system binaries (/bin/sh,
# /bin/cat, /usr/bin/touch) UNDER the exact shipped profile via `sandbox-exec`, and
# checks — for each row — that a legitimate inference access SUCCEEDS and a hostile
# access FAILS. A single wrong outcome fails the whole script (non-zero exit), so a
# regression in the profile (an over-broad allow, a missing deny) is caught here.
#
# It uses ONLY the shipped profile file and macOS's own `sandbox-exec`; no build step,
# no cx-agent binary required — so it runs anywhere macOS runs, including CI's
# macos-latest runner (wired in .github/workflows/ci.yml).
#
# Usage:   macapp/ComputeExchangeAgent/sandbox-profile-test.sh
# Exit:    0 = every containment row held; non-zero = a row regressed.

set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "SKIP: seatbelt/sandbox-exec is macOS-only (uname=$(uname -s))"
  exit 0
fi
command -v sandbox-exec >/dev/null 2>&1 || { echo "FAIL: sandbox-exec not found"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="$SCRIPT_DIR/cx-agent.sb"
[ -f "$PROFILE" ] || { echo "FAIL: profile not found at $PROFILE"; exit 1; }

# Throwaway fake HOME under the REAL home (so it is NOT under /private/tmp, which the
# profile re-allows for writes — putting the fixture there would mask the write denies).
FAKE="$(mktemp -d "$HOME/.cx-sandbox-test.XXXXXX")"
cleanup() { rm -rf "$FAKE"; }
trap cleanup EXIT INT TERM

H="$FAKE/home"
MODELCACHE="$H/.cache/huggingface"
DATADIR="$H/.compute-exchange"
mkdir -p "$H/.ssh" "$H/.gnupg" "$H/.aws" "$H/Library/Keychains" "$H/Library/LaunchAgents" \
         "$H/Documents" "$H/Desktop" "$H/Downloads" "$MODELCACHE" "$DATADIR"
echo "PRIVATE-SSH-KEY"        > "$H/.ssh/id_rsa"
echo "AWS_SECRET=deadbeef"    > "$H/.aws/credentials"
echo "login-keychain-secret"  > "$H/Library/Keychains/login.keychain-db"
echo "user tax return 2026"   > "$H/Documents/taxes.txt"
echo "MODEL WEIGHTS"          > "$MODELCACHE/model.gguf"
echo '{"state":"idle"}'       > "$DATADIR/status.json"

TMPDIR_REAL="${TMPDIR:-/private/var/folders}"

# run <binary> [args...]  — launch a standalone system binary under the shipped
# profile with the same params the app's launcher passes.
run() {
  sandbox-exec -f "$PROFILE" \
    -D HOME="$H" \
    -D MODELCACHE="$MODELCACHE" \
    -D DATADIR="$DATADIR" \
    -D TMPDIR="$TMPDIR_REAL" \
    "$@"
}

PASS=0; FAIL=0
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31m✗ %s\033[0m\n' "$*" >&2; FAIL=$((FAIL+1)); }

# expect_allow <label> <cmd...>  — the command MUST succeed under the sandbox.
expect_allow() {
  local label="$1"; shift
  if run "$@" >/dev/null 2>&1; then ok "ALLOW  $label"; else bad "ALLOW  $label — legitimate access was BLOCKED"; fi
}
# expect_deny <label> <cmd...>  — the command MUST fail under the sandbox.
expect_deny() {
  local label="$1"; shift
  if run "$@" >/dev/null 2>&1; then bad "DENY   $label — hostile access SUCCEEDED (containment breach)"; else ok "DENY   $label"; fi
}

echo "sandbox-profile-test: proving cx-agent.sb against standalone binaries"
echo "  profile : $PROFILE"
echo "  fakeHOME: $H"
echo

# ── The process must run at all under the profile ────────────────────────────
expect_allow "process executes under the profile"           /bin/sh -c 'exit 0'

# ── Legitimate inference access (must be ALLOWED) ────────────────────────────
expect_allow "read the model cache (weights/tokenizer)"     /bin/cat "$MODELCACHE/model.gguf"
expect_allow "write the model cache (hf-hub download)"      /bin/sh -c "printf x > '$MODELCACHE/new-weight.bin'"
expect_allow "read the agent data dir"                      /bin/cat "$DATADIR/status.json"
expect_allow "write the agent data dir (status.json)"       /bin/sh -c "printf y > '$DATADIR/status.json.tmp'"
expect_allow "write system temp (inference scratch)"        /bin/sh -c "printf z > \"\${TMPDIR:-/private/tmp}/cx-scratch.\$\$\""

# ── Hostile filesystem access by a buyer payload (must be DENIED) ────────────
# Persistence: plant a LaunchAgent so cx-agent restarts attacker code at login.
expect_deny  "plant a LaunchAgent (persistence)"            /bin/sh -c "printf evil > '$H/Library/LaunchAgents/com.evil.plist'"
# Tamper: overwrite the operator's own documents.
expect_deny  "overwrite the operator's Documents"          /bin/sh -c "printf x > '$H/Documents/taxes.txt'"
# Exfiltration: read SSH private key, AWS creds, the login keychain, personal docs.
expect_deny  "read ~/.ssh/id_rsa (SSH private key)"        /bin/cat "$H/.ssh/id_rsa"
expect_deny  "read ~/.aws/credentials (cloud secrets)"     /bin/cat "$H/.aws/credentials"
expect_deny  "read ~/Library/Keychains (login keychain)"   /bin/cat "$H/Library/Keychains/login.keychain-db"
expect_deny  "read ~/Documents (personal files)"           /bin/cat "$H/Documents/taxes.txt"
# Write outside every allowed scope (a new dotfile / rc-file injection).
expect_deny  "write a new ~/.zshrc (rc injection)"         /bin/sh -c "printf pwn > '$H/.zshrc'"

# ── NETWORK CONTAINMENT ──────────────────────────────────────────────────────
# The child is a pure network CLIENT. These rows prove the profile's network half:
# no inbound/listen at all, and outbound pinned to the ports inference needs. Each
# uses python3's socket errno to distinguish a SANDBOX deny (EPERM / errno 1) from an
# ordinary connection outcome — an assertion no amount of profile-reading can give.
# Python3 ships with the macOS Command Line Tools; skip (not fail) if it is absent.
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  printf '  \033[1;33m•\033[0m SKIP  network rows — python3 not found (socket-level probe unavailable)\n'
else
  echo
  echo "  network containment:"

  # (1) LISTEN/BIND is denied outright — a payload cannot open a backdoor listener.
  #     Deterministic and offline: bind()+listen() on loopback must raise EPERM.
  if run "$PY" -c 'import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1",0)); s.listen(1); sys.exit(1)   # bind SUCCEEDED = containment breach
except OSError as e:
    sys.exit(0 if e.errno==1 else 2)                    # errno 1 = EPERM = sandbox denied it' >/dev/null 2>&1; then
    ok  "DENY   open a listening socket (no inbound backdoor)"
  else
    bad "DENY   open a listening socket — bind was ALLOWED (a payload could listen)"
  fi

  # (2) Loopback egress stays ALLOWED (the dev control plane at localhost:8080, any
  #     local sidecar). Offline-deterministic: stand up a loopback listener OUTSIDE the
  #     sandbox, connect to it from INSIDE. `localhost` is allowed, so this must succeed.
  LPORT="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()' 2>/dev/null || echo 0)"
  if [ "$LPORT" != "0" ]; then
    "$PY" -c "import socket
srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
srv.bind(('127.0.0.1',$LPORT)); srv.listen(1); srv.settimeout(8)
try:
    c,_=srv.accept(); c.close()
except OSError:
    pass
srv.close()" >/dev/null 2>&1 &
    LSRV=$!
    sleep 1
    if run "$PY" -c "import socket,sys
s=socket.socket(); s.settimeout(4)
try:
    s.connect(('127.0.0.1',$LPORT)); sys.exit(0)
except OSError:
    sys.exit(1)" >/dev/null 2>&1; then
      ok "ALLOW  outbound to loopback (dev control plane / local sidecar)"
    else
      bad "ALLOW  outbound to loopback — legitimate local egress was BLOCKED"
    fi
    kill "$LSRV" >/dev/null 2>&1 || true
    wait "$LSRV" 2>/dev/null || true
  fi

  # The remaining two rows need a real remote host. Guard on reachability so an offline
  # CI runner SKIPS them cleanly rather than reporting a false pass/fail. The probe
  # itself runs UNSANDBOXED — it only decides whether the network is up at all.
  REMOTE_IP="1.1.1.1"
  if "$PY" -c "import socket,sys
s=socket.socket(); s.settimeout(4)
try:
    s.connect(('$REMOTE_IP',443)); sys.exit(0)
except OSError:
    sys.exit(1)" >/dev/null 2>&1; then

    # (3) Outbound to an ALLOWED remote port (443) must NOT be blocked by the sandbox.
    #     Pass unless the failure is an EPERM (errno 1), which would mean the profile
    #     wrongly denied a port inference needs.
    if run "$PY" -c "import socket,sys
s=socket.socket(); s.settimeout(6)
try:
    s.connect(('$REMOTE_IP',443)); sys.exit(0)
except OSError as e:
    sys.exit(1 if e.errno==1 else 0)   # only an EPERM is a failure here" >/dev/null 2>&1; then
      ok "ALLOW  outbound HTTPS :443 (control plane / storage / HuggingFace)"
    else
      bad "ALLOW  outbound :443 — the sandbox blocked a port inference needs"
    fi

    # (4) Outbound to a NON-allowed remote port (6667, a classic C2 port) must be denied
    #     IN-KERNEL: an EPERM (errno 1) that returns near-instantly, distinct from the
    #     multi-second hang a real network timeout would take. This is the row that
    #     proves egress is pinned to the agent's own ports, not open to arbitrary ports.
    if run "$PY" -c "import socket,sys,time
s=socket.socket(); s.settimeout(6); t=time.time()
try:
    s.connect(('$REMOTE_IP',6667)); sys.exit(1)          # connect SUCCEEDED = containment breach
except OSError as e:
    dt=time.time()-t
    sys.exit(0 if (e.errno==1 and dt<1.0) else 2)        # fast EPERM = in-kernel sandbox deny" >/dev/null 2>&1; then
      ok "DENY   outbound to an arbitrary port :6667 (no C2/exfil egress)"
    else
      bad "DENY   outbound :6667 — a payload could phone home on an arbitrary port"
    fi
  else
    printf '  \033[1;33m•\033[0m SKIP  remote-port rows — no network to %s:443 (offline runner)\n' "$REMOTE_IP"
  fi
fi

echo
if [ "$FAIL" -gt 0 ]; then
  printf '\033[1;31mFAIL: %d containment row(s) regressed (%d ok)\033[0m\n' "$FAIL" "$PASS" >&2
  exit 1
fi
printf '\033[1;32mPASS: all %d containment rows held — cx-agent.sb contains the blast radius\033[0m\n' "$PASS"
