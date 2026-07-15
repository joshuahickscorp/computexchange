# Inference ≥50× execution plan

## Outcome

Produce independently immutable, end-to-end Compute Exchange inference
receipts. The active delivery bar is **≥50×** for every reuse lane we choose to
advertise; **25×** is an intermediate evidence gate, not the finish line.
Never relabel a reuse result as fresh inference. Exact-request reuse, prefix
reuse, and fresh decode remain separate lanes.

## Completion definition for this program

The numerical goal has been met experimentally for exact-request reuse. The
program is complete only when that result has separate operational hardening
evidence, every advertised reuse lane has its own ≥50× p50/p95 receipt and
coverage envelope, and CX's route/policy layer labels the eligible cohort and
fallback honestly. Fresh non-reuse decode is not included in that reuse claim:
it needs its own direct milestones and may remain an open research track rather
than being hidden inside an aggregate multiplier.

| Lane | Near-term target | What may be claimed |
| --- | ---: | --- |
| Exact-request reuse | ≥50× measured; harden before route promotion | Only the declared eligible-request distribution and observed hit coverage |
| Shared-prefix reuse | ≥25× first proof → ≥50× delivery bar | Only workloads with the bound shared-prefix distribution |
| Fresh non-reuse decode | 2× → 5× → 10× → 25× → 50× research path; likely CUDA/model-specific above 10× | Same-work fresh inference only |

## Current evidence (2026-07-15)

The process-per-arm local control is retained at
`.artifacts/cx-inference/exact-cache-real-v1/qualifying-receipt.json`. Its
strict result was **7.01× p50** and **6.97× p95**. It demonstrated that the
cache itself was fast, but charged roughly 0.4–0.5 seconds of one-shot Python
dispatch to every candidate arm, so it was not evidence for either threshold.

