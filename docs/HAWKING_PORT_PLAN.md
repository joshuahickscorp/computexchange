# Hawking continuous-batch scheduler — port plan

> The plan-of-record for porting the founder's Hawking continuous-batching scheduler as
> computexchange's Apple-Silicon continuous-batch lane (engine tag `hawking`). Source
> recommendation: `docs/PERF_AND_CAPABILITY_AUDIT.md` Wave 2 ("Port Hawking's
> continuous-batching scheduler as the Apple-Silicon lane") and the Hawking-integration
> section. The SKELETON lands in this change (`agent/src/continuous_batch.rs`); it is
> inert-by-default and falls back to the existing per-task batched decode, so behavior
> today is UNCHANGED. The wired lane is a ~4-6 week, Apple-hardware-gated build.

## Why this lane

Hawking interleaves prefill and decode across slots so concurrent requests share one
model forward pass per step. Measured **5.0x aggregate vs single-stream at B=8 on an M3
Pro**. This is the genuine differentiator no wired engine sells determinism-gated: a
deterministic continuous-batching scheduler on the Apple lane. Hawking is
Apple-Silicon ONLY (Metal, `objc2-metal`, zero Candle, zero CUDA), so it never touches
the CUDA cloud lane — that is vLLM (`docs/VLLM_LANE.md`). The kernels are NOT the asset
(Hawking is ~0.62x llama.cpp at batch-1 decode); the SCHEDULER and the determinism
discipline are.

## What is PROVEN vs UNVALIDATED (be honest)

PROVEN (M3 Pro measured):
- **B=8, 5.0x aggregate** throughput vs single-stream.
- The greedy-vs-sampled lane routing (token-only `B*4`-byte readback when all-greedy,
  full `B*vocab` logits otherwise).
- Slot-strided KV (a slot keeps its KV region as the ready set churns).
- The deterministic prefix-affinity prefill cohort (`group_by_prefix`, `min_shared=8`).

UNVALIDATED (do NOT market these):
- **B=16** and larger batches (the GEMMs are independently capped at `1..=8` in Hawking).
- **Big models** (the Apple large-model runner does not exist; cap is 7B).
- Paged-KV.
- The project's own ceiling oracle self-warns it overpredicts ~2.5-4x, so "6-8x"
  extensions are spike-gated.

## Port mapping (Hawking module -> cx module)

The skeleton (`agent/src/continuous_batch.rs`) lands the data structures and the PURE,
GPU-free selection logic — the parts testable on any host. The GPU forward pass is the
documented-only seam.

| Hawking (`crates/hawking-serve/src/batch/`) | computexchange (`agent/src/continuous_batch.rs`) | Status |
| --- | --- | --- |
| `mod.rs::SlotState` (Idle/Prefilling/Decoding/Finishing) | `SlotState` | ported (skeleton) |
| `mod.rs::Slot` (id, state, prompt_ids, generated_ids, last_token, position, max_new_tokens) | `Slot` (+ `greedy` flag) | ported (trimmed; GPU KV handle added when kernel lands) |
| `mod.rs::DecodeStep` (slot_id, token, position) | `DecodeStep` | ported (skeleton) |
| `scheduler.rs::BatchPolicy` (Default/GreedyFirst/PrefixGrouped) | `BatchPolicy` (Default/PrefixGrouped) | ported subset |
| `scheduler.rs::common_prefix_len` | `common_prefix_len` | ported verbatim |
| `scheduler.rs::group_by_prefix` (prefix-affinity cohort, `min_shared`) | `group_by_prefix` | ported verbatim (deterministic, tie-broken) |
| `scheduler.rs::Scheduler` (slot pool, `decode_batch`, `prefill_slots_*`) | `Scheduler` (`decode_plan`, `prefill_plan`) | ported subset (selection only) |
| `driver.rs::decode_ready_once` all-greedy lane routing | `Scheduler::decode_lane` -> `DecodeLane::{GreedyTokens,FullLogits}` | ported (routing decision only) |
| `driver.rs::forward_multiseq_greedy_tokens` / `forward_multiseq_batched` | **documented-only seam** (Metal multi-seq slot-strided KV kernel) | NOT ported (the ~4-6 wk build) |
| slot-strided KV in `qwen_dense.rs` | KV region keyed by `slot_id` (the `Scheduler` already keys plans by `slot_id`, not the compacted index) | shape only; kernel NOT ported |

What deliberately did NOT come over: `apply_decode_logits` / `apply_decode_tokens` /
`sample_next` (they mutate slot state from a real forward pass — meaningless without the
kernel) and the `PrefixIndex` KV-copy path (that is a serve-layer optimization, not the
scheduler core). They are added with the kernel in week 3-4 below.

## The fallback (why behavior is unchanged today)

`continuous_batch::Scheduler` plans steps but is wired to NO GPU kernel. When an operator
sets `inference_backend = "hawking"`, `main.rs` registers the engine tag (so the
control-plane verification class is correct ahead of time) but does NOT insert a runner
that consumes the plan — generative jobs keep running through the existing per-task
`LlamaBackend::generate_batch` on Candle. The skeleton's `decode_plan` / `prefill_plan` /
`decode_lane` are PURE and emit no model output, so there is zero behavior change today.
The seam exists so it does not move when the Metal kernel lands.

## Determinism re-gating plan

The lane is re-gated cross-worker against the **Apple verification class**
(`docs/DETERMINISM_CLASS.md`). The governing truth from Hawking's own research:
token-level determinism is IMPOSSIBLE across heterogeneous Mac generations ("semantic
replay, not bit-exact", up to 15% accuracy variance at temp=0). So:

- The batched path is B-dependent kernel routing and is NOT bit-identical to the solo
  Candle path (Hawking's parity tests assert `atol=1e-3`, not bit-identity). This is the
  SAME cross-hardware fp16 risk the single-stream path already carries, not a new risk
  class — but it means a `hawking` worker must NEVER be byte-compared against a `candle`
  worker. The engine tag (`hawking` vs `candle`) already enforces that: they are
  different verification classes, so a cross-engine byte difference is `pass_with_penalty`
  + a `redundancy_cross_class` receipt, never an auto-dock.
- Within the Apple lane, pin redundancy peers to one `(apple_silicon_*, hawking,
  build_hash)` class. `build_hash` folds the Metal `shader_hash` analogue (agent version +
  device backend + the multi-seq kernel identity) so a kernel change lands in a new class
  automatically.
- Seed `batch_infer` honeypot answers WITH their producing `(apple, hawking, build_hash)`
  class (the same hw_class-aware seeding the vLLM lane needs) before the lane carries
  byte-exact work. A `candle`-seeded honeypot would byte-fail a correct `hawking` result.
- Seed the golden-hash baseline for the `hawking` class (`CX_GOLDEN_RECORD=1` on a pinned
  reference Mac) so a future kernel drift is caught on the build that changed.

The skeleton already enforces the determinism PRECONDITION the market depends on: every
selection function is PURE and deterministic (ascending slot-id order, stable tie-breaks
in `group_by_prefix`), proven by `group_by_prefix_is_deterministic`.

## ~4-6 week breakdown

- **Week 1 — scheduler core (LANDED as skeleton).** Slot table, `SlotState`, `DecodeStep`,
  `BatchPolicy`, `group_by_prefix`, `decode_plan`/`prefill_plan`/`decode_lane`, unit
  tests. Done in this change, GPU-free.
- **Week 2 — Metal multi-seq KV kernel.** Port slot-strided KV (a slot keeps its KV
  region; the multi-seq path keys KV by `slot_id`). The single hardest, Apple-only piece:
  `forward_multiseq_greedy_tokens` (token-only `B*4` readback) + `forward_multiseq_batched`
  (full logits). Requires Apple hardware.
- **Week 3 — wire the decode loop.** Connect `decode_plan` -> kernel -> `apply_decode_*`
  -> slot advance; bring over `sample_next`, EOS handling, the lane stats. Add a
  `HawkingRunner` (mirrors `MlxRunner`) that consumes the plan and replaces the
  per-task fallback for the generative job types on an Apple host.
- **Week 4 — prefill + prefix reuse.** Prefix-grouped prefill (`group_by_prefix` is
  already ported) + the `PrefixIndex` KV-copy path so a shared prefix is prefilled once.
- **Week 5 — determinism re-gate.** B=8 batched==serial parity (`atol=1e-3`), seed the
  `(apple, hawking, build_hash)` golden baseline + hw_class-aware honeypots, cross-Mac
  class-boundary test (prove cross-generation is correctly NOT byte-compared).
- **Week 6 — soak + buffer.** Sustained-load soak at B=8, slot-churn stress (ready set
  churning while slots hold KV), and schedule slack for the Metal kernel (the riskiest
  item). B=16 and big models are explicitly OUT of this window (unvalidated).

## Do NOT pull in (per the audit)

Spec decode (net-negative on the tested Mac: EAGLE head 0.40x-0.21x, n-gram τ=1.43
sub-gate); the megakernel (a no-op skeleton in a Type-1-dead region, uses f16 weights =
2x the bandwidth of production Q4); the sub-Q4/condense codec (every requant-from-quantized
form is NO-GO). F16 KV cache is a SEPARATE Wave-3 non-byte-exact lane (88% argmax-identity),
not part of this port.
