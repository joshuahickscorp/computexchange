# Resident paged speculative engine

Status: implementation frontier. The host-side safety components are being built
behind an inert seam. This document is not a production-support claim.

## Objective

Replace request-owned model execution with one long-lived engine per exact model
build. The engine should continuously combine chunked prefill, ordinary decode,
and speculative verification while preserving per-request cancellation, deadlines,
sampling state, exact target verification, and economic isolation.

The target is not a source-compatible vLLM clone. It is a Rust engine whose
ownership and safety model fits Compute Exchange and whose Metal and CUDA backends
can be selected independently.

## Data path

```text
request ingress
    -> resident admission + epoch handle
    -> namespaced prefix-block lookup
    -> paged KV logical block table
    -> token-budget scheduler
         -> chunked prefill dispatch
         -> dense decode dispatch
         -> packed ragged speculative dispatch
    -> target-model result validation
    -> per-slot KV commit / rollback / COW
    -> sampled or exact token emission
    -> completion, cancellation, or release
```

The scheduler owns lifecycle state. The paged allocator owns logical-to-physical
KV mappings and refcounts. The speculative planner owns only a bounded transaction
for one concrete `(slot_id, admission_epoch)`. Device code receives immutable plans
and returns results tagged with those same epochs.

## Load-bearing invariants

1. A reusable slot id is never sufficient authority. Every mutation carries its
   admission epoch, and stale results are rejected before host or device KV writes.
2. Allocation, append, COW, rollback, cancellation, and release are atomic from the
   scheduler's point of view. OOM cannot leave leaked blocks or a partial table.
3. Prefix-cache identity includes model weights, tokenizer, quantization, RoPE and
   context settings, engine build, and tenant-sharing policy. Hash hits are confirmed
   against exact token blocks before reuse.
4. Shared prefix blocks are immutable. A sequence that changes a shared tail first
   receives a private copy-on-write block.
5. Speculation never changes externally visible tokens. Each row accepts only the
   longest target-verified prefix and emits the target bonus token. Ragged rows never
   inherit another row's minimum acceptance.
6. EOS, max-token limits, context ceilings, cancellation, and verifier-shape errors
   are validated before committing any row in a packed dispatch.
7. The scheduler never exceeds its token, sequence, KV-block, or deadline budgets.
   Aging prevents a sustained decode stream from starving queued prefill work.
8. A performance optimization that lacks a build- and hardware-bound gate stays off.

## Host components

The first implementation phase is intentionally independent of Candle tensors:

- `agent/src/paged_kv.rs`: physical block allocator, logical block tables,
  refcount/COW, transactional append/rollback, prefix cache, eviction, and
  device-facing mutation plans.
- `agent/src/resident_engine.rs`: admission, queues, token-budget scheduling,
  fairness, deadlines, cancellation/preemption, stale-result rejection, and
  telemetry through a mockable executor boundary.
- `agent/src/slot_speculation.rs`: independent per-slot proposal widths, packed
  span metadata, exact acceptance, bonus-token emission, and transactional outcomes.

These modules must pass randomized churn and failure-injection tests before a real
model bridge is allowed to consume their plans.

## Device bridge

The device bridge should be a two-phase operation:

1. The host prepares a bounded plan without exposing mutated state. The plan lists
   physical block allocations, COW copies, logical offsets, packed token spans,
   slot epochs, and the pre-operation version.
2. Metal or CUDA validates epochs, executes writes and model work, and reports a
   tagged result. Only then does the host commit its prepared state. On an execution
   error it aborts the plan and returns every reserved block.

The initial Metal bridge can map physical blocks into preallocated layer slabs.
The eventual kernels should consume block tables directly. F16 KV is the first
target; lower-precision KV needs an independent quality and throughput gate.

## Scheduling policy

Scheduling is token-budgeted rather than request-count-budgeted:

- Decode has a small predictable cost and normally receives latency priority.
- Prefill is chunked so a long prompt cannot monopolize a tick.
- Waiting age raises priority deterministically until starvation is impossible.
- Speculative rows declare `1 + K` target tokens and are admitted only when the
  packed budget and calibrated verifier economics allow it.
- Cancellation and memory-pressure checks occur between dispatches, never only at
  request boundaries.

Production exploration should use signed offline calibration keyed by hardware,
engine build, model/quantization, batch width, context band, and proposal width.
Buyer work should not pay repeated speculative probe regret when a class is already
known to lose.

## Prefix caching

Prefix reuse is a capacity and latency feature, not only a hash map:

- cache complete immutable blocks first; partial tails remain private;
- refcount cached blocks separately from active sequence pins;
- evict only unpinned least-recently-used entries;
- confirm exact tokens after a digest match;
- default to tenant-scoped reuse, with cross-tenant reuse requiring an explicit
  policy because timing and cache occupancy can leak prompt relationships;
- bind every entry to the full runtime namespace so incompatible RoPE, tokenizer,
  LoRA, model, or quantization state can never alias.

## Speculative execution

The current synchronized cohort commits the minimum acceptance across at most four
equal-length rows. The resident engine replaces that constraint with packed spans:

```text
slot A: pending + 4 proposals  offsets 0..5
slot B: pending + 1 proposal   offsets 5..7
slot C: pending only           offsets 7..8
```

Target results are interpreted row by row. Each row obtains its own accepted prefix,
bonus token, new logical KV length, and rollback boundary. A failed row or malformed
packed result aborts the whole prepared host transaction before any row is exposed.
Later device kernels may support partial dispatch retry, but the first bridge stays
all-or-nothing for simpler correctness.

## Promotion ladder

1. Host invariants: exhaustive acceptance patterns, randomized allocator/scheduler
   churn, OOM/error atomicity, ABA rejection, COW/refcount checks, and leak checks.
2. Shadow mode: build plans beside the existing engine without changing output or
   device state; compare dispatch/accounting traces.
3. Metal experimental mode: pinned M3/M4 build, exact greedy parity, real cancellation
   and churn, memory high-water reduction, TTFT/ITL/throughput gates.
4. CUDA comparison mode: identical manifest against the pinned vLLM service, including
   output hashes, batch policy, power/thermal state, and end-to-end timing.
5. Default promotion: multiple hardware classes and workload bands must clear lower
   confidence bounds; any uncalibrated class falls back to the established engine.

## Competitive measurements

Each lane records at least:

- time to first token, inter-token latency p50/p95/p99, and output-token throughput;
- prefill and decode tokens per dispatch, queue age, and cancellation latency;
- physical KV occupancy, logical tokens per block, prefix hit rate, COW copies,
  eviction count, OOM/retry count, and leaked-block invariant failures;
- speculative attempted/accepted tokens, verifier calls, rollback tokens, selector
  credit/regret, and exact-output match rate;
- end-to-end provisioning, queue, transfer, compute, and cleanup boundaries when
  comparing a marketplace lane.

No average may hide a failing batch size, context band, hardware class, or quality
gate.

## Upstream reuse boundary

Permissively licensed upstream code can be used when it is a coherent component and
its license, provenance, modifications, and tests remain attached. Algorithms worth
adapting include paged block tables, prefix-cache hashing, token-budget scheduling,
packed attention metadata, and fused kernel layouts. CUDA-specific Python scheduler
state should not be copied blindly into the Rust/Metal ownership model. Every imported
hotspot must beat the existing path under the same performance-proof manifest.
