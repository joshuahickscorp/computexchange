<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Candle fork & patch surface

> **Update (feat/perf-wave pass 1) — corrections + new patches.** The owned surface is larger
> than the "ONLY `.contiguous()`" claim further down: the vendored `quantized_llama_batched.rs`
> now carries P-contiguous, P-kv (`KvCacheSlot`), and P-arch/P-rope/P-qkvbias (architecture-aware
> load: `general.architecture`-prefixed metadata keys, NEOX-vs-interleaved rotary, optional Qwen2
> q/k/v biases). The module header is now the authoritative ledger. Also:
> - **Lever #2 (route to `candle_nn::ops::sdpa` on CUDA) is INFEASIBLE** — candle 0.10.2 / 0.11.0 /
>   main have no CUDA `Sdpa` (it errors on a CUDA tensor). The CUDA fused-attention win is the vLLM
>   lane, not a Candle patch. See docs/VLLM_LANE.md.
> - **Fork THRESHOLD (the flip trigger):** stay vendored-module until CX must edit a
>   `candle-core`/`candle-nn`/kernel op that cannot be a vendored MODEL module AND the win is not
>   delivered by the vLLM or Hawking lane AND it clears a measured >=1.3x on a named job type at
>   temp=0. None in flight, so the anti-fork stance below still holds.
> - **build_hash now self-tracks this surface:** any edit here moves the verification class via
>   `hardware::infer_content_id()` (a source hash), so a kernel patch can no longer ship silently
>   into the same class. See docs/DETERMINISM_CLASS.md.

This is the deliberate, documented place we land engine improvements on top of
`candle` 0.10.x. The founder wants a fork so we own a hot-path we can improve;
this doc is the contract for *how* we own it without breaking the build or the
determinism moat.

## Strategy: vendored-module patch surface, not a GitHub fork

We do **not** maintain a network fork of candle or a full local vendor under
`[patch.crates-io]`. We tried that path mentally and rejected it: a full vendor
(candle-core + candle-nn + candle-transformers + their Metal/CUDA build glue)
is a large, fragile tree to keep green offline, and the only files we actually
need to change are a handful of model modules. The cost/benefit is bad.

Instead we keep the crates.io dependency (`candle-core`/`candle-nn`/
`candle-transformers` 0.10) and **vendor only the exact upstream modules we
patch**, as plain modules inside `agent/src/`. We already did this for one file
and this is now the sanctioned pattern:

- `agent/src/quantized_llama_batched.rs` — a VENDORED + PATCHED copy of
  candle-transformers 0.10.2 `quantized_llama`. It is wired in at
  `agent/src/main.rs:15` (`mod quantized_llama_batched;`) and used at
  `agent/src/runners.rs:11` (`use crate::quantized_llama_batched::ModelWeights
  as QLlama;`). Everything else still comes from the unmodified crates.io
  candle.

Why this is the safest viable "fork":

- `cd agent && cargo build` stays green with zero Cargo surgery — no
  `[patch.crates-io]`, no path deps, no lockfile churn, no offline-vendor
  breakage risk.
- The blast radius of every patch is one file we own outright. Lints are
  blanket-allowed in the vendored file (`#![allow(clippy::all, dead_code)]`)
  because it is upstream code, not ours to restyle — only the behavioural
  deltas are ours.
- Each patch is greppable. Behavioural deltas are tagged in-source (search
  `PATCH` and the `KvCacheSlot` block) so a future upstream bump can re-apply
  them deliberately.

