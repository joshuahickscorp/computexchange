#!/usr/bin/env bash
# bench-regression-gate.sh — retain raw bench-batch results and gate on regression
# (docs/internal/CREED_AND_PATH_TO_TEN.md, "Benchmark harness validity & methodology"
# 7 → 8).
#
# What this closes: before this script existed, every `bench-batch` sweep was a
# throwaway — the raw JSON went to a tmp file or a human's terminal and nothing
# retained it, so there was no evidence trail across time and no automated way to
# notice a kernel/dependency change quietly making inference slower. This script:
#
#   1. Runs the REAL `cx-agent bench-batch` harness (no synthetic numbers, ever).
#   2. Commits the raw JSON record under docs/bench-records/, keyed by
#      (device, build_hash, model, timestamp) — never gitignored, never overwritten,
#      so the full history of every accepted AND rejected run is inspectable later.
#   3. Looks up the last ACCEPTED baseline for this exact
#      (device, build_hash, model, max_tokens, batch_sizes) key from
#      docs/bench-records/baseline.json (a small pointer table, not another copy
#      of the data) and compares this run's peak_tok_s against it — the sweep
#      shape (decode length + batch-size list) is part of the key so a short
#      --max-tokens 16 smoke run is never compared against a --max-tokens 48
#      baseline, which would be an unfair peak_tok_s comparison even on
#      identical hardware and kernel build.
#   4. FAILS LOUDLY (non-zero exit) if peak_tok_s dropped more than
#      REGRESSION_THRESHOLD_PCT (default 15%) versus that baseline — before a
#      regressed build could ship.
#   5. On a passing run with no prior baseline for this key, or when invoked with
#      --accept, updates baseline.json to this run's numbers (the new accepted
#      baseline for future comparisons).
#
# `build_hash` (agent/src/hardware.rs::engine_build_hash) hashes the vendored
# quantized-Llama kernel source + engine + agent version + device + quant catalogue —
# it is stable across unrelated commits and moves ONLY when something on the
# determinism-sensitive inference path actually changed. Keying the baseline on it
# (not on git commit) means "compare against the last accepted run of THIS exact
# kernel build on THIS exact device", which is the meaningful regression comparison —
# a baseline is never silently compared across two different kernel builds.
#
# ── What you type ────────────────────────────────────────────────────────────────
#   bash scripts/bench-regression-gate.sh                    # run + gate, default model
#   bash scripts/bench-regression-gate.sh --accept           # run + gate, then ALWAYS
#                                                             # accept this run as the
#                                                             # new baseline if it passes
#   MODEL=qwen2.5-0.5b-instruct-q4 bash scripts/bench-regression-gate.sh
#
# ── Knobs (env) ──────────────────────────────────────────────────────────────────
#   MODEL                      model ref to bench              (default llama-3.2-1b-instruct-q4)
#   MAX_TOKENS                 decode length                    (default 48)
#   BATCH_SIZES                sweep, comma-separated           (default 1,2,4,8,16,32)
#   REPS                       repetitions per sweep point       (default 3 — see the
#                                                                 6→6.5 dispersion rung
#                                                                 this reuses)
#   REGRESSION_THRESHOLD_PCT   fail if peak_tok_s drops more     (default 15)
#                              than this many percent vs baseline
#   BIN                        path to the cx-agent binary       (default agent/target/release/cx-agent)
#   RECORDS_DIR                where raw records + baseline.json live (default docs/bench-records)
#   SKIP_BUILD=1                use an existing release binary, don't rebuild
#
# Exit codes: 0 = ran clean, no regression (or no baseline yet to compare against).
#             1 = regression caught (>= threshold drop) — THE GATE FIRED.
#             2 = harness itself failed to run (build error, bench-batch crashed, etc).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

MODEL="${MODEL:-llama-3.2-1b-instruct-q4}"
MAX_TOKENS="${MAX_TOKENS:-48}"
BATCH_SIZES="${BATCH_SIZES:-1,2,4,8,16,32}"
REPS="${REPS:-3}"
REGRESSION_THRESHOLD_PCT="${REGRESSION_THRESHOLD_PCT:-15}"
BIN="${BIN:-agent/target/release/cx-agent}"
RECORDS_DIR="${RECORDS_DIR:-docs/bench-records}"

