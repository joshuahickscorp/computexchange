<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# vLLM CUDA serving lane

> The plan-of-record for wiring vLLM as the `nvidia_*` serving lane, behind the
> verification contract. Source recommendation: `docs/PERF_AND_CAPABILITY_AUDIT.md`
> Wave 2 ("Wire vLLM as the CUDA serving lane"). The seam lands in this change; the
> wired throughput is gated behind the determinism soak below and MUST NOT ship before
> it passes.

## Why this lane

The Candle CUDA decode path leaves tensor cores idle. The fused SDPA fast path is gated
to `is_metal() && seq_len == 1` (`agent/src/quantized_llama_batched.rs`), so on CUDA the
decode falls through to a manual `q.matmul(k.t()) + softmax + matmul` over a
dequant-to-f32 `QMatMul`. vLLM's paged-attention + FP16/BF16 tensor-core GEMMs replace
that with the kernels the hardware was built for.

Realistic win at the cheapest sampling computexchange runs (greedy / temp=0,
chunk-bounded concurrency): **~3-6x batch_infer per GPU**, NOT vLLM's headline 5-15x
serving numbers. computexchange already recovers some prefill batching via
`generate_batch`, so the marginal gain is the decode-side tensor-core GEMM +
paged-attention, not the whole serving stack. The dominant cost of building this lane is
NOT the FFI/shell-out (~1-2 wk) — it is the cross-worker determinism harness (the
load-bearing remainder), because byte-exact verification on the `nvidia_*` lane requires
two vLLM workers to agree to the byte.

## What the seam is (this change)

- `config::InferenceBackend::Vllm` (engine tag `vllm`). An operator opts in with
  `inference_backend = "vllm"` in `agent.toml`.
- `runners::VllmRunner`, inserted FIRST (right after `ClusterRunner`, `agent/src/main.rs`)
  so generative LLM jobs route to it ahead of the Candle runners, exactly like the MLX
  seam. It claims `batch_infer`, `batch_classification`, `json_extraction`, and `rerank`;
  it yields cluster models to `ClusterRunner` (the Plane B boundary) and leaves embed
  (MiniLM) and audio_transcribe (whisper) on Candle.
- The runner is **inert by default and honest**. With the default Candle backend it is
  not inserted at all, so dispatch is byte-for-byte unchanged. When selected, `run`:
  1. reads `CX_VLLM_BASE_URL`; if unset it returns a typed `NotImplemented` boundary
     ("vLLM lane not configured", naming the env var) — NEVER a fabricated result,
     exactly like the MLX stub;
  2. even when the env var IS set, it STILL returns the boundary, because the verified
     shell-out path is gated behind the determinism soak below. Enabling it before the
     soak would put unverified bytes on the redundancy market.

The advertised `engine = "vllm"` flows to the control plane and becomes the second axis
of the worker's verification class (`docs/DETERMINISM_CLASS.md`), so vLLM output is ONLY
ever byte-compared with other vLLM workers.

## The pinning contract (engine + dtype)

Byte-equality on the `nvidia_*` lane is achievable ONLY with a fully pinned server. The
wired path will shell out (through the same locked-down `sandbox::run_sandboxed` egress
the `custom` lane uses, so the only network destination is the pinned server) to a vLLM
OpenAI-compatible endpoint configured as:

- **vLLM version pinned** (exact wheel/commit). A vLLM minor bump can change kernels.
- **dtype pinned** (e.g. `--dtype float16`, not `auto`). Mixed/`auto` dtype is a class break.
- **greedy / temp=0**: request body `temperature=0`, `top_p=1`, `seed` fixed, `n=1`,
  no `frequency_penalty`/`presence_penalty`. This matches the Candle greedy contract.
- **model + quant pinned** to the catalogue id (the same `model_ref` the manifest carries).
- **tensor-parallel / attention-backend pinned** (e.g. a fixed `--tensor-parallel-size`
  and a fixed attention backend), since paged-attention block size and TP reduction
  order both affect the last bit.

The pinned tuple `(vllm_version, dtype, model, quant, tp, attn_backend)` is the vLLM
analogue of Hawking's `shader_hash`. It folds into the agent's `build_hash`
(`hardware::engine_build_hash`) so two vLLM workers on DIFFERENT pinned servers land in
DIFFERENT verification classes automatically and are never byte-compared.

