//! Model resolution + compute device.
//!
//! Maps our stable model ids (`all-minilm-l6-v2`, `whisper-base`, the llama id)
//! to a HuggingFace repo + the files we need, and downloads/caches them via
//! `hf-hub`. Downloads are SACRED: hf-hub never re-fetches a file already in the
//! cache, and we never delete anything. `CX_MODEL_CACHE` overrides the cache
//! root; otherwise hf-hub uses the standard HF cache (`~/.cache/huggingface`).
//!
//! Device selection is explicit and logged: Metal on Apple Silicon when the
//! `metal` feature is built, CUDA on NVIDIA when the `cuda` feature is built
//! (the RunPod / data-center lane), else CPU. No silent fallback — if a GPU was
//! requested but the device fails to open, we log the real error and continue on
//! CPU rather than pretending the GPU is in use.

use std::path::PathBuf;
use std::sync::OnceLock;

use anyhow::{Context, Result};
use candle_core::Device;
use hf_hub::api::sync::{Api, ApiBuilder};

use crate::runners::RunError;

/// The one compute device for this process, picked once and reused.
static DEVICE: OnceLock<Device> = OnceLock::new();

/// Pick (once) the best available device and log which one. With the `metal`
/// (Apple Silicon) or `cuda` (NVIDIA) feature we try that GPU first; on any
/// failure we surface the error and fall back to CPU. With neither feature we
/// are CPU-only and say so.
pub fn device() -> &'static Device {
    DEVICE.get_or_init(|| {
        // Metal (the Apple Silicon GPU) when the feature is built; else CPU.
        #[cfg(feature = "metal")]
        {
            match Device::new_metal(0) {
                Ok(d) => {
                    tracing::info!("compute device: Metal (Apple Silicon GPU)");
                    return d;
                }
                // Surfaced, not swallowed: we say exactly why we're on CPU.
                Err(e) => tracing::warn!(error = %e, "Metal requested but unavailable; using CPU"),
            }
        }
        #[cfg(feature = "cuda")]
        {
            match Device::new_cuda(0) {
                Ok(d) => {
                    tracing::info!("compute device: CUDA (NVIDIA GPU)");
                    return d;
                }
                // Surfaced, not swallowed: we say exactly why we're on CPU.
                Err(e) => tracing::warn!(error = %e, "CUDA requested but unavailable; using CPU"),
            }
        }
        #[cfg(not(any(feature = "metal", feature = "cuda")))]
        {
            tracing::info!("compute device: CPU (built without a GPU feature)");
        }
        Device::Cpu
    })
}

/// Short label of the active compute device for honest benchmark/telemetry logs:
/// `metal` | `cpu`.
pub fn device_label() -> &'static str {
    let d = device();
    if d.is_metal() {
        "metal"
    } else if d.is_cuda() {
        "cuda"
    } else {
        "cpu"
    }
}

/// A model's HuggingFace location and the files we pull from it.
pub struct ModelSpec {
    /// HF repo, e.g. `sentence-transformers/all-MiniLM-L6-v2`.
    pub repo: &'static str,
    /// Files to fetch from the repo (relative paths within it).
    pub files: &'static [&'static str],
}

/// How an embedding model condenses the per-token BERT hidden states into one
/// sentence vector. Each sentence-transformer model card fixes this; choosing
/// the wrong one silently degrades quality, so it is part of the model's spec.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pooling {
    /// Mean over real (attention-masked) tokens. all-MiniLM-L6-v2.
    Mean,
    /// The first ([CLS]) token's hidden state. BAAI/bge-small-en-v1.5 · its
    /// model card pools `last_hidden_state[:, 0]` then L2-normalizes.
    Cls,
}

/// Our DEFAULT embedding model: 384-dim MiniLM sentence-transformer (BERT
/// weights), mean-pooled. Kept as the proven default; `embed_spec` resolves it
/// for any ref that is not explicitly a higher-quality alternate.
pub const EMBED: ModelSpec = ModelSpec {
    repo: "sentence-transformers/all-MiniLM-L6-v2",
    files: &["config.json", "tokenizer.json", "model.safetensors"],
};

/// Higher-quality 384-dim BERT embedder: BAAI/bge-small-en-v1.5. Same BERT
/// architecture and SAME 384 output dim as MiniLM, so it drops into the exact
/// same Candle embedder with ZERO downstream ripple (binary encoder, catalogue
/// dim, verification thresholds all unchanged). It is a big MTEB jump over
/// MiniLM-L6. Pooling is CLS (not mean) per its model card; both normalize.
pub const EMBED_BGE_SMALL: ModelSpec = ModelSpec {
    repo: "BAAI/bge-small-en-v1.5",
    files: &["config.json", "tokenizer.json", "model.safetensors"],
};

/// Canonical id of the default MiniLM embedder (matches the catalogue id).
pub const EMBED_MINILM_ID: &str = "all-minilm-l6-v2";
/// Canonical id of the bge-small-en-v1.5 embedder (the NEW alternate).
pub const EMBED_BGE_SMALL_ID: &str = "bge-small-en-v1.5";