ACCEPT=0
for arg in "$@"; do
  case "$arg" in
    --accept) ACCEPT=1 ;;
    *) echo "ERROR · unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

die()  { echo "ERROR · $*" >&2; exit 2; }
info() { echo "·· $*" >&2; }

command -v jq >/dev/null 2>&1 || die "jq is required (brew install jq)"

mkdir -p "$RECORDS_DIR"
BASELINE_FILE="$RECORDS_DIR/baseline.json"
[ -f "$BASELINE_FILE" ] || echo '{"baselines": []}' > "$BASELINE_FILE"

# ── 0. build (unless the caller already has a fresh binary) ─────────────────────
if [ "${SKIP_BUILD:-0}" = "1" ] && [ -x "$BIN" ]; then
  info "using existing binary $BIN (SKIP_BUILD=1)"
else
  info "building release agent (cargo build --release)"
  BUILD_LOG="$(mktemp)"
  if ! ( cd agent && cargo build --release ) >"$BUILD_LOG" 2>&1; then
    tail -30 "$BUILD_LOG" >&2
    die "release build FAILED — see above"
  fi
fi
[ -x "$BIN" ] || die "binary not found/executable at $BIN after build"

# ── 1. run the REAL bench-batch harness ──────────────────────────────────────────
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RAW_LOG="$(mktemp)"
RUN_JSON="$(mktemp)"
info "running: $BIN bench-batch --model $MODEL --max-tokens $MAX_TOKENS --batch-sizes $BATCH_SIZES --reps $REPS"
if ! "$BIN" bench-batch --model "$MODEL" --max-tokens "$MAX_TOKENS" \
      --batch-sizes "$BATCH_SIZES" --reps "$REPS" >"$RUN_JSON" 2>"$RAW_LOG"; then
  tail -30 "$RAW_LOG" >&2
  die "bench-batch itself failed to run — see above (not a regression finding, a harness failure)"
fi
grep -E "serial|batch=|peak" "$RAW_LOG" | sed 's/^/  /' >&2

DEVICE="$(jq -r '.device' "$RUN_JSON")"
BUILD_HASH="$(jq -r '.build_hash' "$RUN_JSON")"
PEAK_TOK_S="$(jq -r '.peak_tok_s' "$RUN_JSON")"
DETERMINISTIC="$(jq -r '.batched_deterministic_vs_serial' "$RUN_JSON")"
[ -n "$DEVICE" ] && [ "$DEVICE" != "null" ] || die "bench-batch record missing 'device' field — cannot key a baseline"
[ -n "$BUILD_HASH" ] && [ "$BUILD_HASH" != "null" ] || die "bench-batch record missing 'build_hash' field — cannot key a baseline"

# ── 2. retain the raw record, gitignored never, keyed (device, build_hash, model, ts) ──
SAFE_MODEL="$(echo "$MODEL" | tr -cd 'A-Za-z0-9._-')"
RAW_RECORD="$RECORDS_DIR/${DEVICE}-${BUILD_HASH}-${SAFE_MODEL}-${TS}.json"
cp "$RUN_JSON" "$RAW_RECORD"
info "raw record retained → $RAW_RECORD"

# ── 3. look up the last accepted baseline for this exact (device, build_hash, model,
# max_tokens, batch_sizes) ── the sweep shape is part of the key, not just the
# hardware/build/model, so a --max-tokens 16 run is never compared against a
# --max-tokens 48 baseline: different decode lengths and batch-size sweeps are not
# a fair peak_tok_s comparison even on identical hardware and kernel build.
BASELINE_ROW="$(jq -c --arg d "$DEVICE" --arg b "$BUILD_HASH" --arg m "$MODEL" \
  --arg mt "$MAX_TOKENS" --arg bs "$BATCH_SIZES" \
  '.baselines[] | select(.device == $d and .build_hash == $b and .model == $m
    and (.max_tokens | tostring) == $mt and .batch_sizes == $bs)' "$BASELINE_FILE")"

