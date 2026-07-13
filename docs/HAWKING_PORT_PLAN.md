<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
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
- **A real GGUF decodes CORRECTLY through the whole cx port** (Week 4, entry 82): a real
  Llama-3.2-1B Q4_K_M GGUF through `hawking_decode_step` (RoPE + Q4_K projections + flat
  multi-region KV) is coherent + token-matching serial for a fixed cohort.
- **Churn safety** (Week 5, entry 84): dynamic admission + slot churn + stable-KV-region
  REUSE keep every sequence byte-for-byte equal to solo serial (6 prompts through a
  2-slot pool, 4 real region reuses, staggered arrival/completion). The lone documented
  exception is a genuine argmax near-tie flip under differing co-batch membership — the
  `atol` reduction-order property, proven benign and non-spreading, never corruption.

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

## The fallback (what changes only on explicit opt-in)

> **Historical note:** this section originally described the Week-1 skeleton state
> ("wired to NO GPU kernel … does NOT insert a runner"). As of Week 6a (2026-07-06)
> that is no longer true: `inference_backend = "hawking"` on a Metal build inserts a
> REAL `HawkingRunner` that dispatches `batch_infer` through the proven churn engine.

What remains true and load-bearing: the DEFAULT (`inference_backend = "candle"`,
i.e. every operator who did not opt in) inserts no hawking runner and is
byte-for-byte unchanged. On the hawking opt-in, only `batch_infer` on the small GGUF
family routes to the lane; `batch_classification`/`json_extraction`, the 7B GGUF,
and cluster models fall through to their existing runners exactly as before (see
Week 6a and `HawkingRunner`'s own doc comment for the wired-vs-not list). On a
non-Metal build the arm stays a log line only.

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
- **Week 2 — Metal multi-seq KV kernel (LANDED, 2026-07-05, `agent/src/hawking_metal_kernel.rs`).**
  The slot-strided KV decode attention kernel (`mha_decode_f32_batched_multiseq`) and its
  companion scatter-append (`kv_scatter_append`), ported from the real Hawking source
  (`~/Downloads/hawking/crates/hawking-core/shaders/{mha,common}.metal` and
  `src/model/qwen_dense.rs`'s `forward_tokens_multiseq_stack_tcb`) as real Candle
  `CustomOp3`/`InplaceOp2` implementations, dispatching runtime-compiled MSL directly on
  Metal via `MetalDevice::command_encoder`/`new_library_with_source` (no candle "ug"
  feature needed — this hand-rolls the same compile step outside that gate). **Proof
  artifact:** two tests run the kernel on the REAL Metal GPU (`Device::new_metal(0)`,
  skipped only if none is present) and verify its output against an INDEPENDENT reference
  built from Candle's own `matmul`/`softmax` (not a copy of the kernel's own math), plus a
  third test proving the core continuous-batching property directly — two slots at
  DIFFERENT history lengths sharing one dispatch do not corrupt each other. All pass on
  the real M3 Pro in this session (`cargo test --features metal hawking_metal_kernel`: 5
  passed); the full agent suite (113 tests) and the no-metal CPU/CUDA build (108 tests)
  both stay green.
  **What this does NOT do** (see the module's own doc comment for the full list): no RoPE
  fusion, no Q4_K quantized projection GEMMs (accepts plain F32 Q/K/V), and — most
  importantly — nothing calls this module yet. It is not wired into
  `continuous_batch::Scheduler`, there is no `HawkingRunner`, and `continuous_batch.rs`'s
  inert-by-default behavior is completely unchanged. This closes the "requires Apple
  hardware, single hardest piece" risk item for real; weeks 3-6 remain open.
- **Week 3 — wire the decode loop (LANDED, 2026-07-05).** `decode_plan` -> the real
  Week-2 Metal kernel -> `apply_decode_*` -> slot advance, all real, all proven on this
  M3 Pro (see `docs/internal/CREED_AND_PATH_TO_TEN.md` Implementation Log entry 69 for
  the full proof). `sample_next`/EOS handling/lane stats ported from the real upstream
  source. `HawkingRunner` added, mirroring `MlxRunner`'s honest-boundary seam, gated
  behind an explicit `inference_backend = "hawking"` opt-in — the default Candle path
  for every other operator is unchanged. **What Week 3 did NOT do (now DONE in Week 4,
  below):** drive a real GGUF model through the loop.
- **Week 4 — real GGUF wired through the kernel, correctness-proven (LANDED,
  2026-07-06; `docs/internal/CREED_AND_PATH_TO_TEN.md` Implementation Log entry 82).**
  The model-integration rewrite Week 3 named as the remaining boundary — the three
  pieces `HawkingRunner::run`'s error listed — is BUILT and PROVEN on this M3 Pro, for
  real: (a) per-slot RoPE ahead of the kernel (reusing entry 73's per-row rotary
  `apply_rotary_emb_per_row`), (b) the Q4_K quantized projection GEMMs producing real
  F32 Q/K/V (`LayerWeights::hawking_project_decode`, using the layer's OWN real
  `attention_wq/wk/wv` — no dequant-reproject stand-in), and (c) the flat, per-layer,
  multi-region slot-strided KV cache (`HawkingKvCache` + `ModelWeights::
  hawking_decode_step`, replacing `LayerWeights`'s private single-contiguous
  `KvCacheSlot` on the Hawking path only — the default Candle path's `KvCacheSlot` is
  completely untouched). **Proof, on real Metal:** the `#[ignore]`d, real-Metal gate
  `runners::tests::hawking_real_gguf_decode_matches_serial_and_is_coherent` loads the
  real Llama-3.2-1B-Instruct Q4_K_M GGUF and drives greedy generation ENTIRELY through
  this path (`LlamaBackend::hawking_generate` -> `hawking_decode_step` -> the
  `hawking_metal_kernel` ops), asserting (1) COHERENCE — real factual completions
  ("...is Paris.", "...is Jupiter."); (2) TOKEN-MATCH vs serial `generate` byte-for-byte
  (greedy argmax is stable across the tree-softmax kernel's different reduction order —
  NOT a byte-exact logit claim, which is impossible per the determinism section below,
  but the right correctness bar and one a genuinely-wrong integration breaks); and (3)
  the MODEL-LEVEL continuous-batching property — two DIFFERENT-length prompts decoded
  TOGETHER through one shared forward pass per step each equal their SOLO generation
  ("Paris" and "2, 3, and 5"), lifting the kernel-only `slots_are_independent_...`
  proof up a full real-model forward pass. All four pre-existing determinism gates
  (`batch_padded_bucket_equals_serial_mixed_lengths`,
  `batch_active_shrink_equals_serial_mixed_lengths`, `batch_shared_prefix_equals_serial`,
  `batch_width_split_matches_unsplit_batch`) still pass byte-for-byte on real Metal
  (0 regressions); build+clippy clean on both feature configs at the 4-warning baseline;
  full non-ignored suite metal 172 / no-metal 166. **What Week 4 did NOT do (honestly
  remaining):** wire `hawking_generate` into DISPATCH. `HawkingRunner::run` still
  surfaces its honest boundary because the SCHEDULER integration — dynamic admission and
  slot churn (the ready set changing while slots hold KV), not a fixed cohort;
  connecting the proven `continuous_batch::Scheduler`'s `decode_plan`/`admit`/`release`
  to `hawking_decode_step`; and prefix reuse — is the remaining piece. `hawking_generate`
  proves the model path is CORRECT for a fixed cohort of concurrent sequences; making the
  runner drive real production traffic through the scheduler is weeks 5-6. `#[allow(dead_
  code)]` on `hawking_generate` marks exactly that not-yet-dispatch-wired state honestly.
- **Week 5 — dynamic admission + slot churn wired through the scheduler (LANDED,
  2026-07-06; `docs/internal/CREED_AND_PATH_TO_TEN.md` Implementation Log entry 84).**
  The piece Week 4's boundary named — connect the proven `continuous_batch::Scheduler`'s
  DYNAMIC admission and slot churn (the ready set changing WHILE slots hold KV, not a
  fixed cohort) to `hawking_decode_step` — is BUILT and PROVEN on this M3 Pro.
  `LlamaBackend::hawking_generate_churn` (runners.rs) drives a real Llama-3.2-1B Q4_K_M
  GGUF through a churn loop: (1) `Scheduler::admit` fills free slots with arrived
  requests; (2) newly-admitted slots prefill token-by-token through the SAME
  `hawking_decode_step` primitive, interleaved with slots already many tokens deep;
  (3) each tick's active compacted set is re-pointed at the flat KV pool via the new
  `HawkingKvCache::set_regions` (allocated once for `pool_size` regions by
  `ModelWeights::hawking_kv_cache_pool`, never reallocated); (4) `Scheduler::
  apply_decode_tokens` samples/EOS-detects/advances + updates lane stats; (5)
  `Scheduler::release_slot` retires finished slots, FREEING their stable KV region for a
  LATER arrival to REUSE — all keyed by stable slot id so a slot keeps its own KV region
  as the set churns around it. **Proof, on real Metal (`#[ignore]`d,
  `runners::tests`):** `hawking_churn_reuses_freed_slots_and_matches_solo_serial` runs 6
  prompts through a `pool_size=2` pool with STAGGERED arrival + completion (hard
  `ChurnStats` evidence: 6 admissions, 6 releases, **4 region reuses**, max_concurrent=2,
  77 shared forward passes) and asserts EVERY prompt's churn output equals its SOLO
  serial `generate` output BYTE-FOR-BYTE — the region-reuse-under-churn no-corruption
  property, the one that makes continuous batching trustworthy for real money. The
  companion gate `hawking_churn_neartie_flip_is_membership_dependent_not_corruption`
  characterizes (does NOT hide) the single place byte-identity legitimately does not
  hold: a genuine argmax near-tie ("List the first three prime numbers." → "1. 2\n2.
  3\n3. 5" vs "2, 3, and 5") can flip ONE token depending on which exact slots are
  co-batched at that step (the documented `atol` reduction-order property) — proven
  benign by showing the same prompt matches solo under every controlled membership
  (pool=1, co-batch, staggered), stays coherent (all three primes present), and leaves
  every neighbor's output byte-identical. All four pre-existing determinism gates + the
  Week-2 kernel wired-decode gate + the Week-4 capstone still pass byte-for-byte in the
  SAME real-Metal process (0 regressions); build+clippy clean both configs at the
  4-warning baseline. **What Week 5 did NOT do (honestly remaining, now Week 6):**
  LIVE DISPATCH through `HawkingRunner::run`. Two real gaps remain: (a) the cross-worker
  DETERMINISM RE-GATE — the `hawking` lane is a distinct Apple verification class and
  needs its `(apple_silicon, hawking, build_hash)` honeypots + golden-hash baseline
  seeded before it carries byte-exact money work (a `candle`-seeded honeypot would
  byte-fail a correct `hawking` result); and (b) plumbing `run`'s input through the
  `ModelPool`'s concurrency-safe model handle (touches pool.rs/main.rs). PREFIX REUSE
  (`group_by_prefix` is ported; the `PrefixIndex` KV-copy path is not wired) is a
  throughput optimization, NOT a correctness gap — deferred with Week 6.
  `#[allow(dead_code)]` on `hawking_generate_churn` marks exactly that not-yet-dispatch
  -wired state honestly, the same convention `hawking_generate` uses.
- **Week 6a — LIVE DISPATCH WIRED (LANDED, 2026-07-06; CREED draft
  `docs/internal/creed-drafts/entry-hawking-dispatch.md`).** The gap Week 5 named as
  "(b) dispatch plumbing" is closed: `HawkingRunner::run` is REAL. It parses the same
  JSONL chunk `BatchInferRunner` parses, takes the same warm concurrency-safe
  `ModelPool::llama` handle, and drives the proven `LlamaBackend::hawking_generate_churn`
  under `spawn_blocking` + the model mutex with `arrival` = all zeros — a dispatched
  chunk's prompts all arrive at once, the DEGENERATE churn case the proven scheduler
  handles naturally (with prompts > pool_size, admission back-pressures and churns
  freed slots; no artificial staggering is invented). Output is the exact
  `BatchInferResult` JSON in input order with real per-row token counts. `main.rs`
  inserts the runner for `inference_backend = "hawking"` (Metal builds only), and the
  new `hawking_pool_size` knob (default 8, HARD-CLAMPED `1..=8` — B=16 stays
  explicitly unvalidated, so the clamp is a safety bound, not a suggestion) sizes the
  slot pool. **Honest scope:** `can_run` claims ONLY the wired-and-proven lane —
  `batch_infer` on the small GGUF family. `batch_classification`/`json_extraction`
  (no dispatch gate yet), the big 7B GGUF (unvalidated on this lane), and cluster
  models all fall through to their existing runners unchanged. Checkpoint boundary:
  `run_with_checkpoints` is deliberately NOT overridden — the trait default commits a
  byte-identical final result; a hawking task just emits no mid-task partial flushes
  and is not preemptible between slices (a continuous-batch run has no natural slice
  boundary; slot-level preemption is future work). Greedy-only is dispatch PARITY:
  the Candle `generate_batch` path it replaces is also greedy-only. **Proof, on real
  Metal (`#[ignore]`d, `runners::tests`):**
  `hawking_dispatch_end_to_end_matches_batchinfer_format_and_solo_serial` drives real
  JSONL bytes → `HawkingRunner::run` → real `ModelPool` → the real Llama-3.2-1B
  Q4_K_M GGUF at BOTH pool_size=2 (real back-pressure + region reuse under
  arrival=all-zeros) and pool_size=8, and asserts the result document is
  BYTE-IDENTICAL to `BatchInferRunner`'s for the same input, every completion equals
  its SOLO serial `generate` byte-for-byte (the argmax near-tie membership property
  remains the one documented exception, characterized by the Week-5 companion gate
  and excluded from this well-separated prompt set), and `tokens_used` is the real
  per-row sum. **Dispatch-level throughput was MEASURED (not modeled) on the M3 Pro,
  and the honest verdict is NEGATIVE: 88.3 tok/s (hawking, pool=8) vs 132.1 tok/s
  (candle per-task batched) on the mixed real-traffic bench shape = 0.67x — the
  wiring is correct and proven, but NO dispatch-level speedup exists today and none
  is claimed.** Full method + numbers + why (token-by-token prefill vs bulk prefill;
  the kernels were never the asset):
  `docs/batching-efficiency-reports/2026-07-06-m3pro-hawking-dispatch.md`.
- **Week 6b — prefix reuse + cross-worker determinism re-gate + soak (PARTIALLY
  LANDED 2026-07-06).** LANDED (wave 2B, CREED entry — hawking re-gate): the
  `(apple, hawking, build_hash)` class-aware honeypot is SEEDED (`control/seed.go`)
  from a real membership-stable blob produced on real Metal by the
  `hawking_honeypot_seed_blob_membership_stable_across_pool_sizes` harness
  (byte-identical at pool_size 1/2/4/8 — the harness rejected one genuinely
  membership-unstable prompt on its first run, proving the requirement is real),
  with the control-side pass/fraud/cross-class-skip matrix proven on real
  Postgres; the seed blob doubles as the class's golden record. Seeding flow +
  validity bounds: docs/DETERMINISM_CLASS.md. REMAINING: the `PrefixIndex`
  KV-copy path so a shared prefix is prefilled once + bulk prefill (the
  throughput levers the measured 0.67x dispatch number points at); B=8
  batched==serial parity (`atol=1e-3`); cross-Mac class-boundary test (needs a
  second physical Mac — owner-gated); sustained-load soak at B=8 and slot-churn
  stress at scale. B=16 and big models are explicitly OUT of this window
  (unvalidated).

## Do NOT pull in (per the audit)

Spec decode (net-negative on the tested Mac: EAGLE head 0.40x-0.21x, n-gram τ=1.43
sub-gate); the megakernel (a no-op skeleton in a Type-1-dead region, uses f16 weights =
2x the bandwidth of production Q4); the sub-Q4/condense codec (every requant-from-quantized
form is NO-GO). F16 KV cache is a SEPARATE Wave-3 non-byte-exact lane (88% argmax-identity),
not part of this port.