The resident-gateway experiment replaced that artificial process lifecycle with
the same already-resident customer-facing local RPC path for both arms. Its raw
ABBA record is at
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-abba-receipt.json`;
the immutable strict receipt is at
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-qualifying-receipt.json`
with retained artifacts rooted at
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-abba-work`.
Strict revalidation passed artifact hashing, same logical work, exact
output-token parity, direct fallback, eight ABBA trials per arm, and 32/32
eligible exact-cache hits. It measured **230.20575× p50** and **207.90023×
p95** against the pinned target-only baseline.

This clears the 25× and 50× numerical gates only as
**experimental, local-unattested, warm-resident exact-request reuse**. Timing
starts before the resident RPC request is serialized and ends after response,
receipt capture, token validation, and pin revalidation. Worker startup and
cold-start behavior are explicitly outside this per-request experiment. It is
not a fresh-decode, shared-prefix, video, image, WAN-latency, billing, public,
or production claim; the global 1000× scorecard also remains a different
contract and must not be force-bound to this 50× receipt.

The recovered historical C1/K3 server replay is also retained as a failed
profile hypothesis at
`/Users/scammermike/Downloads/vllm-metal-cx/.artifacts/cx-inference/sonnet-c1-regression/historical-c1-k3-endpoint-recovered-server-profile-v1.json`.
Both arms produced token 702 at the historical signature position. This does
not repair, erase, or authorize speculative C1; the source-fork direct fallback
and separate parity gates remain mandatory. The most important remaining
fidelity gap is client protocol: the historical `vllm bench serve` path omitted
`logprobs` and streamed responses, while the diagnostic supplied `logprobs: 0`
and was non-streaming. On this Metal runtime that can switch native greedy
sampling to a different sampler. Profile A must replay the original client path
before any speculative route is promoted.

The retained raw-launch facts for Profile A are: the installed pre-fallback
wheel; `VLLM_ENABLE_V1_MULTIPROCESSING=0`,
`VLLM_METAL_USE_PAGED_ATTENTION=1`, and `VLLM_METAL_MEMORY_FRACTION=0.5`;
`--max-model-len 2048`, `--max-num-seqs 32`,
`--max-num-batched-tokens 2048`, `--no-enable-prefix-caching`, and
`--no-async-scheduling`. Each fresh server must be launched from a clean
directory with `PYTHONPATH` and `VLLM_METAL_BUILD_FROM_SOURCE` removed. The
baseline uses no speculative config; the n-gram arm uses K=3 / lookup 2–4.
Use the installed streaming `vllm bench serve` Sonnet client with 16 prompts,
two warmups, seed 0, 550/200/150 input-prefix-output lengths, `--ignore-eos`,
and deliberately no `--logprobs` flag. Retain server/bench logs, resolved
configuration, `/v1/models`, wheel/extension hashes, raw detailed benchmark
output, and offline-retokenized output hashes for two fresh ABBA repetitions.
`scripts/spec-lab/reproduce_historical_c1_vllm_bench_profile_a.py` now
implements that replay as an explicit-`--execute` correctness harness; its
preflight passed without starting a server at
`/Users/scammermike/Downloads/vllm-metal-cx/.artifacts/cx-inference/sonnet-c1-regression/profile-a-harness-preflight-20260715/preflight.json`.
That preflight is only a pin/fixture check, never an execution or speed result.

## Phase 1 — establish the non-negotiable comparison contract

1. Pin model revision, weights, tokenizer, runtime/core, Metal configuration,
   sampling, input token IDs, request order, concurrency, and output cap.
2. Define one charged customer-visible timing boundary: admission/routing,
   lookup, engine work, verification, serialization, and delivery.
3. Use target-only greedy execution for every baseline. Do not reuse a prior
   response in baseline or use a synthetic zero-work candidate.
4. Before priming or timing, create and pin an endpoint-attestation artifact:
   the prepared launch plan, rehashed engine/core/Metal trees, live process
   argv/start time, `/v1/models` response, and immutable startup-log snapshot
   must all bind to the target endpoint and served model. This is a local
   operator attestation, not an independent remote security proof.
5. Run balanced ABBA alternation and retain all arm receipts. Require exact
   output token IDs, same logical work, repeat stability, p50/p95, and an
   immutable workload/receipt binding.

**Exit:** the strict receipt adapter accepts the workload and arm manifests,
but no performance result has been claimed yet.

## Phase 2 — make vLLM-Metal a safe substrate

1. Keep the source fork’s C1 n-gram path target-only while a single request is
   active; C1 is an active-forward shape, not a configured sequence capacity.
2. Reproduce the retained historical C1/K3 issue through a server-level
   harness, or record the attempt as a failed historical-profile hypothesis.
3. Run process-isolated C1/C16 × K1/K3 exact-token parity gates on the source
   fork. C1 must show direct fallback; non-C1 cells must show actual draft
   activity before they are considered speculative experiments.
4. Treat any mismatch as a route quarantine. Never time or promote it.

**Exit:** source-bound parity receipts and explicit fallback telemetry exist;
no fresh speculative speed claim is authorized merely by parity.

## Phase 3 — obtain the first ≥50× result through exact-request reuse

1. Use `cx_inference_exact_cache_runner_v1.py` to prime a real
   OpenAI-compatible endpoint from a predeclared eligible workload, after
   `cx_vllm_endpoint_attestation_v1.py` has bound that endpoint to the
   prepared runtime.
2. Emit the pinned target-only baseline and identity-bound candidate manifests.
   The candidate may return only a response whose tenant, model, revision,
   tokenizer, runtime, sampling, input IDs, payload, and completion token IDs
   all match. The endpoint-attestation digest is pinned in both arm manifests
   and retained by the strict receipt. Every miss directly uses the target.
3. Execute the full ABBA trial block and validate with
   `cx_inference_receipt_v1.py`.
4. Bind a receipt into a matching-lane scorecard only if the direct multiplier
   meets that scorecard's target, every token matches, the required
   eligible-hit rate is met in observation, and the reuse scope is explicit.
   For an explicitly intermediate experiment, pass `--target-multiplier 25`
   to `screen_inference_lane_abba.py`; its default remains 50× and the
   selected target is self-hashed into the strict receipt.

**Exit (met experimentally):** one exact-request-reuse receipt passed **50×**
p50/p95 gates: `abba-exact-request-reuse-b3f51b47a412`. It is not yet a
fresh-decode or production/billing claim.

### Phase 3a — remove benchmark-process overhead with a resident CX gateway

1. Start one pinned resident CX worker before the ABBA block. Bind its source,
   worker configuration, cache artifact, endpoint attestation, process identity,
   and startup evidence into immutable files. Its startup cost is a service
   lifecycle cost, never silently excluded from a per-request claim.
2. Send both baseline and candidate through the same persistent customer-facing
   loopback/production-equivalent request protocol. Start each timer before the
   request is serialized and end it only after the complete exact-token response
   and every receipt stage has returned.
3. Baseline still calls the real pinned target for every request. Candidate may
   return only an identity-bound exact hit and must directly call the target on
   every miss. Keep full request validation, routing, lookup, verification,
   serialization, delivery, tenant scope, and cancellation/fallback behavior
   on the charged path.
4. Preserve ABBA, eight-or-more trials per arm, immutable captures, exact token
   parity, and observed coverage. The resident transport is a new measurement
   configuration, so it must produce a new receipt rather than being compared
   numerically to the process-per-arm receipt.

**Exit (met experimentally):** the direct resident-gateway receipt reached
both 50× p50 and p95 targets honestly. No process-start residual was subtracted
from the 7× control and no derived multiplier was constructed.

### Phase 3b — harden the qualified exact-reuse route before promotion

1. Independently measure cache-miss fallback, cold service startup,
   cancellation, invalid/expired identity, tenant isolation, and concurrent
   session behavior. Do not blend any of these distributions into the warm-hit
   receipt.
2. Bind worker process identity and startup-log evidence more strongly, then
   repeat the warm-hit experiment with 16–32 trials per arm to characterize
   tails without changing the claim scope.
3. Only after those controls and authorization are reviewed may the route be
   considered for customer selection, publication, production, or billing.

**Exit:** promotion evidence exists separately from the already-qualified
warm-hit performance evidence.

## Phase 4 — extend reuse honestly

1. Build a separately pinned long shared-prefix corpus with realistic prefix
   diversity and concurrency. Every measured request must have a genuinely new
   suffix/completion; an exact-response cache is forbidden in this lane.
2. Attest two otherwise-identical local endpoints with a shared-prefix-specific
   attester: the target-only baseline explicitly disables prefix caching, while
   the candidate enables only prefix caching. Both disable response reuse and
   speculative decode initially. The existing exact-cache attester is not
   sufficient because it correctly rejects enabled prefix caching.
3. Prime the candidate prefix as a separately retained prior-request/cache-
   history event. For every eligible candidate request, require an immutable
   `cx_prefix_cache_trace` bound to a per-call nonce, request digest, prefix
   digest, runtime digest, endpoint-attestation digest, tenant scope, cache
   instance/generation, engine-derived full-input digest, cached-token count,
   and trace artifact. It must prove
   `prefix_cache_hit: true`, `response_cache_hit: false`, and
   `speculative_decode_used: false`; an absent or miss trace falls back to
   direct target decode and cannot qualify as a hit. The candidate endpoint must
   expose this as a namespaced OpenAI-compatible response extension plus
   canonical immutable artifact bytes; retain a per-trial trace ledger. Validate
   this bridge with `cx_inference_prefix_trace_v1.py`; it is telemetry evidence
   only until the endpoint/runtime binding and ABBA receipt are also present.
4. Measure prefix reuse with its own target-only ABBA baseline, cache state,
   exact-token parity, and observed coverage.
5. Compare cache residency, prefix length, queueing, and tail latency rather
   than trying to multiply a prefix ratio by the exact-cache ratio.

**Exit:** independently qualified prefix lane(s), each with its own ≥50×
p50/p95 result and coverage envelope. A 25× result is useful for tuning but is
not the delivery exit.

## Phase 5 — lift fresh work and productize qualified routes

1. Build a separate fresh-only preparer and runner rather than reusing the
   exact-response-cache path: `prepare_vllm_fresh_decode_workload_v1.py` must
   emit two cache-free endpoint plans and a common base runtime identity;
   `cx_inference_fresh_decode_runner_v1.py` must pin both endpoint
   attestations, the candidate parity/activation preflight, endpoint-provided
   output token IDs, and the existing seven ABBA stage artifacts. Extend the
   endpoint attester narrowly for a target-only baseline and a candidate with
   immutable speculative configuration. Both arms keep response/prefix caches
   off, while their resolved-engine-config digests may differ.
2. For fresh inference, run target-only versus one candidate at a time:
   n-gram, draft model/MTP, batching, kernel work, or scheduling. Hold logical
   work and quality fixed.
3. Promote only after each direct 2×, 5×, 10×, then 25× milestone is repeated
   with exact parity; a genuine fresh 50× requires its own direct receipt. It
   is a research objective, not something to fabricate by combining reuse,
   batching, and speculative ratios.
   On the current M3 Ultra / small 4-bit Llama path, only a narrow 2.848×
   Candle diagnostic is positive; vLLM-Metal C16 is 1.25× and the apparent C1
   result is parity-quarantined. Treat 2× as the immediate fresh proof target,
   5–10× as engine research, and move 25–50× to a pinned CUDA-compatible
   model/draft architecture if direct Mac evidence does not support it.
4. Have CX own cost estimates, route eligibility, authorization, cache scope,
   fallback, observability, and receipt retention. vLLM remains the backend
   substrate, not the product policy.
5. Test tenant isolation, cache eviction, cancellation, authorization changes,
   overload behavior, and fallbacks before production/billing flags are turned
   on.

**Exit:** user-visible pricing and speed estimates are backed by the eligible
lane’s evidence rather than an aspirational multiplier.

## Rules that prevent a false 25× / 50×

- Do not multiply cache, speculative, batch, hardware, codec, or scheduling
  ratios.
- Do not score a cache result as fresh decode, video, image, or project work.
- Do not include priming in a candidate arm or omit it from the declared reuse
  model.
- Do not silently cross tenant or runtime identity boundaries.
- Do not report a median without the slowest trial and p95.
- Do not set customer, publication, production, billing, or authorization flags
  merely because an experimental receipt cleared 50×.
