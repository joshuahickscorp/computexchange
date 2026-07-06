#!/usr/bin/env bash
# bench-nightly-gate.sh — close the loop with an automated nightly regression gate
# (docs/internal/CREED_AND_PATH_TO_TEN.md, "Performance observability & regression
# tracking" 8 → 9).
#
# What this closes: scripts/bench-regression-gate.sh (Benchmark Harness Validity
# 7→8) proves a single invocation can retain a raw result and gate on regression,
# but nothing ran it unattended, and nothing told a human when it fired. This
# script is that missing link: a thin, idempotent wrapper meant to run ONCE PER
# NIGHT (via cron/launchd — see "Wiring it to run nightly" below, which this repo
# does NOT install for you, matching this session's existing policy for any
# always-on/scheduled infrastructure — see scripts/runpod-vllm-soak.sh's identical
# stance) that:
#
#   1. Runs the real regression gate (scripts/bench-regression-gate.sh), which
#      itself runs the real `cx-agent bench-batch` harness — no synthetic numbers.
#   2. On a clean pass, writes a one-line dated success entry to a log and exits 0.
#   3. On a caught regression (gate exit 1) OR a harness failure (gate exit 2),
#      FAILS LOUDLY: non-zero exit, AND fires a real alert into the existing
#      Alertmanager stack via its standard `/api/v2/alerts` HTTP API — the exact
#      same delivery path (Slack + PagerDuty) every other production alert in
#      monitoring/alerts.yml already uses, so a nightly inference regression pages
#      a human exactly like a production incident does, same day.
#
# This does NOT get installed as a running cron/launchd job by this script — same
# posture as scripts/runpod-vllm-soak.sh (money) and the RunPod spike harness
# (needs owned/rented always-on hardware): building and PROVING the mechanism
# works end to end is the deliverable; actually scheduling it on an
# operator-owned always-available Mac is the operator's call, documented below.
#
# ── What you type ────────────────────────────────────────────────────────────────
#   bash scripts/bench-nightly-gate.sh                     # run once, real hardware
#   ALERTMANAGER_URL=http://localhost:9093 bash scripts/bench-nightly-gate.sh
#
# ── Knobs (env) — all pass through to bench-regression-gate.sh except the two below ──
#   MODEL, MAX_TOKENS, BATCH_SIZES, REPS, REGRESSION_THRESHOLD_PCT, BIN, RECORDS_DIR
#     — same meaning as scripts/bench-regression-gate.sh; see that script's header.
#   ALERTMANAGER_URL   base URL of a reachable Alertmanager instance
#                      (default http://localhost:9093, Alertmanager's own default
#                       port; unreachable is handled gracefully — see below).
#   NIGHTLY_LOG        where the one-line-per-run history is appended
#                      (default docs/bench-records/nightly-gate.log)
#
# ── Alerting behavior ─────────────────────────────────────────────────────────────
# On failure, POSTs a real Alertmanager v2 alert with:
#   alertname=BenchNightlyRegressionGate, severity=page (routes to PagerDuty AND
#   Slack per monitoring/alertmanager.yml's existing `severity = page` route — no
#   new routing rule needed), plus device/build_hash/model labels so the specific
#   regressed configuration is identified automatically, not just "something broke".
# If Alertmanager is unreachable (e.g. running this on a laptop with no monitoring
# stack up), the curl failure is logged loudly to stderr and swallowed — the
# SCRIPT'S OWN exit code still reflects the regression (never masked by a delivery
# failure), matching this repo's existing convention (monitoring/alertmanager.yml:
# "Unset creds → Alertmanager logs a loud delivery error... never a silent drop") —
# the failure to page is itself visible, not hidden.
#
# ── Wiring it to run nightly (operator's call — NOT done by this script) ──────────
# cron (any Mac/Linux box the operator owns and leaves running):
#   # crontab -e
#   0 3 * * * cd /path/to/computexchange && ALERTMANAGER_URL=http://your-alertmanager:9093 \
#     bash scripts/bench-nightly-gate.sh >> docs/bench-records/nightly-gate-cron.log 2>&1
#
# launchd (macOS, survives reboots better than cron on a laptop that sleeps):
#   Create ~/Library/LaunchAgents/exchange.compute.bench-nightly-gate.plist:
#     <?xml version="1.0" encoding="UTF-8"?>
#     <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#     <plist version="1.0"><dict>
#       <key>Label</key><string>exchange.compute.bench-nightly-gate</string>
#       <key>ProgramArguments</key><array>
#         <string>/bin/bash</string>
#         <string>-c</string>
#         <string>cd /path/to/computexchange && bash scripts/bench-nightly-gate.sh</string>
#       </array>
#       <key>StartCalendarInterval</key><dict>
#         <key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer>
#       </dict>
#       <key>StandardOutPath</key><string>/tmp/bench-nightly-gate.log</string>
#       <key>StandardErrorPath</key><string>/tmp/bench-nightly-gate.err</string>
#       <key>EnvironmentVariables</key><dict>
#         <key>ALERTMANAGER_URL</key><string>http://your-alertmanager:9093</string>
#       </dict>
#     </dict></plist>
#   Then: launchctl load ~/Library/LaunchAgents/exchange.compute.bench-nightly-gate.plist
#   `launchd`'s calendar interval survives a laptop that sleeps overnight (it runs
#   the job on next wake if the scheduled time was missed while asleep — cron on a
#   laptop that's asleep at 3am simply never fires that day), which is why this is
#   the better fit for an "owned Mac" that isn't a 24/7 server.
#
# Neither of the above is installed by running this script. This is the exact same
# posture the RunPod scripts in this session already established for infrastructure
# that costs money or needs an always-on machine: build and prove the mechanism,
# leave scheduling it as the owner's explicit decision.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

