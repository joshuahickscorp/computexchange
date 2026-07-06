//! Quantized llama model implementation.
//!
//! This provides a quantized implementation of the llama language model architecture.
//! The model implements parameter efficient quantization for reduced memory usage
//! while maintaining model quality.
//!
//! Key characteristics:
//! - Transformer decoder architecture
//! - Support for 2/3/4/8-bit quantization
//! - Optimized memory usage through quantization
//! - Configurable model sizes and parameter counts
//!
//! - 💻 [GH Link](https://github.com/facebookresearch/llama)
//! - 📝 [Paper](https://arxiv.org/abs/2302.13971)
//!
//! ![](https://raw.githubusercontent.com/huggingface/candle/main/candle-examples/examples/quantized/assets/aoc.gif)
//!
//! VENDORED + PATCHED copy of candle-transformers 0.10.2 `quantized_llama`. This file
//! carries SEVERAL CX behavioral deltas, each tagged `PATCH` in-source (grep for it):
//!   - P-contiguous: `.contiguous()` on the output-projection's last-position slice so
//!     a batch (bsz>1) prefill's quantized matmul succeeds (candle rejects the strided
//!     slice).
//!   - P-kv: `KvCacheSlot` preallocated KV append via `slice_set` (replaces the per-step
//!     `Tensor::cat`); byte-identical, plus snapshot/restore for prefix-fork.
//!   - P-arch + P-rope + P-qkvbias: architecture-aware load. Reads GGUF
//!     `general.architecture`, prefixes the metadata keys with it (official Qwen2 GGUFs
//!     use `qwen2.*`, not `llama.*`), selects NEOX vs interleaved rotary, and loads the
//!     optional Qwen2 q/k/v biases. Llama is byte-identical (arch defaults to `llama`,
//!     interleaved rope, no bias). Ports the intent of candle 0.11 PR #3411 onto 0.10.2.
//! Any edit here changes `hardware::infer_content_id()` and therefore the worker's
//! (hw_class, engine, build_hash) class, so golden hashes + honeypots must be reseeded.
//! See docs/CANDLE_FORK.md and docs/CANDLE_EXPANSION_RESEARCH.md.
//! Keep in sync if candle is bumped. Lints are
//! blanket-allowed: this is upstream code, not ours to restyle.
#![allow(clippy::all, dead_code)]

use std::collections::{HashMap, VecDeque};

use candle_core::quantized::QTensor;
use candle_core::quantized::{ggml_file, gguf_file};
use candle_core::{DType, Device, IndexOp, Result, Tensor};
use candle_nn::{Embedding, Module};
use candle_transformers::quantized_nn::RmsNorm;

/// Hard ceiling on total sequence length (prompt + generated tokens) any
/// `KvCacheSlot`/rotary table this module builds can ever hold.
///
/// PATCH (Workload & Model Breadth 6→7, docs/internal/CREED_AND_PATH_TO_TEN.md
/// "lift the context ceiling with a real bounds check"): raised from 4096 to
/// 8192 — the doc's own "even an 8K-16K tier unblocks real long-document
/// extraction" floor. 8192 (not 16384) is the conservative end of that range:
/// the precomputed rotary cos/sin table (`precomput_freqs_cis`) and the
/// worst-case KV fallback (any `KvCacheSlot` that never got a `reset_with_cap`/
/// `set_next_seq_cap` hint still allocates this many positions) both scale
/// linearly with this constant, and every registered worker — including the
/// reference M3 Pro this codebase is measured on — must still be able to
/// afford that worst case for the SMALL (1B) model's real dispatch path.
/// Doubling once (not twice) keeps that worst-case allocation inside a range
/// this codebase has already exercised (see `docs/CANDLE_EXPANSION_RESEARCH.md`
/// L4). Raising further is a follow-up once a real long-context workload
/// justifies the larger worst-case footprint.
///
/// This is a ceiling, not a promise every job gets this much: real dispatch
/// paths (`generate_batch`'s `set_next_seq_cap`) still right-size each job's
/// OWN allocation to `(prompt_len + max_tokens).min(MAX_SEQ_LEN)`, so a short
/// job's memory cost is completely unaffected by this constant — only the
/// worst-case ceiling (and the rotary table) grew.
pub const MAX_SEQ_LEN: usize = 8192;

/// Upper bound on distinct masks retained in the per-model mask cache. Each
/// long prefill mask is ~16MB, so an unbounded cache leaks GBs on a long-lived
/// warm model. Eviction is insertion-order (oldest first) and determinism-safe:
/// a recomputed mask is bitwise identical to the evicted one.
const MASK_CACHE_CAP: usize = 64;

/// Preallocated per-layer KV cache that appends new keys/values via
/// `slice_set` at a running offset instead of the per-decode-step
/// `Tensor::cat` (the Wave-2 "preallocate the KV cache" lever in
/// docs/PERF_AND_CAPABILITY_AUDIT.md). This mirrors candle-nn's `Cache`
/// (candle-nn/src/kv_cache.rs): a buffer of `(b_sz, n_kv_head, MAX_SEQ_LEN,
/// head_dim)` is allocated once on the first append, each step writes its
/// `seq_len` tokens at `cur_len` along the sequence dim (dim 2), and the live
/// region is exposed as `narrow(2, 0, cur_len)`.
///
/// DETERMINISM: safe by construction. `slice_set` copies the source bytes
/// verbatim to the same logical (batch, head, position, dim) coordinates that
/// `Tensor::cat` wrote them to, and `attention()` consumes the live region as
/// `.narrow(..).contiguous()`, which is byte-for-byte the same contiguous
/// tensor `Tensor::cat` previously produced. Logits are bitwise identical.
/// The `slice_set == cat` byte-equality is pinned by the
/// `prealloc_kv_append_matches_cat` test below.
///
/// Two wrinkles this code carries that vanilla candle-nn's `Cache` does not:
///  - Reset on prefill: a warm model is reused across jobs and the caller
///    signals a fresh sequence with `index_pos == 0` (see `forward_attn`),
///    which drops the buffer so the next append reallocates at the new batch
///    size — exactly the old `Tensor::cat` reset behaviour.
///  - Batch-row compaction: `compact_kv_cache` drops finished (EOS) rows from
///    dim 0 mid-decode. We `index_select` the live region and re-seat it as a
///    fresh, tightly-batched buffer so the next append's `slice_set` shape
///    matches.
#[derive(Debug, Clone)]
struct KvCacheSlot {
    /// `(b_sz, n_kv_head, cap, head_dim)`, allocated lazily on first append at
    /// whatever `cap` was set to by the most recent `reset_with_cap` (or
    /// `MAX_SEQ_LEN` if `reset_with_cap` was never called — the original,
    /// always-worst-case-sized behavior). The live keys/values occupy
    /// `[.., .., 0..cur_len, ..]`.
    buf: Option<Tensor>,
    /// Number of valid sequence positions currently written into `buf`.
    cur_len: usize,
    /// PATCH (P-rightsize): the sequence-dim capacity the NEXT allocation will
    /// use, instead of always the worst-case `MAX_SEQ_LEN`. A 48-token
    /// batch_infer job at batch 32 preallocating a 4096-slot KV cache per row
    /// wastes ~10-40x the memory it needs (docs/CREED_AND_PATH_TO_TEN.md,
    /// "Inference hot path" 8→9); sizing to the caller's real
    /// `prompt_len + max_tokens` removes that waste without changing a single
    /// byte of the math (`slice_set`/`narrow` are unchanged; only the buffer's
    /// allocated width differs). Never exceeds `MAX_SEQ_LEN` — the rotary
    /// table precomputed at load time only covers positions `0..MAX_SEQ_LEN`.
    cap: usize,
}

impl KvCacheSlot {
    fn new() -> Self {
        Self {
            buf: None,
            cur_len: 0,
            cap: MAX_SEQ_LEN,
        }
    }

    /// Drop the buffer so the next `append` reallocates, WITHOUT changing
    /// `cap` — used where no better size estimate is available (matches the
    /// original, always-`MAX_SEQ_LEN` behavior when `reset_with_cap` is never
    /// called; see `reset_with_cap` for the right-sized path).
    fn reset(&mut self) {
        self.buf = None;
        self.cur_len = 0;
    }

    /// Like `reset`, but also sets the capacity the next `append`/`restore`
    /// will allocate at. `cap` is clamped to `[1, MAX_SEQ_LEN]` so a caller can
    /// never under-allocate to zero or exceed the precomputed rotary table.
    fn reset_with_cap(&mut self, cap: usize) {
        self.buf = None;
        self.cur_len = 0;
        self.cap = cap.clamp(1, MAX_SEQ_LEN);
    }

    /// Append `src` (shape `(b_sz, n_kv_head, seq_len, head_dim)`) at the
    /// current offset and return the live region `(b_sz, n_kv_head, cur_len,
    /// head_dim)` as a contiguous tensor — byte-identical to what
    /// `Tensor::cat(&[cache, src], 2)` produced.
    fn append(&mut self, src: &Tensor) -> Result<Tensor> {
        let (b_sz, n_kv_head, seq_len, head_dim) = src.dims4()?;
        if self.buf.is_none() {
            // A sequence can outgrow `cap` (e.g. a decode that runs past the
            // caller's original max_tokens estimate); grow to MAX_SEQ_LEN in
            // that case rather than erroring — this only ever WIDENS a bucket
            // that turns out to need more room, never narrows a safe one.
            let alloc_len = self.cap.max(self.cur_len + seq_len).min(MAX_SEQ_LEN);
            let buf = Tensor::zeros(
                (b_sz, n_kv_head, alloc_len, head_dim),
                src.dtype(),
                src.device(),
            )?;
            self.buf = Some(buf);
            self.cur_len = 0;
        }
        let mut buf = self.buf.as_ref().unwrap().clone();
        if self.cur_len + seq_len > buf.dim(2)? {
            // Same growth guard as above, for a buffer that already existed
            // (mid-decode) but is about to overflow its current allocation.
            let grown_len = (self.cur_len + seq_len).min(MAX_SEQ_LEN);
            let grown = Tensor::zeros(
                (b_sz, n_kv_head, grown_len, head_dim),
                src.dtype(),
                src.device(),
            )?;
            if self.cur_len > 0 {
                let live = buf.narrow(2, 0, self.cur_len)?.contiguous()?;
                grown.slice_set(&live, 2, 0)?;
            }
            buf = grown;
            self.buf = Some(buf.clone());
        }
        let buf = self.buf.as_mut().unwrap();
        // slice_set requires both operands contiguous; the source comes from a
        // transpose/rotary chain so make it contiguous first (a no-op when it
        // already is). The narrowed read below is then made contiguous too,
        // reproducing the exact contiguous tensor the old `cat` returned.
        buf.slice_set(&src.contiguous()?, 2, self.cur_len)?;
        self.cur_len += seq_len;
        buf.narrow(2, 0, self.cur_len)?.contiguous()
    }

    /// Keep only the named batch rows (indices into dim 0, ascending), used by
    /// `compact_kv_cache` when EOS rows are dropped. Re-seats the live region
    /// as a fresh tightly-batched buffer so the next append's `slice_set`
    /// shape matches the shrunk batch.
    fn compact(&mut self, idx: &Tensor) -> Result<()> {
        if let Some(buf) = &self.buf {
            // narrow yields a strided view; index_select needs contiguous.
            let live = buf.narrow(2, 0, self.cur_len)?.contiguous()?;
            let kept = live.index_select(idx, 0)?;
            let (b_sz, n_kv_head, _seq, head_dim) = kept.dims4()?;
            // Re-seat at this slot's existing `cap` (not always the worst-case
            // MAX_SEQ_LEN) — a compacted batch still has the same per-row
            // length budget as before compaction; only the batch WIDTH shrank.
            let alloc_len = self.cap.max(self.cur_len).min(MAX_SEQ_LEN);
            let new_buf = Tensor::zeros(
                (b_sz, n_kv_head, alloc_len, head_dim),
                kept.dtype(),
                kept.device(),
            )?;
            new_buf.slice_set(&kept.contiguous()?, 2, 0)?;
            self.buf = Some(new_buf);
        }
        Ok(())
    }

    /// Snapshot the live region as a standalone contiguous tensor + its length,
    /// used by the prefix-KV-sharing path (`prefill_shared_prefix` + per-item
    /// `restore`). The snapshot is a deep copy of `[.., .., 0..cur_len, ..]`, so
    /// later in-place mutation of this slot (or restore into a fresh buffer)
    /// cannot disturb it. `None` when nothing has been prefilled yet.
    fn snapshot(&self) -> Result<Option<(Tensor, usize)>> {
        match &self.buf {
            Some(buf) if self.cur_len > 0 => {
                let live = buf.narrow(2, 0, self.cur_len)?.contiguous()?;
                Ok(Some((live, self.cur_len)))
            }
            _ => Ok(None),
        }
    }

    /// Re-seat this slot from a `snapshot` (live region + length) as a fresh
    /// tightly-allocated buffer, exactly the way `compact` re-seats a kept
    /// region. The next `append` continues from `cur_len`. Used to fork a shared
    /// prefix into a per-item sequence: every item restores the SAME prefix
    /// snapshot, then appends only its own remaining tokens.
    ///
    /// DETERMINISM: `slice_set` writes the snapshot bytes verbatim at offset 0,
    /// so the restored live region is bitwise identical to the prefix the
    /// snapshot captured — the per-item forward sees the same KV it would have
    /// seen had the prefix been prefilled inline. Byte-for-byte equal to serial.
    fn restore(&mut self, snapshot: &(Tensor, usize)) -> Result<()> {
        let (live, len) = snapshot;
        let (b_sz, n_kv_head, _seq, head_dim) = live.dims4()?;
        // Re-seat at this slot's existing `cap` (unchanged by `restore` itself —
        // callers that want a right-sized fork should `reset_with_cap` before
        // restoring); always at least big enough to hold the restored prefix.
        let alloc_len = self.cap.max(*len).min(MAX_SEQ_LEN);
        let new_buf = Tensor::zeros(
            (b_sz, n_kv_head, alloc_len, head_dim),
            live.dtype(),
            live.device(),
        )?;
        new_buf.slice_set(&live.contiguous()?, 2, 0)?;
        self.buf = Some(new_buf);
        self.cur_len = *len;
        Ok(())
    }

