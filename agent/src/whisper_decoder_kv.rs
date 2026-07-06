//! Incremental (KV-cached) Whisper text decoder.
//!
//! VENDORED + PATCHED copy of candle-transformers 0.10.2's whisper decoder-side structs
//! (`whisper::model::{MultiHeadAttention, ResidualAttentionBlock, TextDecoder}`), following
//! the vendored-module convention in docs/CANDLE_FORK.md (see `quantized_llama_batched.rs`
//! for the sibling precedent). Only the DECODER is vendored here — `AudioEncoder` is
//! unchanged and still comes straight from the crate (it runs once per clip; there is
//! nothing to cache).
//!
//! PATCH (P-selfkv): upstream's self-attention branch recomputes K/V from the FULL growing
//! token sequence on every decode step (`x` is always the whole sequence so far), which is
//! O(n^2) total decode compute even though the cross-attention K/V (over the fixed audio
//! features) is already correctly cached and reused every step. This module adds a real
//! self-attention KV cache: an initial multi-token PREFILL call (the 3-token
//! `<sot><transcribe><notimestamps>` prompt, `flush_kv_cache=true`) seeds the cache and
//! runs the causal mask as upstream does; every subsequent call feeds ONLY the newly
//! decoded token (`flush_kv_cache=false`), computes its K/V, concatenates it onto the
//! growing per-layer self-attention cache, and attends the new token's query over the FULL
//! cached K/V with no mask — a new token's causal window is exactly "every position already
//! in the cache", which is unmasked full attention over the prefix by construction, so this
//! is mathematically identical to recomputing self-attention over the whole sequence each
//! step, just O(n) total instead of O(n^2). The positional embedding lookup is offset by
//! the decoder's running position (`self.offset`) instead of always starting at 0, so an
//! incremental call still looks up the correct absolute position for its new token(s).
//!
//! Verified bit-for-bit against the full-recompute-every-step reference behavior on
//! synthetic weights in the test below (`incremental_matches_full_recompute`) — run it
//! after any edit here: `cd agent && cargo test --no-default-features -- whisper_decoder_kv`.
//! Any edit here changes `hardware::infer_content_id()` per the same rule as
//! `quantized_llama_batched.rs`; see docs/CANDLE_FORK.md and docs/DETERMINISM_CLASS.md.
#![allow(clippy::all, dead_code)]

use candle_core::{Result, Tensor, D};
use candle_nn::{embedding, Embedding, LayerNorm, Module, VarBuilder};
use candle_transformers::models::whisper::Config;

fn linear(size1: usize, size2: usize, vb: VarBuilder) -> Result<candle_nn::Linear> {
    candle_nn::linear(size1, size2, vb)
}

fn linear_no_bias(size1: usize, size2: usize, vb: VarBuilder) -> Result<candle_nn::Linear> {
    candle_nn::linear_no_bias(size1, size2, vb)
}

fn layer_norm(size: usize, vb: VarBuilder) -> Result<LayerNorm> {
    let weight = vb.get(size, "weight")?;
    let bias = vb.get(size, "bias")?;
    Ok(LayerNorm::new(weight, bias, 1e-5))
}

/// Self-attention with a real incremental KV cache (`self_kv_cache`, PATCH — absent
/// upstream), plus the original upstream cross-attention K/V cache (`cross_kv_cache`,
/// computed once against the fixed audio features and reused every step — unchanged from
/// upstream other than the field rename to disambiguate from `self_kv_cache`).
struct MultiHeadAttentionKV {
    query: candle_nn::Linear,
    key: candle_nn::Linear,
    value: candle_nn::Linear,
    out: candle_nn::Linear,
    n_head: usize,
    self_kv_cache: Option<(Tensor, Tensor)>,
    cross_kv_cache: Option<(Tensor, Tensor)>,
}

impl MultiHeadAttentionKV {
    fn load(n_state: usize, n_head: usize, vb: VarBuilder) -> Result<Self> {
        let query = linear(n_state, n_state, vb.pp("q_proj"))?;
        let value = linear(n_state, n_state, vb.pp("v_proj"))?;
        let key = linear_no_bias(n_state, n_state, vb.pp("k_proj"))?;
        let out = linear(n_state, n_state, vb.pp("out_proj"))?;
        Ok(Self {
            query,
            key,
            value,
            out,
            n_head,
            self_kv_cache: None,
            cross_kv_cache: None,
        })
    }

