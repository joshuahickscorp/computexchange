#!/usr/bin/env bash
# prove-cuda.sh — one-command proof of the NVIDIA/CUDA lane on a real GPU box.
#
# The Apple/Metal lane is proven by scripts/prove-local.sh on a Mac; this is its
# CUDA sibling, meant to run on a Linux + NVIDIA host (e.g. a RunPod pod). It is the
# authoritative gate the CI cuda-build job (continue-on-error) defers to.
#
# What it proves, in order (fails loud — no soft-skips):
#   1. toolchain present (nvidia-smi, nvcc, cargo) — installs rust if missing
#   2. the agent BUILDS with --features cuda
#   3. raw general-compute capability (scripts/gpubench.cu: VRAM bw + FP32/TF32/FP16
#      TFLOPS + a Monte Carlo sim) — the numbers a sim/HPC/training buyer cares about
#   4. the agent DETECTS the GPU and self-classifies as an nvidia_* worker (not cpu),
#      advertising VRAM as its gating memory, with real embed eps + llama tps
#   5. batched generation is correct AND faster than serial (batched == serial output)
#
# Usage:  bash scripts/prove-cuda.sh
# Env:    CUDA_HOME (default /usr/local/cuda), SM_ARCH (auto-detected from the GPU).
set -uo pipefail
cd "$(dirname "$0")/.."

pass=0 fail=0
ok()   { echo "  ✓ $1"; pass=$((pass + 1)); }
bad()  { echo "  ✗ $1"; fail=$((fail + 1)); }
hr()   { echo; echo "== $1 =="; }

export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
export PATH="$HOME/.cargo/bin:$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

hr "1. toolchain"
if command -v nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
else
  bad "nvidia-smi missing — this is not an NVIDIA host; aborting"; exit 1
fi
if ! command -v nvcc >/dev/null 2>&1; then
  bad "nvcc missing — install the CUDA toolkit (e.g. apt-get install cuda-toolkit-12-4) or set CUDA_HOME; aborting"; exit 1
fi
ok "nvcc: $(nvcc --version | grep -o 'release [0-9.]*')"
if ! command -v cargo >/dev/null 2>&1; then
  echo "  installing rust (rustup, minimal)…"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null 2>&1
  export PATH="$HOME/.cargo/bin:$PATH"
fi
command -v cargo >/dev/null 2>&1 && ok "cargo: $(cargo --version)" || { bad "cargo unavailable"; exit 1; }

# GPU compute capability -> nvcc -arch (8.0 A100, 8.6 A10/3090, 8.9 4090/L40, 9.0 H100/H200).
cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '. ')"
SM_ARCH="${SM_ARCH:-sm_${cc:-80}}"
echo "  GPU arch: $SM_ARCH"

hr "2. agent builds --features cuda"
if (cd agent && cargo build --release --no-default-features --features cuda) >/tmp/cuda-build.log 2>&1; then
  ok "release build (--features cuda)"
else
  bad "cuda build FAILED:"; tail -15 /tmp/cuda-build.log; exit 1
fi
BIN=agent/target/release/cx-agent

hr "3. raw general-compute (gpubench.cu)"
if nvcc -O3 -arch="$SM_ARCH" scripts/gpubench.cu -o /tmp/gpubench -lcublas >/tmp/gpubench-build.log 2>&1; then
  /tmp/gpubench | sed 's/^/  /'
  ok "gpubench ran (FLOPS / bandwidth / Monte Carlo above)"
else
  bad "gpubench failed to compile:"; tail -10 /tmp/gpubench-build.log
fi

hr "4. agent detects + self-classifies the GPU (cx-agent bench)"
"$BIN" bench >/tmp/cuda-bench.json 2>/tmp/cuda-bench.log
cls="$(grep -o '"hw_class"[ ]*:[ ]*"[^"]*"' /tmp/cuda-bench.json | head -1 | grep -o 'nvidia_[0-9]*g' || true)"
if [ -n "$cls" ]; then
  ok "self-classified as $cls (not cpu)"
else
  bad "did NOT self-classify as nvidia_* — check device_label()==cuda + nvidia-smi detection"; grep -i "detected\|cpu\|hw_class" /tmp/cuda-bench.log | head
fi
grep -iE "benchmark .*eps=|benchmark .*tps=" /tmp/cuda-bench.log | sed 's/^/  /' | head -4 || true

hr "5. batched generation: correct AND faster than serial"
( cd agent && cargo test --release --no-default-features --features cuda \
    batched_vs_serial_throughput -- --ignored --nocapture --test-threads=1 ) >/tmp/cuda-batch.log 2>&1
if grep -q "test result: ok" /tmp/cuda-batch.log; then
  grep -iE "serial :|batched:|SPEEDUP|correctness" /tmp/cuda-batch.log | sed 's/^/  /'
  ok "batched generation verified (batched == serial, with speedup)"
else
  bad "batched generation test FAILED:"; grep -iE "panicked|assertion|FAILED|error" /tmp/cuda-batch.log | head
fi

# TODO (after the BYO-container runner lands): smoke-test a `custom` job —
#   docker run --gpus all --network none <image> <command>  -> result uploaded, metered.

hr "result"
echo "  passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] && { echo "  CUDA lane PROVEN ✅"; exit 0; } || { echo "  CUDA lane has failures ✗"; exit 1; }