/// REAL cross-encoder reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2`.
///
/// This is a BERT `SequenceClassification` head (`num_labels = 1`), NOT a
/// bi-encoder. It takes a `(query, doc)` PAIR jointly — the query and doc share
/// one attention window, so every doc token can attend to every query token —
/// and emits ONE relevance logit. That query↔doc cross-attention is exactly what
/// a bi-encoder (embed each side separately, then cosine) cannot see, and is why
/// a cross-encoder reranks materially better on hard cases (the truly-relevant
/// doc that is NOT the lexically-closest one). Same BERT trunk as MiniLM-L6
/// (6 layers, hidden 384, vocab 30522), so its `config.json` deserializes into
/// the very same Candle `bert::Config`; the only extra weights are the classifier
/// `Linear(384 -> 1)`. Weights: `model.safetensors` carries `bert.*` (the trunk,
/// loaded by Candle's `BertModel` via its `model_type`-prefixed fallback) plus
/// `classifier.weight`/`classifier.bias` (the head, loaded directly in
/// `runners::CrossEncoder`). The `bert.pooler.*` tensors are present in the file
/// but UNUSED — ms-marco cross-encoders read the raw `[CLS]` hidden state, not the
/// pooler output (verified against the reference `CrossEncoder` scoring path).
pub const RERANK_CROSS_ENCODER: ModelSpec = ModelSpec {
    repo: "cross-encoder/ms-marco-MiniLM-L-6-v2",
    files: &["config.json", "tokenizer.json", "model.safetensors"],
};

/// Canonical id of the real cross-encoder reranker (matches the catalogue id).
pub const RERANK_CROSS_ENCODER_ID: &str = "ms-marco-minilm-l6-v2";

/// True if `model_ref` selects the REAL cross-encoder rerank path. Matched
/// case-insensitively on the markers a rerank manifest would carry to ask for it:
/// our canonical id, the HF repo name, or a bare `cross-encoder` / `reranker`
/// marker. Any ref that does NOT name the cross-encoder (including the empty ref
/// and the historical `all-minilm-l6-v2` embed id) resolves to `false`, so the
/// existing bi-encoder cosine rerank stays byte-for-byte the default — the
/// cross-encoder is opt-in via the catalogue, and `RerankRunner` also falls back
/// to the bi-encoder if the cross-encoder weights cannot load.
pub fn is_cross_encoder_rerank(model_ref: &str) -> bool {
    let r = model_ref.to_ascii_lowercase();
    r.contains("ms-marco") || r.contains("cross-encoder") || r.contains("reranker")
}

/// Resolve an embed `model_ref` to (canonical id, spec, pooling). The MiniLM
/// default is returned for the empty ref and for any ref that does NOT name the
/// bge alternate, so existing embed/rerank jobs are byte-for-byte unchanged.
/// A ref naming `bge-small` (our id, the HF repo, or a bare `bge` marker)
/// selects the higher-quality model. Matched case-insensitively.
pub fn embed_spec(model_ref: &str) -> (&'static str, ModelSpec, Pooling) {
    if model_ref.to_ascii_lowercase().contains("bge") {
        (EMBED_BGE_SMALL_ID, EMBED_BGE_SMALL, Pooling::Cls)
    } else {
        (EMBED_MINILM_ID, EMBED, Pooling::Mean)
    }
}

/// Our speech model: whisper-tiny (smallest; 80 mel bins, multilingual).
/// `whisper-base` resolves here too — both are honored by `resolve_embed`'s
/// sibling for whisper (see `whisper_spec`).
pub fn whisper_spec(model_ref: &str) -> ModelSpec {
    // Accept our id, HF id, or a bare size; default to tiny (smallest, cached).
    let r = model_ref.to_ascii_lowercase();
    let repo = if r.contains("base") {
        "openai/whisper-base"
    } else {
        "openai/whisper-tiny"
    };
    ModelSpec {
        repo,
        files: &["config.json", "tokenizer.json", "model.safetensors"],
    }
}

/// Minimum advertised memory (GB) below which the big 7B batch_infer model is
/// refused. Q4_K_M 7B weights are ~4.7 GB on disk and the working set (weights +
/// KV cache + activations for a batched prefill) needs real headroom, so this is
/// gated to the high-VRAM workers (nvidia_48g/80g/180g and the large Apple
/// unified-memory / cluster classes). The small Llama-3.2-1B (catalogue floor 4 GB)
/// stays the default for everyone else. `BatchInferRunner::can_run` enforces this
/// as a hard agent-side floor so a mis-constrained manifest can never load the big
/// model on a worker that cannot hold it — it surfaces NoRunner, never an OOM.
pub const BIG_LLAMA_MIN_MEMORY_GB: f32 = 40.0;

/// True if `model_ref` selects the bigger (7B-class) quantized LLM. Matched
/// case-insensitively on a `7b` marker so the catalogue id (`qwen2.5-7b-instruct-q4`),
/// the HF repo, or a bare `7b` all resolve to it. Kept as one predicate so
/// `llama_gguf_spec`, the tokenizer resolver, and the memory gate agree on exactly
/// one definition of "the big model".
pub fn is_big_llama(model_ref: &str) -> bool {
    model_ref.to_ascii_lowercase().contains("7b")
}

