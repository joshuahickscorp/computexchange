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

pub const MAX_SEQ_LEN: usize = 4096;

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
    /// `(b_sz, n_kv_head, MAX_SEQ_LEN, head_dim)`, allocated lazily on first
    /// append. The live keys/values occupy `[.., .., 0..cur_len, ..]`.
    buf: Option<Tensor>,
    /// Number of valid sequence positions currently written into `buf`.
    cur_len: usize,
}

impl KvCacheSlot {
    fn new() -> Self {
        Self {
            buf: None,
            cur_len: 0,
        }
    }

    /// Drop the buffer so the next `append` reallocates. Used on a fresh
    /// prefill (`index_pos == 0`), matching the old cat path's reset semantics.
    fn reset(&mut self) {
        self.buf = None;
        self.cur_len = 0;
    }

    /// Append `src` (shape `(b_sz, n_kv_head, seq_len, head_dim)`) at the
    /// current offset and return the live region `(b_sz, n_kv_head, cur_len,
    /// head_dim)` as a contiguous tensor — byte-identical to what
    /// `Tensor::cat(&[cache, src], 2)` produced.
    fn append(&mut self, src: &Tensor) -> Result<Tensor> {
        let (b_sz, n_kv_head, seq_len, head_dim) = src.dims4()?;
        if self.buf.is_none() {
            let buf = Tensor::zeros(
                (b_sz, n_kv_head, MAX_SEQ_LEN, head_dim),
                src.dtype(),
                src.device(),
            )?;
            self.buf = Some(buf);
            self.cur_len = 0;
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
            let new_buf = Tensor::zeros(
                (b_sz, n_kv_head, MAX_SEQ_LEN, head_dim),
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
        let new_buf = Tensor::zeros(
            (b_sz, n_kv_head, MAX_SEQ_LEN, head_dim),
            live.dtype(),
            live.device(),
        )?;
        new_buf.slice_set(&live.contiguous()?, 2, 0)?;
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

    fn forward_attn(
        &mut self,
        x: &Tensor,
        mask: Option<&Tensor>,
        index_pos: usize,
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
            self.kv_k.reset();
            self.kv_v.reset();
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

    pub fn forward(&mut self, x: &Tensor, index_pos: usize) -> Result<Tensor> {
        let (_b_sz, seq_len) = x.dims2()?;
        let mask = if seq_len == 1 {
            None
        } else {
            Some(self.mask(seq_len, index_pos, x.device())?)
        };
        let _enter = self.span.enter();
        let mut layer_in = self.tok_embeddings.forward(x)?;
        for layer in self.layers.iter_mut() {
            let x = layer_in;
            let residual = &x;
            let x = layer.attention_norm.forward(&x)?;
            let attn = layer.forward_attn(&x, mask.as_ref(), index_pos)?;
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
}

#[cfg(test)]
mod tests {
    use super::KvCacheSlot;
    use candle_core::{Device, Result, Tensor};
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
}
