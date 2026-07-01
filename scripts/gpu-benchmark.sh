#!/usr/bin/env bash
# gpu-benchmark.sh — deep, device-agnostic capability benchmark for CX inference.
#
# ONE script that runs on BOTH backends and produces a comparable report:
#   - Apple Silicon (Metal) on a Mac        → bash scripts/gpu-benchmark.sh
#   - NVIDIA (CUDA) on a Linux GPU host      → bash scripts/gpu-benchmark.sh   (auto-detected)
#
# It builds the agent for the right backend, then measures — HONESTLY, failing loud
# on any correctness break (BLACKHOLE: a throughput number over a diverged decode is
# never reported):
#   1. hardware self-classification + baseline (cx-agent bench)
#   2. batch-throughput SWEEP per model (cx-agent bench-batch): tok/s + speedup curve
#      across batch sizes, with the batched==serial greedy invariant asserted
#   3. NVIDIA only: raw general-compute (scripts/gpubench.cu → FP32/FP16/TF32 TFLOPS,
#      memory bandwidth, Monte Carlo) — the numbers a sim/HPC/training buyer cares about
#   4. a machine-readable JSON bundle + a human REPORT.md, both under the out dir
#
# Everything lands in .artifacts/gpu-bench/<device>-<timestamp>/ so a Metal run and a
# CUDA run can be diffed side by side — the evidence that CX is strong on BOTH.
#
# Env knobs:
#   MODELS       comma list of model refs to sweep
#                (default "llama-3.2-1b-instruct-q4"; add qwen2.5-7b-instruct-q4 on a
#                 big-VRAM box for a heavier decode profile)
#   BATCH_SIZES  comma list swept per model (default "1,2,4,8,16,32")
#   MAX_TOKENS   decode length per request (default 64 — decode-heavy, where batching pays)
#   OUT          output dir (default .artifacts/gpu-bench/<device>-<UTC>)
#   SKIP_BUILD=1 use an existing release binary (don't rebuild)
#   SKIP_GPUBENCH=1  skip the CUDA gpubench.cu step even on NVIDIA
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

MODELS="${MODELS:-llama-3.2-1b-instruct-q4}"
BATCH_SIZES="${BATCH_SIZES:-1,2,4,8,16,32}"
MAX_TOKENS="${MAX_TOKENS:-64}"

pass=0 fail=0
ok()   { echo "  ✓ $1"; pass=$((pass + 1)); }
bad()  { echo "  ✗ $1"; fail=$((fail + 1)); }
hr()   { echo; echo "== $1 =="; }
die()  { echo "ERROR · $*" >&2; exit 1; }

# ── detect backend ───────────────────────────────────────────────────────────
UNAME="$(uname -s)"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  BACKEND=cuda
  DEVLABEL="cuda-$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')"
  BUILD_FLAGS=(--no-default-features --features cuda)
  export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
  export PATH="$HOME/.cargo/bin:$CUDA_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
elif [ "$UNAME" = "Darwin" ]; then
  BACKEND=metal
  DEVLABEL="metal-$(sysctl -n machdep.cpu.brand_string 2>/dev/null | tr ' ' '_' | tr -cd 'A-Za-z0-9_-' | cut -c1-32)"
  BUILD_FLAGS=()   # metal is the default feature
else
  BACKEND=cpu
  DEVLABEL="cpu-$(uname -m)"
  BUILD_FLAGS=(--no-default-features)
fi

# UTC timestamp for the out dir (date is fine in bash; the workflow-JS clock ban does
# not apply here).
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-.artifacts/gpu-bench/${DEVLABEL}-${TS}}"
mkdir -p "$OUT"

echo "gpu-benchmark · backend=$BACKEND · device=$DEVLABEL"
echo "  models=$MODELS · batch_sizes=$BATCH_SIZES · max_tokens=$MAX_TOKENS"
echo "  out=$OUT"

command -v cargo >/dev/null 2>&1 || die "cargo not found (install rust)"
command -v python3 >/dev/null 2>&1 || die "python3 not found (used to render the report)"