If we ever DO need to patch a candle crate that cannot be expressed as a
single vendored module (e.g. a change inside `candle-core`'s tensor ops), the
escalation is a `[patch.crates-io]` entry pointing at a local vendored copy of
*that one crate only* — added ONLY if it can be kept green. Until then, do not
add it. Never leave Cargo in a broken state; if an experiment breaks the build,
revert the Cargo change.

## How to sync upstream candle 0.10.x

When we bump the crates.io candle version (`agent/Cargo.toml:33-35`):

1. Note the new version (e.g. 0.10.3). Pull the upstream source for the
   vendored modules from the local cargo cache after the bump:
   `~/.cargo/registry/src/index.crates.io-*/candle-transformers-<ver>/src/models/quantized_llama.rs`.
2. Diff our vendored file against the *old* upstream to extract our deltas (see
   the patch ledger below — they are all tagged in-source).
3. Re-apply each delta onto the *new* upstream module. Keep the file header's
   "VENDORED + PATCHED copy of candle-transformers <ver>" line accurate.
4. Re-run the determinism pins:
   `cd agent && cargo test --no-default-features -- prealloc_kv quantized_llama_batched`
   then the model-backed parity tests on a GPU box
   (`batched_vs_serial_throughput`, `batch_active_shrink_equals_serial_mixed_lengths`,
   both `#[ignore]` — they need the ~800MB GGUF and a GPU).
5. `cd agent && cargo build` (default = metal) AND
   `cargo build --no-default-features` (CPU/CI) must both be green.

## Patch ledger (what we have changed vs. upstream)

| # | Patch | Where | Determinism | Status |
|---|-------|-------|-------------|--------|
| P1 | `.contiguous()` on the output-projection's last-position slice so a `bsz>1` batched prefill's quantized matmul succeeds (candle rejects the non-contiguous slice) | `quantized_llama_batched.rs` — search `PATCH` (the `x.i((.., seq_len - 1, ..))?.contiguous()` line) | SAFE — slice values unchanged, only made contiguous | shipped |
| P2 | Bounded mask cache (`MASK_CACHE_CAP`, insertion-order eviction) keyed on `(seq_len, kv_len)` — upstream recomputes the prefill mask every call; we cache it but bound the cache so a warm model does not leak ~16MB/mask | `ModelWeights.masks` / `mask_order` / `mask()` | SAFE — a recomputed mask is bitwise identical to the evicted one | shipped |
| P3 | Rectangular causal mask for prefix-KV reuse (`build_causal_mask(seq_len, index_pos)`), so a non-zero `index_pos` prefill attends correctly over cached prefix keys | `mask()` + the mask tests | SAFE — values follow the documented triangle | shipped |
| P4 | `compact_kv_cache(keep)` — drop finished (EOS) batch rows from every layer's KV cache mid-decode so the active batch shrinks | `compact_kv_cache` + `KvCacheSlot::compact` | SAFE — `index_select` copies kept rows verbatim; survivors share one `index_pos` | shipped |
| P5 | **KV-cache preallocation** — replace the per-decode-step `Tensor::cat` append with a preallocated buffer + `slice_set` at a running offset (`KvCacheSlot`), mirroring candle-nn's `Cache` | `KvCacheSlot`, `LayerWeights.kv_k/kv_v`, `forward_attn`, `compact_kv_cache` | SAFE — see below | **shipped (this stage)** |

### P5 determinism note (KV-cache preallocation)

This is the one improvement implemented in this stage. It is determinism-SAFE
by construction and pinned by tests:

- The append now writes the new keys/values into a preallocated
  `(b_sz, n_kv_head, MAX_SEQ_LEN, head_dim)` buffer via `slice_set` at offset
  `cur_len` and reads the live region back as `narrow(2, 0, cur_len)?
  .contiguous()` — which is **byte-for-byte the same contiguous tensor**
  `Tensor::cat(&[cache, new], 2)` produced. `slice_set` copies the source bytes
  verbatim to the same `(batch, head, position, dim)` coordinates `cat` wrote
  them to. Logits are bitwise identical.
- `index_pos == 0` (a fresh prefill on a reused warm model) resets the slot,
  exactly mirroring the old cat path where `index_pos == 0` ignored the cached
  tensors (`forward_attn`, ~`quantized_llama_batched.rs:329`).
- `compact_kv_cache` re-seats the live region as a fresh tightly-batched buffer
  so the next append's `slice_set` shape matches the shrunk batch.
- Pins: `prealloc_kv_append_matches_cat` (the load-bearing one: slice_set ==
  cat byte-equality across a prefill + 6 decode steps), `prealloc_kv_reset_starts_fresh`,
  and `prealloc_kv_compact_keeps_rows_verbatim`. All CPU-runnable, all green.

Impact is modest on its own (~1.05-1.15x on 256-token jobs per the audit) — the
real value is removing the per-step O(seq_len) realloc-and-copy and laying the
groundwork for ragged/continuous batching.

## Prioritized improvement ledger

Grounded in `docs/PERF_AND_CAPABILITY_AUDIT.md`. The patch surface above is
where these land. Ordered by value/risk.

### 1. KV-cache preallocation via `slice_set` — DONE (P5)

Was: per-decode-step `Tensor::cat` of `(k_cache, k_new)` at the old
`quantized_llama_batched.rs:230-231`. Now: preallocated `slice_set` append
(`KvCacheSlot`). Determinism: SAFE (identical bytes at offset). Status:
**implemented this stage**, see P5 above. This was the one clearly
determinism-safe, self-contained win and it is shipped + pinned.

### 2. Ungate the fused SDPA fast path so it runs on CUDA too — NOT YET

Today the fused scaled-dot-product-attention fast path is gated to
`q.device().is_metal() && seq_len == 1` (`forward_attn`,
~`quantized_llama_batched.rs:339`). On CUDA the code falls through to the
manual path: `repeat_kv` + `q.matmul(&k.t())` / sqrt(head_dim) + masked-fill +
`softmax_last_dim` + `att.matmul(v)` over a dequant-to-f32 `QMatMul`
(~`quantized_llama_batched.rs:354-369`), leaving tensor cores idle.

- **What**: extend the SDPA branch to fire on CUDA decode steps too (and
  ideally prefill via a masked SDPA variant), matching the Metal fast path.
- **DETERMINISM — GATED, NOT SAFE**: SDPA's fused softmax+matmul accumulates
  differently from the manual `matmul -> softmax_last_dim -> matmul` chain, so
  this **changes CUDA byte-output**. It must NOT ship silently. It is only
  acceptable behind the `nvidia_*` verification class (a distinct hw family
  never cross-compared with Apple per the audit's determinism ledger), and only
  after a within-tier byte-stability soak and `hw_class`-aware honeypot seeding.
  Do not enable it on the Metal/CPU lanes.
- **How (when we do it)**: a `cuda` cfg/runtime branch inside `forward_attn`
  that routes to `candle_nn::ops::sdpa` on CUDA, pinned to the nvidia class.
  This belongs with the broader vLLM CUDA-lane work (audit Wave 2), since the
  determinism harness it depends on is the same one.

### 3. Hooks for a deterministic batch scheduler — NOT YET

The audit's "build the deterministic continuous-batching scheduler" lever
(ported from Hawking's `crates/hawking-serve/src/batch/{scheduler.rs,driver.rs}`
+ slot-strided KV). The KvCacheSlot preallocation (P5) is the prerequisite: a
slot that owns a fixed KV region and appends at an offset is exactly what a
slot-strided continuous-batch scheduler needs (a slot keeps its KV region as
the ready set churns).

- **What hooks to add later**: (a) per-slot `cur_len`/offset already exists on
  `KvCacheSlot`; expose a slot-id-keyed map so independent sequences can share
  one preallocated arena; (b) a `reset`/`evict(slot)` surface (we have `reset`
  and `compact`); (c) greedy-vs-sampled readback routing at the driver layer
  (token-only readback when all-greedy, full logits otherwise) — that lives in
  `runners.rs`'s decode loop, not in this module.
- **DETERMINISM**: B-dependent kernel routing is not bit-identical to solo
  (parity at `atol=1e-3`, the same cross-hw fp16 risk the single-stream path
  already carries). Apple-only lane, re-gated cross-worker on a pinned
  `(device, shader_hash)` class. Not a byte-exact change to the existing lane.

### Explicitly out of scope for this patch surface (per the audit's "rejected")

- F16 activations via `forward_via_f16` (NET LOSS, breaks byte-equality).
- mv->mm batched-decode GEMM fusion (breaks greedy argmax vs the B=1 honeypot
  seed).
- F16 KV cache (88% argmax-identity, must be a separate non-byte-exact lane).
- A from-scratch portable engine (candle already runs Metal+CUDA).
