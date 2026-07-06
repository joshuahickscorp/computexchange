# Candle / Inference-Engine Expansion Research

> **Implementation pass 1 — DONE (branch feat/perf-wave, uncommitted; agent build green, cargo test 89/0/13-ignored).**
> - **L17 build_hash hardening (PROVEN).** `hardware::engine_build_hash` folds
>   `infer_content_id()` (SHA-256 of the vendored `quantized_llama_batched.rs` source), so any
>   output-changing kernel/forward edit moves the verification class automatically, even without
>   an `agent_version` bump. New unit test pins the sensitivity.
> - **L2 Qwen RoPE — implemented, with a correction to this doc.** The bottom line below says
>   "rope_i is the only bug." Verified deeper: CX fetches the OFFICIAL Qwen GGUFs
>   (`Qwen/Qwen2.5-*-GGUF`), which are **qwen2-arch** (`qwen2.*` metadata keys + q/k/v biases).
>   The old llama-only `from_gguf` could not load them, so a rope-only fix was
>   necessary-but-insufficient. Implemented the full architecture-aware load:
>   `general.architecture`-prefixed metadata keys (P-arch), NEOX-vs-interleaved rotary
>   dispatch (P-rope), optional q/k/v bias (P-qkvbias). **Llama is byte-identical**; **Qwen
>   output parity is UNPROVEN** until a real-GGUF Metal run (load a Qwen2.5 GGUF, diff greedy
>   tokens vs llama.cpp).
> - **Reseed required:** editing the inference module changed every Candle worker's `build_hash`,
>   so golden hashes + honeypots must be re-recorded for the new `(device, candle, build_hash)`
>   class. Llama bytes are unchanged, so the reseed is mechanical.

> **Activation pass — DONE on an Apple M3 Pro (Metal), 2026-06-30. Full proof in docs/BUILD_STATUS.md.**
> - Golden-hash gate SEEDED + PROVEN for class `candle|29788fb25f948522` (record, then no-record gate, both green).
> - Qwen2.5-0.5B proven COHERENT through the patched path ("...Paris", "...Jupiter") and cross-checked vs
>   llama.cpp ("Paris") on the same GGUF (content match, not byte-parity, which is cross-engine-impossible).
> - Activation surfaced + fixed a 3rd Qwen gap: the GGUF omits `qwen2.rope.dimension_count`, so `from_gguf`
>   now falls back to `head_dim` (Llama unaffected). This moved build_hash to `29788fb25f948522`.
> - All Wave-1 ignored perf tests pass on Metal with `--test-threads=1` (parallel runs share the one global
>   Metal device and interfere; production serializes per-model via the pool mutex). Throughput ~2.3x in debug.
> - Class-aware honeypots remain NOT seeded (mechanism built; seeded answer is a placeholder) - exact
>   operational reseed procedure is in docs/BUILD_STATUS.md.

**Bottom line.** Stay on the vendored-module Candle strategy for the single-stream lane; it is correct, cheap, and the patch surface CX actually owns is *smaller* than the ledger implies (about 4 narrow deltas, not 5, and one listed delta — the rectangular causal mask — is upstream, not ours). But the aggressive mandate has two genuinely high-value, repo-grounded next moves that do not require forking Candle: (1) a **correctness fix** — CX serves both Qwen2.5 catalogue models with the WRONG (interleaved) RoPE convention today because `quantized_llama_batched.rs:342` calls `rope_i` unconditionally, which upstream PR #3411 fixes for NEOX-style models in 0.11.0; and (2) the **Hawking continuous-batch Apple lane**, whose 5.0x-at-B=8 win is reachable only through a new Metal multi-seq slot-strided KV decode kernel that lives *beside* Candle in a **cx-infer layer that already ~70% exists** in `runners.rs` + `quantized_llama_batched.rs`. The fork question flips from "vendored module" to "single-crate `[patch.crates-io]`" only when CX must edit a `candle-core`/`candle-nn`/kernel op that cannot be a vendored model module AND that edit is not already delivered by the vLLM (CUDA) or Hawking (Apple) lane AND it clears a measured >=1.3x on a named job type at temp=0; no such lever is in flight. Before going faster, the literal next actions are: **commit the perf-wave working tree, seed the golden-hash gate, and run the 4 GPU-ignored parity tests on this M3 Pro** — the differentiating work is currently uncommitted and its headline speedups are GPU-unmeasured.

---

## 1. Fork decision

### The four options, scored against the repo

**Option 1 — Vendored-module patch (status quo).** One file: `agent/src/quantized_llama_batched.rs` (1127 LOC, test mod at line 808), wired at `main.rs:16` (`mod quantized_llama_batched`) and `runners.rs:11` (`use ... as QLlama`). Sync = re-apply our deltas onto one upstream module per Candle bump (`CANDLE_FORK.md:48-66`). Build risk ~0 (no Cargo surgery, no lockfile churn). Determinism risk ~0 (P1/P4/P5 byte-safe and pinned). Upside: zero new capability, but it is the safe home for every model-module-expressible lever.