## The within-`nvidia_*` byte-equality contract

`nvidia_*` is a DISTINCT hardware family from Apple (`control/types.go validHWClasses`),
never cross-compared with the Apple lane (their FP kernels differ). So the contract this
lane must satisfy is narrow and achievable: **vLLM-vs-vLLM byte-equality within one
pinned `(hw_class, engine=vllm, build_hash)` class.** It is NOT cross-engine
(vLLM-vs-Candle) and NOT cross-Mac. The verification machinery already enforces the class
gate: a cross-class byte difference is `pass_with_penalty` + a `redundancy_cross_class`
receipt, never an auto-dock (`docs/DETERMINISM_CLASS.md`, `control/verification.go`).

Per-job-type within-class contract:

- `batch_infer` — byte-equality of the serialized `BatchInferResult`. The HARD case:
  two pinned vLLM workers must emit byte-identical greedy completions.
- `batch_classification` — top-1 label agreement (tolerant; survives FP jitter).
- `json_extraction` — canonical-JSON agreement (tolerant).
- `rerank` — exact order-array agreement (tolerant of score jitter, intolerant of an
  order flip; pin so the greedy scoring order is stable).

## The REQUIRED de-risk spike (BEFORE any throughput is promised)

This is the gate. The seam's `run` deliberately keeps returning the boundary until these
pass. Order matches the audit's de-risking order (#1).

1. **Stand up two pinned-vLLM `nvidia_*` workers** with the identical pinned tuple above.
2. **Cross-SKU byte-stability soak.** Run the `batch_infer` honeypot/redundancy corpus on
   both and prove **byte-identical greedy output across A100/H100 SKUs**. If A100 vs H100
   are NOT byte-identical under the pin, they are DIFFERENT classes — encode the SKU into
   `build_hash` and only ever pair same-SKU peers. Record which holds; do not assume.
3. **Restart byte-stability soak.** Prove byte-identical output across server restarts and
   across `--tensor-parallel-size` reschedules. A non-deterministic reduction order here
   is a class break.
4. **hw_class-aware honeypot seeding (audit de-risk #1).** The honeypot is hw_class-blind
   today: seed honeypots are still class-blind (`""`) so a byte-exact honeypot does NOT
   auto-quarantine yet (the safe default — see `docs/DETERMINISM_CLASS.md` "documented
   for follow-up"). Before go-live, seed `batch_infer` honeypot answers WITH their
   producing `(nvidia_*, vllm, build_hash)` class into `honeypots.answer_class`, recorded
   from a pinned vLLM reference box — NOT from a Candle worker. A Candle-seeded answer
   would byte-fail a correct vLLM result and auto-quarantine an honest worker.
5. **Seed the golden baseline** for the vLLM class: run the golden-hash harness with
   `CX_GOLDEN_RECORD=1` on the pinned vLLM reference box and pin the recorded rows, so a
   future vLLM kernel drift is caught on the build that changed, not as a false
   cross-worker dock.

Only after 1-5 pass does the wired shell-out body in `VllmRunner::run` get connected and
the lane carry verified work.

## Wired-path sketch (documented-only, not yet connected)

When the soak gates it on, `VllmRunner::run` will:

1. parse the job input (`parse_jsonl`), build a greedy/temp=0 OpenAI request body keyed
   on the manifest's `model_ref` and job type;
2. shell out via `sandbox::run_sandboxed` to `CX_VLLM_BASE_URL` (or a thin pinned HTTP
   client through the same egress policy), capturing the OpenAI `choices`;
3. map `choices` back into the existing result contracts (`BatchInferResult` /
   `ClassificationResult` / `ExtractionResult` / `RerankResult`) so verification is
   UNCHANGED — the control plane sees the same bytes-or-semantics it sees from Candle.

The result-contract mapping is the only code that must be byte-careful; everything else
is request plumbing.

## Person-weeks

~3-5 total: runner seam + OpenAI shell-out + result mapping ~1-2 wk; the load-bearing
remainder is the cross-worker determinism harness + the soak above. The seam (this
change) is the cheap part; the soak is the expensive, gating part.