/// Our batch-inference model: a quantized (GGUF) Llama-architecture LLM.
/// Default is Llama-3.2-1B-Instruct (Q4_K_M). Qwen2.5-0.5B-Instruct is an accepted
/// small alternate, and Qwen2.5-7B-Instruct (Q4_K_M) is the BIG model for high-VRAM
/// workers (selected by a `7b` ref, gated by `BIG_LLAMA_MIN_MEMORY_GB`). The Llama
/// GGUF is llama-arch; the Qwen GGUFs are qwen2-arch (`qwen2.*` metadata keys, q/k/v
/// biases, NEOX rope) and load through the architecture-aware path in
/// `quantized_llama_batched::from_gguf` (P-arch / P-rope / P-qkvbias). NOTE: Qwen
/// output parity is UNPROVEN until a real-GGUF Metal parity run — see
/// docs/CANDLE_EXPANSION_RESEARCH.md. Earlier this comment wrongly called all three
/// llama-arch; the official Qwen GGUFs are not.
///
/// PATCH (Per-Device Speed & Throughput 7→8, docs/internal/CREED_AND_PATH_TO_TEN.md):
/// the 7B repo/file used to point at `Qwen/Qwen2.5-7B-Instruct-GGUF`'s
/// `qwen2.5-7b-instruct-q4_k_m.gguf` — a real HTTP HEAD against that exact path now
/// 404s. The upstream repo re-quantized and now ships that quant level split across
/// two shard files (`qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf` /
/// `-00002-of-00002.gguf`), and `LlamaBackend::load`/`gguf_file::Content::read` only
/// know how to read a single-file GGUF — there is no multi-shard reassembly in this
/// codebase. Repointed at `bartowski/Qwen2.5-7B-Instruct-GGUF`'s single-file
/// `Qwen2.5-7B-Instruct-Q4_K_M.gguf` (verified live via HTTP HEAD: resolves,
/// content-length 4,683,074,240 bytes ≈ 4.7GB, matching this file's own doc comment)
/// — same base model (`Qwen/Qwen2.5-7B-Instruct`), same GGUF architecture/metadata
/// shape (bartowski's quantizer output is llama.cpp-standard, read by the same
/// qwen2-arch path above), same Q4_K_M quant level, just a different (still
/// single-file) host repo. The tokenizer resolver (`load_llama_tokenizer` in
/// runners.rs) already points at the original `Qwen/Qwen2.5-7B-Instruct` repo,
/// independent of the GGUF host, so it is unaffected by this change.
pub fn llama_gguf_spec(model_ref: &str) -> ModelSpec {
    let r = model_ref.to_ascii_lowercase();
    if is_big_llama(&r) {
        // Bigger model — only dispatched to high-VRAM workers (see the memory gate
        // in BatchInferRunner::can_run + the catalogue's higher min_memory_gb).
        ModelSpec {
            repo: "bartowski/Qwen2.5-7B-Instruct-GGUF",
            files: &["Qwen2.5-7B-Instruct-Q4_K_M.gguf"],
        }
    } else if r.contains("qwen") {
        ModelSpec {
            repo: "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            files: &["qwen2.5-0.5b-instruct-q4_k_m.gguf"],
        }
    } else {
        ModelSpec {
            repo: "unsloth/Llama-3.2-1B-Instruct-GGUF",
            files: &["Llama-3.2-1B-Instruct-Q4_K_M.gguf"],
        }
    }
}

/// Build the hf-hub API, honoring `CX_MODEL_CACHE`. Cached files are reused as-is
/// (hf-hub checks the cache before the network); existing downloads are never
/// touched.
fn api() -> Result<Api> {
    let mut b = ApiBuilder::new();
    if let Ok(dir) = std::env::var("CX_MODEL_CACHE") {
        if !dir.is_empty() {
            b = b.with_cache_dir(PathBuf::from(dir));
        }
    }
    b.build().context("building hf-hub API")
}

/// Download (or reuse cached) every file in `spec`, returning their local paths
/// in the same order. The first file is conventionally the primary weight.
pub fn fetch(spec: &ModelSpec) -> Result<Vec<PathBuf>, RunError> {
    let api = api().map_err(|e| RunError::ModelFetch {
        repo: spec.repo.to_string(),
        msg: format!("{e:#}"),
    })?;
    let repo = api.model(spec.repo.to_string());
    let mut paths = Vec::with_capacity(spec.files.len());
    for f in spec.files {
        tracing::info!(
            repo = spec.repo,
            file = f,
            "resolving model file (cache first)"
        );
        let p = repo.get(f).map_err(|e| RunError::ModelFetch {
            repo: spec.repo.to_string(),
            msg: format!("fetching `{f}`: {e}"),
        })?;
        paths.push(p);
    }
    Ok(paths)
}