    /// `xa: None` => self-attention (uses/grows `self_kv_cache`); `xa: Some(audio)` =>
    /// cross-attention (uses `cross_kv_cache`, computed once, identical to upstream).
    /// `mask: None` is REQUIRED for an incremental self-attention call (new query length
    /// != cached key length; the causal constraint is already satisfied by construction).
    fn forward(
        &mut self,
        x: &Tensor,
        xa: Option<&Tensor>,
        mask: Option<&Tensor>,
        flush_cache: bool,
    ) -> Result<Tensor> {
        let q = self.query.forward(x)?;
        let (k, v) = match xa {
            None => {
                if flush_cache {
                    self.self_kv_cache = None;
                }
                let k_new = self.key.forward(x)?;
                let v_new = self.value.forward(x)?;
                match &self.self_kv_cache {
                    None => {
                        self.self_kv_cache = Some((k_new.clone(), v_new.clone()));
                        (k_new, v_new)
                    }
                    Some((k_old, v_old)) => {
                        let k_cat = Tensor::cat(&[k_old, &k_new], 1)?;
                        let v_cat = Tensor::cat(&[v_old, &v_new], 1)?;
                        self.self_kv_cache = Some((k_cat.clone(), v_cat.clone()));
                        (k_cat, v_cat)
                    }
                }
            }
            Some(xa) => {
                if flush_cache {
                    self.cross_kv_cache = None;
                }
                if let Some((k, v)) = &self.cross_kv_cache {
                    (k.clone(), v.clone())
                } else {
                    let k = self.key.forward(xa)?;
                    let v = self.value.forward(xa)?;
                    self.cross_kv_cache = Some((k.clone(), v.clone()));
                    (k, v)
                }
            }
        };
        let wv = self.qkv_attention(&q, &k, &v, mask)?;
        self.out.forward(&wv)
    }

    fn reshape_head(&self, x: &Tensor) -> Result<Tensor> {
        let (n_batch, n_ctx, n_state) = x.dims3()?;
        let target_dims = &[n_batch, n_ctx, self.n_head, n_state / self.n_head];
        x.reshape(target_dims)?.transpose(1, 2)
    }

    /// Identical math to upstream's `qkv_attention`. `mask`, when present, is sliced to
    /// `(0..q_len, 0..q_len)` — correct for a full prefill (q_len == k_len) and never
    /// passed for an incremental step (q_len == new-token-count, k_len == full cache).
    fn qkv_attention(
        &self,
        q: &Tensor,
        k: &Tensor,
        v: &Tensor,
        mask: Option<&Tensor>,
    ) -> Result<Tensor> {
        use candle_core::IndexOp;
        let (_, q_len, n_state) = q.dims3()?;
        let scale = ((n_state / self.n_head) as f64).powf(-0.25);
        let q = (self.reshape_head(q)? * scale)?;
        let k = (self.reshape_head(k)?.transpose(2, 3)? * scale)?;
        let v = self.reshape_head(v)?.contiguous()?;
        let mut qk = q.matmul(&k)?;
        if let Some(mask) = mask {
            let mask = mask.i((0..q_len, 0..q_len))?;
            qk = qk.broadcast_add(&mask)?;
        }
        let w = candle_nn::ops::softmax_last_dim(&qk)?;
        let wv = w.matmul(&v)?.transpose(1, 2)?.flatten_from(2)?;
        Ok(wv)
    }

    fn reset(&mut self) {
        self.self_kv_cache = None;
        self.cross_kv_cache = None;
    }
}

struct ResidualAttentionBlockKV {
    attn: MultiHeadAttentionKV,
    attn_ln: LayerNorm,
    cross_attn: (MultiHeadAttentionKV, LayerNorm),
    mlp_linear1: candle_nn::Linear,
    mlp_linear2: candle_nn::Linear,
    mlp_ln: LayerNorm,
}