    /// Like `restore`, but re-seats the snapshot BROADCAST to `b_sz` batch rows
    /// instead of the snapshot's own (always 1) batch dim — the "fork the KV
    /// cache to B rows" lever (docs/internal/CREED_AND_PATH_TO_TEN.md, "Inference
    /// hot path" 7→7.5 / "Batching efficiency" 6.5→7: "Batch the shared-prefix
    /// remainder decode" / "Restore batched decode to the shared-prefix path" —
    /// the follow-up `generate_batch_shared_prefix`'s own doc comment already
    /// named: "expand the prefix KV snapshot to `(B, ...)`").
    ///
    /// DETERMINISM: `Tensor::repeat` materializes `b_sz` literal copies of the
    /// snapshot's single row via `Tensor::cat` (candle-core's own repeat impl),
    /// so row `r` of the broadcast buffer is byte-for-byte the same bytes
    /// `restore` would have written for a lone fork of that same snapshot — a
    /// bucketed batched decode over these `b_sz` rows therefore sees, per row,
    /// EXACTLY the KV state a serial per-item restore+decode would have seen.
    /// This is not a numerical broadcast (no strided/expand view survives into
    /// `slice_set`, which requires real contiguous storage) — it is `b_sz` real,
    /// independent copies, so subsequent per-row `compact` (EOS active-set
    /// shrink) can freely diverge rows without disturbing shared storage.
    fn restore_broadcast(&mut self, snapshot: &(Tensor, usize), b_sz: usize) -> Result<()> {
        let (live, len) = snapshot;
        let (src_b, n_kv_head, _seq, head_dim) = live.dims4()?;
        if src_b != 1 {
            candle_core::bail!("restore_broadcast: snapshot batch dim must be 1, got {src_b}");
        }
        let live_b = live.repeat((b_sz, 1, 1, 1))?;
        let alloc_len = self.cap.max(*len).min(MAX_SEQ_LEN);
        let new_buf = Tensor::zeros(
            (b_sz, n_kv_head, alloc_len, head_dim),
            live_b.dtype(),
            live_b.device(),
        )?;
        new_buf.slice_set(&live_b.contiguous()?, 2, 0)?;
        self.buf = Some(new_buf);
        self.cur_len = *len;
        Ok(())
    }
}

/// One layer's snapshotted KV state for the prefix-KV-sharing path. `None` when
/// that slot held nothing (no prefix prefilled). Produced by
/// `ModelWeights::snapshot_kv_cache` and consumed by `restore_kv_cache`; each
/// inner tensor is `(b_sz, n_kv_head, prefix_len, head_dim)` with its length.
#[derive(Debug, Clone)]
pub struct KvSnapshot {
    k: Option<(Tensor, usize)>,
    v: Option<(Tensor, usize)>,
}

// QMatMul wrapper adding some tracing.
#[derive(Debug, Clone)]
struct QMatMul {
    inner: candle_core::quantized::QMatMul,
    span: tracing::Span,
}

impl QMatMul {
    fn from_qtensor(qtensor: QTensor) -> Result<Self> {
        let inner = candle_core::quantized::QMatMul::from_qtensor(qtensor)?;
        let span = tracing::span!(tracing::Level::TRACE, "qmatmul");
        Ok(Self { inner, span })
    }

    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let _enter = self.span.enter();
        self.inner.forward(xs)
    }
}

#[derive(Debug, Clone)]
struct Mlp {
    feed_forward_w1: QMatMul,
    feed_forward_w2: QMatMul,
    feed_forward_w3: QMatMul,
}

impl Module for Mlp {
    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let w1 = self.feed_forward_w1.forward(xs)?;
        let w3 = self.feed_forward_w3.forward(xs)?;
        self.feed_forward_w2
            .forward(&(candle_nn::ops::silu(&w1)? * w3)?)
    }
}

#[derive(Debug, Clone)]
enum MlpOrMoe {
    Mlp(Mlp),
    MoE {
        n_expert_used: usize,
        feed_forward_gate_inp: QMatMul,
        experts: Vec<Mlp>,
    },
}

impl Module for MlpOrMoe {
    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        match self {
            Self::MoE {
                feed_forward_gate_inp,
                experts,
                n_expert_used,
            } => {
                let (b_size, seq_len, hidden_dim) = xs.dims3()?;
                let xs = xs.reshape(((), hidden_dim))?;
                let router_logits = feed_forward_gate_inp.forward(&xs)?;
                let routing_weights = candle_nn::ops::softmax_last_dim(&router_logits)?;

                // In order to extract topk, we extract the data from the tensor and manipulate it
                // directly. Maybe we will want to use some custom ops instead at some point.
                let routing_weights = routing_weights.to_dtype(DType::F32)?.to_vec2::<f32>()?;

                // routing_weights, selected_experts = torch.topk(routing_weights, self.top_k, dim=-1)
                // top_x contains the row indexes to evaluate for each expert.
                let mut top_x = vec![vec![]; experts.len()];
                let mut selected_rws = vec![vec![]; experts.len()];
                for (row_idx, rw) in routing_weights.iter().enumerate() {
                    let mut dst = (0..rw.len() as u32).collect::<Vec<u32>>();
                    dst.sort_by(|&i, &j| rw[j as usize].total_cmp(&rw[i as usize]));
                    let mut sum_routing_weights = 0f32;
                    for &expert_idx in dst.iter().take(*n_expert_used) {
                        let expert_idx = expert_idx as usize;
                        let routing_weight = rw[expert_idx];
                        sum_routing_weights += routing_weight;
                        top_x[expert_idx].push(row_idx as u32);
                    }
                    for &expert_idx in dst.iter().take(*n_expert_used) {
                        let expert_idx = expert_idx as usize;
                        let routing_weight = rw[expert_idx];
                        selected_rws[expert_idx].push(routing_weight / sum_routing_weights)
                    }
                }

                // routing_weights /= routing_weights.sum(dim=-1, keepdim=True)
                // expert_mask = torch.nn.functional.one_hot(selected_experts, num_classes=self.num_experts).permute(2, 1, 0)

                let mut ys = xs.zeros_like()?;
                for (expert_idx, expert_layer) in experts.iter().enumerate() {
                    let top_x = &top_x[expert_idx];
                    if top_x.is_empty() {
                        continue;
                    }
                    let top_x = Tensor::new(top_x.as_slice(), xs.device())?;
                    let selected_rws =
                        Tensor::new(selected_rws[expert_idx].as_slice(), xs.device())?
                            .reshape(((), 1))?;
                    // Index the correct hidden states and compute the expert hidden state for
                    // the current expert. We need to make sure to multiply the output hidden
                    // states by `routing_weights` on the corresponding tokens (top-1 and top-2)
                    let current_state = xs.index_select(&top_x, 0)?.reshape(((), hidden_dim))?;
                    // current_hidden_states = expert_layer(current_state, routing_weights[top_x_list, idx_list, None])
                    let current_hidden_states = expert_layer.forward(&current_state)?;
                    let current_hidden_states =
                        current_hidden_states.broadcast_mul(&selected_rws)?;
                    ys = ys.index_add(&top_x, &current_hidden_states, 0)?;
                }

                let ys = ys.reshape((b_size, seq_len, hidden_dim))?;
                Ok(ys)
            }
            Self::Mlp(mlp) => mlp.forward(xs),
        }
    }
}

#[derive(Debug, Clone)]
struct LayerWeights {
    attention_wq: QMatMul,
    attention_wk: QMatMul,
    attention_wv: QMatMul,
    attention_wo: QMatMul,
    attention_norm: RmsNorm,
    mlp_or_moe: MlpOrMoe,
    ffn_norm: RmsNorm,
    n_head: usize,
    n_kv_head: usize,
    head_dim: usize,
    cos: Tensor,
    sin: Tensor,
    neg_inf: Tensor,
    /// PATCH (P-qkvbias): optional attention q/k/v biases. Qwen2 carries them; Llama
    /// does not. Applied to q/k/v BEFORE rotary in `forward_attn`. `None` -> the
    /// bias-free path, byte-identical to upstream for Llama.
    attention_bq: Option<Tensor>,
    attention_bk: Option<Tensor>,
    attention_bv: Option<Tensor>,
    /// PATCH (P-rope): true for NEOX-style rotary (Qwen/Falcon/Phi/...), false for
    /// interleaved (Llama/Mistral). Chosen from GGUF `general.architecture`.
    rope_is_neox: bool,
    /// Preallocated KV cache (k, v) appended via `slice_set` (see
    /// `KvCacheSlot`). Replaces the old per-step `Tensor::cat`; byte-identical
    /// logits, no behavioural change.
    kv_k: KvCacheSlot,
    kv_v: KvCacheSlot,
    span_attn: tracing::Span,
    span_rot: tracing::Span,
    span_mlp: tracing::Span,
}

fn masked_fill(on_false: &Tensor, mask: &Tensor, on_true: &Tensor) -> Result<Tensor> {
    let shape = mask.shape();
    let m = mask.where_cond(&on_true.broadcast_as(shape.dims())?, on_false)?;
    Ok(m)
}

/// PATCH (P-padbucket): per-ROW attention mask for near-length padded bucketing,
/// shape `(b_sz, 1, seq_len, kv_len)` where `1` == FORBIDDEN (matches the
/// `build_causal_mask`/`masked_fill` convention — a `1` entry is set to `-inf`
/// before softmax). Broadcast over the head dim in `forward_attn_per_row`.
///
/// For each row `r`:
///   - `real_len[r]` real prefix keys occupy cache columns `0..real_len[r]`; any
///     column `>= real_len[r]` in the ALREADY-CACHED region is a pad/garbage KV
///     entry and is FORBIDDEN for every query of that row.
///   - `q_pos[i]` is the GLOBAL position of query `i` (`prefill_offset + i` for a
///     prefill, or the row's own decode position for a step). A query may attend
///     to a NEW key column `j` (one written THIS call, i.e. `j >= cached_len`)
///     only when that key's global position `<= q_pos[i]` (causality). Because
///     the batch is right-padded, a real query's causal window already excludes
///     the pad columns that sit AFTER it, but a pad-position query (whose output
///     the caller discards) still needs a valid, non-all-masked row to avoid a
///     NaN — so every query is given at least its own diagonal.
///
/// `real_len`:     per-row count of real keys already in the cache region
///                 (`0..cached_len`) — pad columns are those at `[real_len..
///                 cached_len)`.
/// `q_global_pos`: `(b_sz, seq_len)` global position of each query slot (same
///                 tensor `forward_padded` uses for rotary; passed as a slice
///                 here so the mask and rotary can never diverge).
/// `cached_len`:   number of key columns already in the cache BEFORE this call
///                 (0 at prefill; grows each decode step).
/// `seq_len`:      number of NEW key/query columns this call appends.
///
/// The new columns are appended at cache positions `cached_len..cached_len+
/// seq_len`; new key `j` (0-indexed within this call) carries global position
/// `q_global_pos[r][j]` — the same table, since in a decode a row writes exactly
/// one key at its own next position, and in a prefill key `j` == query `j`.
#[allow(clippy::needless_range_loop)]
pub fn build_padded_mask(
    real_len: &[usize],
    q_global_pos: &[Vec<usize>],
    cached_len: usize,
    seq_len: usize,
    device: &Device,
) -> Result<Tensor> {
    let b_sz = real_len.len();
    let kv_len = cached_len + seq_len;
    let mut data: Vec<u8> = Vec::with_capacity(b_sz * seq_len * kv_len);
    for r in 0..b_sz {
        for i in 0..seq_len {
            let qpos = q_global_pos[r][i];
            for j in 0..kv_len {
                let forbidden = if j < cached_len {
                    // Already-cached key column: real iff j < real_len[r].
                    j >= real_len[r]
                } else {
                    // New key written this call: column (j - cached_len) carries
                    // global position q_global_pos[r][j-cached_len]. Causal: a
                    // query may attend to a new key only if that key's global
                    // position is <= the query's own global position.
                    q_global_pos[r][j - cached_len] > qpos
                };
                data.push(u8::from(forbidden));
            }
        }
    }
    Tensor::from_vec(data, (b_sz, 1, seq_len, kv_len), device)
}

impl LayerWeights {
    fn apply_rotary_emb(&self, x: &Tensor, index_pos: usize) -> Result<Tensor> {
        let _enter = self.span_rot.enter();
        let (_b_sz, _n_head, seq_len, _n_embd) = x.dims4()?;
        let cos = self.cos.narrow(0, index_pos, seq_len)?;
        let sin = self.sin.narrow(0, index_pos, seq_len)?;
        // The call to contiguous below is only necessary when processing the prompt.
        // When the seq_len is 1 in the inference loop, this is a no-op.
        let x = x.contiguous()?;
        // PATCH (P-rope, candle 0.11 #3411): dispatch the rotary convention by
        // architecture. NEOX (Qwen/Falcon/Phi/...) pairs i with i+d/2; interleaved
        // (Llama/Mistral) pairs 2i with 2i+1. Llama keeps rope_i -> byte-identical.
        if self.rope_is_neox {
            candle_nn::rotary_emb::rope(&x, &cos, &sin)
        } else {
            candle_nn::rotary_emb::rope_i(&x, &cos, &sin)
        }
    }

    /// PATCH (P-padbucket): per-ROW rotary. Unlike `apply_rotary_emb`'s single
    /// scalar `index_pos` (one position range shared by the whole batch), this
    /// takes a `cos3`/`sin3` pair already gathered to shape `(b_sz, seq_len,
    /// head_dim/2)` — one position PER ROW PER STEP — so a padded near-length
    /// batch can place each row's real tokens at its own true global positions
    /// (right-padded rows continue decode from their own real length, not the
    /// bucket-max). candle's `rope`/`rope_i` natively accept a 3D `(b, t, d)`
    /// cos/sin (`rope_check_cs`), on both the CPU and Metal `CustomOp3` paths, so
    /// this is the SAME kernel — only the position table differs per row.
    ///
    /// DETERMINISM: when every row of `cos3`/`sin3` carries the SAME positions
    /// (i.e. no real padding), `rope_i` over a 3D cos/sin whose rows are literal
    /// copies of the 2D slice is bit-identical to the 2D `apply_rotary_emb` path
    /// — the per-row byte-equality is pinned by `rope_per_row_equals_scalar_when_
    /// positions_uniform`. The 3D cos/sin must be contiguous (candle's rope
    /// requires it); the caller builds it via `index_select` on the precomputed
    /// `self.cos`/`self.sin` tables, which yields a fresh contiguous tensor.
    fn apply_rotary_emb_per_row(&self, x: &Tensor, cos3: &Tensor, sin3: &Tensor) -> Result<Tensor> {
        let _enter = self.span_rot.enter();
        let x = x.contiguous()?;
        if self.rope_is_neox {
            candle_nn::rotary_emb::rope(&x, cos3, sin3)
        } else {
            candle_nn::rotary_emb::rope_i(&x, cos3, sin3)
        }
    }