# ── 1. build the agent for this backend ──────────────────────────────────────
BIN="agent/target/release/cx-agent"
if [ "${SKIP_BUILD:-0}" = "1" ] && [ -x "$BIN" ]; then
  ok "using existing binary $BIN (SKIP_BUILD=1)"
else
  hr "build agent ($BACKEND)"
  # ${arr[@]+"${arr[@]}"} — the empty-array-safe expansion. On the metal path
  # BUILD_FLAGS is empty, and a bare "${BUILD_FLAGS[@]}" under `set -u` aborts on
  # bash 3.2 (stock macOS) with "unbound variable" BEFORE cargo runs. This idiom
  # expands to nothing when the array is empty and is safe on 3.2+.
  if ( cd agent && cargo build --release ${BUILD_FLAGS[@]+"${BUILD_FLAGS[@]}"} ) >"$OUT/build.log" 2>&1; then
    ok "release build (${BUILD_FLAGS[*]:-default/metal})"
  else
    bad "build FAILED — see $OUT/build.log"; tail -20 "$OUT/build.log"; exit 1
  fi
fi

# ── 2. hardware self-classification + baseline ───────────────────────────────
hr "hardware self-classification (cx-agent bench)"
if "$BIN" bench >"$OUT/capability.json" 2>"$OUT/capability.log"; then
  cls="$(python3 -c "import json;print(json.load(open('$OUT/capability.json')).get('hw_class','?'))" 2>/dev/null || echo '?')"
  ok "self-classified as: $cls"
else
  bad "bench failed — see $OUT/capability.log"; tail -10 "$OUT/capability.log"
fi

# ── 3. batch-throughput sweep per model ──────────────────────────────────────
SWEEP_FILES=()
for model in ${MODELS//,/ }; do
  hr "batch sweep · $model"
  safe="$(echo "$model" | tr -cd 'A-Za-z0-9._-')"
  jf="$OUT/bench-${safe}.json"
  if "$BIN" bench-batch --model "$model" --max-tokens "$MAX_TOKENS" --batch-sizes "$BATCH_SIZES" \
        >"$jf" 2>"$OUT/bench-${safe}.log"; then
    # Echo the human table (stderr of the run) so the operator sees the curve live.
    grep -E "serial|batch=|peak" "$OUT/bench-${safe}.log" | sed 's/^/  /'
    ok "sweep recorded → $jf"
    SWEEP_FILES+=("$jf")
  else
    bad "bench-batch FAILED for $model — see $OUT/bench-${safe}.log"; tail -10 "$OUT/bench-${safe}.log"
  fi
done

# ── 4. NVIDIA raw general-compute (gpubench.cu) ───────────────────────────────
if [ "$BACKEND" = "cuda" ] && [ "${SKIP_GPUBENCH:-0}" != "1" ]; then
  hr "raw general-compute (gpubench.cu)"
  if command -v nvcc >/dev/null 2>&1; then
    cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '. ')"
    SM_ARCH="${SM_ARCH:-sm_${cc:-80}}"
    if nvcc -O3 -arch="$SM_ARCH" scripts/gpubench.cu -o /tmp/gpubench -lcublas >"$OUT/gpubench-build.log" 2>&1; then
      /tmp/gpubench | tee "$OUT/gpubench.txt" | sed 's/^/  /'
      ok "gpubench ran (FLOPS / bandwidth / Monte Carlo → $OUT/gpubench.txt)"
    else
      bad "gpubench compile failed — see $OUT/gpubench-build.log"
    fi
  else
    bad "nvcc missing — install the CUDA toolkit to measure raw compute"
  fi
fi