if [ -z "$BASELINE_ROW" ]; then
  info "no prior accepted baseline for (device=$DEVICE, build_hash=$BUILD_HASH, model=$MODEL, max_tokens=$MAX_TOKENS, batch_sizes=$BATCH_SIZES) — nothing to gate against yet"
  RESULT="no-baseline"
  DROP_PCT="0"
else
  BASELINE_PEAK="$(echo "$BASELINE_ROW" | jq -r '.peak_tok_s')"
  BASELINE_RECORD_PATH="$(echo "$BASELINE_ROW" | jq -r '.record')"
  DROP_PCT="$(awk -v base="$BASELINE_PEAK" -v cur="$PEAK_TOK_S" 'BEGIN {
    if (base <= 0) { print "0"; exit }
    d = (base - cur) / base * 100.0;
    if (d < 0) d = 0;
    printf "%.2f", d
  }')"
  info "baseline peak_tok_s=$BASELINE_PEAK (from $BASELINE_RECORD_PATH) · this run peak_tok_s=$PEAK_TOK_S · drop=${DROP_PCT}%"
  PASSES_THRESHOLD="$(awk -v d="$DROP_PCT" -v t="$REGRESSION_THRESHOLD_PCT" 'BEGIN { print (d >= t) ? "0" : "1" }')"
  if [ "$PASSES_THRESHOLD" = "0" ]; then
    RESULT="REGRESSION"
  else
    RESULT="ok"
  fi
fi

# ── 4. update the accepted baseline on a clean pass (or explicit --accept) ──────────
update_baseline() {
  TMP_BASELINE="$(mktemp)"
  jq --arg d "$DEVICE" --arg b "$BUILD_HASH" --arg m "$MODEL" \
     --arg mt "$MAX_TOKENS" --arg bs "$BATCH_SIZES" \
     --argjson p "$PEAK_TOK_S" --arg r "$RAW_RECORD" --arg ts "$TS" --argjson det "$DETERMINISTIC" \
     '.baselines = ([.baselines[] | select(.device == $d and .build_hash == $b and .model == $m
        and (.max_tokens | tostring) == $mt and .batch_sizes == $bs | not)]
        + [{device: $d, build_hash: $b, model: $m, max_tokens: ($mt | tonumber), batch_sizes: $bs,
            peak_tok_s: $p, record: $r, accepted_at: $ts, batched_deterministic_vs_serial: $det}])' \
     "$BASELINE_FILE" > "$TMP_BASELINE"
  mv "$TMP_BASELINE" "$BASELINE_FILE"
  info "baseline.json updated: (device=$DEVICE, build_hash=$BUILD_HASH, model=$MODEL, max_tokens=$MAX_TOKENS, batch_sizes=$BATCH_SIZES) -> peak_tok_s=$PEAK_TOK_S"
}

case "$RESULT" in
  REGRESSION)
    echo "REGRESSION CAUGHT · peak_tok_s dropped ${DROP_PCT}% (>= ${REGRESSION_THRESHOLD_PCT}% threshold) for device=$DEVICE build_hash=$BUILD_HASH model=$MODEL" >&2
    echo "  baseline: $BASELINE_PEAK tok/s (accepted run: $BASELINE_RECORD_PATH)" >&2
    echo "  this run: $PEAK_TOK_S tok/s (raw record: $RAW_RECORD)" >&2
    echo "  NOT updating baseline.json — the regressed run is retained as evidence but is never promoted to 'accepted'." >&2
    exit 1
    ;;
  no-baseline)
    update_baseline
    echo "OK · no prior baseline existed; this run accepted as the first baseline (peak_tok_s=$PEAK_TOK_S)" >&2
    exit 0
    ;;
  ok)
    if [ "$ACCEPT" = "1" ]; then
      update_baseline
    else
      info "run within threshold (drop=${DROP_PCT}% < ${REGRESSION_THRESHOLD_PCT}%) — baseline.json left unchanged (pass --accept to promote this run)"
    fi
    echo "OK · peak_tok_s=$PEAK_TOK_S, drop=${DROP_PCT}% (threshold ${REGRESSION_THRESHOLD_PCT}%)" >&2
    exit 0
    ;;
esac