**Option 2 — `[patch.crates-io]` single-crate local fork.** Touches `agent/Cargo.toml:37-39` plus a vendored copy of ONE candle crate (e.g. candle-nn) and its Metal/CUDA build glue (`Cargo.lock` shows candle-kernels, candle-metal-kernels, candle-ug, cudarc as transitive deps). Sync = keep that crate green offline on both `--features metal` and `--no-default-features`. Build risk HIGH (the doc's stated rejection reason, `CANDLE_FORK.md:11-14`). Upside only if CX must edit a tensor op or kernel that a model module cannot reach.

**Option 3 — Network fork `github.com/computexchange/candle`.** Same surface as option 2 plus a git source line, an upstream-merge cadence, and CI/supply-chain surface. NO upside over option 2 for a private agent the founder never distributes. **Reject outright.**

**Option 4 — cx-infer layer on Candle primitives.** A cx-owned serve loop + batching + KV + sampling that calls Candle only for the per-step kernel (`ModelWeights::forward`, `Tensor`, `argmax`, `QMatMul`). **This already substantially exists, unnamed:** `runners.rs:1059-1171` owns the decode loop (length-bucketing, shared `index_pos`, argmax sampling at :1141-1145, `compact_kv_cache` at :1166); `runners.rs:1212-1314` owns prefix-fork via snapshot/restore; `quantized_llama_batched.rs:67-175` owns the KV slot (`KvCacheSlot`). What is NOT yet cx-owned is the attention/matmul *inside* `forward_attn` (`quantized_llama_batched.rs:345-420`: `sdpa`, `repeat_kv`, `softmax_last_dim`, `QMatMul`), which still come from candle-nn. So option 4 is a *naming + module-boundary* refactor of existing code, not a greenfield build.

### Decision

**Keep option 1 for the single-stream Candle lane indefinitely. Do NOT adopt option 2 or 3 today. Formalize option 4 as the named home for the Hawking continuous-batch lane** (its second kernel backend), because that lane needs a Metal multi-seq slot-strided KV decode kernel that Candle does not expose and that is NOT a candle-core edit — it attaches *through* Candle's public `CustomOp3` + `MetalDevice` surface (candle-core `custom_op.rs` metal_fwd hook; `metal_backend/device.rs` compile/command_encoder; `MetalStorage::buffer()`), so it needs no `[patch.crates-io]`.

### The precise THRESHOLD that flips it

Flip from option 1 to a **single-crate `[patch.crates-io]`** (option 2) the first time ALL THREE hold:

1. CX needs to change byte-output behavior **inside** `candle-core` / `candle-nn` / a kernel crate (not a transformers model module) — concretely a fused **CUDA SDPA / flash-attention** path (candle-nn 0.10.2 `Sdpa` has cpu_fwd that bails + metal_fwd only, NO `cuda_fwd`, verified in 0.10.2 / 0.11.0 / main; the default `CustomOp3::cuda_fwd` returns `Err("no cuda implementation for metal-sdpa")`), OR a quantized matmul / KV codec edit (Q5/Q6, int4-KV) that the public `scatter_set`/`slice_set`/`index_select` API genuinely cannot express; AND
2. that win is NOT already delivered by the vLLM CUDA lane or the Hawking Apple lane (which sidestep candle-internal edits entirely); AND
3. a microbench shows the candle-internal edit clears **>=1.3x on a named job type at greedy/temp=0**, and `cargo build` (metal) + `cargo build --no-default-features` stay green with the single vendored crate.

A **network fork** (option 3) is justified only if CX maintains kernel deltas across BOTH `candle-core` AND the kernel crates simultaneously AND needs weekly upstream tracking — none of which is plausible.

A **from-scratch cx-infer engine** is negative-value now: Hawking's own kernels are ~0.62x llama.cpp at batch-1 decode (hawking `docs/dead_levers.md:100`), so a from-zero kernel layer loses on raw speed. The differentiator is the *scheduler + determinism discipline*, not the kernels.

### Does `CANDLE_FORK.md`'s anti-fork stance still hold?

**Yes — for the SYNC and for model-module levers it holds and survives the aggressive mandate.** Nothing in candle 0.11.0 trips the escalation. But the doc must change in three ways (see §8): (a) its lever #2 ("route to `candle_nn::ops::sdpa` on CUDA") describes an operation that *errors at runtime* — there is no CUDA SDPA to ungate, so lever #2 must be reclassified as a kernel-vendor escalation and redirected to the vLLM lane; (b) the doc never states the quantitative flip threshold above; (c) the patch ledger overstates CX authorship (P3 rectangular mask is upstream; P2 is bounding-only).

---

## 2. Upstream Candle 0.10.2 -> latest

### Version truth (primary sources)

- **Latest stable: candle-core 0.11.0**, published **2026-06-26T21:14:33Z** (https://crates.io/api/v1/crates/candle-core). Previous: 0.10.2, published 2026-04-01.
- CX pins **0.10.2** across candle-core/nn/transformers (`agent/Cargo.lock:257,311,329`; `agent/Cargo.toml:37-39` spec `"0.10"`, which will NOT semver-auto-bump to 0.11).
- Tag SHAs: 0.11.0 = `31f35b147389700ed2a178ee66a91c3cc25cc80d`, 0.10.2 = `7c7a8c570e6a16ea24cc30a7501c8ffbbdb51680` (https://api.github.com/repos/huggingface/candle/tags).
- Diff 0.10.2...0.11.0 = **67 commits / 198 files** (https://api.github.com/repos/huggingface/candle/compare/0.10.2...0.11.0). NOTE: candle ships no GitHub Releases and `CHANGELOG.md` is stale; version truth is crates.io + tags + compare API.

### What changed in the modules CX touches

| Area | 0.10.2 -> 0.11.0 | Relevance to CX |
|---|---|---|
| **rope / quantized_llama** | **PR #3411** adds `rope_is_neox` field: reads `general.architecture` from GGUF and dispatches `rope()` (NEOX half-split) for qwen/qwen2/qwen2moe/qwen3/falcon/phi/stablelm/starcoder2/olmo2, keeping `rope_i()` (interleaved) only for standard Llama. (https://github.com/huggingface/candle/pull/3411) | **DECISIVE correctness fix.** See §3 rope lever. CX's vendored `:342` calls `rope_i` unconditionally -> both Qwen models are wrong today. |
| **kv_cache** | candle-nn `KvCache`/`Cache` already shipped prealloc `slice_set` append + dynamic grow at 0.10.2; 0.11.0 adds nothing CX needs. `ScatteredKvCache`/`ScatteredCacheBuilder` exist at 0.10.2 (multi-slot, per-row ring positions, built on stock `scatter_set`). | CX's P5 `KvCacheSlot` re-implements the single-seq prealloc candle-nn already had; upstream's quantized_llama model still uses `Tensor::cat`, so a sync does NOT let CX drop P5. |
| **sdpa** | Identical signature across 0.10.2 / 0.11.0 / main: `sdpa(q,k,v, mask: Option<&Tensor>, do_causal: bool, scale, softcapping)`. Metal-only (cpu_fwd bails, **NO cuda_fwd in any version**). 0.11.0 did NOT add CUDA SDPA. | The masked full-attention Metal prefill kernel (`supports_sdpa_full` for q_seq>8) already exists at 0.10.2 and CX gates it off. CUDA SDPA remains upstream-absent. |
| **CUDA quantized kernels** | **PR #3463 (MMVQ)** + **#3465 (MMQ)**: native BF16 in/out, no F32 round-trip, on-the-fly Q8_1 activation quant, batch 1-8 compile-time specialization. #3465 reports +30% **prefill** T/s (Gemma q8_0 1545->2014 t/s). | Partially eats the audit's "CUDA leaves tensor cores idle" framing **for the projection GEMMs only** (attention scores stay manual on CUDA). UNVERIFIED for decode-bound Q4_K_M; re-bench before funding vLLM. |
| **metal kernels** | **PR #3471** fixes `GgmlDType::BF16` mapping. | Only relevant if a BF16 GGUF is ever added. |
| **flash-attn** | Absent from `agent/Cargo.lock` (grep exit 1); no `candle-flash-attn` dep in candle-nn 0.11.0. | CUDA fused attention requires a flash dep or vLLM. |
| **models** | gemma4 / new Google model (#3443, #3457). | Not in CX catalogue; ignore. |

### SYNC LIST

**PULL (port into the vendored file):**
1. **#3411 `rope_is_neox` dispatch — MANDATORY** (Qwen correctness, active bug). ~6-line `general.architecture` metadata read (a generic `md_get`, NOT the hardcoded `llama.` prefix in `from_gguf`) + one dispatch branch in `apply_rotary_emb` at `:335-342`. Byte-changing for Qwen, no-op for Llama.
2. **#3471 BF16 mapping** — only if a BF16 GGUF is added (not today).
3. **#3463/#3465 CUDA GGUF kernels** — only matter if a Candle CUDA lane is built; vLLM is the planned CUDA path, so low priority.

**ALREADY HAVE (a sync will NOT let CX drop these):**
- **P5 KV prealloc** — upstream's quantized_llama model file is still `Tensor::cat`-based in 0.10.2 AND main; CX is ahead. (To "drop" P5, refactor `KvCacheSlot` to wrap `candle_nn::KvCache`, which gains dynamic grow past `MAX_SEQ_LEN`; doable on 0.10.2, unaffected by the bump.)
- **Masked Metal prefill SDPA** — already in candle-nn 0.10.2; CX just gates it off with `seq_len==1`.

**WOULD BREAK on bump (re-vendor hazards):**
- 0.11.0's `LayerWeights` gains the `rope_is_neox` FIELD and `from_gguf` gains a `general.architecture` read, so re-applying P1-P5 onto the new struct is a mechanical graft, not a clean cherry-pick.
- The single in-source `PATCH` tag (verified: only `:802`) under-documents the surface; a future sync engineer who greps `PATCH` finds ONE line and could silently drop P4/P5 + the snapshot/restore prefix path. Tag every CX site before the next bump (see §8).

**UNVERIFIED — do not cite as fact:**
- The "+11.7 logit inflation / parity within 0.01" magnitude from #3411's description (direction is solid: swapped rotary convention corrupts attention across layers and drives repetition; exact numbers are PR-page summary, not a hard primary source).
- Any decode-path multiplier for #3463/#3465 (the +30% is prefill, q8_0, not Q4_K_M decode).

### Bump determinism caveat (load-bearing, corrects a common error)

**A candle bump does NOT change `build_hash` by construction.** `engine_build_hash` (`hardware.rs:130-147`) folds only `(engine, agent_version, device_label, CATALOGUE_QUANT)` — the candle crate version is NOT an input, and `agent_version` is pinned at `"0.1.0"` (`Cargo.toml:3`). So bumping candle (or porting the RoPE fix) while leaving `agent_version` at 0.1.0 produces a **byte-identical build_hash**, and 0.11.0 (or RoPE-fixed) workers would be byte-compared against unpatched peers in the same class — a moat break for Qwen. **The sync MUST bump `agent/Cargo.toml:3` `version` in the same commit and assert `engine_build_hash` actually changed.**

---

## 3. Remaining engine levers

Status legend: **ship-now** / **benchmark-first** / **spike-first** / **reject**. Impact is hype-discounted. "Moat risk" is vs the `(hw_class, engine, build_hash)` byte-exact contract.

| # | Lever | Why it helps CX | Surfaces | Impact (discounted) | Moat risk | Proof required | STATUS |
|---|---|---|---|---|---|---|---|
| L1 | **Commit perf-wave + seed golden gate** | The differentiating work is uncommitted working-tree state; golden gate has 0 rows (verified) so the within-class regression guard is inert | working tree; `agent/tests/golden/llama32_1b_q4k_greedy.hashes`; `runners.rs` `golden_token_baseline_gate` (#[ignore]) | Process safety; converts IMPLEMENTED->PROVEN | None (recording changes no bytes) | Commit to `feat/perf-wave`; run gate with `CX_GOLDEN_RECORD=1` on this M3 Pro; commit rows | **ship-now** |
| L2 | **Qwen RoPE fix (#3411 port)** | Both Qwen2.5 catalogue models served with WRONG interleaved RoPE today (`:342` unconditional `rope_i`); Qwen2.5-7B is the entire big-model value prop | `quantized_llama_batched.rs:335-342, 528-556`; `models.rs:180-186` | Correctness, not perf. Likely fixes degenerate/repetition Qwen output | **Byte-changing for Qwen** (Llama unaffected). New build_hash + re-seed Qwen honeypots | Qwen2.5 greedy parity vs llama.cpp reference: NEOX matches, interleaved diverges | **ship-now** (highest-priority correctness) |
| L3 | **KV prealloc via slice_set (P5)** | Removes per-step `Tensor::cat` realloc-copy; prerequisite for ragged batching | `quantized_llama_batched.rs:67-175` | ~1.05-1.15x on 256-tok jobs | None (byte-exact to cat, pinned `prealloc_kv_append_matches_cat`) | Already pinned; run GPU parity on Metal | **ship-now** (landed) |
| L4 | **KvCacheSlot bounds/grow guard** — **LANDED** (Workload & Model Breadth 6→7, docs/internal/CREED_AND_PATH_TO_TEN.md) | `MAX_SEQ_LEN` raised 4096→8192 (the doc's own "8K-16K tier", conservative end); `ModelWeights::forward` now checks `index_pos+seq_len<=MAX_SEQ_LEN` FIRST and `candle_core::bail!`s with an explicit, greppable message (real token counts + the real ceiling) before the rotary `narrow`/KV `append` truncation paths are ever reached | `quantized_llama_batched.rs` (`MAX_SEQ_LEN` const + the bounds check at the top of `ModelWeights::forward`) | Latent correctness bug closed; ceiling doubled | None for a guard (same offsets/bytes below the ceiling); rotary table cost doubles (cheap, ~4MB) | Test: prefill+decode past MAX_SEQ_LEN returns a typed error naming the actual token counts, not an opaque tensor-shape error | **shipped** |
| L5 | **Prefix-KV sharing (landed)** | Shared instruction/label/schema prefill once; helps classify (12-tok out) most | `runners.rs:1212-1314`; `quantized_llama_batched.rs:933` | ~2-4x classify, ~1.5-2.5x extract; low end on bandwidth-bound 1B; remainder+decode is PER-ITEM SERIAL | Byte-exact to serial (longest-common-TOKEN-prefix, post-tokenization) | Run `batch_shared_prefix_equals_serial` (#[ignore]) on GPU; measure high-prefix-ratio tps | **benchmark-first** |
| L6 | **Batched shared-prefix remainder+decode** | The deferred "full 2-4x"; today B sequences pay B serial decode passes | `runners.rs:1206-1211` TODO; `expand_kv_cache(B)` does not exist (grep) | The remaining 2-4x on high-prefix batches | Inherits generate_batch bucketing determinism; tolerant verification survives drift | Build `expand_kv_cache(B)` + bucket remainder; re-prove batched==serial | **spike-first** |
| L7 | **Mutex -> per-model serve loop** | `pool.rs:65` per-model `Mutex` serializes ALL same-model decode; the precondition for any continuous batching | `pool.rs:48,65,115`; `runners.rs:1397` `blocking_lock` | Structural; enables B concurrent requests/forward pass | Output-neutral if it keeps generate_batch math; B-dependent composition is the gated Hawking risk | Prototype mpsc serve loop; prove byte-identical to mutex path; measure aggregate tps | **spike-first** |
| L8 | **Hawking continuous-batch lane** | The 5.0x-at-B=8 Apple differentiator; only path to concurrent multi-request batching on Apple | `continuous_batch.rs` (430-line INERT skeleton, no kernel); `main.rs:591-598` (log-only, no runner) | ~5.0x aggregate B=8 (M3-Pro-measured, UNVALIDATED B=16/big); kernels ~0.62x llama.cpp (scheduler is the asset) | Separate `(apple,hawking,build_hash)` class; atol=1e-3 NOT byte-exact; **batch-composition determinism hazard within the class** (see §4) | Port `forward_multiseq_*` Metal kernel; B=8 batched==serial atol=1e-3; cross-Mac boundary test | **spike-first / fork-trigger** |
| L9 | **Metal SDPA prefill ungate** | candle-nn 0.10.2 ships masked GQA full-attention prefill SDPA (`supports_sdpa_full`, q_seq>8); CX gates it off at `seq_len==1`, leaving prefill on the manual repeat_kv+matmul path | gate `quantized_llama_batched.rs:388`; manual `:401-414` | Single-digit to ~1.3-1.5x on prefill-bound classify/extract/rerank; ~0 on decode-bound long gen | **HIGH** — fused softmax+matmul changes Metal bytes for EVERY Apple worker; fleet-wide reseed (new build_hash). The q_seq<=8 SDPA vector path drops mask/causal -> WRONG for 2-8-tok prefill | New build_hash + golden re-record; prefill-isolating harness (the named `batched_vs_serial_throughput` is decode-dominated); cross-M-series byte soak | **spike-first** |
| L10 | **CUDA SDPA ungate** | CUDA decode runs manual matmul over dequant-to-f32 QMatMul, tensor cores idle | gate `:388`; manual `:401-414` | Real, but candle 0.10.2 has NO `cuda_fwd` for Sdpa -> cannot be ungated; requires a vendored CUDA kernel or vLLM | Moot for Candle (no kernel); for vLLM, nvidia_* never cross-compared with Apple | Redirect to vLLM lane | **reject as Candle lever** (->vLLM) |
| L11 | **vLLM CUDA lane** | The ONLY fused-CUDA-attention option short of CX writing a kernel; unlocks 13B/32B/70B band | `runners.rs:1989,2037,2063-2074` (double-NotImplemented seal); `config.rs:68` | ~3-6x/GPU at greedy/temp=0 (discounted from 5-15x) | Favorable: `(nvidia_*, vllm, build_hash)`, never byte-compared with Apple. Seed answer_class for COVERAGE (class gate already prevents wrongful dock) | Cross-SKU A100/H100 + restart byte-stability soak; seed honeypots from a vLLM box | **benchmark-first** |
| L12 | **Constrained JSON decode** | `json_extraction` is post-hoc brace-balance (`runners.rs:1631`) with `{_error}` fallback; constrained decode guarantees schema-valid, cuts wasted tokens | `runners.rs:992-994` greedy-only; no GrammarConstraint in agent (grep clean) | Reliability unlock, not throughput | SAFE if vocab-index built deterministically (masks logits before SAME argmax). json_extraction is canonical-JSON tolerant, NOT byte-exact (`verification.go:382`), so target is looser than byte-identity | Port JsonVocabIndex; prove canonical-JSON agreement; measure `{_error}` drop | **spike-first** |
| L13 | **F16 KV / F16 activations / Q8** | F16 KV is -50% KV (long-context); F16 activations expand Q4->dense F16 (4x traffic, NET LOSS) | docs only (`forward_via_f16` is a doc-named hypothetical; no symbol in agent) | F16 KV memory win only (88% argmax-identity, NOT byte-identical); F16 activations negative | BREAKS byte-equality within class. New engine tag + non-redundant/tolerant lane only | Distinct engine tag; prove no byte-exact job draws it as a redundancy peer | **reject** (separate opt-in lane at most) |
| L14 | **Quant change Q5/Q6/Q8** | Higher quality lanes | `hardware.rs:106` `CATALOGUE_QUANT` hardcoded `q4_k_m` (folded into build_hash) | Quality, not throughput | A per-model requant currently stays in the SAME class silently (`CATALOGUE_QUANT` is a constant, not weight-derived) — latent hazard | Make CATALOGUE_QUANT weight-derived; test that a requant moves build_hash | **spike-first** (fix hazard first) |
| L15 | **Active-set EOS shrink (compact_kv_cache)** | Drops finished rows mid-decode so active batch shrinks | `quantized_llama_batched.rs` `compact_kv_cache`; `runners.rs:1166` | Real on mixed-length batches | Byte-safe (`index_select` copies verbatim) | `batch_active_shrink_equals_serial` (#[ignore]) on GPU | **benchmark-first** (landed, GPU-unproven) |
| L16 | **Length-bucketed embedder** | One change fixes embed AND rerank (shared path) | `runners.rs:603` | 1.15-1.6x on length-skewed, ~0 uniform | Byte-safe (tolerant cosine/order + stable scatter-back, pinned) | `embed_bucketed_matches_single_pad` (#[ignore]) on real BERT | **benchmark-first** (landed) |
| L17 | **Build_hash kernel-content gap** | `build_hash` derives from `agent_version` string, NOT kernel bytes; a kernel patch WITHOUT a version bump ships byte-changing into the SAME class | `hardware.rs:130-147` | Moat-protection control | This IS the determinism control; folding a kernel-content hash is output-neutral (changes only class label) | Test that mutating vendored kernel bytes changes build_hash; OR CI gate failing if module changed without version bump | **spike-first** (primary moat risk under aggressive mandate) |

---

## 4. Verification-moat gating rubric

The moat: results are cross-checked within a `(hw_class, engine, build_hash)` class. **Byte-exact** (`bytes.Equal`) for `batch_infer`/audio/custom (`verification.go:382-389` `byteExactJobType`); **tolerant** for embed (cosine>=0.999), classify (label), json (canonical-JSON), rerank (order). Cross-class differences are NEVER an auto-dock (`pass_with_penalty` + `redundancy_cross_class`/`tiebreak_cross_class`, `verification.go:191-199, 476-484`). This invariant is fully wired and test-pinned (`TestSameVerificationClass`, `TestMatchPinsVerificationClass`) and is the moat's load-bearing wall — every new engine tag / build_hash is safe to add because it cannot corrupt an existing class's byte comparisons.

**Class machinery (verified):** the matcher pins redundancy peers to the anchor on BOTH axes (`scheduler.go:259-266` `PinEngine`/`PinBuildHash`); `validEngines = {candle, mlx, vllm, hawking}` (`types.go`); `classKey("")` = non-matching. So an F16 / Hawking / vLLM lane is shippable as a new engine tag OR a new build_hash today — the isolation already exists.

### Per-lever gating rules

- **Metal SDPA prefill ungate (L9):** new build_hash on Metal (bump agent_version) — a FLEET-WIDE Apple reseed because it changes bytes for every Apple worker. Strictly harder than the CUDA ungate.
- **CUDA SDPA / vLLM (L10/L11):** nvidia_* is a distinct hw family never cross-compared with Apple; vLLM-vs-vLLM within `(nvidia_*, vllm, build_hash)`. Seed `answer_class` per-SKU; encode A100!=H100 into build_hash if they diverge under pin.
- **Hawking (L8):** separate `(apple, hawking, build_hash)` class, atol=1e-3, never byte-compared to Candle (engine_tag enforces). **CRITICAL UNDER-SPECIFIED HAZARD:** `batch_infer` is a BYTE-EXACT job type, but Hawking's batch routing is B-dependent. Two SAME-class `(apple, hawking, build_hash)` redundancy peers that compose different batches for the same job (B=8 on a busy worker, B=1 on an idle one) produce logits differing by ~1e-3, flipping occasional argmax ties -> different output bytes -> `bytes.Equal` FAILS two honest workers within one class. `engine_build_hash` does NOT fold batch composition. **Fix before Hawking carries byte-exact work:** either (a) a batch-INVARIANT determinism guarantee (canonical/fixed batch composition or batch-invariant kernels), OR (b) reclassify hawking generation as a non-byte-exact job type with a tolerant comparator. The port plan's "B=8 batched==serial atol=1e-3" gate does NOT certify the byte-exact redundancy contract.
- **F16 KV / activations (L13):** BREAKS determinism within any class; new engine tag + non-redundant lane, or do not ship.
- **Constrained JSON (L12):** SAFE within class (masks before same argmax); but forbid a silent per-worker constrained/post-hoc toggle inside a fixed build_hash (mixed-class skew on messy inputs); ship as a new build via agent_version bump.

### The two unseeded gates (COVERAGE holes, not correctness holes)

1. **Golden-hash regression gate UNSEEDED** (0 data rows, verified). It is the SECOND-line within-class regression guard (the first is a disciplined agent_version bump). With 0 rows it asserts nothing; a byte-shifting engine change ships undetected within a class until two same-class workers diverge in production (and on a single-build single-box fleet, latent until a second peer exists). **Seed on each shipped reference box; re-record on every build_hash bump.**
2. **Honeypot byte-exact auto-quarantine INERT** (`answer_class` ships blank; `seed.go:109-116` omits it; `db/schema.sql:223` DEFAULT ''). For byte-exact job types a blank class makes `byteExactComparable` false -> probe SKIPPED -> provisional trust. The demo `batch_infer` honeypot `{"text":"42"}` also does not match the `BatchInferResult{completions:[...]}` schema, so it could never byte-equal a real result. **Tolerant honeypots (embed) DO fire today; generation honeypots are off.** Seeding `answer_class` is determinism-SAFE; the hazard is seeding it WRONG (cross-class), which would wrongly quarantine honest cross-engine workers — so seed per `(device, engine, build_hash)` reference box, and regenerate `known_answer` as a schema-valid greedy output.

**Cross-Mac / cross-engine byte comparison is structurally PREVENTED** end-to-end (verified). Token-level determinism is impossible across heterogeneous Mac generations (Hawking research, ~15% variance at temp=0); the harness proves byte-identity WITHIN a class, not ACROSS. The single un-run validation is a **two-physical-Mac field test**.

---

## 5. Aggressive sequential roadmap

Ordered by dependency and ROI. Each step is a gate for the next.

1. **Commit the perf-wave working tree to `feat/perf-wave`.** It is uncommitted and one `git checkout`/`git clean` from loss. (Branch exists but `git diff` is empty; files untracked.) Un-loseable prerequisite for everything below.
2. **Seed the golden-hash gate on this M3 Pro** (`CX_GOLDEN_RECORD=1`, GGUF already on disk at `~/Downloads/hawking/models/Llama-3.2-1B-Instruct-Q4_K_M.gguf`). Commit rows. Activates the within-class regression guard.
3. **Run the 4 GPU-ignored parity tests** on this box: `batch_shared_prefix_equals_serial`, `embed_bucketed_matches_single_pad`, `batch_active_shrink_equals_serial_mixed_lengths`, `batched_vs_serial_throughput`. Converts the Wave-1 speedups from audit estimates to measured. (Author a prefill-isolating variant for the Metal SDPA decision.)
4. **Port the Qwen RoPE fix (#3411)** into `quantized_llama_batched.rs:335-342` with a `general.architecture` read, BUMP `agent/Cargo.toml:3` version atomically, re-seed Qwen honeypots + golden under the new build_hash. Add a Qwen2.5 NEOX parity test vs llama.cpp. This is the highest-priority correctness item and is independent of any perf work.
5. **Seed `answer_class` honeypots** (schema-valid greedy `batch_infer` answer recorded per reference class) and add the **build_hash kernel-content hash** (or a CI gate failing on a module change without a version bump). Closes the two moat coverage holes before any byte-changing lever ships.
6. **Decide the cx-infer architecture (option 4).** Give the existing `runners.rs` + `quantized_llama_batched.rs` pair a module boundary and name; declare it the home for the Hawking lane's second kernel backend. Free, output-neutral.
7. **Mutex -> per-model serve loop spike (L7).** Prove byte-identical to the mutex path on a fixed workload; measure aggregate tps under N concurrent same-model tasks. Precondition for continuous batching.
8. **Complete shared-prefix batched remainder+decode (L6).** `expand_kv_cache(B)` + length-bucket the remainder; re-prove batched==serial on GPU. Recovers the deferred 2-4x.
9. **Sync candle to 0.11.0** (optional, after step 4 lands the only mandatory delta). Re-apply P1-P5 onto the new `LayerWeights` shape; pull #3471 if BF16 GGUFs are added; bump version; assert build_hash moved; full prove-local green; Llama golden hashes UNCHANGED.
10. **vLLM CUDA lane (L11)** — the dominant CUDA win, process-isolated and fork-neutral. Cross-SKU + restart byte-stability soak FIRST; seed nvidia_* honeypots; THEN wire `run()`. Re-bench against 0.11 candle MMVQ/MMQ on Qwen2.5-7B Q4_K_M greedy DECODE before fully funding — only fund if vLLM beats 0.11 candle by a margin exceeding the determinism-harness + reseed cost.
11. **Hawking Apple lane (L8)** — the highest in-house upside. Port `forward_multiseq_*` Metal kernel (week 2, the long pole) into the cx-infer layer; wire `HawkingRunner`; **resolve the batch-composition byte-exact hazard (§4) — either batch-invariant determinism or a tolerant comparator for hawking generation**; B=8 batched==serial atol=1e-3; seed `(apple, hawking, build_hash)` golden + honeypots; cross-Mac class-boundary test. Re-measure 5.0x on CX's reference Mac (do not assume Hawking's M3 Pro number).
12. **Two-physical-Mac field test** — the only un-run validation of the class boundary.

---

## 6. Kill-list (do NOT build)

- **Network fork of candle (option 3).** No upside over a single-crate `[patch.crates-io]` for a private agent; adds git-source + supply-chain + CI surface. (Verifier: confirmed reject across all fork findings.)
- **From-scratch portable engine / cx-infer kernels from zero.** Candle already runs Metal+CUDA in one codebase (`Cargo.toml` cuda mirrors metal). Hawking's own kernels are ~0.62x llama.cpp at batch-1 (hawking `dead_levers.md:100`) — a from-zero kernel layer is negative-value.
- **F16 activations (`forward_via_f16`).** Expands Q4->dense F16 = 4x native 4-bit traffic, NET LOSS, breaks byte-equality. (Audit `:83`; verifier confirms it is a doc-named hypothetical, no symbol in agent.)
- **mv->mm batched-decode GEMM fusion.** ~1.2-1.6x at B>=8 but mm's F16-tiled accumulation flips greedy argmax vs the B=1 honeypot seed. (Audit `:84`.)
- **CUDA SDPA ungate as a Candle lever (L10).** candle 0.10.2/0.11.0/main have NO `cuda_fwd` for Sdpa — `candle_nn::ops::sdpa` ERRORS on a CUDA tensor. (Verifier refuted `CANDLE_FORK.md` lever #2 as a vendored-module change; redirect to vLLM.)
- **Megakernel, spec decode, sub-Q4/condense codec, int4-KV multiseq, RWKV-7 (now).** All net-negative or unbuilt-and-gated per the audit + Hawking kill ledger.
- **MLX FFI lane.** ~10-16 weeks for ~1.3-2x over CX's already-batched Candle path; superseded by Hawking for the Apple lane. Defer.
- **"Sync candle to drop P5".** Refuted: upstream's quantized_llama model is still `Tensor::cat`-based; CX is ahead. (Drop P5 only by refactoring `KvCacheSlot` to wrap `candle_nn::KvCache`, which is a separate, optional robustness refactor.)

---

## 7. Nothing-meaningful-left checklist

The inference layer is fully squeezed when ALL hold:

- [ ] Perf-wave is **committed** (not working-tree state).
- [ ] Golden-hash gate **seeded** on every shipped `(device, engine, build_hash)` reference box; re-recorded on every build_hash bump.
- [ ] Honeypot `answer_class` **seeded** per reference class with schema-valid greedy answers; byte-exact auto-quarantine live.
- [ ] `build_hash` folds a **kernel-content hash** (or a CI gate forbids a module change without a version bump) — kernel patches cannot silently stay in-class.
- [ ] **Qwen RoPE fix shipped** under a new build_hash; Qwen2.5 NEOX parity test green vs llama.cpp.
- [x] **KvCacheSlot bounds/grow** returns a typed error (no opaque slice_set failure) — `ModelWeights::forward`'s early bounds check; `MAX_SEQ_LEN` raised 4096→8192.
- [ ] Wave-1 levers (L3/L5/L15/L16) **GPU-measured** on Metal, speedups confirmed or downgraded.
- [ ] Shared-prefix **batched remainder+decode (L6)** shipped and proven, OR benchmarked and rejected for this workload.
- [ ] Mutex -> **per-model serve loop (L7)** shipped (continuous-batch precondition), OR proven unnecessary.
- [ ] **Hawking lane (L8)** shipped with the batch-composition byte-exact hazard resolved, B=8 re-measured on CX hardware, cross-Mac boundary tested — OR deferred for an external reason (Apple hardware / the 4-6wk Metal kernel).
- [ ] **vLLM lane (L11)** shipped within nvidia_* with cross-SKU soak, OR re-benched against 0.11 candle MMVQ/MMQ and deferred.
- [ ] Metal SDPA prefill ungate (L9): benchmarked on a prefill-isolating harness and either shipped under a fleet reseed OR rejected as not worth the reseed.
- [ ] Constrained JSON decode (L12) shipped or rejected on a measured `{_error}`-rate basis.
- [ ] **Two-physical-Mac field test** passed.
- [ ] Every remaining lever is shipped, benchmarked-and-rejected, or deferred for a NAMED external reason (hardware not present / upstream kernel absent / capability gated on an unbuilt large-model runner).

When the only open items are external (Apple hardware for the Hawking kernel, CUDA boxes for vLLM, an unbuilt 30-70B Apple runner, or an upstream candle CUDA SDPA that does not exist), the layer is squeezed.

---

## 8. Stale-doc fixes

1. **`docs/CANDLE_FORK.md` — patch ledger overstates CX authorship.** P3 ("Rectangular causal mask") is **upstream** candle 0.10.2 (`build_causal_mask`, `utils.rs:17-23`, used by ~12 models; CX CALLS it at `:680/:812`, does not author it). Mark P3 as upstream. P2's rationale "upstream recomputes the prefill mask every call" is FALSE — upstream `mask()` already caches in a HashMap unbounded; the genuine CX delta is the **eviction bound only** (`MASK_CACHE_CAP`). Net owned surface ≈ 4 narrow deltas (P1 contiguous, P2 bound, P4 compact, P5 prealloc + the snapshot/restore prefix path), not 5.
2. **`docs/CANDLE_FORK.md` — lever #2 (SDPA-on-CUDA) mis-scoped.** candle 0.10.2 has NO CUDA SDPA kernel; `candle_nn::ops::sdpa` ERRORS on a CUDA tensor. Rewrite to state this, reclassify lever #2 as a kernel-vendor / `[patch.crates-io]` escalation, and redirect the CUDA-attention win to `docs/VLLM_LANE.md`. Re-anchor its line refs: gate is `:388` (not ~339), manual path `:401-414` (not ~354-369).
3. **`docs/CANDLE_FORK.md` — add an explicit THRESHOLD section** with the §1 flip trigger; state that version-bump discipline is **unenforced** and require folding a kernel-content hash into `engine_build_hash` before aggressive kernel patching.
4. **`agent/src/quantized_llama_batched.rs:18-22` header + `CANDLE_FORK.md:37-38`.** The header "the ONLY behavioral change is `.contiguous()`" is FALSE (only `:802` carries a `PATCH` tag; P4/P5/snapshot are untagged structural grafts). Tag every CX site `PATCH Pn:`, fix the header, and add a sync-checklist assertion (e.g. grep-count of tagged sites == owned-delta count) so a future bump cannot silently drop a delta.
5. **`docs/PERF_AND_CAPABILITY_AUDIT.md` — re-anchor stale line refs** (`quantized_llama_batched.rs` citations are off by ~150 lines: gate is `:388` not `:238`, manual path `:401-414` not `:250-264`). Add **Metal-prefill-SDPA (L9)** as an in-strategy Apple-lane vendored-module patch candidate (the ADR's "build nothing" does NOT preclude widening the existing gate; it is byte-changing and gateable, not free). MLX/ggml-GEMM path string should read `candle-metal-kernels-0.10.2/src/kernels/{mlx_gemm.rs,quantized.rs}` (under `kernels/`, not `src/`).
6. **`docs/internal/SHIP_AND_DOMINATE.md` (archived, see `docs/ARCHIVE_INDEX.md`; retrieve with `git checkout pre-consolidation-2026-07-01 -- docs/internal/SHIP_AND_DOMINATE.md`) — mark superseded** for engine strategy (by `ROADMAP_REVIEW.md` + `PERF_AND_CAPABILITY_AUDIT.md`). Strike "no receipt exists" / ":61 zero verification fields" (the receipt now lives in `JobStatus.Verification` + `deriveVerificationLabel`, `types.go:318-355`). Strike "vLLM/PagedAttention mostly hype" framing for CUDA (true only for Metal; candle has no CUDA SDPA so vLLM IS the CUDA story). Reconcile "104/104" (Go control-plane integration matrix, `make prove-local SKIP_LIVE=1`) vs "88/0/13" (Rust agent unit suite) by labeling which harness each count is — they are different populations, not a contradiction. Migrate the two durable claims forward: Apple wedge = cheap statistically-verified BATCH inference on idle Macs (`:139`); competitors DO ship attested inference (Phala/Cocoon/Hyperbolic, `:146`) so "uncopyable moat" is an overclaim.
7. **`docs/BUILD_STATUS.md` / `docs/ROADMAP_REVIEW.md` — stop calling uncommitted working-tree files a "GREEN branch."** State the literal next action: commit `feat/perf-wave`, run the 13 #[ignore] tests, seed the golden gate.
8. **`docs/VLLM_LANE.md` — fix the inverted honeypot hazard.** Seed `answer_class` to GAIN byte-honeypot COVERAGE for the vLLM class, NOT to PREVENT wrongful docking (the class gate at `verification.go:113-114` already prevents that, fail-safe — a class-blind byte honeypot is SKIPPED, not failed). Also note the pinned `(vllm_version,dtype,tp,attn_backend)` tuple does NOT currently fold into `engine_build_hash` (which hashes only engine/agent_version/device_label/CATALOGUE_QUANT); add it before go-live.
9. **`docs/HAWKING_PORT_PLAN.md` — add the within-class batch-composition byte-exact hazard** (§4). The Week-5 "B=8 batched==serial atol=1e-3" gate does NOT certify the `bytes.Equal` redundancy contract for `batch_infer`; add either a batch-invariant guarantee or a tolerant-comparator reclassification for hawking generation.