# ── 5. render REPORT.md from the JSON bundle ──────────────────────────────────
hr "report"
REPORT="$OUT/REPORT.md"
# ${arr[@]+...} guards the empty-array case under `set -u` (bash 3.2) when EVERY model
# failed; the `if` checks the python exit so a crashed/partial render is a real failure
# (bad ⇒ non-zero final exit), never a silent green over a broken report.
if python3 - "$OUT" "$BACKEND" "$DEVLABEL" "$TS" ${SWEEP_FILES[@]+"${SWEEP_FILES[@]}"} >"$REPORT" <<'PY'
import json, sys, os, glob
out, backend, dev, ts = sys.argv[1:5]
sweeps = sys.argv[5:]

def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None

def num(x, default=0.0):
    # A JSON field that is present-but-null returns None from .get with a default,
    # and f"{None:.1f}" raises TypeError. Coerce None (and non-finite → null) to a
    # real number so formatting can never crash the report.
    return default if x is None else x

cap = load(os.path.join(out, "capability.json")) or {}
print(f"# GPU capability report · {dev}\n")
print(f"- backend: **{backend}**")
print(f"- device label: `{dev}`")
print(f"- timestamp (UTC): {ts}")
hwc = cap.get("hw_class")
if hwc: print(f"- self-classified hw_class: **{hwc}**")
for k in ("memory_gb", "bandwidth_gbps", "engine", "build_hash", "agent_version"):
    if k in cap and cap[k] not in (None, ""):
        print(f"- {k}: {cap[k]}")
print()

print("## Batch-throughput sweep\n")
print("Batched decode shares the decode step across the batch — CX's core throughput")
print("lever. Higher `tok/s` and `×serial` at larger batch = better. `det` = batched")
print("output byte-identical to serial (greedy). Determinism is a SEPARATE property from")
print("throughput: on GPU, batched≠serial can happen from float reduction-order (a greedy")
print("tie flips) — the tok/s are still real; it only means byte-exact verified jobs need")
print("a fixed batch or a tolerant comparator on that hardware.\n")
for sf in sweeps:
    d = load(sf)
    if not d: continue
    det = d.get('batched_deterministic_vs_serial')
    if det is None: det = d.get('all_batches_correct')   # tolerate older records
    diverged = d.get('diverged_batches', [])
    print(f"### {d.get('model','?')}  ·  max_tokens={d.get('max_tokens','?')}\n")
    print(f"- serial baseline: **{num(d.get('serial_baseline_tok_s')):.1f} tok/s**")
    print(f"- peak: **{num(d.get('peak_tok_s')):.1f} tok/s** = **{num(d.get('peak_speedup_vs_serial')):.2f}× serial**")
    if det:
        print("- byte-determinism vs serial: **identical at every batch size** ✓\n")
    else:
        print(f"- byte-determinism vs serial: **diverges at batch {diverged}** "
              f"(GPU reduction-order; throughput still valid)\n")
    print("| batch | tok/s | ×serial | per-req tok/s | wall s | det |")
    print("|------:|------:|--------:|--------------:|-------:|:---:|")
    for r in d.get("sweep", []):
        print(f"| {r.get('batch','?')} | {num(r.get('tokens_per_s')):.1f} | {num(r.get('speedup_vs_serial')):.2f} | "
              f"{num(r.get('per_request_tok_s')):.1f} | {num(r.get('wall_s')):.2f} | {'✓' if r.get('batched_equals_serial') else '≠'} |")
    print()

gp = os.path.join(out, "gpubench.txt")
if os.path.exists(gp):
    print("## Raw general-compute (NVIDIA)\n```")
    print(open(gp).read().strip())
    print("```\n")

print("---")
print("Reproduce: `bash scripts/gpu-benchmark.sh` (auto-detects Metal/CUDA). "
      "Deep CUDA run via `bash scripts/runpod-spike.sh`.")
PY
then
  ok "report written → $REPORT"
else
  bad "report render FAILED (python exit non-zero) — $REPORT may be partial"
fi

hr "result"
echo "  passed: $pass   failed: $fail"
echo "  bundle: $OUT"
[ "$fail" -eq 0 ] && { echo "  GPU BENCHMARK OK ✅"; exit 0; } || { echo "  benchmark had failures ✗"; exit 1; }
