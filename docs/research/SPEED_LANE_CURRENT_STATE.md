# Speed lane — our current measured state (grounding for the frontier grading)

> **SUPERSEDED 2026-07-06.** The "beat an A100 on wall-clock" thesis this baseline grounds was
> REFUTED by real measurement: a real A100-SXM4-80GB under vLLM serves 44,269 tok/s (~19× the
> Candle-bench A100 figure tabled below); honest break-even is ~318 M3-Pro-class nodes, not
> ~18. The salvaged, honest routing rule and current state of play:
> `docs/speed-lane-reports/A100_REFERENCE_MEASURED.md`, `A100_CAPABILITY_SWEEP.md`,
> `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md`. Kept unedited below for the receipt trail.

*Captured 2026-07-06 as the "where we are" baseline for grading computexchange's inference
speed against the researched frontier (see SPEED_LANE_RESEARCH.md) and the beyond-frontier
potential.*

## Measured single-node throughput (real, on-device)

| Platform / model | serial tok/s | batched (best) | batched (real traffic) |
|---|---|---|---|
| M3 Pro · Llama-3.2-1B Q4_K_M | 91–111 | **1.67× / 185 tok/s** @ batch 32 (identical prompts) | **1.34× / 139 tok/s** @ batch 32 (mixed lengths) |
| Rented A100 · 1B Q4 (spike) | 245 | **9.56× / 2345 tok/s** @ batch 64 | (not measured mixed) |

Batched==serial byte-identity is asserted per-row at every batch size (the trust invariant).

## Techniques we HAVE (landed + proven this session)
- Continuous-batching scheduler (Hawking port) + Metal multi-seq slot-strided KV kernel:
  model correctness + dynamic-admission/slot-churn PROVEN byte/token-exact on real Metal,
  and (2026-07-06 wave 1A) WIRED into live dispatch (`HawkingRunner::run` is real, opt-in
  via `inference_backend = "hawking"`, batch_infer only). **Measured at dispatch level:
  0.67x vs the Candle per-task batched path (88.3 vs 132.1 tok/s, M3 Pro, mixed 24-prompt
  regime)** — correct but NOT a throughput win today; the named levers are bulk prefill
  (the churn driver prefills token-by-token), PrefixIndex prefix reuse, and true
  continuous cross-task arrival. See
  docs/batching-efficiency-reports/2026-07-06-m3pro-hawking-dispatch.md.
- Near-length padded bucketing: byte-exact vs serial, landed (turns 1.0× real-traffic
  collapse toward the batched curve).
- Right-sized KV preallocation; memory-aware batch-width cap; warm model pool.
- Job-level distribution: chunk-based splitting across nodes, warm-routing bonus,
  dispatch-interleave fairness, per-node measured tok/s feeding claim ordering.

## Techniques we DON'T have (the frontier gap — fill from the research)
- Speculative decoding (draft/Medusa/EAGLE) — the single biggest quality-NEUTRAL latency
  win; directly on-thesis ("speed not quality"). Not started.
- FlashAttention-2/3 / FlashDecoding kernels. Not started.
- PagedAttention. Not started (our KV is slot-strided but not paged).
- CUDA graphs / torch.compile / FP8 / Marlin-class speed kernels. CUDA lane is a vLLM
  seam (NotImplemented until the soak runs).
- Disaggregated prefill/decode. Not started.
- SPEED-OPTIMAL distributed sharding across the heterogeneous fleet (node-speed-weighted
  shard sizing, straggler mitigation, optimal-N math) — the structural moat, unbuilt.
- Distributed speculative decoding across nodes — the exotic beyond-frontier move.

## The thesis (from the owner, 2026-07-06)
Win on WALL-CLOCK (save the buyer TIME), not on cost (quantization only saves money).
The structural edge no single-GPU vendor has: an embarrassingly-parallel batch of
thousands of prompts split across N heterogeneous machines run in parallel can beat one
A100/H100 on total wall-clock — IF the splitting/scheduling is speed-optimal. Grade us
vs the frontier, then vs the beyond-frontier potential, then hand a goal-prompt to the
next iterative build loop.