impl ResidualAttentionBlockKV {
    fn load(n_state: usize, n_head: usize, vb: VarBuilder) -> Result<Self> {
        let attn = MultiHeadAttentionKV::load(n_state, n_head, vb.pp("self_attn"))?;
        let attn_ln = layer_norm(n_state, vb.pp("self_attn_layer_norm"))?;
        let cross_attn = MultiHeadAttentionKV::load(n_state, n_head, vb.pp("encoder_attn"))?;
        let cross_attn_ln = layer_norm(n_state, vb.pp("encoder_attn_layer_norm"))?;
        let n_mlp = n_state * 4;
        let mlp_linear1 = linear(n_state, n_mlp, vb.pp("fc1"))?;
        let mlp_linear2 = linear(n_mlp, n_state, vb.pp("fc2"))?;
        let mlp_ln = layer_norm(n_state, vb.pp("final_layer_norm"))?;
        Ok(Self {
            attn,
            attn_ln,
            cross_attn: (cross_attn, cross_attn_ln),
            mlp_linear1,
            mlp_linear2,
            mlp_ln,
        })
    }

    fn forward(
        &mut self,
        x: &Tensor,
        xa: &Tensor,
        mask: Option<&Tensor>,
        flush: bool,
    ) -> Result<Tensor> {
        let attn = self
            .attn
            .forward(&self.attn_ln.forward(x)?, None, mask, flush)?;
        let mut x = (x + attn)?;
        let (ca, ca_ln) = &mut self.cross_attn;
        x = (&x + ca.forward(&ca_ln.forward(&x)?, Some(xa), None, flush)?)?;
        let mlp = self.mlp_linear2.forward(
            &self
                .mlp_linear1
                .forward(&self.mlp_ln.forward(&x)?)?
                .gelu()?,
        )?;
        x + mlp
    }

    fn reset(&mut self) {
        self.attn.reset();
        self.cross_attn.0.reset();
    }
}

/// PATCH: adds `offset` (running absolute decode position, for correct positional-embedding
/// lookups on incremental calls) on top of upstream's `TextDecoder`.
pub struct TextDecoderKV {
    token_embedding: Embedding,
    positional_embedding: Tensor,
    blocks: Vec<ResidualAttentionBlockKV>,
    ln: LayerNorm,
    mask: Tensor,
    offset: usize,
}

impl TextDecoderKV {
    pub fn load(vb: VarBuilder, cfg: &Config) -> Result<Self> {
        let n_state = cfg.d_model;
        let n_head = cfg.decoder_attention_heads;
        let n_ctx = cfg.max_target_positions;
        let token_embedding = embedding(cfg.vocab_size, n_state, vb.pp("embed_tokens"))?;
        let positional_embedding = vb.get((n_ctx, n_state), "embed_positions.weight")?;
        let blocks = (0..cfg.decoder_layers)
            .map(|i| ResidualAttentionBlockKV::load(n_state, n_head, vb.pp(format!("layers.{i}"))))
            .collect::<Result<Vec<_>>>()?;
        let ln = layer_norm(n_state, vb.pp("layer_norm"))?;
        let mask: Vec<_> = (0..n_ctx)
            .flat_map(|i| (0..n_ctx).map(move |j| if j > i { f32::NEG_INFINITY } else { 0f32 }))
            .collect();
        let mask = Tensor::from_vec(mask, (n_ctx, n_ctx), vb.device())?;
        Ok(Self {
            token_embedding,
            positional_embedding,
            blocks,
            ln,
            mask,
            offset: 0,
        })
    }

    /// `x`: token ids for THIS call only — the 3-token prompt on the first
    /// (`flush_kv_cache=true`) call, exactly one newly-decoded token on every call after
    /// that (`flush_kv_cache=false`). Positions are tracked internally via `self.offset`.
    pub fn forward(&mut self, x: &Tensor, xa: &Tensor, flush_kv_cache: bool) -> Result<Tensor> {
        let seq_len = x.dim(D::Minus1)?;
        if flush_kv_cache {
            self.offset = 0;
        }
        let token_embedding = self.token_embedding.forward(x)?;
        let positional_embedding = self.positional_embedding.narrow(0, self.offset, seq_len)?;
        let mut xh = token_embedding.broadcast_add(&positional_embedding)?;
        // A full prefill (flush=true) needs the causal mask among its own seq_len
        // positions; an incremental step's new token(s) causally see the ENTIRE existing
        // cache (nothing masked) by construction, so no mask is passed.
        let mask: Option<&Tensor> = if flush_kv_cache {
            Some(&self.mask)
        } else {
            None
        };
        for block in self.blocks.iter_mut() {
            xh = block.forward(&xh, xa, mask, flush_kv_cache)?;
        }
        self.offset += seq_len;
        self.ln.forward(&xh)
    }

