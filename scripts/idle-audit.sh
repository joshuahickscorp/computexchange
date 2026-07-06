#!/usr/bin/env bash
# idle-audit.sh — measure the cx-agent idle footprint that has never been recorded
# anywhere in the repo (docs/internal/CREED_AND_PATH_TO_TEN.md, "Agent idle footprint
# & startup overhead" 5→6): average %CPU, RSS, idle wakeups/sec, network bytes, and
# status.json write frequency over a real observation window, checked against a
# stated budget. This does not run the agent itself — point it at an ALREADY
# RUNNING cx-agent process (real supplier usage, or your own local `cx-agent run`).
#
# HONESTY (BLACKHOLE): every number below is sampled from the real process via
# `ps`/`lsof`/`powermetrics` — never estimated or hand-typed. If a data source is
# unavailable (e.g. `powermetrics` needs root and no non-interactive sudo is set
# up), the report says so explicitly and omits that figure rather than guessing.
#
# ── What you type ─────────────────────────────────────────────────────────────
#   bash scripts/idle-audit.sh <pid>                  # 600s (10 min) window, default budget
#   bash scripts/idle-audit.sh <pid> --duration 60    # shorter window (e.g. for a quick check)
#   bash scripts/idle-audit.sh --find                 # locate a running cx-agent's pid for you
#
# ── Knobs (env) ────────────────────────────────────────────────────────────────
#   DURATION_SECS     observation window                 (default 600, i.e. 10 minutes)
#   SAMPLE_INTERVAL   seconds between ps samples          (default 5)
#   CPU_BUDGET_PCT    stated budget: average %CPU         (default 0.5)
#   RSS_BUDGET_MB     stated budget: RSS, cold/idle       (default 80)
#   STATUS_JSON_PATH  path to the agent's status file     (default ~/.compute-exchange/status.json)
#   OUT_DIR           where the report is written         (default .artifacts/idle-audit)
set -uo pipefail

DURATION_SECS="${DURATION_SECS:-600}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-5}"
CPU_BUDGET_PCT="${CPU_BUDGET_PCT:-0.5}"
RSS_BUDGET_MB="${RSS_BUDGET_MB:-80}"
STATUS_JSON_PATH="${STATUS_JSON_PATH:-$HOME/.compute-exchange/status.json}"
OUT_DIR="${OUT_DIR:-.artifacts/idle-audit}"

die()  { echo "ERROR · $*" >&2; exit 1; }
info() { echo "·· $*" >&2; }

find_pid() {
  pgrep -x cx-agent 2>/dev/null | head -1
}

if [ "${1:-}" = "--find" ]; then
  pid="$(find_pid)"
  [ -n "$pid" ] || die "no running cx-agent process found (pgrep -x cx-agent)"
  echo "$pid"
  exit 0
fi

PID="${1:-}"
[ -n "$PID" ] || { PID="$(find_pid)"; info "no pid given — found running cx-agent at pid $PID"; }
[ -n "$PID" ] || die "no pid given and no running cx-agent found. Usage: bash $0 <pid> [--duration N]"
kill -0 "$PID" 2>/dev/null || die "pid $PID is not a running process"

shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --duration) DURATION_SECS="$2"; shift 2 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || echo run)"
RAW="$OUT_DIR/raw-$STAMP.tsv"
REPORT="$OUT_DIR/report-$STAMP.md"

info "sampling pid $PID every ${SAMPLE_INTERVAL}s for ${DURATION_SECS}s (Ctrl-C to stop early; a partial report still writes)"
echo -e "epoch\tcpu_pct\trss_kb" > "$RAW"

# --- ps-based sampling loop (works everywhere, no privilege needed) ----------------
started=$(date +%s)
samples=0
while true; do
  now=$(date +%s)
  elapsed=$((now - started))
  [ "$elapsed" -ge "$DURATION_SECS" ] && break
  kill -0 "$PID" 2>/dev/null || { info "pid $PID exited at ${elapsed}s — stopping early"; break; }
  line="$(ps -o %cpu=,rss= -p "$PID" 2>/dev/null)"
  [ -n "$line" ] || { sleep "$SAMPLE_INTERVAL"; continue; }
  cpu="$(echo "$line" | awk '{print $1}')"
  rss="$(echo "$line" | awk '{print $2}')"
  echo -e "${now}\t${cpu}\t${rss}" >> "$RAW"
  samples=$((samples + 1))
  printf '  … %ds/%ds (cpu=%s%% rss=%sKB)\r' "$elapsed" "$DURATION_SECS" "$cpu" "$rss" >&2
  sleep "$SAMPLE_INTERVAL"
done
echo >&2
[ "$samples" -gt 0 ] || die "no samples collected — pid $PID never responded to ps"

