#!/usr/bin/env bash
# runpod-all-cuda.sh — maximal CUDA evidence on RunPod, HARD-CAPPED to a dollar budget.
#
# The $5-STRICT default is `reference spike` — two SINGLE-pod jobs run SEQUENTIALLY
# (never more than one A100 billing at a time), plus a background BUDGET WATCHDOG that
# force-terminates every cx-* pod at a wall-clock deadline computed from the budget. The
# two-pod vLLM soak is the one job that bills TWO GPUs at once, so it is NOT in the
# default run — add it explicitly (`… reference spike soak`) only if you have headroom.
#
#   1. runpod-a100-reference.sh — T_ref: time the competitive batch on ONE A100 via vLLM
#      offline batching, UNGATED TinyLlama-1.1B (no HF token). The number the fleet beats.
#   2. runpod-spike.sh          — CUDA correctness GATE (builds --features cuda,
#      batched==serial) + capability sweep. Trimmed here to the 1B model, batch ≤ 32, to
#      stay cheap (the default sweep pulls a 7B model — skipped via BENCH_MODELS below).
#   3. runpod-vllm-soak.sh      — OPT-IN ONLY (two pods). Not in the default $5 run.
#
# MONEY-SAFETY, three layers: (a) each child script tears down its OWN pod on any exit;
# (b) this wrapper runs a background watchdog that, at MAX_RUN_MINUTES, queries RunPod
# for ANY pod named cx-* and terminates it — a backstop if a child is SIGKILLed before
# its trap fires; (c) the default scope never runs two pods at once. Worst case for the
# default run at a ~$1.70/hr community A100 with the 120-min cap: ~$3.40 — strictly under
# $5. A normal completion is ~65-75 min ≈ ~$2.
#
# ── What you type ─────────────────────────────────────────────────────────────
#   export RUNPOD_API_KEY=...     # RunPod → Settings → API Keys
#   bash scripts/runpod-all-cuda.sh                       # $5-safe: reference + spike
#   MAX_RUN_MINUTES=90 bash scripts/runpod-all-cuda.sh    # tighter hard cap (~$2.55)
#   bash scripts/runpod-all-cuda.sh reference spike soak  # add the 2-pod soak (needs more $)
#
# ── Knobs ─────────────────────────────────────────────────────────────────────
#   GPU_TYPE         NVIDIA GPU display id (default "NVIDIA A100 80GB PCIe")
#   MAX_RUN_MINUTES  hard wall-clock kill deadline for ALL cx-* pods (default 120)
#   ASSUMED_HR_RATE  $/hr used only for the printed cost estimate (default 1.70)
#   (all per-child knobs — PROMPT_COUNT, MAX_TOKENS, VLLM_*, CLOUD_TYPE — pass through)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

API="https://api.runpod.io/graphql"
# A100 SXM (higher-bandwidth than PCIe; the PCIe SKU was unavailable). Slightly pricier
# per hour, so the cap below is tightened to keep the default run strictly under $5.
export GPU_TYPE="${GPU_TYPE:-NVIDIA A100-SXM4-80GB}"
export GPU_TYPE_A="${GPU_TYPE_A:-$GPU_TYPE}"
export GPU_TYPE_B="${GPU_TYPE_B:-$GPU_TYPE}"
MAX_RUN_MINUTES="${MAX_RUN_MINUTES:-105}"
ASSUMED_HR_RATE="${ASSUMED_HR_RATE:-2.20}"

die()  { echo "ERROR · $*" >&2; exit 1; }
hr()   { echo; echo "######## $* ########"; }

[ -n "${RUNPOD_API_KEY:-}" ] || die "RUNPOD_API_KEY unset — RunPod console → Settings → API Keys, then: export RUNPOD_API_KEY=..."
command -v jq >/dev/null 2>&1 || die "jq not found (brew install jq)"

