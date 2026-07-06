# 2026-07-06 — M3 Pro — Hawking dispatch lane vs Candle per-task batched (dispatch-level)

**Verdict up front, honestly: the wired Hawking continuous-batch dispatch lane is
SLOWER than the existing Candle per-task batched path on this hardware and this
workload shape — 88.3 vs 132.1 tok/s median, i.e. 0.67x.** The dispatch WIRING is
correct and real-Metal-proven (byte-identical output to `BatchInferRunner` on the
well-separated gate set; every completion equals its solo serial generation); the
throughput number is simply not a win at the per-task dispatch level today, and this
report says so instead of hiding it. No speedup is claimed anywhere for this lane at
dispatch level.

## What was measured

Dispatch-level, end-to-end through the REAL runner path (`Runner::run` on real JSONL
bytes through the same warm `ModelPool`), not a kernel microbench:

- **Arm A (baseline):** `BatchInferRunner::run` — the shipped per-task batched Candle
  path (`LlamaBackend::generate_batch`, exact-length bucketing + padded singleton
  bands).
- **Arm B (new):** `HawkingRunner::run` — the Week-6-wired continuous-batch lane
  (`LlamaBackend::hawking_generate_churn`, scheduler admission + slot churn + flat
  slot-strided KV over `hawking_decode_step`), `pool_size = 8` (the shipped default,
  hard-clamped 1..=8).

Proof artifact / harness: `runners::tests::hawking_dispatch_vs_candle_batched_throughput_measured`
(`#[ignore]`d, metal-gated, acquires `METAL_HARDWARE_TEST_LOCK`). Raw run log:
`2026-07-06-m3pro-hawking-dispatch.log` (this directory).

## Method (mirrors the bench-batch `--mode mixed` precedent in this directory)

- Hardware: Apple M3 Pro (Metal), the same reference box as the 2026-07-05 records.
- Model: `llama-3.2-1b-instruct-q4` (Llama-3.2-1B-Instruct Q4_K_M GGUF) — the same
  model every hawking gate uses.
- Workload: 24 mixed-length, real-traffic-shaped prompts (the bench-batch mixed
  regime: ocean stem + 0..=6 filler clauses cycled, so exact-length bucketing
  fragments — the honest case), `max_tokens = 48`, greedy (both lanes are
  greedy-only; parity).
- Order control: one untimed warm-up run per arm (model load + first-kernel JIT land
  there), then 3 interleaved rep pairs (candle, hawking, candle, hawking, …) so
  thermal/ordering drift cannot favor an arm; median tok/s per arm; tok/s uses each
  arm's OWN real generated-token count (`tokens_used` from the runner's JobOutput).

## Measured results (real, this box, 2026-07-06)

| rep | candle batched (tok/s) | hawking pool=8 (tok/s) |
|-----|------------------------|------------------------|
| 0   | 131.4 (1152 tok, 8.77s) | 88.3 (1152 tok, 13.04s) |
| 1   | 132.1 (1152 tok, 8.72s) | 88.3 (1152 tok, 13.05s) |
| 2   | 132.1 (1152 tok, 8.72s) | 89.3 (1152 tok, 12.90s) |

- **Median: candle 132.1 tok/s · hawking 88.3 tok/s · hawking/candle = 0.67x.**
- Wall-clock for the whole 24-prompt chunk: candle ≈ 8.7s, hawking ≈ 13.0s.
- Variance was tiny across reps (both arms within ~1%), so the 0.67x is stable, not
  noise.
- Reference points from the 2026-07-05 records in this directory (same box, same
  model, same mixed regime, measured then, not re-measured today): serial
  single-stream baseline ≈ 104 tok/s; candle batched peak 139.6 tok/s at batch 32.
  The hawking dispatch number (88.3) is below even the serial single-stream Candle
  baseline for this workload shape.

Dispatch-gate side observation (6 short prompts, 44 total tokens, from
`hawking_dispatch_end_to_end_matches_batchinfer_format_and_solo_serial` in the same
session): pool_size=8 completed in 933 ms vs pool_size=2 in 1255 ms — the lane does
scale with its own pool width; it is the absolute level that loses to candle batched.

## Cross-lane divergence data point (reported, not asserted)

11/24 rows differed textually between the two lanes on this free-form 48-token
workload. This is the documented, characterized argmax near-tie membership property
(`hawking_churn_neartie_flip_is_membership_dependent_not_corruption`) — both lanes
emit valid greedy decodes; long free-form generation simply hits genuine near-ties
far more often than the short factual gate prompts (where byte-identity holds and is
asserted). Operational consequence, already enforced by design: a `hawking` worker is
a DISTINCT verification class (engine tag + build_hash) and must never be
byte-compared against a `candle` worker; byte-exact money work on this lane stays
gated on the Week-6b hawking-class honeypot/golden seeding (control-side, a separate
bundle).

## Why the lane loses here (analysis — hypothesis, NOT a measured attribution)

- **Prefill:** the ported churn driver prefills token-by-token through the same
  decode primitive (one code path builds all KV — chosen for correctness
  provability). Candle's `generate_batch` prefills each bucket's full prompts in ONE
  forward pass. At this workload (~12-60-token prompts vs 48 generated tokens),
  hundreds of extra full forward passes land on the hawking arm.
- **Kernel level:** the port plan itself says the kernels are not the asset
  (upstream Hawking ≈ 0.62x llama.cpp at batch-1 decode); the PROVEN 5.0x at B=8 was
  aggregate vs SINGLE-STREAM serial in the original engine — nothing in the plan ever
  claimed a win over candle's per-task BATCHED path, and this measurement confirms
  there isn't one today.
- Named levers if this lane is to win at dispatch level (Week 6b+, unbuilt, no
  numbers claimed): bulk prefill through a real prefill pass, `PrefixIndex` KV-copy
  prefix reuse, and the lane's actual thesis — CONTINUOUS arrival (streaming
  admission across task boundaries), which per-task dispatch with arrival=all-zeros
  structurally cannot exhibit.

## Status consequence

The wiring stays landed (correct, proven, opt-in via `inference_backend = "hawking"`,
default Candle path byte-for-byte unchanged), but nothing routes to it by default and
no throughput claim attaches to it. The speed-lane wall-clock thesis continues to
rest on the fan-out moat (control-plane) and the measured candle batched path.