# --- powermetrics (root-only): idle wakeups/sec, only if non-interactive sudo works ---
WAKEUPS_LINE=""
if sudo -n true 2>/dev/null; then
  info "sudo available non-interactively — sampling wakeups via powermetrics"
  PM_SECS=$((DURATION_SECS < 30 ? DURATION_SECS : 30))
  PM_OUT="$(sudo -n powermetrics --samplers tasks -i 1000 -n "$PM_SECS" 2>/dev/null | awk -v pid="$PID" '
    $0 ~ ("^" pid " ") || $0 ~ (" " pid " ") { print }
  ' | tail -5)"
  if [ -n "$PM_OUT" ]; then
    WAKEUPS_LINE="$PM_OUT"
  else
    info "powermetrics ran but no per-process wakeup line matched pid $PID — omitting wakeups/sec"
  fi
else
  info "no non-interactive sudo — omitting idle-wakeups/sec (needs: sudo powermetrics, root-only on macOS)"
fi

# --- network bytes (best-effort; nettop needs no special privilege for own-user procs) ---
NET_LINE=""
if command -v nettop >/dev/null 2>&1; then
  NET_LINE="$(nettop -p "$PID" -L 1 -x 2>/dev/null | tail -1)"
fi

# --- status.json write frequency: mtime deltas over the window --------------------
STATUS_NOTE="status.json not found at $STATUS_JSON_PATH — write frequency omitted"
if [ -f "$STATUS_JSON_PATH" ]; then
  m1="$(stat -f %m "$STATUS_JSON_PATH" 2>/dev/null || stat -c %Y "$STATUS_JSON_PATH" 2>/dev/null)"
  STATUS_NOTE="last write to status.json: $(date -u -r "$m1" 2>/dev/null || echo "epoch $m1") (mtime sampled once at report time, not tracked continuously)"
fi

# --- compute avg/max from raw samples (pure awk, no external stats dependency) ----
read -r avg_cpu max_cpu avg_rss_kb max_rss_kb <<EOF_STATS
$(awk -F'\t' 'NR>1 {
  cpu=$2+0; rss=$3+0;
  sum_cpu+=cpu; if (cpu>max_cpu) max_cpu=cpu;
  sum_rss+=rss; if (rss>max_rss) max_rss=rss;
  n++;
} END {
  if (n==0) { print "0 0 0 0"; exit }
  printf "%.3f %.3f %.0f %.0f", sum_cpu/n, max_cpu, sum_rss/n, max_rss
}' "$RAW")
EOF_STATS

avg_rss_mb=$(awk -v k="$avg_rss_kb" 'BEGIN{printf "%.1f", k/1024}')
max_rss_mb=$(awk -v k="$max_rss_kb" 'BEGIN{printf "%.1f", k/1024}')

cpu_verdict="within budget"
awk -v a="$avg_cpu" -v b="$CPU_BUDGET_PCT" 'BEGIN{exit !(a>b)}' && cpu_verdict="OVER BUDGET"
rss_verdict="within budget"
awk -v a="$avg_rss_mb" -v b="$RSS_BUDGET_MB" 'BEGIN{exit !(a>b)}' && rss_verdict="OVER BUDGET"

{
  echo "# cx-agent idle-footprint audit"
  echo
  echo "Generated $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo now) · pid $PID · window ${DURATION_SECS}s · ${samples} samples every ${SAMPLE_INTERVAL}s"
  echo
  echo "| metric | measured | budget | verdict |"
  echo "|---|---|---|---|"
  echo "| avg %CPU | ${avg_cpu}% | < ${CPU_BUDGET_PCT}% | ${cpu_verdict} |"
  echo "| max %CPU | ${max_cpu}% | (informational) | |"
  echo "| avg RSS | ${avg_rss_mb} MB | < ${RSS_BUDGET_MB} MB | ${rss_verdict} |"
  echo "| max RSS | ${max_rss_mb} MB | (informational) | |"
  echo
  if [ -n "$WAKEUPS_LINE" ]; then
    echo "**powermetrics tasks sample (pid $PID):**"
    echo '```'
    echo "$WAKEUPS_LINE"
    echo '```'
  else
    echo "**idle wakeups/sec:** not measured this run — requires \`sudo powermetrics\` (root-only on macOS); see script output above."
  fi
  echo
  if [ -n "$NET_LINE" ]; then
    echo "**network (nettop, best-effort):** \`$NET_LINE\`"
  else
    echo "**network:** not measured (nettop unavailable or produced no output for this pid)."
  fi
  echo
  echo "**status.json:** $STATUS_NOTE"
  echo
  echo "Raw per-sample data: \`$RAW\`"
} > "$REPORT"

cat "$REPORT" >&2
info "report written to $REPORT"
[ "$cpu_verdict" = "within budget" ] && [ "$rss_verdict" = "within budget" ]