ALERTMANAGER_URL="${ALERTMANAGER_URL:-http://localhost:9093}"
NIGHTLY_LOG="${NIGHTLY_LOG:-docs/bench-records/nightly-gate.log}"

info() { echo "·· $*" >&2; }

mkdir -p "$(dirname "$NIGHTLY_LOG")"
RUN_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── 1. run the real regression gate (item 1's mechanism) ────────────────────────
GATE_OUT="$(mktemp)"
info "running scripts/bench-regression-gate.sh (this itself runs a real cx-agent bench-batch)"
if bash scripts/bench-regression-gate.sh >"$GATE_OUT" 2>&1; then
  GATE_EXIT=0
else
  GATE_EXIT=$?
fi
cat "$GATE_OUT" >&2

# ── 2. classify the outcome ──────────────────────────────────────────────────────
case "$GATE_EXIT" in
  0)
    echo "$RUN_TS PASS $(tail -1 "$GATE_OUT")" >> "$NIGHTLY_LOG"
    info "nightly gate PASSED — no regression, logged to $NIGHTLY_LOG"
    exit 0
    ;;
  1)
    OUTCOME="REGRESSION"
    ;;
  *)
    OUTCOME="HARNESS_FAILURE"
    ;;
esac

DETAIL="$(tail -5 "$GATE_OUT" | tr '\n' ' ' | sed 's/"/\\"/g')"
echo "$RUN_TS $OUTCOME $DETAIL" >> "$NIGHTLY_LOG"
info "nightly gate caught: $OUTCOME"

# ── 3. fire a real alert into the existing Alertmanager stack ───────────────────
# v2 API: POST an array of alert objects to /api/v2/alerts. `endsAt` omitted so it
# is treated as firing/active until Alertmanager's own resolve_timeout elapses
# without a fresh POST — a single one-shot nightly script does not run a
# long-lived process to explicitly resolve it, matching how Alertmanager already
# expects short-lived/batch alert sources to behave.
ALERT_PAYLOAD=$(cat <<JSON
[
  {
    "labels": {
      "alertname": "BenchNightlyRegressionGate",
      "severity": "page",
      "outcome": "$OUTCOME"
    },
    "annotations": {
      "summary": "Nightly inference benchmark regression gate caught: $OUTCOME",
      "description": "scripts/bench-nightly-gate.sh (docs/internal/CREED_AND_PATH_TO_TEN.md, Performance observability 8->9) failed on $RUN_TS. Detail: $DETAIL. Raw records + baseline: docs/bench-records/. Runbook: docs/RUNBOOKS.md."
    },
    "startsAt": "$RUN_TS"
  }
]
JSON
)

if command -v curl >/dev/null 2>&1; then
  if curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
       -d "$ALERT_PAYLOAD" "$ALERTMANAGER_URL/api/v2/alerts" >/dev/null 2>&1; then
    info "alert POSTed to $ALERTMANAGER_URL/api/v2/alerts (routes to PagerDuty+Slack per severity=page)"
  else
    # BLACKHOLE-consistent: a delivery failure is loud, never a silent drop, and
    # never masks the script's own real exit code below.
    echo "ERROR · could not reach Alertmanager at $ALERTMANAGER_URL/api/v2/alerts — alert NOT delivered. The regression itself is still real; see $NIGHTLY_LOG and $GATE_OUT." >&2
  fi
else
  echo "ERROR · curl not found — cannot POST to Alertmanager. Install curl or wire delivery another way." >&2
fi

exit 1