# terminate_all_cx_pods — the watchdog's teeth: find every pod named cx-* on the account
# and terminate it. Used both as the deadline backstop and as a final safety sweep.
terminate_all_cx_pods() {
  local resp ids id
  resp="$(curl -fsS -X POST "$API?api_key=$RUNPOD_API_KEY" -H 'Content-Type: application/json' \
    --data "$(jq -nc '{query:"query { myself { pods { id name desiredStatus } } }"}')" 2>/dev/null)" || return 0
  ids="$(echo "$resp" | jq -r '.data.myself.pods[]? | select(.name|startswith("cx-")) | .id' 2>/dev/null)"
  for id in $ids; do
    echo "  watchdog: terminating stray pod $id" >&2
    curl -fsS -X POST "$API?api_key=$RUNPOD_API_KEY" -H 'Content-Type: application/json' \
      --data "$(jq -nc --arg i "$id" '{query:("mutation { podTerminate(input:{podId:\""+$i+"\"}) }")}')" >/dev/null 2>&1 || true
  done
}

# Background budget watchdog: sleep to the deadline, then nuke every cx-* pod. This is a
# backstop for a child that dies without running its own teardown trap (SIGKILL, OOM-kill).
WATCHDOG_PID=""
start_watchdog() {
  ( sleep "$((MAX_RUN_MINUTES * 60))"
    echo >&2
    echo "!!!! BUDGET WATCHDOG: ${MAX_RUN_MINUTES}-min deadline hit — force-terminating ALL cx-* pods so they cannot keep charging !!!!" >&2
    terminate_all_cx_pods
  ) &
  WATCHDOG_PID=$!
}
stop_watchdog() { [ -n "$WATCHDOG_PID" ] && kill "$WATCHDOG_PID" 2>/dev/null || true; }
trap 'stop_watchdog' EXIT INT TERM

JOBS=("$@")
[ "${#JOBS[@]}" -eq 0 ] && JOBS=(reference spike)   # $5-safe default (no 2-pod soak)

# Warn loudly if the 2-pod soak is in scope — it bills two GPUs at once.
for j in "${JOBS[@]}"; do
  [ "$j" = "soak" ] && echo "·· NOTE: 'soak' runs TWO pods simultaneously — it can push spend toward/over \$5. Watchdog cap: ${MAX_RUN_MINUTES} min." >&2
done

hr "PLAN · jobs: ${JOBS[*]} · GPU: $GPU_TYPE"
est="$(awk -v m="$MAX_RUN_MINUTES" -v r="$ASSUMED_HR_RATE" 'BEGIN{printf "%.2f", m/60*r}')"
echo "  hard kill deadline: ${MAX_RUN_MINUTES} min · assumed rate \$${ASSUMED_HR_RATE}/hr"
echo "  worst-case spend for a SINGLE pod alive the whole window: ~\$${est}  (default scope keeps ≤1 pod alive at a time)"
echo "  each child also tears its own pod down on exit; watchdog is the backstop."

start_watchdog

declare -a NAMES RESULTS
run_job() {
  local name="$1"; shift
  hr "START · $name"
  if "$@"; then NAMES+=("$name"); RESULTS+=("PASS"); hr "DONE · $name · PASS"
  else local rc=$?; NAMES+=("$name"); RESULTS+=("FAIL(rc=$rc)"); hr "DONE · $name · FAIL (rc=$rc) — continuing"; fi
}

for job in "${JOBS[@]}"; do
  case "$job" in
    reference) run_job reference bash scripts/runpod-a100-reference.sh run ;;
    # Trim the spike to the 1B model + batch ≤ 32 so it stays cheap (the default sweep
    # pulls a 7B model and sweeps to batch 64 — both a real time/cost hit).
    spike)     run_job spike BENCH_MODELS="llama-3.2-1b-instruct-q4" BENCH_BATCH_SIZES="1,2,4,8,16,32" bash scripts/runpod-spike.sh run ;;
    soak)      run_job soak bash scripts/runpod-vllm-soak.sh run ;;
    *)         die "unknown job '$job' (use: reference | spike | soak)" ;;
  esac
done

# Belt-and-suspenders: after all children (each of which tore down its own pod), sweep
# once more for any cx-* pod that somehow survived, THEN stop the watchdog.
terminate_all_cx_pods
stop_watchdog

hr "CONSOLIDATED CUDA EVIDENCE SUMMARY"
allpass=1
for i in "${!NAMES[@]}"; do
  printf '  %-10s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}"
  case "${RESULTS[$i]}" in PASS) ;; *) allpass=0 ;; esac
done
echo
echo "  Artifacts:"
echo "    reference → .artifacts/a100-reference/  (REFERENCE.md, result.json)"
echo "    spike     → .artifacts/gpu-bench/        (REPORT.md) + .artifacts/runpod-spike-*.log"
echo "    soak      → .artifacts/vllm-soak/        (SUMMARY.txt, *.diff)"
echo
echo "  ·· verify no pod is still up: RunPod console → Pods (should be empty). Watchdog + per-child"
echo "     teardown + final sweep all fired; if you EVER see a lingering pod, run any of:"
echo "       bash scripts/runpod-a100-reference.sh down"
echo
if [ "$allpass" = "1" ]; then echo "  ALL REQUESTED CUDA JOBS PASSED ✅"; exit 0
else echo "  one or more CUDA jobs FAILED ✗ — see the per-job artifacts above"; exit 1; fi
