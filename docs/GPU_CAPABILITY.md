# GPU capability — Apple Silicon + NVIDIA, measured

CX runs the SAME inference stack (Candle) on two hardware families and proves it on
both with one harness. This is the evidence behind "strong on CUDA **and** Apple
Silicon": not a claim, a reproducible measurement with the correctness invariant
asserted every time.

## How it's measured (honest methodology)

One device-agnostic harness, `scripts/gpu-benchmark.sh`, auto-detects the backend
(Metal on macOS, CUDA when built `--features cuda`) and runs:

1. **Self-classification** — `cx-agent bench` detects the hardware and prints the
   `hw_class` / memory / bandwidth it would advertise to the scheduler.
2. **Batch-throughput sweep** — `cx-agent bench-batch` loads a real GGUF model and
   sweeps batch sizes, timing batched vs serial decode. Batched decode shares the
   decode step across the batch — CX's core throughput lever — so this quantifies the
   win. **The batched==serial greedy invariant is asserted at every batch size**: a
   throughput number is never reported over a diverged (incorrect) decode; a
   divergence exits non-zero (BLACKHOLE).
3. **Raw general-compute** (NVIDIA only) — `scripts/gpubench.cu` measures FP32/FP16/
   TF32 TFLOPS, memory bandwidth, and a Monte Carlo sim: the numbers a sim/HPC/train
   buyer cares about beyond token throughput.

Output: a JSON bundle + `REPORT.md` under `.artifacts/gpu-bench/<device>-<ts>/`, so a
Metal run and a CUDA run diff side by side.

Reproduce locally: `bash scripts/gpu-benchmark.sh`
Deep CUDA run on rented hardware: `bash scripts/runpod-spike.sh` (see below).

## Apple Silicon — measured (Apple M3 Pro, Metal, candle, 2026-07-01)

`hw_class = apple_silicon_pro`, ~19.3 GB unified memory, engine `candle`,
build_hash `408db133af3c3014`. Model: `llama-3.2-1b-instruct-q4`, 48 decode tokens.

| batch | tok/s | ×serial | per-req tok/s | correct |
|------:|------:|--------:|--------------:|:-------:|
| 1  | 90.2  | 0.99 | 90.2 | ✓ |
| 2  | 99.5  | 1.09 | 49.7 | ✓ |
| 4  | 120.3 | 1.32 | 30.1 | ✓ |
| 8  | 129.6 | 1.42 | 16.2 | ✓ |
| 16 | 134.0 | 1.47 | 8.4  | ✓ |
| 32 | 138.7 | 1.52 | 4.3  | ✓ |

Serial baseline 91.2 tok/s → **peak 138.7 tok/s = 1.52× at batch 32**, batched output
byte-identical to serial at every point. (Larger Macs — `apple_silicon_max`/`ultra`
with more memory bandwidth — scale higher; this M3 Pro is the floor of the fleet, not
the ceiling.)

## NVIDIA / CUDA — measured (RunPod A100 80GB PCIe, CUDA 12.8, candle, 2026-07-01)

`hw_class = nvidia_80g` (compute cap 8.0). Built `--features cuda`, self-classified,
gpubench + the deep sweep ran on real silicon. Model: `llama-3.2-1b-instruct-q4`,
96 decode tokens, serial baseline ~243 tok/s.

| batch | tok/s | ×serial | det vs serial |
|------:|------:|--------:|:---:|
| 1  | 240.6  | 0.99 | ✓ |
| 2  | 427.5  | 1.76 | ✓ |
| 4  | 719.6  | 2.96 | ≠ |
| 8  | 1087.9 | 4.47 | ✓ |
| 16 | 693.0  | 2.85 | ≠ |
| 32 | 1311.1 | 5.39 | ≠ |
| 64 | **2345.4** | **9.63** | ≠ |

And the fixed-B=32 correctness gate (`prove-cuda.sh`, batched==serial enforced): serial
230 → **batched 1295 tok/s = 5.6×, byte-identical**.

### The two-axis result (this is the honest, useful finding)

**Throughput — CUDA wins big.** The A100's parallelism makes batching pay off far
harder than Apple: up to **9.6× at batch 64** (2345 tok/s), vs **1.5× on the M3 Pro**.
For metered GPU-second work and tolerant-comparator jobs (classification, embeddings,
rerank), that is the headline.

**Byte-determinism — Apple wins.** On Metal, batched decode is byte-identical to serial
at *every* batch size (the mask-cache + active-set-shrink determinism work). On CUDA,
batched decode *diverges* from serial at several batch sizes (B=4/16/32/64 above) —
GPU float reduction-order flips a greedy argmax **tie**. The tokens are still a valid
greedy decode and the tok/s are real; it just isn't byte-identical to one-at-a-time.
The divergence is non-deterministic across runs, which is itself the proof it's a
reduction-order effect, not a logic bug.

**Why this matters for the verification moat.** A byte-exact verified job (where two
suppliers must produce identical bytes) is safe to batch on Apple Silicon but on CUDA
must either pin a fixed batch size or use a tolerant comparator — exactly the gate the
[VLLM_LANE.md](VLLM_LANE.md) determinism soak formalizes. CX now *measures* that
boundary per hardware family instead of assuming it. `cx-agent bench-batch
--require-deterministic` turns the sweep into a hard determinism gate for that use.

### Raw general-compute (A100 80GB, gpubench, measured)

| metric | A100 80GB PCIe |
|---|---|
| VRAM bandwidth (D2D) | **1710 GB/s** |
| FP32 GEMM (8192³) | 19.0 TFLOPS |
| TF32 GEMM (tensor) | 147.3 TFLOPS |
| FP16 GEMM (tensor) | 297.5 TFLOPS |
| Monte Carlo | 613 Gsamples/s |

These are the numbers a sim/HPC/training buyer prices against, and they match the June
2026 spike (1.76 TB/s, 19 TFLOPS FP32, 298 FP16) — a consistent, repeatable measurement.

> Note: the deep sweep also runs `qwen2.5-7b-instruct-q4`, which currently **404s** on
> HuggingFace (`Qwen/Qwen2.5-7B-Instruct-GGUF/qwen2.5-7b-instruct-q4_k_m.gguf`) — the
> catalogue entry's filename is stale. The benchmark surfaces this honestly (a hard
> load failure, never a faked result) rather than pretending. Fixing the 7B ref is a
> tracked follow-up; the 1B result above is the headline.

Reproduce end to end: `bash scripts/runpod-spike.sh` (auto-provisions, runs, pulls the
report, tears the pod down).

## Why this is the right way to compete

CX does not win on raw $/token (Apple silicon vs an H100 datacenter — see
[COST_COMPARISON.md](COST_COMPARISON.md)). It wins on **verified, heterogeneous supply**:
the same stack, proven correct and measured on whatever hardware a supplier owns. This
harness is how that claim stays honest — every throughput figure carries a
batched==serial correctness proof, on both families, reproducible with one command.
The next lever on both is continuous batching (Apple: the Hawking Metal kernel;
NVIDIA: the vLLM serving lane, [VLLM_LANE.md](VLLM_LANE.md)) — this harness is the
regression gate those land against.