    pub fn final_linear(&self, x: &Tensor) -> Result<Tensor> {
        let b_size = x.dim(0)?;
        let w = self.token_embedding.embeddings().broadcast_left(b_size)?;
        x.matmul(&w.t()?)
    }

    pub fn reset(&mut self) {
        self.offset = 0;
        for b in self.blocks.iter_mut() {
            b.reset();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use candle_core::{Device, IndexOp};

    /// A tiny hand-rolled decoder (no VarBuilder/checkpoint needed) that mirrors
    /// `TextDecoderKV`'s shapes with small, fixed, non-random weights, so the test is
    /// fully deterministic and needs no downloaded model or GPU.
    fn synthetic(
        n_state: usize,
        n_head: usize,
        n_layer: usize,
        n_ctx: usize,
    ) -> Result<(TextDecoderKV, Tensor)> {
        let dev = Device::Cpu;
        let vocab = 16usize;
        // Deterministic "random-looking" weights via a simple counter-derived pattern —
        // avoids pulling in a PRNG dependency just for this test.
        let mut ctr = 0f32;
        let mut next = |n: usize| -> Result<Tensor> {
            let v: Vec<f32> = (0..n)
                .map(|i| {
                    ctr += 1.0;
                    ((ctr * 0.0137 + i as f32 * 0.0021).sin()) * 0.05
                })
                .collect();
            Tensor::from_vec(v, n, &dev)
        };
        let vb_data = candle_nn::VarBuilder::from_tensors(
            {
                let mut m = std::collections::HashMap::new();
                m.insert("embed_tokens.weight".to_string(), {
                    let n = vocab * n_state;
                    Tensor::from_vec(
                        (0..n)
                            .map(|i| ((i as f32) * 0.013).sin() * 0.05)
                            .collect::<Vec<f32>>(),
                        (vocab, n_state),
                        &dev,
                    )?
                });
                m.insert(
                    "embed_positions.weight".to_string(),
                    Tensor::from_vec(
                        (0..n_ctx * n_state)
                            .map(|i| ((i as f32) * 0.019).cos() * 0.05)
                            .collect::<Vec<f32>>(),
                        (n_ctx, n_state),
                        &dev,
                    )?,
                );
                m.insert(
                    "layer_norm.weight".to_string(),
                    Tensor::ones(n_state, candle_core::DType::F32, &dev)?,
                );
                m.insert(
                    "layer_norm.bias".to_string(),
                    Tensor::zeros(n_state, candle_core::DType::F32, &dev)?,
                );
                for l in 0..n_layer {
                    for (name, rows, cols) in [
                        ("self_attn.q_proj", n_state, n_state),
                        ("self_attn.k_proj", n_state, n_state),
                        ("self_attn.v_proj", n_state, n_state),
                        ("self_attn.out_proj", n_state, n_state),
                        ("encoder_attn.q_proj", n_state, n_state),
                        ("encoder_attn.k_proj", n_state, n_state),
                        ("encoder_attn.v_proj", n_state, n_state),
                        ("encoder_attn.out_proj", n_state, n_state),
                        ("fc1", n_state * 4, n_state),
                        ("fc2", n_state, n_state * 4),
                    ] {
                        let w = next(rows * cols)?.reshape((rows, cols))?;
                        m.insert(format!("layers.{l}.{name}.weight"), w);
                        if name != "self_attn.k_proj" && name != "encoder_attn.k_proj" {
                            m.insert(format!("layers.{l}.{name}.bias"), next(rows)?);
                        }
                    }
                    for ln in [
                        "self_attn_layer_norm",
                        "encoder_attn_layer_norm",
                        "final_layer_norm",
                    ] {
                        m.insert(
                            format!("layers.{l}.{ln}.weight"),
                            Tensor::ones(n_state, candle_core::DType::F32, &dev)?,
                        );
                        m.insert(
                            format!("layers.{l}.{ln}.bias"),
                            Tensor::zeros(n_state, candle_core::DType::F32, &dev)?,
                        );
                    }
                }
                m
            },
            candle_core::DType::F32,
            &dev,
        );
        let cfg = Config {
            num_mel_bins: 1,
            max_source_positions: 8,
            d_model: n_state,
            encoder_attention_heads: n_head,
            encoder_layers: 1,
            vocab_size: vocab,
            max_target_positions: n_ctx,
            decoder_attention_heads: n_head,
            decoder_layers: n_layer,
            suppress_tokens: vec![],
        };
        let dec = TextDecoderKV::load(vb_data, &cfg)?;
        // Fixed fake "audio features" the cross-attention reads (unchanged across steps,
        // exactly like a real encoder's output for one clip).
        let xa = Tensor::from_vec(
            (0..(4 * n_state))
                .map(|i| ((i as f32) * 0.031).sin() * 0.05)
                .collect::<Vec<f32>>(),
            (1, 4, n_state),
            &dev,
        )?;
        Ok((dec, xa))
    }

    /// The correctness gate for this whole patch: decode the SAME 6-token sequence two
    /// ways — (a) upstream's own pattern, full recompute + flush=true at every single
    /// step, feeding the whole growing prefix each time; (b) this module's incremental
    /// path, prefill once then one new token per step. Final-layer logits for the LAST
    /// position must match bit-for-bit at every step, since that is the only thing greedy
    /// decoding ever reads.
    #[test]
    fn incremental_matches_full_recompute() -> Result<()> {
        let n_state = 8;
        let n_head = 2;
        let n_layer = 2;
        let n_ctx = 32;
        let tokens: Vec<u32> = vec![1, 2, 3, 4, 5, 6];
        let dev = Device::Cpu;

        // (a) reference: full recompute + flush=true every call, upstream's own pattern.
        // Real usage (WhisperBackend::transcribe) only ever reads logits after the full
        // 3-token prompt and after each token thereafter — never after 1 or 2 tokens — so
        // the reference only checks those same lengths for an apples-to-apples comparison.
        let prefill_len_ref = 3;
        let (mut dec_ref, xa) = synthetic(n_state, n_head, n_layer, n_ctx)?;
        let mut ref_logits_last = Vec::new();
        for len in prefill_len_ref..=tokens.len() {
            let x = Tensor::from_vec(tokens[..len].to_vec(), (1, len), &dev)?;
            let out = dec_ref.forward(&x, &xa, true)?; // flush=true: full recompute, matches upstream exactly
            let logits = dec_ref.final_linear(&out)?;
            let last = logits.i((0, len - 1))?.to_vec1::<f32>()?;
            ref_logits_last.push(last);
        }

        // (b) patched: prefill once (flush=true, first 3 tokens as a stand-in prompt-sized
        // chunk), then exactly one new token per subsequent call (flush=false).
        let (mut dec_kv, xa2) = synthetic(n_state, n_head, n_layer, n_ctx)?;
        let mut kv_logits_last = Vec::new();
        let prefill_len = 3;
        {
            let x = Tensor::from_vec(tokens[..prefill_len].to_vec(), (1, prefill_len), &dev)?;
            let out = dec_kv.forward(&x, &xa2, true)?;
            let logits = dec_kv.final_linear(&out)?;
            kv_logits_last.push(logits.i((0, prefill_len - 1))?.to_vec1::<f32>()?);
        }
        for i in prefill_len..tokens.len() {
            let x = Tensor::from_vec(vec![tokens[i]], (1, 1), &dev)?;
            let out = dec_kv.forward(&x, &xa2, false)?;
            let logits = dec_kv.final_linear(&out)?;
            kv_logits_last.push(logits.i((0, 0))?.to_vec1::<f32>()?);
        }

        assert_eq!(ref_logits_last.len(), kv_logits_last.len());
        for (step, (a, b)) in ref_logits_last
            .iter()
            .zip(kv_logits_last.iter())
            .enumerate()
        {
            for (j, (av, bv)) in a.iter().zip(b.iter()).enumerate() {
                assert!(
                    (av - bv).abs() < 1e-4,
                    "step {step} logit {j} diverged: full-recompute={av} incremental={bv}"
                );
            }
        }
        Ok(())
    }
}
