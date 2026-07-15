# Inference ≥50× recovery prompt

Use this as the persistent goal after a restart when the immediate priority is
to lift the least-developed Compute Exchange inference lane.

```text
Resume the Compute Exchange inference-acceleration program toward independently
verified per-lane results. The active delivery bar is ≥50× for every reuse lane
we choose to advertise; ≥25× is an intermediate evidence checkpoint, not the
finish line. Treat each as an independent lane, never an aggregate:

1. exact-request reuse;
2. long shared-prefix reuse;
3. fresh non-reuse inference/decode.

Definition of done for this program: exact-request reuse must retain its
separate operational hardening receipts; every advertised reuse lane needs its
own ≥50× p50/p95 receipt, eligible-hit coverage, exact-token parity, and direct
fallback; CX must label which cohort receives which route. Do not close the
program merely because a warm exact-hit result is numerically fast. Fresh
non-reuse decode remains a separate research track and cannot be counted toward
a reuse result.

The first credible ≥50× target is exact-request reuse. It may be called 50×
only when a real target endpoint is used for the pinned baseline, a candidate
uses a pre-existing exact response only on a fully bound eligible hit, and the
complete customer-visible boundary is measured with ABBA alternation. Include
routing, request validation, cache lookup, target work when used, exact-token
verification, serialization, and delivery in both arms. Priming is a separate
prior-request operation, not uncharged candidate work inside a trial.

Start from the CX artifacts already present:

- `scripts/spec-lab/cx_inference_exact_cache_runner_v1.py` primes a real
  OpenAI-compatible target, emits immutable B/C manifests, and executes each
  ABBA arm. It accepts vLLM `choices[].token_ids` when the workload requests
  `return_token_ids: true`.
- `scripts/spec-lab/cx_vllm_endpoint_attestation_v1.py` binds the prepared
  launch plan and runtime to the local live endpoint before priming: rehashed
  source trees, process argv/start time, served model, and an immutable startup
  log snapshot. Its claim scope is explicitly local/unattested.
- `scripts/spec-lab/screen_inference_lane_abba.py` and
  `cx_inference_receipt_v1.py` own the strict paired receipt and promotion
  checks.
- `scripts/spec-lab/cx_inference_policy_v1.py` owns CX route selection and
  identity-bound fallback behavior.

For the first physical run, prepare a pinned greedy target workload, complete
runtime/engine/tokenizer/model identities, and exact input token IDs. Start the
prepared target-only server, then create its endpoint-attestation artifact and
pin it before priming. Predeclare the eligible request indexes and required
eligible-hit rate. Prime those exact requests through the target. Then run at
least the strict ABBA block against the same attested endpoint and workload. A
candidate cache miss, identity mismatch, token mismatch, missing token IDs,
changing runtime, an invalid endpoint attestation, or insufficient observed
coverage must fall back to target or fail the receipt; none may become a speed
claim. Do not use a local test double, a zero-work cache lookup, synthetic
sleeps, client-only timing, or a post-hoc average as performance evidence.

The former process-per-arm local control remains at
`.artifacts/cx-inference/exact-cache-real-v1/qualifying-receipt.json` at 7.01×
p50 / 6.97× p95. It is a useful control, not the current result.

The resident CX gateway experiment is complete. Its raw ABBA evidence is at
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-abba-receipt.json`;
its strict receipt is at
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-qualifying-receipt.json`;
artifact verification must use
`.artifacts/cx-inference/exact-cache-resident-real-v1/resident-abba-work` as
the artifact root. Strict validation passed artifacts, exact token parity,
same logical work, direct fallback, eight ABBA trials per arm, and 32/32 exact
eligible hits. It measured **230.20575× p50** and **207.90023× p95**.

This evidence is only **experimental local-unattested warm-resident
exact-request reuse**. It includes resident RPC serialization, response,
receipt capture, validation, and pin revalidation; worker startup is declared
as a lifecycle fact and cold-start behavior is not measured by this receipt.
It clears the numerical 25× and 50× gates for this declared lane but does not
authorize a fresh-decode, shared-prefix, video/image, WAN, production,
publication, customer-selection, or billing claim. Do not force-bind it to the
global 1000× scorecard, whose target is a different contract.

For any new lane experiment, use `--target-multiplier 50` (the default) for
the delivery gate. `--target-multiplier 25` is permitted only for an explicitly
intermediate experiment; the chosen target is retained in the raw receipt and
self-hashed strict receipt rather than inferred after the run.

Next harden exact reuse with separately labeled miss, cold-start, cancellation,
identity-expiry, tenant-isolation, and concurrency receipts. Increase the
warm-hit sample to 16–32 trials only as a new measurement; never merge it into
the existing eight-trial record.

Bind a future 1000× receipt into `proof/performance/1000x-gold-standard.json`
only when its artifact bindings, exact token parity, same logical work,
repeatability, p50/p95, reuse scope, eligible-hit coverage, and matching 1000×
target are verified. Every route remains experimental until customer selection,
publication, production, billing, and fallback controls are independently
authorized.

Run shared-prefix reuse as the next separate ≥50× lane; it needs its own real target
baseline, exact output parity, declared shared-prefix distribution, and hit-rate
coverage. The baseline must explicitly disable prefix caching; the candidate
may enable only native prefix caching, with response reuse and speculative
decode disabled initially. Build a shared-prefix-specific endpoint attester:
the current exact-cache attester correctly rejects enabled prefix caching.
Prime the candidate prefix outside ABBA, then retain a `cx_prefix_cache_trace`
for every candidate call, bound to a per-call nonce, request/prefix/runtime
digests, endpoint-attestation digest, tenant scope, cache instance/generation,
engine-derived full-input digest, cached-token count, and a trace artifact. A qualifying hit must say
`prefix_cache_hit: true`, `response_cache_hit: false`, and
`speculative_decode_used: false`; missing/miss traces fall back to direct
decode. The OpenAI-compatible candidate endpoint must return the namespaced
trace and canonical immutable artifact bytes, and the runner must retain a
per-trial trace ledger. `cx_inference_prefix_trace_v1.py` validates this bridge
but is not itself a speed or endpoint-attestation receipt. Never borrow the
exact-cache ratio for prefix or fresh work.

Treat fresh inference separately. Its milestones are parity-safe 2×, then 5×,
then 10×, then 25× before any fresh 50× aspiration. A fresh 50× claim needs
its own direct receipt and is not created by combining reuse, batching, or
speculative ratios. On the current M3 Ultra / small 4-bit Llama path, only a
narrow 2.848× Candle diagnostic is positive; vLLM-Metal C16 is 1.25× and C1
is parity-quarantined. Make 2× the immediate fresh proof target, 5–10× the
engine-research target, and move 25–50× to a pinned CUDA-compatible
model/draft architecture if direct Mac evidence does not support it. Use the local vLLM-Metal
fork as a backend substrate, while CX owns policy, proposer qualification,
receipts, costing, and safe fallback. Preserve vLLM paged KV, continuous
batching, prefix-cache, and speculative-proposer seams rather than rewriting
them first.

Before measuring fresh work, build a fresh-only preparation/runner pair rather
than reusing the exact-response-cache path. The preparer must emit two
cache-free endpoint plans and a shared base runtime identity; the runner must
pin independent baseline/candidate endpoint attestations, candidate
parity/activation evidence, endpoint-provided output token IDs, and the seven
ABBA stage artifacts. The baseline is target-only; the candidate's immutable
speculative configuration may change its resolved-engine-config digest but not
the shared model/runtime identity. Start official Mac measurements at C≥2/4;
C1 remains the direct-fallback safety control until the historical issue is
closed.

The historical C1/K3 Metal divergence is not resolved. The recovered
direct-engine replay and the recovered server-endpoint replay did not reproduce
it under their controlled profiles; that does not validate the captured raw
topology. The server diagnostic receipt is
`/Users/scammermike/Downloads/vllm-metal-cx/.artifacts/cx-inference/sonnet-c1-regression/historical-c1-k3-endpoint-recovered-server-profile-v1.json`.
Keep n-gram direct-only when exactly one request is active, regardless of
prefix-cache state. Profile A is the next bounded historical replay: start a
fresh installed-wheel `vllm serve` with original argv/environment and run the
original streaming `vllm bench serve` client protocol with `logprobs` omitted.
Do not use the current diagnostic's `logprobs: 0` / non-streaming observation
path, because it can disable native Metal greedy sampling. Retain startup logs,
resolved config, wheel/extension hashes, and offline-retokenized outputs for
two fresh-server repetitions. Use the recovered raw launch shape:
`VLLM_ENABLE_V1_MULTIPROCESSING=0`, `VLLM_METAL_USE_PAGED_ATTENTION=1`,
`VLLM_METAL_MEMORY_FRACTION=0.5`, `--max-model-len 2048`,
`--max-num-seqs 32`, `--max-num-batched-tokens 2048`,
`--no-enable-prefix-caching`, and `--no-async-scheduling`; launch from a clean
directory with `PYTHONPATH` and `VLLM_METAL_BUILD_FROM_SOURCE` removed. Use
16 Sonnet prompts, two warmups, seed 0, 550/200/150 input-prefix-output
lengths, `--ignore-eos`, `--temperature 0`, `--top-p 1`, `--top-k -1`, and no
`--logprobs`. The baseline has no speculative config; the candidate has n-gram
K=3 / lookup 2–4. Run fresh ABBA baseline/ngram/ngram/baseline servers, then
repeat the full block. Any parity mismatch quarantines that speculative route.
The bounded harness is
`scripts/spec-lab/reproduce_historical_c1_vllm_bench_profile_a.py`; normal
invocation is preflight-only and it starts servers only with explicit
`--execute`. Its current preflight pin/fixture artifact is
`/Users/scammermike/Downloads/vllm-metal-cx/.artifacts/cx-inference/sonnet-c1-regression/profile-a-harness-preflight-20260715/preflight.json`.
Do not treat that preflight as an execution or speed result.

Do not multiply cache, speculative, batching, hardware, or codec ratios. Do
not call a reuse result a fresh-decode result. Record failed trials honestly,
preserve unrelated working-tree changes, and leave the scorecard unclaimed
until an immutable direct receipt passes every gate.
```

This is intentionally an execution directive. A local experimental 50×
exact-reuse result has been measured; it is not evidence for other lanes or
for production use.