    fn forward_attn(
        &mut self,
        x: &Tensor,
        mask: Option<&Tensor>,
        index_pos: usize,
        seq_cap: usize,
    ) -> Result<Tensor> {
        let _enter = self.span_attn.enter();
        let (b_sz, seq_len, n_embd) = x.dims3()?;
        let q = self.attention_wq.forward(x)?;
        let k = self.attention_wk.forward(x)?;
        let v = self.attention_wv.forward(x)?;
        // PATCH (P-qkvbias): Qwen2 attention carries q/k/v biases; Llama does not.
        // broadcast_add over the last (projection) dim. None -> untouched, so the
        // Llama path is byte-identical to upstream.
        let q = match &self.attention_bq {
            Some(b) => q.broadcast_add(b)?,
            None => q,
        };
        let k = match &self.attention_bk {
            Some(b) => k.broadcast_add(b)?,
            None => k,
        };
        let v = match &self.attention_bv {
            Some(b) => v.broadcast_add(b)?,
            None => v,
        };

        let q = q
            .reshape((b_sz, seq_len, self.n_head, self.head_dim))?
            .transpose(1, 2)?;
        let k = k
            .reshape((b_sz, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?;
        let v = v
            .reshape((b_sz, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?
            // This call to contiguous ensures that the fast kernel can be called below. It's
            // actually a no-op except when processing the initial prompt so has no significant
            // impact on performance.
            .contiguous()?;

        let q = self.apply_rotary_emb(&q, index_pos)?;
        let k = self.apply_rotary_emb(&k, index_pos)?;

        // A fresh prefill (index_pos == 0) starts a new sequence: drop any
        // stale buffer (a warm model is reused across jobs and possibly at a
        // different batch size) so the append below reallocates. This mirrors
        // the old cat path, where index_pos == 0 ignored the cached tensors.
        if index_pos == 0 {
            self.kv_k.reset_with_cap(seq_cap);
            self.kv_v.reset_with_cap(seq_cap);
        }
        // Append the new keys/values at the running offset via slice_set into a
        // preallocated buffer and read back the live region. Byte-identical to
        // the previous `Tensor::cat(&[cache, new], 2)` (see KvCacheSlot).
        let k = self.kv_k.append(&k)?;
        let v = self.kv_v.append(&v)?;

        let y = if q.device().is_metal() && seq_len == 1 {
            // SDPA will do MQA for us
            candle_nn::ops::sdpa(
                &q,
                &k,
                &v,
                None,
                false,
                1. / (self.head_dim as f32).sqrt(),
                1.,
            )?
        } else {
            // Support for MQA, useful for 70B models and mistral.
            let k = candle_transformers::utils::repeat_kv(k, self.n_head / self.n_kv_head)?;
            let v = candle_transformers::utils::repeat_kv(v, self.n_head / self.n_kv_head)?;

            let att = (q.matmul(&k.t()?)? / (self.head_dim as f64).sqrt())?;
            let att = match mask {
                None => att,
                Some(mask) => {
                    let mask = mask.broadcast_as(att.shape())?;
                    masked_fill(&att, &mask, &self.neg_inf)?
                }
            };
            let att = candle_nn::ops::softmax_last_dim(&att)?;
            // Convert to contiguous as matmul doesn't support strided vs for now.
            att.matmul(&v.contiguous()?)?
        };

        let y = y.transpose(1, 2)?.reshape(&[b_sz, seq_len, n_embd])?;
        let y = self.attention_wo.forward(&y)?;
        Ok(y)
    }

    /// PATCH (P-padbucket): per-ROW attention for near-length padded bucketing.
    /// Identical arithmetic to `forward_attn`'s masked (`else`) branch, with two
    /// deliberate differences the padded batch requires:
    ///
    ///   1. Rotary uses `apply_rotary_emb_per_row` (per-row `cos3`/`sin3`), so a
    ///      right-padded row whose real length is below the bucket max continues
    ///      decode from its OWN true global position, not the bucket-max scalar.
    ///
    ///   2. The mask is ALWAYS applied (never the mask-free Metal SDPA fast path).
    ///      SDPA with `mask=None` attends over EVERY cached key, including the
    ///      pad-position KV entries a padded row carries — so the padded decode
    ///      MUST route through the explicit `(b_sz,1,seq,kv)` per-row mask that
    ///      forbids each real query from attending to its own pad columns (and to
    ///      any other row's — rows never share keys, the batch dim already
    ///      separates them). Bypassing SDPA here is what makes the pad KV inert.
    ///
    /// `fresh` replaces the scalar path's `index_pos == 0` fresh-sequence test:
    /// with per-row positions there is no single "position 0", so the caller
    /// states explicitly whether this call opens a new sequence (prefill, drop the
    /// stale buffer) or continues one (decode, append onto the live cache).
    ///
    /// DETERMINISM: for a batch with NO real padding (every row the same length,
    /// uniform positions, an all-attend mask over exactly the real keys), the
    /// `att = softmax(QKᵀ/√d) · V` computed here is bit-identical to the scalar
    /// `forward_attn`'s `else` branch — the padded columns contribute `exp(-inf)
    /// = 0.0` exactly, and adding `0.0` to the softmax denominator is exact in
    /// IEEE-754, so a real query's output over `[real…, pad→-inf]` equals its
    /// output over `[real…]` alone. Pinned end-to-end (real weights) by
    /// `batch_padded_bucket_equals_serial_mixed_lengths`.
    #[allow(clippy::too_many_arguments)]
    fn forward_attn_per_row(
        &mut self,
        x: &Tensor,
        mask: &Tensor,
        cos3: &Tensor,
        sin3: &Tensor,
        seq_cap: usize,
        fresh: bool,
    ) -> Result<Tensor> {
        let _enter = self.span_attn.enter();
        let (b_sz, seq_len, n_embd) = x.dims3()?;
        let q = self.attention_wq.forward(x)?;
        let k = self.attention_wk.forward(x)?;
        let v = self.attention_wv.forward(x)?;
        let q = match &self.attention_bq {
            Some(b) => q.broadcast_add(b)?,
            None => q,
        };
        let k = match &self.attention_bk {
            Some(b) => k.broadcast_add(b)?,
            None => k,
        };
        let v = match &self.attention_bv {
            Some(b) => v.broadcast_add(b)?,
            None => v,
        };

        let q = q
            .reshape((b_sz, seq_len, self.n_head, self.head_dim))?
            .transpose(1, 2)?;
        let k = k
            .reshape((b_sz, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?;
        let v = v
            .reshape((b_sz, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?
            .contiguous()?;

        let q = self.apply_rotary_emb_per_row(&q, cos3, sin3)?;
        let k = self.apply_rotary_emb_per_row(&k, cos3, sin3)?;

        if fresh {
            self.kv_k.reset_with_cap(seq_cap);
            self.kv_v.reset_with_cap(seq_cap);
        }
        let k = self.kv_k.append(&k)?;
        let v = self.kv_v.append(&v)?;

        // Always masked — never the mask-free SDPA fast path (see doc note #2).
        let k = candle_transformers::utils::repeat_kv(k, self.n_head / self.n_kv_head)?;
        let v = candle_transformers::utils::repeat_kv(v, self.n_head / self.n_kv_head)?;
        let att = (q.matmul(&k.t()?)? / (self.head_dim as f64).sqrt())?;
        let mask = mask.broadcast_as(att.shape())?;
        let att = masked_fill(&att, &mask, &self.neg_inf)?;
        let att = candle_nn::ops::softmax_last_dim(&att)?;
        let y = att.matmul(&v.contiguous()?)?;

        let y = y.transpose(1, 2)?.reshape(&[b_sz, seq_len, n_embd])?;
        let y = self.attention_wo.forward(&y)?;
        Ok(y)
    }

    /// HAWKING lane (docs/HAWKING_PORT_PLAN.md Week 4 — wire a real GGUF through the
    /// continuous-batch kernel). Produce this layer's REAL, RoPE'd, F32 Q/K/V for
    /// ONE decode step of a batch of independent slots, in exactly the memory layout
    /// `hawking_metal_kernel`'s ops require, using this layer's OWN real Q4_K
    /// quantized projections (`attention_wq/wk/wv`) — no synthetic tensors, no
    /// dequant-and-reproject stand-in. This is facet (b) of the Week-3 boundary the
    /// runner names ("the Q4_K quantized projection GEMMs producing real F32 Q/K/V to
    /// feed the kernel") plus facet (a) (per-slot RoPE ahead of the kernel), reusing
    /// the entry-73 per-row rotary primitive (`apply_rotary_emb_per_row`) so a real
    /// right-continuing decode places each slot's token at its OWN absolute position.
    ///
    /// `x`:         `(batch, 1, n_embd)` — one already-normed hidden state per slot.
    /// `cos1/sin1`: `(batch, 1, head_dim/2)` per-row rotary tables gathered at each
    ///              slot's own absolute position (the Q4_K path shares the exact same
    ///              `precomput_freqs_cis` table the serial path uses).
    ///
    /// Returns `(q, k_new, v_new)` where:
    ///   - `q`      is `(batch, n_head, head_dim)`     — `MultiSeqDecodeAttention`'s
    ///              q layout (`q + (batch*n_heads + h)*head_dim`).
    ///   - `k_new`/`v_new` are `(batch, n_kv_head*head_dim)` — `KvScatterAppend`'s
    ///              src layout (one `(n_kv_head, head_dim)` row per slot), K carrying
    ///              RoPE, V raw — identical to what `forward_attn` appends per step.
    ///
    /// DETERMINISM / equivalence: the projection, the optional Qwen2 q/k/v bias, and
    /// the rotary are byte-for-byte the SAME ops `forward_attn`/`forward_attn_per_row`
    /// run — only the downstream attention (the multi-seq tree-softmax kernel vs
    /// candle SDPA) differs, and that difference is the documented, non-byte-exact
    /// batched-reduction-order one the port plan's determinism section already
    /// accounts for (`atol` bounded, argmax-stable — pinned by the real-model gate).
    #[cfg(feature = "metal")]
    fn hawking_project_decode(
        &self,
        x: &Tensor,
        cos1: &Tensor,
        sin1: &Tensor,
    ) -> Result<(Tensor, Tensor, Tensor)> {
        let (batch, seq_len, _n_embd) = x.dims3()?;
        debug_assert_eq!(seq_len, 1, "hawking_project_decode is one token per slot");
        let q = self.attention_wq.forward(x)?;
        let k = self.attention_wk.forward(x)?;
        let v = self.attention_wv.forward(x)?;
        let q = match &self.attention_bq {
            Some(b) => q.broadcast_add(b)?,
            None => q,
        };
        let k = match &self.attention_bk {
            Some(b) => k.broadcast_add(b)?,
            None => k,
        };
        let v = match &self.attention_bv {
            Some(b) => v.broadcast_add(b)?,
            None => v,
        };
        // (batch, 1, n_head, head_dim) -> (batch, n_head, 1, head_dim), same as
        // forward_attn, so apply_rotary_emb_per_row sees the layout it expects.
        let q = q
            .reshape((batch, seq_len, self.n_head, self.head_dim))?
            .transpose(1, 2)?;
        let k = k
            .reshape((batch, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?;
        let v = v
            .reshape((batch, seq_len, self.n_kv_head, self.head_dim))?
            .transpose(1, 2)?
            .contiguous()?;
        let q = self.apply_rotary_emb_per_row(&q, cos1, sin1)?;
        let k = self.apply_rotary_emb_per_row(&k, cos1, sin1)?;
        // q -> (batch, n_head, head_dim); k/v -> (batch, n_kv_head*head_dim), each
        // slot's `(n_kv_head, head_dim)` row contiguous for KvScatterAppend.
        let q = q.squeeze(2)?.contiguous()?;
        let k = k
            .transpose(1, 2)?
            .reshape((batch, self.n_kv_head * self.head_dim))?
            .contiguous()?;
        let v = v
            .transpose(1, 2)?
            .reshape((batch, self.n_kv_head * self.head_dim))?
            .contiguous()?;
        Ok((q, k, v))
    }
}

#[derive(Debug, Clone)]
pub struct ModelWeights {
    tok_embeddings: Embedding,
    layers: Vec<LayerWeights>,
    norm: RmsNorm,
    output: QMatMul,
    /// Mask cache keyed by (seq_len, kv_len).
    /// kv_len = index_pos + seq_len, so the mask is rectangular when prefix
    /// KV cache entries exist (index_pos > 0).
    ///
    /// Bounded to `MASK_CACHE_CAP` entries with insertion-order eviction
    /// (`mask_order` holds the keys oldest-first); a long-lived warm model
    /// otherwise leaks one ~16MB prefill mask per distinct (seq_len, kv_len).
    /// A recomputed mask is bitwise identical, so eviction is determinism-safe.
    masks: HashMap<(usize, usize), Tensor>,
    mask_order: VecDeque<(usize, usize)>,
    /// PATCH (P-rightsize): a one-shot capacity hint for the NEXT fresh sequence
    /// (`forward` call with `index_pos == 0`), set via `set_next_seq_cap` and
    /// consumed (reset to `None`) the moment that `forward` call happens. `None`
    /// means "no hint" — every KV slot allocates at the original, safe
    /// `MAX_SEQ_LEN` worst case, so a caller that never calls
    /// `set_next_seq_cap` sees byte-for-byte the same behavior as before this
    /// patch.
    next_seq_cap: Option<usize>,
    span: tracing::Span,
    span_output: tracing::Span,
}

fn precomput_freqs_cis(
    head_dim: usize,
    freq_base: f32,
    device: &Device,
) -> Result<(Tensor, Tensor)> {
    let theta: Vec<_> = (0..head_dim)
        .step_by(2)
        .map(|i| 1f32 / freq_base.powf(i as f32 / head_dim as f32))
        .collect();
    let theta = Tensor::new(theta.as_slice(), device)?;
    let idx_theta = Tensor::arange(0, MAX_SEQ_LEN as u32, device)?
        .to_dtype(DType::F32)?
        .reshape((MAX_SEQ_LEN, 1))?
        .matmul(&theta.reshape((1, theta.elem_count()))?)?;
    let cos = idx_theta.cos()?;
    let sin = idx_theta.sin()?;
    Ok((cos, sin))
}

/// PATCH (P-rope): NEOX-style rotary architectures (pair index `i` with `i+d/2`), per
/// candle 0.11 PR #3411. Everything not listed (Llama, Mistral, DeepSeek-arch GGUFs) is
/// interleaved (`rope_i`, pairs `2i` with `2i+1`). Keyed on GGUF `general.architecture`;
/// an absent/unknown arch defaults to interleaved, which is the Llama-safe default.
fn is_neox_arch(arch: &str) -> bool {
    matches!(
        arch,
        "qwen"
            | "qwen2"
            | "qwen2moe"
            | "qwen3"
            | "qwen3moe"
            | "falcon"
            | "grok"
            | "dbrx"
            | "phi2"
            | "phi3"
            | "phimoe"
            | "stablelm"
            | "starcoder2"
            | "olmo2"
    )
}

impl ModelWeights {
    pub fn from_ggml(mut ct: ggml_file::Content, gqa: usize) -> Result<Self> {
        let head_dim = (ct.hparams.n_embd / ct.hparams.n_head) as usize;
        let (cos, sin) = precomput_freqs_cis(head_dim, 10000., &ct.device)?;
        let neg_inf = Tensor::new(f32::NEG_INFINITY, &ct.device)?;
        let tok_embeddings = ct.remove("tok_embeddings.weight")?;
        let tok_embeddings = tok_embeddings.dequantize(&ct.device)?;
        let norm = RmsNorm::from_qtensor(ct.remove("norm.weight")?, 1e-5)?;
        let output = ct.remove("output.weight")?;
        let mut layers = Vec::with_capacity(ct.hparams.n_layer as usize);
        for layer_idx in 0..ct.hparams.n_layer {
            let prefix = format!("layers.{layer_idx}");
            let attention_wq = ct.remove(&format!("{prefix}.attention.wq.weight"))?;
            let attention_wk = ct.remove(&format!("{prefix}.attention.wk.weight"))?;
            let attention_wv = ct.remove(&format!("{prefix}.attention.wv.weight"))?;
            let attention_wo = ct.remove(&format!("{prefix}.attention.wo.weight"))?;
            let mlp_or_moe = {
                let feed_forward_w1 = ct.remove(&format!("{prefix}.feed_forward.w1.weight"))?;
                let feed_forward_w2 = ct.remove(&format!("{prefix}.feed_forward.w2.weight"))?;
                let feed_forward_w3 = ct.remove(&format!("{prefix}.feed_forward.w3.weight"))?;
                MlpOrMoe::Mlp(Mlp {
                    feed_forward_w1: QMatMul::from_qtensor(feed_forward_w1)?,
                    feed_forward_w2: QMatMul::from_qtensor(feed_forward_w2)?,
                    feed_forward_w3: QMatMul::from_qtensor(feed_forward_w3)?,
                })
            };
            let attention_norm = ct.remove(&format!("{prefix}.attention_norm.weight"))?;
            let ffn_norm = ct.remove(&format!("{prefix}.ffn_norm.weight"))?;
            let span_attn = tracing::span!(tracing::Level::TRACE, "attn");
            let span_rot = tracing::span!(tracing::Level::TRACE, "attn-rot");
            let span_mlp = tracing::span!(tracing::Level::TRACE, "attn-mlp");
            layers.push(LayerWeights {
                attention_wq: QMatMul::from_qtensor(attention_wq)?,
                attention_wk: QMatMul::from_qtensor(attention_wk)?,
                attention_wv: QMatMul::from_qtensor(attention_wv)?,
                attention_wo: QMatMul::from_qtensor(attention_wo)?,
                attention_norm: RmsNorm::from_qtensor(attention_norm, 1e-5)?,
                mlp_or_moe,
                ffn_norm: RmsNorm::from_qtensor(ffn_norm, 1e-5)?,
                n_head: ct.hparams.n_head as usize,
                n_kv_head: ct.hparams.n_head as usize / gqa,
                head_dim: (ct.hparams.n_embd / ct.hparams.n_head) as usize,
                cos: cos.clone(),
                sin: sin.clone(),
                neg_inf: neg_inf.clone(),
                // Legacy GGML format is Llama-arch only: no biases, interleaved rope.
                attention_bq: None,
                attention_bk: None,
                attention_bv: None,
                rope_is_neox: false,
                kv_k: KvCacheSlot::new(),
                kv_v: KvCacheSlot::new(),
                span_attn,
                span_rot,
                span_mlp,
            })
        }
        let span = tracing::span!(tracing::Level::TRACE, "model");
        let span_output = tracing::span!(tracing::Level::TRACE, "output");
        Ok(Self {
            tok_embeddings: Embedding::new(tok_embeddings, ct.hparams.n_embd as usize),
            layers,
            norm,
            output: QMatMul::from_qtensor(output)?,
            masks: HashMap::new(),
            mask_order: VecDeque::new(),
            next_seq_cap: None,
            span,
            span_output,
        })
    }

    pub fn from_gguf<R: std::io::Seek + std::io::Read>(
        ct: gguf_file::Content,
        reader: &mut R,
        device: &Device,
    ) -> Result<Self> {
        let md_get = |s: &str| match ct.metadata.get(s) {
            None => candle_core::bail!("cannot find {s} in metadata"),
            Some(v) => Ok(v),
        };

        // PATCH (P-arch, candle 0.11 #3411): GGUF metadata keys are prefixed by the
        // model architecture, NOT hardcoded "llama." — official Qwen2 GGUFs use
        // "qwen2.*". Read `general.architecture` and prefix every dimension key with
        // it; default "llama" so a Llama GGUF (or one without the field) reads exactly
        // the same keys as before -> byte-identical. The rope convention (P-rope) and
        // the optional q/k/v bias (P-qkvbias) follow from the same architecture.
        let arch = ct
            .metadata
            .get("general.architecture")
            .and_then(|v| v.to_string().ok())
            .map(|s| s.to_string())
            .unwrap_or_else(|| "llama".to_string());
        let rope_is_neox = is_neox_arch(&arch);
        let mdk = |k: &str| format!("{arch}.{k}");

        // Parameter extraction from metadata.
        let n_expert = md_get(&mdk("expert_count"))
            .and_then(|v| v.to_u32())
            .unwrap_or(0) as usize;
        let n_expert_used = md_get(&mdk("expert_used_count"))
            .and_then(|v| v.to_u32())
            .unwrap_or(0) as usize;
        let head_count = md_get(&mdk("attention.head_count"))?.to_u32()? as usize;
        let head_count_kv = md_get(&mdk("attention.head_count_kv"))?.to_u32()? as usize;
        let block_count = md_get(&mdk("block_count"))?.to_u32()? as usize;
        let embedding_length = md_get(&mdk("embedding_length"))?.to_u32()? as usize;
        // PATCH (P-arch): Qwen2 GGUFs omit `rope.dimension_count` (rotary spans the full
        // head_dim); Llama always sets it. Fall back to head_dim when absent so both load.
        // For Llama the key is present and equals head_dim, so this is byte-identical.
        let rope_dim = md_get(&mdk("rope.dimension_count"))
            .and_then(|v| v.to_u32())
            .map(|d| d as usize)
            .unwrap_or(embedding_length / head_count);
        // Strangely this value is generally 1e-6 in GGUF file but used to be 1e-5 by default.
        let rms_norm_eps = md_get(&mdk("attention.layer_norm_rms_epsilon"))?.to_f32()? as f64;

        let rope_freq_base = md_get(&mdk("rope.freq_base"))
            .and_then(|m| m.to_f32())
            .unwrap_or(10000f32);
        let (cos, sin) = precomput_freqs_cis(rope_dim, rope_freq_base, device)?;
        let neg_inf = Tensor::new(f32::NEG_INFINITY, device)?;

        let tok_embeddings_q = ct.tensor(reader, "token_embd.weight", device)?;
        let tok_embeddings = tok_embeddings_q.dequantize(device)?;
        let norm = RmsNorm::from_qtensor(
            ct.tensor(reader, "output_norm.weight", device)?,
            rms_norm_eps,
        )?;
        let output = match ct.tensor(reader, "output.weight", device) {
            Ok(tensor) => tensor,
            Err(_) => tok_embeddings_q,
        };
        let mut layers = Vec::with_capacity(block_count);
        for layer_idx in 0..block_count {
            let prefix = format!("blk.{layer_idx}");
            let attention_wq = ct.tensor(reader, &format!("{prefix}.attn_q.weight"), device)?;
            let attention_wk = ct.tensor(reader, &format!("{prefix}.attn_k.weight"), device)?;
            let attention_wv = ct.tensor(reader, &format!("{prefix}.attn_v.weight"), device)?;
            let attention_wo =
                ct.tensor(reader, &format!("{prefix}.attn_output.weight"), device)?;
            // PATCH (P-qkvbias): load optional q/k/v biases (dequantized to the device).
            // Qwen2 has them; Llama does not (absent -> None -> bias-free, byte-identical).
            // A missing tensor returns Err without touching the reader, so the fallback
            // is safe. attn_output has no bias in Qwen2, so it is not loaded.
            let attention_bq = match ct.tensor(reader, &format!("{prefix}.attn_q.bias"), device) {
                Ok(t) => Some(t.dequantize(device)?),
                Err(_) => None,
            };
            let attention_bk = match ct.tensor(reader, &format!("{prefix}.attn_k.bias"), device) {
                Ok(t) => Some(t.dequantize(device)?),
                Err(_) => None,
            };
            let attention_bv = match ct.tensor(reader, &format!("{prefix}.attn_v.bias"), device) {
                Ok(t) => Some(t.dequantize(device)?),
                Err(_) => None,
            };
            let mlp_or_moe = if n_expert <= 1 {
                let feed_forward_w1 =
                    ct.tensor(reader, &format!("{prefix}.ffn_gate.weight"), device)?;
                let feed_forward_w2 =
                    ct.tensor(reader, &format!("{prefix}.ffn_down.weight"), device)?;
                let feed_forward_w3 =
                    ct.tensor(reader, &format!("{prefix}.ffn_up.weight"), device)?;
                MlpOrMoe::Mlp(Mlp {
                    feed_forward_w1: QMatMul::from_qtensor(feed_forward_w1)?,
                    feed_forward_w2: QMatMul::from_qtensor(feed_forward_w2)?,
                    feed_forward_w3: QMatMul::from_qtensor(feed_forward_w3)?,
                })
            } else {
                let feed_forward_gate_inp =
                    ct.tensor(reader, &format!("{prefix}.ffn_gate_inp.weight"), device)?;
                let mut experts = Vec::with_capacity(n_expert);
                for i in 0..n_expert {
                    let feed_forward_w1 =
                        ct.tensor(reader, &format!("{prefix}.ffn_gate.{i}.weight"), device)?;
                    let feed_forward_w2 =
                        ct.tensor(reader, &format!("{prefix}.ffn_down.{i}.weight"), device)?;
                    let feed_forward_w3 =
                        ct.tensor(reader, &format!("{prefix}.ffn_up.{i}.weight"), device)?;
                    experts.push(Mlp {
                        feed_forward_w1: QMatMul::from_qtensor(feed_forward_w1)?,
                        feed_forward_w2: QMatMul::from_qtensor(feed_forward_w2)?,
                        feed_forward_w3: QMatMul::from_qtensor(feed_forward_w3)?,
                    })
                }
                MlpOrMoe::MoE {
                    n_expert_used,
                    feed_forward_gate_inp: QMatMul::from_qtensor(feed_forward_gate_inp)?,
                    experts,
                }
            };
            let attention_norm =
                ct.tensor(reader, &format!("{prefix}.attn_norm.weight"), device)?;
            let ffn_norm = ct.tensor(reader, &format!("{prefix}.ffn_norm.weight"), device)?;
            let span_attn = tracing::span!(tracing::Level::TRACE, "attn");
            let span_rot = tracing::span!(tracing::Level::TRACE, "attn-rot");
            let span_mlp = tracing::span!(tracing::Level::TRACE, "attn-mlp");
            layers.push(LayerWeights {
                attention_wq: QMatMul::from_qtensor(attention_wq)?,
                attention_wk: QMatMul::from_qtensor(attention_wk)?,
                attention_wv: QMatMul::from_qtensor(attention_wv)?,
                attention_wo: QMatMul::from_qtensor(attention_wo)?,
                attention_norm: RmsNorm::from_qtensor(attention_norm, rms_norm_eps)?,
                mlp_or_moe,
                ffn_norm: RmsNorm::from_qtensor(ffn_norm, rms_norm_eps)?,
                n_head: head_count,
                n_kv_head: head_count_kv,
                head_dim: embedding_length / head_count,
                cos: cos.clone(),
                sin: sin.clone(),
                neg_inf: neg_inf.clone(),
                attention_bq,
                attention_bk,
                attention_bv,
                rope_is_neox,
                kv_k: KvCacheSlot::new(),
                kv_v: KvCacheSlot::new(),
                span_attn,
                span_rot,
                span_mlp,
            })
        }
        let span = tracing::span!(tracing::Level::TRACE, "model");
        let span_output = tracing::span!(tracing::Level::TRACE, "output");
        Ok(Self {
            tok_embeddings: Embedding::new(tok_embeddings, embedding_length),
            layers,
            norm,
            output: QMatMul::from_qtensor(output)?,
            masks: HashMap::new(),
            mask_order: VecDeque::new(),
            next_seq_cap: None,
            span,
            span_output,
        })
    }

    /// Build a causal attention mask of shape `(seq_len, kv_len)` where
    /// `kv_len = index_pos + seq_len`.
    ///
    /// When `index_pos == 0` the mask is square `(seq_len, seq_len)` — the
    /// classic case with an empty KV cache.
    ///
    /// When `index_pos > 0` the KV cache already holds `index_pos` entries from
    /// a previously fed prefix.  The mask becomes rectangular: the first
    /// `index_pos` columns are all 0 (every query attends to every prefix key)
    /// and the remaining `seq_len` columns form the standard causal triangle
    /// (query at global position `index_pos + i` cannot attend to keys at global
    /// positions `> index_pos + i`).
    ///
    /// # Shape example  (index_pos=65, seq_len=4)
    /// ```text
    ///              kv 0..64 (prefix)   kv 65  kv 66  kv 67  kv 68
    /// query 65:       0  0 … 0           0      1      1      1
    /// query 66:       0  0 … 0           0      0      1      1
    /// query 67:       0  0 … 0           0      0      0      1
    /// query 68:       0  0 … 0           0      0      0      0
    /// ```
    fn mask(&mut self, seq_len: usize, index_pos: usize, device: &Device) -> Result<Tensor> {
        let kv_len = index_pos + seq_len;
        let key = (seq_len, kv_len);
        if let Some(mask) = self.masks.get(&key) {
            Ok(mask.clone())
        } else {
            let mask = candle_transformers::utils::build_causal_mask(seq_len, index_pos, device)?;
            // Insertion-order eviction: cap the cache at MASK_CACHE_CAP entries,
            // dropping the oldest key when full. Recomputing an evicted mask
            // yields a bitwise-identical tensor, so this never alters outputs.
            while self.mask_order.len() >= MASK_CACHE_CAP {
                if let Some(old) = self.mask_order.pop_front() {
                    self.masks.remove(&old);
                } else {
                    break;
                }
            }
            self.masks.insert(key, mask.clone());
            self.mask_order.push_back(key);
            Ok(mask)
        }
    }

    /// Drop finished rows from every layer's per-batch KV cache, keeping only
    /// the batch rows named by `keep` (indices into the CURRENT cached batch
    /// ordering, ascending). The cache buffers are shaped `(b_sz, n_kv_head,
    /// MAX_SEQ_LEN, head_dim)` with the live region in `[.., .., 0..cur_len,
    /// ..]`, so we `index_select` the live region along dim 0 and re-seat it
    /// as a fresh tightly-batched buffer (see `KvCacheSlot::compact`).
    ///
    /// Determinism note: `index_select` copies the kept rows verbatim · the
    /// retained sequences' keys/values are bitwise unchanged, and all surviving
    /// rows still share one `index_pos` (they stepped in lockstep from the same
    /// start), so positions stay aligned. The subsequent `(keep.len(), 1)`
    /// forward produces byte-identical logits for those rows.
    pub fn compact_kv_cache(&mut self, keep: &[usize]) -> Result<()> {
        if keep.is_empty() {
            return Ok(());
        }
        let device = self.tok_embeddings.embeddings().device();
        let idx = Tensor::from_vec(
            keep.iter().map(|&i| i as u32).collect::<Vec<u32>>(),
            keep.len(),
            device,
        )?;
        for layer in self.layers.iter_mut() {
            layer.kv_k.compact(&idx)?;
            layer.kv_v.compact(&idx)?;
        }
        Ok(())
    }

    /// Snapshot every layer's live KV region (the prefix-KV-sharing lever in
    /// docs/PERF_AND_CAPABILITY_AUDIT.md, Wave 1 B). Capture the KV state after
    /// a shared-prefix prefill ONCE, then `restore_kv_cache` it per item so the
    /// shared instruction+labels/schema prefill is computed a single time for the
    /// whole batch instead of once per item. Returns one `(k, v, len)` triple per
    /// layer; the lengths are all equal (the shared prefix length).
    ///
    /// DETERMINISM: a snapshot is a deep contiguous copy of the live region, and
    /// `restore_kv_cache` writes it back verbatim, so a per-item forward that
    /// continues from a restored prefix sees byte-identical KV to a forward that
    /// prefilled the prefix inline. Output stays token-identical to serial — pinned
    /// by `prefix_shared_prefill_matches_inline` and the batched==serial gate.
    pub fn snapshot_kv_cache(&self) -> Result<Vec<KvSnapshot>> {
        let mut out = Vec::with_capacity(self.layers.len());
        for layer in self.layers.iter() {
            let k = layer.kv_k.snapshot()?;
            let v = layer.kv_v.snapshot()?;
            out.push(KvSnapshot { k, v });
        }
        Ok(out)
    }

    /// Restore every layer's KV cache from a `snapshot_kv_cache` result, re-seating
    /// the shared prefix so the next per-item `append` continues from it. The caller
    /// passes the prefix length back to `forward` as `index_pos` so rotary/mask use
    /// the right global positions. See `snapshot_kv_cache` for the determinism note.
    pub fn restore_kv_cache(&mut self, snapshot: &[KvSnapshot]) -> Result<()> {
        if snapshot.len() != self.layers.len() {
            candle_core::bail!(
                "restore_kv_cache: snapshot has {} layers, model has {}",
                snapshot.len(),
                self.layers.len()
            );
        }
        for (layer, snap) in self.layers.iter_mut().zip(snapshot.iter()) {
            match (&snap.k, &snap.v) {
                (Some(k), Some(v)) => {
                    layer.kv_k.restore(k)?;
                    layer.kv_v.restore(v)?;
                }
                // An empty snapshot (no prefix prefilled) resets the slots so the
                // next append reallocates — same as a fresh `index_pos == 0`.
                _ => {
                    layer.kv_k.reset();
                    layer.kv_v.reset();
                }
            }
        }
        Ok(())
    }

    /// Restore every layer's KV cache from a `snapshot_kv_cache` result BROADCAST
    /// to `b_sz` batch rows — the batched-remainder-decode lever (docs/internal/
    /// CREED_AND_PATH_TO_TEN.md "Inference hot path" 7→7.5 / "Batching efficiency"
    /// 6.5→7). Forks the ONE shared-prefix prefill into a `(b_sz, ...)` KV cache in
    /// a single call, so the caller can run its existing bucketed batched-decode
    /// loop (the same active-set-shrink machinery `generate_batch` already uses)
    /// over the whole bucket instead of restoring + decoding one row at a time.
    ///
    /// Requires every snapshotted layer to carry a real (batch-dim-1) prefix — an
    /// entirely-empty snapshot (no prefix ever prefilled) has no batch dim to
    /// broadcast FROM, so that case is rejected rather than silently guessed; the
    /// shared-prefix caller only ever calls this after a real prefix prefill.
    /// See `KvCacheSlot::restore_broadcast` for the determinism argument.
    pub fn restore_kv_cache_broadcast(&mut self, snapshot: &[KvSnapshot], b_sz: usize) -> Result<()> {
        if snapshot.len() != self.layers.len() {
            candle_core::bail!(
                "restore_kv_cache_broadcast: snapshot has {} layers, model has {}",
                snapshot.len(),
                self.layers.len()
            );
        }
        for (layer, snap) in self.layers.iter_mut().zip(snapshot.iter()) {
            match (&snap.k, &snap.v) {
                (Some(k), Some(v)) => {
                    layer.kv_k.restore_broadcast(k, b_sz)?;
                    layer.kv_v.restore_broadcast(v, b_sz)?;
                }
                _ => candle_core::bail!(
                    "restore_kv_cache_broadcast: snapshot has no prefix to broadcast from"
                ),
            }
        }
        Ok(())
    }

    /// PATCH (P-rightsize): set the sequence-dim capacity the NEXT fresh
    /// generation's KV cache should preallocate at, instead of always the
    /// worst-case `MAX_SEQ_LEN` (4096). Call this with
    /// `Some((prompt_len + max_tokens).min(MAX_SEQ_LEN))` immediately before the
    /// first `forward(prompt, 0)` call of a new sequence. Consumed (reset to
    /// `None`) by that very call — every following `forward(_, index_pos > 0)`
    /// decode step for the same sequence just appends into the buffer already
    /// allocated, and any FUTURE sequence needs its own fresh call. Passing
    /// `None` (or never calling this) preserves the original always-`MAX_SEQ_LEN`
    /// behavior exactly.
    pub fn set_next_seq_cap(&mut self, cap: Option<usize>) {
        self.next_seq_cap = cap;
    }

    /// Real per-row, per-token KV cache byte cost for THIS loaded model: K and V,
    /// one f32 entry per (layer, kv_head, head_dim) — the exact per-token growth
    /// `KvCacheSlot::append` allocates in `forward_attn` below. Computed from the
    /// model's own real dimensions (never a guessed constant), so a caller can
    /// size a batch WIDTH to available memory instead of only the per-row KV
    /// LENGTH cap `set_next_seq_cap` already bounds (Memory Management & Dynamic
    /// Throttling 6->7, docs/internal/CREED_AND_PATH_TO_TEN.md — "closes the
    /// single most credible OOM vector": a bucket of many same-length prompts has
    /// no width cap today, only a per-row length cap).
    pub fn kv_bytes_per_token_per_row(&self) -> usize {
        let n_layers = self.layers.len();
        let (n_kv_head, head_dim) = self
            .layers
            .first()
            .map(|l| (l.n_kv_head, l.head_dim))
            .unwrap_or((0, 0));
        2 * n_layers * n_kv_head * head_dim * std::mem::size_of::<f32>()
    }

    pub fn forward(&mut self, x: &Tensor, index_pos: usize) -> Result<Tensor> {
        let (_b_sz, seq_len) = x.dims2()?;
        // PATCH (P-ctxbound, Workload & Model Breadth 6→7, docs/internal/
        // CREED_AND_PATH_TO_TEN.md "lift the context ceiling with a real bounds
        // check" / docs/CANDLE_EXPANSION_RESEARCH.md L4 "KvCacheSlot bounds/grow
        // guard"): before this check existed, a sequence that outgrew
        // MAX_SEQ_LEN failed OPAQUELY — the rotary table's `cos.narrow(0,
        // index_pos, seq_len)` in `apply_rotary_emb` (the first thing that
        // actually touches a position past the precomputed `0..MAX_SEQ_LEN`
        // table) raises a generic candle "narrow invalid args" error with no
        // indication this was a context-length problem, and `KvCacheSlot::
        // append`'s own `.min(MAX_SEQ_LEN)` silently truncated the allocation
        // rather than ever raising itself. This check runs FIRST and fails with
        // an explicit, greppable message naming the actual token counts and the
        // real ceiling — never a silent truncation, never an opaque downstream
        // panic/error. `agent/src/runners.rs` maps this into a typed
        // `RunError::Inference` (surfaced to the buyer verbatim via the
        // existing failure-classification path), so a job whose input is
        // simply too long for this model gets a clear, actionable reason
        // instead of a stack of unrelated tensor-shape errors.
        if index_pos + seq_len > MAX_SEQ_LEN {
            candle_core::bail!(
                "context length exceeded: index_pos={index_pos} + seq_len={seq_len} = {} \
                 tokens, which is beyond this model's MAX_SEQ_LEN={MAX_SEQ_LEN} ceiling — \
                 shorten the prompt/max_tokens or use a job type/model with a larger context window",
                index_pos + seq_len
            );
        }
        let mask = if seq_len == 1 {
            None
        } else {
            Some(self.mask(seq_len, index_pos, x.device())?)
        };
        // Only meaningful on a fresh sequence (index_pos == 0, where
        // forward_attn's reset branch actually reads it); consumed exactly once
        // regardless, so a stale hint can never leak into a later, unrelated
        // sequence that forgot to set its own.
        let seq_cap = self.next_seq_cap.take().unwrap_or(MAX_SEQ_LEN);
        let _enter = self.span.enter();
        let mut layer_in = self.tok_embeddings.forward(x)?;
        for layer in self.layers.iter_mut() {
            let x = layer_in;
            let residual = &x;
            let x = layer.attention_norm.forward(&x)?;
            let attn = layer.forward_attn(&x, mask.as_ref(), index_pos, seq_cap)?;
            let x = (attn + residual)?;

            // MLP
            let _enter = layer.span_mlp.enter();
            let residual = &x;
            let x = layer.ffn_norm.forward(&x)?;
            let x = layer.mlp_or_moe.forward(&x)?;
            let x = (x + residual)?;
            layer_in = x
        }
        let x = self.norm.forward(&layer_in)?;
        let x = x.i((.., seq_len - 1, ..))?.contiguous()?; // PATCH: contiguous so bsz>1 batched prefill works (quantized matmul rejects non-contiguous)
        let _enter = self.span_output.enter();
        self.output.forward(&x)
    }

    /// PATCH (P-padbucket, Inference Hot Path 7.5→8 / Batching Efficiency 7→7.5,
    /// docs/internal/CREED_AND_PATH_TO_TEN.md "Near-length bucketing with padded
    /// prefill"). The per-ROW forward that lets a batch of DIFFERENT-length prompts
    /// (right-padded to a common bucket width) decode together instead of
    /// collapsing to batch-of-1 serial on real, unique-length traffic — the fix
    /// the scalar `forward`'s `index_pos`/2D-mask design could not express (one
    /// scalar position and one `(seq,kv)` mask shared by the whole batch).
    ///
    /// `x`:          `(b_sz, seq_len)` token ids (right-padded rows are filled with
    ///               any valid token id past their real length — the mask makes it
    ///               inert, so the pad token's identity never reaches a real output).
    /// `positions`:  `(b_sz, seq_len)` u32 GLOBAL position of every (row, step)
    ///               slot. A right-padded prefill row of real length `L` uses
    ///               `0..L` then arbitrary values for its pad slots (masked out); a
    ///               decode step uses each row's own next real position `L + t`.
    /// `mask`:       `(b_sz, 1, seq_len, kv_len)`, `1` == FORBIDDEN. Excludes every
    ///               row's pad columns and enforces per-row causality; built by
    ///               `build_padded_mask`.
    /// `fresh`:      `true` on the prefill call (opens a new sequence, drops the
    ///               warm KV buffer), `false` on each decode step.
    /// `seq_cap`:    KV preallocation cap for a fresh sequence (P-rightsize); read
    ///               only when `fresh` (ignored on decode, same as the scalar path).
    ///
    /// Returns FULL logits `(b_sz, seq_len, vocab)` — NOT the scalar path's global
    /// last-position slice, because a right-padded row's real last token sits at
    /// its own `L-1`, not the bucket-max `seq_len-1`. The caller
    /// (`generate_batch`'s padded arm) gathers each row's real last position. On a
    /// decode step (`seq_len == 1`) that is just position 0.
    ///
    /// DETERMINISM: see `forward_attn_per_row`. With uniform positions + an
    /// all-real (no-pad) mask this is bit-identical to the scalar batched path;
    /// pinned end-to-end on real weights by
    /// `batch_padded_bucket_equals_serial_mixed_lengths`.
    pub fn forward_padded(
        &mut self,
        x: &Tensor,
        positions: &Tensor,
        mask: &Tensor,
        fresh: bool,
        seq_cap: usize,
    ) -> Result<Tensor> {
        let (_b_sz, _seq_len) = x.dims2()?;
        // Same P-ctxbound guard as `forward`: the rotary table only covers
        // `0..MAX_SEQ_LEN`, so a position past it would panic opaquely inside
        // `index_select`. Check the true maximum global position any row reaches.
        let max_pos = positions
            .flatten_all()?
            .max(0)?
            .to_scalar::<u32>()? as usize;
        if max_pos >= MAX_SEQ_LEN {
            candle_core::bail!(
                "context length exceeded: max per-row position {max_pos} reaches this model's \
                 MAX_SEQ_LEN={MAX_SEQ_LEN} ceiling — shorten the prompt/max_tokens or use a \
                 job type/model with a larger context window"
            );
        }
        let (cos3, sin3) = self.build_per_row_cos_sin(positions)?;
        let seq_cap = if fresh {
            self.next_seq_cap.take().unwrap_or(seq_cap.min(MAX_SEQ_LEN))
        } else {
            // Consume any stale hint so it can never leak into a later fresh call.
            self.next_seq_cap.take();
            MAX_SEQ_LEN
        };
        let _enter = self.span.enter();
        let mut layer_in = self.tok_embeddings.forward(x)?;
        for layer in self.layers.iter_mut() {
            let x = layer_in;
            let residual = &x;
            let x = layer.attention_norm.forward(&x)?;
            let attn = layer.forward_attn_per_row(&x, mask, &cos3, &sin3, seq_cap, fresh)?;
            let x = (attn + residual)?;

            let _enter = layer.span_mlp.enter();
            let residual = &x;
            let x = layer.ffn_norm.forward(&x)?;
            let x = layer.mlp_or_moe.forward(&x)?;
            let x = (x + residual)?;
            layer_in = x
        }
        let x = self.norm.forward(&layer_in)?.contiguous()?;
        let _enter = self.span_output.enter();
        // Full-sequence logits: the caller gathers each row's REAL last position.
        self.output.forward(&x)
    }

    /// PATCH (P-padbucket): gather the precomputed rotary tables at `positions`
    /// `(b_sz, seq_len)` into per-row `cos3`/`sin3` of shape `(b_sz, seq_len,
    /// head_dim/2)` that candle's `rope`/`rope_i` accept natively (`rope_check_cs`
    /// takes a 3D `(b, t, d)` cos/sin). `index_select` on a flattened
    /// `(b_sz*seq_len)` index yields a fresh CONTIGUOUS tensor (rope requires it).
    ///
    /// DETERMINISM: `index_select` copies table rows verbatim, so a slot at global
    /// position `p` gets exactly `self.cos[p]` — bit-identical to the scalar
    /// `apply_rotary_emb`'s `self.cos.narrow(0, index_pos, seq_len)` for a row
    /// whose positions are the contiguous `index_pos..index_pos+seq_len`. Pinned
    /// by `rope_per_row_equals_scalar_when_positions_uniform`.
    fn build_per_row_cos_sin(&self, positions: &Tensor) -> Result<(Tensor, Tensor)> {
        let (b_sz, seq_len) = positions.dims2()?;
        // Every layer holds a `.clone()` of the SAME precomputed rotary table
        // (`precomput_freqs_cis`, built once at load), so layer 0's is the model's
        // table. `.cos`'s last dim is head_dim/2 (theta stepped by 2).
        let layer0 = self
            .layers
            .first()
            .ok_or_else(|| candle_core::Error::Msg("no layers".into()))?;
        let half = layer0.cos.dim(1)?;
        let flat = positions.flatten_all()?.contiguous()?;
        let cos = layer0.cos.index_select(&flat, 0)?; // (b*seq, half)
        let sin = layer0.sin.index_select(&flat, 0)?;
        let cos3 = cos.reshape((b_sz, seq_len, half))?.contiguous()?;
        let sin3 = sin.reshape((b_sz, seq_len, half))?.contiguous()?;
        Ok((cos3, sin3))
    }

    /// HAWKING lane (docs/HAWKING_PORT_PLAN.md Week 4 — the capstone: drive a REAL
    /// GGUF model through the continuous-batch kernel). ONE decode step for a batch
    /// of `slots.len()` INDEPENDENT sequences that share one forward pass, using this
    /// model's own real Q4_K projections + per-slot RoPE (`hawking_project_decode`)
    /// and the real, Metal-hardware-proven `hawking_metal_kernel` ops — NOT the
    /// serial per-model single-contiguous `KvCacheSlot`, but the FLAT, slot-strided,
    /// multi-region KV layout the kernel's addressing requires
    /// (`HawkingKvCache`, one region per stable slot id). This is the model-integration
    /// rewrite the Week-3 `HawkingRunner::run` boundary named as remaining: facets
    /// (a) per-slot RoPE, (b) real Q4_K → F32 Q/K/V, and (c) the flat multi-region
    /// KV re-layout, wired end-to-end through a real model's every layer.
    ///
    /// `tokens`:   `(batch,)` u32 — the one new token id each slot decodes THIS step
    ///             (the prompt's next token during prefill, or the previous step's
    ///             argmax during generation).
    /// `positions`:`(batch,)` u32 — each slot's ABSOLUTE position for this token
    ///             (its live history length so far); the kernel reads history
    ///             `0..positions[slot]` and this step writes the new K/V AT
    ///             `positions[slot]` before attending, so a slot attends to exactly
    ///             `0..=positions[slot]` (causal).
    /// `cache`:    the persistent per-layer flat KV, one region per slot id (see
    ///             `HawkingKvCache`). Mutated in place (this step's K/V scattered in).
    ///
    /// Returns `(batch, vocab)` logits — one real next-token distribution per slot,
    /// through the real output head. The caller samples/argmaxes (this crate's
    /// `continuous_batch::Scheduler::apply_decode_*`, or a plain argmax loop).
    ///
    /// A slot's `regions[i]` is its STABLE region id (`cache.regions[i]`), decoupled
    /// from the compacted batch index `i` — the core continuous-batching property the
    /// kernel-level `slots_are_independent_across_different_history_lengths` proves and
    /// this lifts up to a real model's full forward pass.
    ///
    /// DETERMINISM: NOT byte-exact with the serial `forward` decode — the multi-seq
    /// tree-softmax kernel reduces in a different order than candle SDPA (the
    /// documented, `atol`-bounded, argmax-stable batched-reduction difference; see
    /// docs/HAWKING_PORT_PLAN.md's determinism re-gating plan). Proven correct at the
    /// model level by `runners::tests`' real-Metal gate: a real Llama-3.2-1B Q4_K_M
    /// GGUF decoded through THIS path produces the SAME greedy tokens as serial
    /// `generate`, and coherent factual completions.
    #[cfg(feature = "metal")]
    pub fn hawking_decode_step(
        &mut self,
        tokens: &Tensor,
        positions: &Tensor,
        cache: &mut HawkingKvCache,
    ) -> Result<Tensor> {
        use crate::hawking_metal_kernel::{KvScatterAppend, MultiSeqDecodeAttention};
        let batch = tokens.dims1()?;
        if positions.dims1()? != batch {
            candle_core::bail!(
                "hawking_decode_step: {batch} tokens but {} positions",
                positions.dims1()?
            );
        }
        if cache.regions.len() != batch {
            candle_core::bail!(
                "hawking_decode_step: {batch} tokens but cache has {} regions",
                cache.regions.len()
            );
        }
        if cache.layers.len() != self.layers.len() {
            candle_core::bail!(
                "hawking_decode_step: cache has {} layers, model has {}",
                cache.layers.len(),
                self.layers.len()
            );
        }
        let pos_u32: Vec<u32> = positions.to_vec1::<u32>()?;
        let max_pos = pos_u32.iter().copied().max().unwrap_or(0) as usize;
        // Same P-ctxbound guard as `forward`/`forward_padded`: the rotary table and
        // the flat KV slot only cover a bounded window.
        if max_pos >= MAX_SEQ_LEN || max_pos >= cache.max_seq_per_slot {
            candle_core::bail!(
                "hawking_decode_step: max position {max_pos} exceeds the KV window \
                 (model MAX_SEQ_LEN={MAX_SEQ_LEN}, cache max_seq_per_slot={})",
                cache.max_seq_per_slot
            );
        }
        let regions = cache.regions.clone();
        let kv_dim = cache.n_kv_heads * cache.head_dim;
        let slot_stride = cache.max_seq_per_slot * kv_dim;
        let scale = 1.0f32 / (cache.head_dim as f32).sqrt();
        let n_head = self.layers.first().map(|l| l.n_head).unwrap_or(0);

        // Per-slot rotary tables at each slot's absolute position: (batch, 1, half).
        let pos2 = positions.reshape((batch, 1))?;
        let (cos1, sin1) = self.build_per_row_cos_sin(&pos2)?;

        // Embed the one new token per slot: (batch,) -> (batch, 1) -> (batch, 1, n_embd).
        let tok2 = tokens.reshape((batch, 1))?;
        let _enter = self.span.enter();
        let mut layer_in = self.tok_embeddings.forward(&tok2)?;

        for (layer, lcache) in self.layers.iter().zip(cache.layers.iter_mut()) {
            let x = layer_in;
            let residual = &x;
            let xn = layer.attention_norm.forward(&x)?;
            // Real Q4_K projection + per-slot RoPE -> F32 Q/K/V for the kernel.
            let (q, k_new, v_new) = layer.hawking_project_decode(&xn, &cos1, &sin1)?;

            // Scatter this step's K/V into this layer's flat cache at each slot's
            // own region/position — the exact index arithmetic
            // `kv_scatter_append_matches_manual_index_arithmetic` proves on real Metal.
            let scatter_k = KvScatterAppend {
                regions: regions.clone(),
                positions: pos_u32.clone(),
                kv_dim,
                slot_stride,
            };
            lcache.k.inplace_op2(&k_new, &scatter_k)?;
            let scatter_v = KvScatterAppend {
                regions: regions.clone(),
                positions: pos_u32.clone(),
                kv_dim,
                slot_stride,
            };
            lcache.v.inplace_op2(&v_new, &scatter_v)?;

            // Multi-seq decode attention: each slot reads its own region/history.
            let op = MultiSeqDecodeAttention {
                positions: pos_u32.clone(),
                regions: regions.clone(),
                n_kv_heads: cache.n_kv_heads,
                kv_slot_stride: slot_stride,
                scale,
            };
            let attn = q.apply_op3_no_bwd(&lcache.k, &lcache.v, &op)?; // (batch, n_head, head_dim)

            // Output projection: (batch, n_head, head_dim) -> (batch, 1, n_embd).
            let attn = attn
                .reshape((batch, 1, n_head * cache.head_dim))?
                .contiguous()?;
            let attn = layer.attention_wo.forward(&attn)?;
            let x = (attn + residual)?;

            // MLP (identical to `forward`).
            let _enter = layer.span_mlp.enter();
            let residual = &x;
            let xn = layer.ffn_norm.forward(&x)?;
            let xm = layer.mlp_or_moe.forward(&xn)?;
            layer_in = (xm + residual)?;
        }
        let x = self.norm.forward(&layer_in)?;
        // (batch, 1, n_embd) -> (batch, n_embd) for the output head.
        let x = x.reshape((batch, x.dim(2)?))?.contiguous()?;
        let _enter = self.span_output.enter();
        self.output.forward(&x) // (batch, vocab)
    }

    /// Allocate the flat, per-layer, slot-strided KV cache `hawking_decode_step`
    /// drives (one region per stable slot id, `max_seq_per_slot` positions each) —
    /// sized from THIS loaded model's own real dimensions (never a guessed constant).
    #[cfg(feature = "metal")]
    pub fn hawking_kv_cache(
        &self,
        regions: Vec<u32>,
        max_seq_per_slot: usize,
    ) -> Result<HawkingKvCache> {
        let device = self.tok_embeddings.embeddings().device().clone();
        let (n_kv_heads, head_dim) = self
            .layers
            .first()
            .map(|l| (l.n_kv_head, l.head_dim))
            .ok_or_else(|| candle_core::Error::Msg("no layers".into()))?;
        let num_regions = regions.iter().copied().max().map(|m| m as usize + 1).unwrap_or(0);
        HawkingKvCache::zeros(
            &device,
            self.layers.len(),
            regions,
            num_regions,
            max_seq_per_slot,
            n_kv_heads,
            head_dim,
        )
    }

    /// HAWKING lane (docs/HAWKING_PORT_PLAN.md Week 5 — continuous-batch CHURN).
    /// Allocate the flat KV cache for a FIXED CEILING of `num_regions` stable slots
    /// (the churn pool), independent of how many are active at any one step. Unlike
    /// `hawking_kv_cache`, which sizes the buffer to exactly the initial region list,
    /// this reserves every region up front so the active set can grow and shrink
    /// (admission/retirement) over the pool WITHOUT reallocating the buffer or moving
    /// any live slot's KV — the region-reuse property continuous batching depends on.
    /// The cache starts with an EMPTY active region list; the driver calls
    /// `set_regions` before each step. `num_regions` is the scheduler's
    /// `max_batch_size`. Sized from THIS model's own real dims (never a guessed
    /// constant).
    #[cfg(feature = "metal")]
    pub fn hawking_kv_cache_pool(
        &self,
        num_regions: usize,
        max_seq_per_slot: usize,
    ) -> Result<HawkingKvCache> {
        let device = self.tok_embeddings.embeddings().device().clone();
        let (n_kv_heads, head_dim) = self
            .layers
            .first()
            .map(|l| (l.n_kv_head, l.head_dim))
            .ok_or_else(|| candle_core::Error::Msg("no layers".into()))?;
        HawkingKvCache::zeros(
            &device,
            self.layers.len(),
            Vec::new(), // empty active set; the driver sets it per step via set_regions
            num_regions,
            max_seq_per_slot,
            n_kv_heads,
            head_dim,
        )
    }
}

/// HAWKING lane: the persistent, PER-LAYER, flat slot-strided KV cache a real GGUF
/// decode loop owns — the (c) piece of the Week-3 boundary (replacing
/// `LayerWeights`'s private single-contiguous `KvCacheSlot` with the flat
/// multi-region buffer `hawking_metal_kernel`'s ops address). Each layer holds its
/// own `(num_regions * max_seq_per_slot, n_kv_heads, head_dim)` K and V buffer,
/// exactly the layout `MultiSeqDecodeAttention`/`KvScatterAppend` require. Regions
/// are STABLE slot ids (decoupled from the compacted batch index) — a slot keeps its
/// KV region as the ready set churns, the core continuous-batching property.
///
/// This is a SEPARATE cache from `LayerWeights::kv_k/kv_v`: the default Candle path
/// and every existing determinism gate use the private per-layer `KvCacheSlot`
/// untouched; this flat cache is only ever built and driven by the opt-in Hawking
/// path (`hawking_decode_step`), so no existing byte-exact path changes.
#[cfg(feature = "metal")]
pub struct HawkingKvCache {
    /// One flat `(num_regions*max_seq_per_slot, n_kv_heads, head_dim)` K/V pair per
    /// model layer.
    layers: Vec<HawkingLayerKv>,
    /// Stable region id per slot (index i is the compacted batch position i).
    regions: Vec<u32>,
    n_kv_heads: usize,
    head_dim: usize,
    max_seq_per_slot: usize,
}

#[cfg(feature = "metal")]
struct HawkingLayerKv {
    k: Tensor,
    v: Tensor,
}

#[cfg(feature = "metal")]
impl HawkingKvCache {
    #[allow(clippy::too_many_arguments)]
    fn zeros(
        device: &Device,
        n_layers: usize,
        regions: Vec<u32>,
        num_regions: usize,
        max_seq_per_slot: usize,
        n_kv_heads: usize,
        head_dim: usize,
    ) -> Result<Self> {
        let shape = (num_regions * max_seq_per_slot, n_kv_heads, head_dim);
        let mut layers = Vec::with_capacity(n_layers);
        for _ in 0..n_layers {
            layers.push(HawkingLayerKv {
                k: Tensor::zeros(shape, DType::F32, device)?,
                v: Tensor::zeros(shape, DType::F32, device)?,
            });
        }
        Ok(Self {
            layers,
            regions,
            n_kv_heads,
            head_dim,
            max_seq_per_slot,
        })
    }

    /// The stable region ids this cache was built for (one per slot, batch order).
    pub fn regions(&self) -> &[u32] {
        &self.regions
    }

    /// HAWKING lane (docs/HAWKING_PORT_PLAN.md Week 5 — continuous-batch CHURN). Set
    /// the compacted active-region list for the NEXT `hawking_decode_step`. This is
    /// the churn primitive: the flat per-layer KV buffer is `num_regions *
    /// max_seq_per_slot` and every op addresses by `region_id * slot_stride`, so a
    /// region that is not in the current list is simply not read or written this step
    /// — its history is preserved untouched while OTHER regions decode. Retiring a
    /// finished slot (drop its id from this list) frees its region for a later
    /// admission WITHOUT reallocating the cache or disturbing any live slot's KV; a
    /// new admission into that region id starts writing at position 0 and overwrites
    /// the stale bytes as it prefills, so region reuse is safe. The default
    /// fixed-cohort `hawking_generate` never calls this (its region list is constant),
    /// so every entry-82 determinism/coherence gate is unaffected.
    ///
    /// The caller MUST pass a set of ids that all fit within the cache's original
    /// `num_regions` (each `< num_regions`); ids are the stable region ids the cache
    /// was allocated over, in the compacted batch order the next step's `tokens` /
    /// `positions` tensors will use.
    pub fn set_regions(&mut self, regions: Vec<u32>) {
        self.regions = regions;
    }

    /// The number of distinct stable regions this cache was ALLOCATED for (the churn
    /// ceiling — every id passed to `set_regions` must be `< num_regions`). Distinct
    /// from `regions().len()`, which is only the count active THIS step.
    pub fn num_regions(&self) -> usize {
        // Each layer's K buffer is (num_regions * max_seq_per_slot, n_kv_heads,
        // head_dim); recover num_regions from that first dimension.
        self.layers
            .first()
            .and_then(|l| l.k.dim(0).ok())
            .map(|rows| rows / self.max_seq_per_slot.max(1))
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::{KvCacheSlot, MAX_SEQ_LEN};
    use candle_core::{Device, IndexOp, Result, Tensor};
    use candle_transformers::utils::build_causal_mask;

    /// PATCH (P-rope / P-arch) regression: the rotary convention is chosen from GGUF
    /// `general.architecture`. Qwen-family + the other NEOX archs select NEOX rope;
    /// Llama/Mistral/unknown/absent stay interleaved (the Llama-safe default), so a
    /// Llama GGUF is byte-identical to upstream. Network-free proof of the dispatch;
    /// full token parity vs llama.cpp needs a real GGUF (see docs/CANDLE_EXPANSION_RESEARCH.md).
    #[test]
    fn neox_arch_selection() {
        for a in [
            "qwen", "qwen2", "qwen2moe", "qwen3", "qwen3moe", "falcon", "phi3", "stablelm",
        ] {
            assert!(super::is_neox_arch(a), "{a} must select NEOX rope");
        }
        for a in ["llama", "mistral", "deepseek2", "", "unknown"] {
            assert!(
                !super::is_neox_arch(a),
                "{a} must stay interleaved (the Llama-safe default)"
            );
        }
    }

    // ── KV-cache preallocation determinism tests ──────────────────────────────

    /// Build a deterministic `(b_sz, n_kv_head, seq_len, head_dim)` tensor whose
    /// every element is a distinct, reproducible value — so a byte-equality
    /// check is meaningful (no accidental all-zero matches).
    fn ramp(b: usize, h: usize, s: usize, d: usize, base: f32) -> Result<Tensor> {
        let n = b * h * s * d;
        let data: Vec<f32> = (0..n).map(|i| base + i as f32).collect();
        Tensor::from_vec(data, (b, h, s, d), &Device::Cpu)
    }

    /// THE determinism pin for the KV-cache preallocation lever: appending
    /// keys/values via `KvCacheSlot` (preallocated buffer + `slice_set` at a
    /// running offset, read back as `narrow(..).contiguous()`) yields a tensor
    /// byte-for-byte identical to the old per-step `Tensor::cat(&[cache,new],2)`
    /// path. If this ever fails, logits would diverge and verification would
    /// quarantine an honest worker — so it must stay green.
    #[test]
    fn prealloc_kv_append_matches_cat() -> Result<()> {
        let (b, h, d) = (3usize, 2usize, 4usize);

        // Reference: the previous behaviour — concatenate each step along dim 2.
        // Prefill of 5 tokens, then 6 single-token decode steps.
        let prefill = ramp(b, h, 5, d, 1.0)?;
        let mut cat_cache = prefill.clone();

        // New path: append into the preallocated slot.
        let mut slot = KvCacheSlot::new();
        let appended = slot.append(&prefill)?;
        // Prefill result must already match.
        assert_eq!(
            appended.flatten_all()?.to_vec1::<f32>()?,
            cat_cache.flatten_all()?.to_vec1::<f32>()?,
            "prefill append must equal the input"
        );

        for step in 0..6 {
            let tok = ramp(b, h, 1, d, 1000.0 + step as f32 * 100.0)?;
            cat_cache = Tensor::cat(&[&cat_cache, &tok], 2)?;
            let got = slot.append(&tok)?;
            assert_eq!(
                got.dims(),
                cat_cache.dims(),
                "shape must match cat at step {step}"
            );
            assert_eq!(
                got.flatten_all()?.to_vec1::<f32>()?,
                cat_cache.flatten_all()?.to_vec1::<f32>()?,
                "slice_set append must be byte-identical to cat at step {step}"
            );
        }
        Ok(())
    }

    /// `KvCacheSlot::reset` mirrors the old `index_pos == 0` reset: after a
    /// reset the next append reallocates and the stale contents are gone, even
    /// if the new prefill has a different batch size.
    #[test]
    fn prealloc_kv_reset_starts_fresh() -> Result<()> {
        let (h, d) = (2usize, 4usize);
        let mut slot = KvCacheSlot::new();

        // First sequence: batch 4, prefill 3.
        let first = ramp(4, h, 3, d, 1.0)?;
        let out1 = slot.append(&first)?;
        assert_eq!(out1.dims(), [4, h, 3, d]);

        // Reset (a fresh prefill at a different batch size).
        slot.reset();
        let second = ramp(2, h, 2, d, 500.0)?;
        let out2 = slot.append(&second)?;
        assert_eq!(
            out2.dims(),
            [2, h, 2, d],
            "reset must reallocate at the new batch size"
        );
        assert_eq!(
            out2.flatten_all()?.to_vec1::<f32>()?,
            second.flatten_all()?.to_vec1::<f32>()?,
            "after reset the slot holds only the new prefill"
        );
        Ok(())
    }

    /// PATCH (P-rightsize) correctness gate: `reset_with_cap` must actually
    /// allocate a SMALLER buffer than the `MAX_SEQ_LEN` worst case when given a
    /// small, realistic (prompt_len + max_tokens) budget — this is the whole
    /// point of the patch — and every value it produces must still match the
    /// `cat`-equivalent reference exactly, including growing past the
    /// original cap when a sequence runs longer than estimated (the overflow
    /// guard in `append`).
    #[test]
    fn prealloc_kv_right_sized_cap_matches_cat_and_shrinks_buffer() -> Result<()> {
        let (b, h, d) = (2usize, 2usize, 4usize);
        let small_cap = 8usize; // stand-in for a real (prompt_len + max_tokens) estimate

        let mut slot = KvCacheSlot::new();
        slot.reset_with_cap(small_cap);

        let prefill = ramp(b, h, 3, d, 1.0)?;
        let mut cat_cache = prefill.clone();
        let appended = slot.append(&prefill)?;
        assert_eq!(
            appended.flatten_all()?.to_vec1::<f32>()?,
            cat_cache.flatten_all()?.to_vec1::<f32>()?,
            "right-sized prefill append must equal the input"
        );
        // The allocated buffer must be the small cap, NOT the worst-case
        // MAX_SEQ_LEN — this is the actual memory saving the patch exists to
        // deliver.
        assert_eq!(
            slot.buf.as_ref().unwrap().dim(2)?,
            small_cap,
            "a right-sized reset must allocate at the given cap, not MAX_SEQ_LEN"
        );

        // Decode 10 steps — deliberately MORE than small_cap=8, so this also
        // exercises the overflow guard (growing past the original estimate)
        // and must still match the cat-reference at every step.
        for step in 0..10 {
            let tok = ramp(b, h, 1, d, 1000.0 + step as f32 * 100.0)?;
            cat_cache = Tensor::cat(&[&cat_cache, &tok], 2)?;
            let got = slot.append(&tok)?;
            assert_eq!(
                got.dims(),
                cat_cache.dims(),
                "shape must match cat at step {step} even past the original cap"
            );
            assert_eq!(
                got.flatten_all()?.to_vec1::<f32>()?,
                cat_cache.flatten_all()?.to_vec1::<f32>()?,
                "right-sized append must stay byte-identical to cat at step {step}, including past cap"
            );
        }
        // Never allocates beyond the hard ceiling either.
        assert!(slot.buf.as_ref().unwrap().dim(2)? <= MAX_SEQ_LEN);
        Ok(())
    }

    /// `KvCacheSlot::compact` drops EOS rows from dim 0 verbatim and keeps the
    /// surviving rows' live KV bytewise identical, so a later append continues
    /// against the shrunk batch. Matches `index_select` on the old `(k,v)` cat
    /// cache exactly.
    #[test]
    fn prealloc_kv_compact_keeps_rows_verbatim() -> Result<()> {
        let (h, d) = (2usize, 3usize);
        let mut slot = KvCacheSlot::new();

        // Prefill batch 4, length 2, then one decode step -> live len 3.
        let prefill = ramp(4, h, 2, d, 1.0)?;
        slot.append(&prefill)?;
        let step = ramp(4, h, 1, d, 9000.0)?;
        let full = slot.append(&step)?; // (4, h, 3, d)

        // Drop rows 1 and 3, keep rows 0 and 2.
        let keep = [0u32, 2u32];
        let idx = Tensor::from_vec(keep.to_vec(), keep.len(), &Device::Cpu)?;
        slot.compact(&idx)?;

        // The compacted live region must equal index_select(full, dim0, keep).
        let expected = full.index_select(&idx, 0)?;
        // Re-read the slot's live region via a zero-length append of the right shape.
        // (append of seq_len 0 is degenerate, so read by appending one more token
        //  and comparing the prefix instead.)
        let next = ramp(2, h, 1, d, 7777.0)?;
        let after = slot.append(&next)?; // (2, h, 4, d)
        let live_prefix = after.narrow(2, 0, 3)?;
        assert_eq!(
            live_prefix.flatten_all()?.to_vec1::<f32>()?,
            expected.flatten_all()?.to_vec1::<f32>()?,
            "compact must keep the named rows' KV bytewise identical"
        );
        Ok(())
    }

    /// THE determinism pin for the prefix-KV-sharing lever (Wave 1 B). After a
    /// shared-prefix prefill, `snapshot` then `restore` must re-seat the SAME KV
    /// the prefix produced, AND a subsequent append must continue exactly as if
    /// the prefix had stayed in place — so a per-item forward that forks the
    /// prefix sees byte-identical KV to one that prefilled the prefix inline. If
    /// this drifts, the prefix-shared path would diverge from serial and break the
    /// batched==serial token-identity gate.
    #[test]
    fn snapshot_restore_continues_prefix_verbatim() -> Result<()> {
        let (h, d) = (2usize, 4usize);

        // Reference: prefill a 5-token prefix, then append a 3-token remainder,
        // all in one continuous slot (the "inline" path).
        let prefix = ramp(1, h, 5, d, 1.0)?;
        let remainder = ramp(1, h, 3, d, 500.0)?;
        let mut inline = KvCacheSlot::new();
        inline.append(&prefix)?;
        let inline_full = inline.append(&remainder)?; // (1, h, 8, d)

        // Forked path: prefill the prefix, snapshot it, then for two independent
        // "items" restore the snapshot and append a remainder. Both must match the
        // inline result byte-for-byte (here both items use the same remainder, so
        // the comparison is exact; the point is restore re-seats the prefix KV).
        let mut shared = KvCacheSlot::new();
        shared.append(&prefix)?;
        let snap = shared.snapshot()?.expect("prefix snapshot");
        assert_eq!(snap.1, 5, "snapshot length is the prefix length");

        for item in 0..2 {
            let mut forked = KvCacheSlot::new();
            // Pre-dirty the slot so we prove restore fully re-seats it.
            forked.append(&ramp(1, h, 2, d, 9000.0)?)?;
            forked.restore(&snap)?;
            let forked_full = forked.append(&remainder)?; // (1, h, 8, d)
            assert_eq!(
                forked_full.dims(),
                inline_full.dims(),
                "item {item}: forked shape must match inline"
            );
            assert_eq!(
                forked_full.flatten_all()?.to_vec1::<f32>()?,
                inline_full.flatten_all()?.to_vec1::<f32>()?,
                "item {item}: forked prefix+remainder must be byte-identical to inline"
            );
        }
        // The original snapshot must be untouched by the forks (deep copy).
        assert_eq!(snap.1, 5, "snapshot length unchanged after forks");
        Ok(())
    }

    /// THE determinism pin for the BATCHED shared-prefix remainder decode
    /// (docs/internal/CREED_AND_PATH_TO_TEN.md "Inference hot path" 7→7.5 /
    /// "Batching efficiency" 6.5→7): `restore_broadcast(snapshot, b_sz)` must
    /// produce, for EVERY one of its `b_sz` rows, bytes identical to what a lone
    /// `restore` of the same snapshot would have produced — i.e. forking the
    /// prefix to a batch is not a numerically-close approximation of B separate
    /// forks, it IS B separate forks, just done in one preallocated buffer. This
    /// is what lets a bucketed batched decode over the forked rows stay
    /// byte-identical to the existing per-item serial fork path.
    #[test]
    fn restore_broadcast_matches_per_item_restore() -> Result<()> {
        let (h, d) = (2usize, 4usize);
        let b_sz = 4usize;

        let prefix = ramp(1, h, 5, d, 1.0)?;
        let mut shared = KvCacheSlot::new();
        shared.append(&prefix)?;
        let snap = shared.snapshot()?.expect("prefix snapshot");

        // Reference: b_sz independent single-row forks, each appending the SAME
        // per-row remainder (so the comparison below is row-for-row exact).
        let remainder_row = ramp(1, h, 3, d, 500.0)?;
        let mut refs: Vec<Tensor> = Vec::with_capacity(b_sz);
        for _ in 0..b_sz {
            let mut forked = KvCacheSlot::new();
            forked.restore(&snap)?;
            refs.push(forked.append(&remainder_row)?); // (1, h, 8, d)
        }

        // Broadcast path: ONE restore_broadcast to b_sz rows, then ONE batched
        // append of the same b_sz-row remainder.
        let remainder_batch = remainder_row.repeat((b_sz, 1, 1, 1))?;
        let mut batched = KvCacheSlot::new();
        batched.restore_broadcast(&snap, b_sz)?;
        let batched_full = batched.append(&remainder_batch)?; // (b_sz, h, 8, d)

        assert_eq!(batched_full.dims(), [b_sz, h, 8, d]);
        for row in 0..b_sz {
            let got = batched_full.i(row)?.flatten_all()?.to_vec1::<f32>()?;
            let want = refs[row].i(0)?.flatten_all()?.to_vec1::<f32>()?;
            assert_eq!(
                got, want,
                "row {row}: broadcast-fork must be byte-identical to a lone fork"
            );
        }
        // Snapshot must still be untouched (deep copy, same guarantee as `restore`).
        assert_eq!(snap.1, 5, "snapshot length unchanged after broadcast fork");
        Ok(())
    }

    /// `restore_broadcast` must reject a snapshot whose batch dim isn't 1 rather
    /// than silently mis-broadcasting — there is no such caller today (the
    /// shared-prefix prefill always runs at bsz=1), but a future misuse must fail
    /// loudly, not produce a quietly-wrong KV shape.
    #[test]
    fn restore_broadcast_rejects_non_unit_batch_snapshot() -> Result<()> {
        let (h, d) = (2usize, 4usize);
        let prefix = ramp(2, h, 5, d, 1.0)?; // batch dim 2, not 1
        let mut slot = KvCacheSlot::new();
        slot.append(&prefix)?;
        let snap = slot.snapshot()?.expect("prefix snapshot");
        let mut target = KvCacheSlot::new();
        let err = target.restore_broadcast(&snap, 4).unwrap_err();
        assert!(
            err.to_string().contains("batch dim must be 1"),
            "unexpected error: {err}"
        );
        Ok(())
    }

    // ── Mask shape tests ──────────────────────────────────────────────────────

    /// Classic square mask: index_pos=0 produces (seq_len, seq_len).
    #[test]
    fn causal_mask_square_shape() -> Result<()> {
        let mask = build_causal_mask(4, 0, &Device::Cpu)?;
        assert_eq!(mask.dims(), [4, 4]);
        Ok(())
    }

    /// Rectangular mask: index_pos=N produces (seq_len, N + seq_len).
    #[test]
    fn causal_mask_rectangular_shape() -> Result<()> {
        let mask = build_causal_mask(4, 65, &Device::Cpu)?;
        assert_eq!(mask.dims(), [4, 69]);
        Ok(())
    }

    // ── Mask value tests ──────────────────────────────────────────────────────

    /// Square mask values: standard lower-triangular pattern (0=attend, 1=block).
    ///
    /// For seq_len=3, index_pos=0:
    ///   row 0 (global pos 0): attend to pos 0             → [0, 1, 1]
    ///   row 1 (global pos 1): attend to pos 0..1           → [0, 0, 1]
    ///   row 2 (global pos 2): attend to pos 0..2           → [0, 0, 0]
    #[test]
    fn causal_mask_square_values() -> Result<()> {
        let mask = build_causal_mask(3, 0, &Device::Cpu)?;
        let data: Vec<u8> = mask.flatten_all()?.to_vec1()?;
        assert_eq!(data, [0, 1, 1, 0, 0, 1, 0, 0, 0]);
        Ok(())
    }

    /// Rectangular mask values: prefix columns are all-zero, user columns
    /// form the causal triangle.
    ///
    /// For seq_len=3, index_pos=2 → kv_len=5:
    ///   row 0 (global pos 2): attend to kv 0..2  → [0,0, 0,1,1]
    ///   row 1 (global pos 3): attend to kv 0..3  → [0,0, 0,0,1]
    ///   row 2 (global pos 4): attend to kv 0..4  → [0,0, 0,0,0]
    #[test]
    fn causal_mask_rectangular_values() -> Result<()> {
        let mask = build_causal_mask(3, 2, &Device::Cpu)?;
        let data: Vec<u8> = mask.flatten_all()?.to_vec1()?;
        #[rustfmt::skip]
        assert_eq!(data, [
            0, 0,  0, 1, 1,
            0, 0,  0, 0, 1,
            0, 0,  0, 0, 0,
        ]);
        Ok(())
    }

    /// A single-token query (seq_len=1) with prefix produces a single row
    /// of all zeros — it can attend to every key including itself.
    #[test]
    fn causal_mask_single_query_with_prefix() -> Result<()> {
        let mask = build_causal_mask(1, 10, &Device::Cpu)?;
        assert_eq!(mask.dims(), [1, 11]);
        let data: Vec<u8> = mask.flatten_all()?.to_vec1()?;
        assert!(
            data.iter().all(|&v| v == 0),
            "single-query mask should be all-zero"
        );
        Ok(())
    }

    // ── Mask broadcast compatibility test ─────────────────────────────────────

    /// Verify the mask can be broadcast to (batch, heads, seq_len, kv_len) —
    /// the exact shape produced by `Q @ K^T` in forward_attn.
    /// This is the broadcast that previously panicked when index_pos > 0.
    #[test]
    fn causal_mask_broadcasts_to_attention_shape() -> Result<()> {
        let batch = 1usize;
        let heads = 8usize;
        let seq_len = 4usize;
        let index_pos = 10usize;

        let mask = build_causal_mask(seq_len, index_pos, &Device::Cpu)?;
        // Simulate the attention score shape Q @ K^T → (batch, heads, seq_len, kv_len)
        let kv_len = index_pos + seq_len;
        let att_shape = &[batch, heads, seq_len, kv_len];
        let broadcasted = mask.broadcast_as(att_shape.as_slice())?;
        assert_eq!(broadcasted.dims(), att_shape);
        Ok(())
    }

    // ── Mask cache bound test ─────────────────────────────────────────────────

    /// The mask cache is bounded at `MASK_CACHE_CAP` with insertion-order
    /// eviction, and eviction is determinism-safe: a recomputed mask is
    /// bitwise identical to the one that was evicted.
    ///
    /// This mirrors the exact map+deque discipline inside `ModelWeights::mask`
    /// (which needs a full model to call directly) so the cache invariants are
    /// covered without loading weights.
    #[test]
    fn mask_cache_is_bounded_and_recompute_is_identical() -> Result<()> {
        use std::collections::{HashMap, VecDeque};

        let device = Device::Cpu;
        let mut masks: HashMap<(usize, usize), Tensor> = HashMap::new();
        let mut order: VecDeque<(usize, usize)> = VecDeque::new();

        // Capture the very first mask's bytes so we can prove the recomputed
        // version (after it is evicted) is byte-for-byte identical.
        let first_index_pos = 0usize;
        let first_seq_len = 1usize;
        let first_bytes: Vec<u8> = build_causal_mask(first_seq_len, first_index_pos, &device)?
            .flatten_all()?
            .to_vec1()?;

        // Insert far more distinct (seq_len, kv_len) keys than the cap.
        let inserts = super::MASK_CACHE_CAP * 3;
        for i in 0..inserts {
            let seq_len = 1usize;
            let index_pos = i; // distinct kv_len each iteration
            let kv_len = index_pos + seq_len;
            let key = (seq_len, kv_len);
            if !masks.contains_key(&key) {
                let mask = build_causal_mask(seq_len, index_pos, &device)?;
                while order.len() >= super::MASK_CACHE_CAP {
                    if let Some(old) = order.pop_front() {
                        masks.remove(&old);
                    } else {
                        break;
                    }
                }
                masks.insert(key, mask);
                order.push_back(key);
            }
            // Invariant on every step: never exceed the cap, map and order agree.
            assert!(masks.len() <= super::MASK_CACHE_CAP, "cache exceeded cap");
            assert_eq!(masks.len(), order.len(), "map and order out of sync");
        }

        // The first key (1, 1) must have been evicted long ago.
        assert!(
            !masks.contains_key(&(first_seq_len, first_index_pos + first_seq_len)),
            "oldest key should have been evicted"
        );

        // Recompute it and prove it is bitwise identical to the original.
        let recomputed: Vec<u8> = build_causal_mask(first_seq_len, first_index_pos, &device)?
            .flatten_all()?
            .to_vec1()?;
        assert_eq!(recomputed, first_bytes, "recomputed mask must be identical");
        Ok(())
    }

    /// PATCH (P-padbucket) primitive gate #1: the per-row padded mask
    /// (`build_padded_mask`) must REDUCE EXACTLY to the classic `build_causal_mask`
    /// when there is no real padding — i.e. a single fresh prefill (`cached_len ==
    /// 0`) where every row's real length equals `seq_len` and every row uses the
    /// same contiguous `0..seq_len` positions. If the two disagree here, the
    /// padded path could never be byte-exact against serial, so this is the
    /// network-free floor under the real-hardware determinism gate.
    #[test]
    fn padded_mask_reduces_to_causal_mask_when_unpadded() -> Result<()> {
        let device = Device::Cpu;
        for seq_len in [1usize, 3, 5, 8] {
            for b_sz in [1usize, 2, 4] {
                let real_len = vec![seq_len; b_sz];
                let q_global_pos: Vec<Vec<usize>> =
                    (0..b_sz).map(|_| (0..seq_len).collect()).collect();
                let padded =
                    super::build_padded_mask(&real_len, &q_global_pos, 0, seq_len, &device)?;
                assert_eq!(padded.dims(), [b_sz, 1, seq_len, seq_len]);
                let causal: Vec<u8> = build_causal_mask(seq_len, 0, &device)?
                    .flatten_all()?
                    .to_vec1()?;
                // Every row of the padded mask must equal the shared causal mask.
                for r in 0..b_sz {
                    let row: Vec<u8> = padded
                        .i((r, 0))?
                        .contiguous()?
                        .flatten_all()?
                        .to_vec1()?;
                    assert_eq!(
                        row, causal,
                        "row {r} of the padded mask (seq_len={seq_len}, b_sz={b_sz}) must equal build_causal_mask"
                    );
                }
            }
        }
        Ok(())
    }

    /// PATCH (P-padbucket) primitive gate #2: a padded row genuinely FORBIDS its
    /// own pad columns and keeps its real columns causal — exercising the branch
    /// the reduction test above cannot (it has no padding). Row 0 is real length 5
    /// (no pad), row 1 is real length 3 right-padded to 5: row 1's queries must be
    /// forbidden from its two pad key columns (3,4) while staying causal over 0..2.
    #[test]
    fn padded_mask_forbids_pad_columns_per_row() -> Result<()> {
        let device = Device::Cpu;
        let seq_len = 5usize;
        let real_len = vec![5usize, 3usize];
        // Right-padded: real tokens at 0..real_len, pad slots after get arbitrary
        // (here-continued) positions — masked out, so their value is irrelevant.
        let q_global_pos: Vec<Vec<usize>> = vec![(0..5).collect(), (0..5).collect()];
        let mask = super::build_padded_mask(&real_len, &q_global_pos, 0, seq_len, &device)?;
        let m: Vec<Vec<u8>> = (0..2)
            .map(|r| {
                mask.i((r, 0))?
                    .contiguous()?
                    .flatten_all()?
                    .to_vec1::<u8>()
            })
            .collect::<Result<_>>()?;
        // Row 0 (real length 5) is a plain lower-triangular causal mask.
        let want0: Vec<u8> = (0..5)
            .flat_map(|i| (0..5).map(move |j| u8::from(j > i)))
            .collect();
        assert_eq!(m[0], want0, "row 0 (unpadded) must be plain causal");
        // Row 1 (real length 3): columns 3 and 4 are pad KV → FORBIDDEN for every
        // query; columns 0..3 stay causal. Note cached_len==0 so ALL columns are
        // "new keys"; the causal branch forbids key j when its global position
        // (== j here) exceeds the query's — and pad queries (i>=3) additionally
        // see their pad keys forbidden by causality anyway. For the REAL queries
        // (i<3), key columns 3,4 have global pos 3,4 > i, so already forbidden.
        for i in 0..5usize {
            for j in 0..5usize {
                let got = m[1][i * 5 + j];
                let want = u8::from(j > i); // right-padding ⇒ causality alone excludes pads
                assert_eq!(
                    got, want,
                    "row1 q{i} k{j}: right-padded causal mask mismatch (pad cols must be forbidden for real queries)"
                );
            }
        }
        Ok(())
    }

    /// PATCH (P-padbucket) primitive gate #3: gathering the rotary table at
    /// per-row positions via `index_select` (what `build_per_row_cos_sin` does)
    /// and running candle's 3D-cos/sin `rope_i` must be BIT-IDENTICAL to the
    /// scalar `narrow`-based `apply_rotary_emb` path when every row uses the same
    /// contiguous `index_pos..index_pos+seq_len` positions. This is the network-
    /// free proof that the per-row rotary primitive collapses to the proven scalar
    /// path in the no-real-padding case — the rotary half of the determinism
    /// argument, provable without loading a model.
    #[test]
    fn rope_per_row_equals_scalar_when_positions_uniform() -> Result<()> {
        let device = Device::Cpu;
        let (b_sz, n_head, seq_len, head_dim) = (2usize, 4usize, 6usize, 8usize);
        let half = head_dim / 2;
        let table_len = 64usize;
        // A deterministic stand-in for the precomputed rope table (values are
        // irrelevant to the equivalence — only that both paths read the SAME
        // rows). Shape (table_len, half), matching self.cos/self.sin.
        let cos_tab = Tensor::from_vec(
            (0..table_len * half).map(|i| (i as f32 * 0.013).cos()).collect::<Vec<f32>>(),
            (table_len, half),
            &device,
        )?;
        let sin_tab = Tensor::from_vec(
            (0..table_len * half).map(|i| (i as f32 * 0.017).sin()).collect::<Vec<f32>>(),
            (table_len, half),
            &device,
        )?;
        let x = Tensor::from_vec(
            (0..b_sz * n_head * seq_len * head_dim)
                .map(|i| (i as f32 * 0.001) - 0.5)
                .collect::<Vec<f32>>(),
            (b_sz, n_head, seq_len, head_dim),
            &device,
        )?;

        for index_pos in [0usize, 7, 40] {
            // Scalar path: narrow the 2D table to [index_pos, index_pos+seq_len).
            let cos2 = cos_tab.narrow(0, index_pos, seq_len)?;
            let sin2 = sin_tab.narrow(0, index_pos, seq_len)?;
            let scalar = candle_nn::rotary_emb::rope_i(&x.contiguous()?, &cos2, &sin2)?;

            // Per-row path: build a (b_sz, seq_len) positions grid where EVERY row
            // is the same contiguous range, gather via index_select into a 3D
            // (b_sz, seq_len, half) cos/sin, then rope_i.
            let positions: Vec<u32> = (0..b_sz)
                .flat_map(|_| (index_pos..index_pos + seq_len).map(|p| p as u32))
                .collect();
            let pos = Tensor::from_vec(positions, (b_sz, seq_len), &device)?;
            let flat = pos.flatten_all()?.contiguous()?;
            let cos3 = cos_tab.index_select(&flat, 0)?.reshape((b_sz, seq_len, half))?.contiguous()?;
            let sin3 = sin_tab.index_select(&flat, 0)?.reshape((b_sz, seq_len, half))?.contiguous()?;
            let per_row = candle_nn::rotary_emb::rope_i(&x.contiguous()?, &cos3, &sin3)?;

            let a: Vec<f32> = scalar.flatten_all()?.to_vec1()?;
            let b: Vec<f32> = per_row.flatten_all()?.to_vec1()?;
            assert_eq!(
                a, b,
                "per-row rope (index_select 3D cos/sin) must byte-equal scalar rope (narrow 2D cos/sin) at index_pos={index_pos}"
            );
        }
        Ok(())
    }
}
