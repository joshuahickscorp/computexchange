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

/// Our embedding model: 384-dim MiniLM sentence-transformer (BERT weights).
pub const EMBED: ModelSpec = ModelSpec {
    repo: "sentence-transformers/all-MiniLM-L6-v2",
    files: &["config.json", "tokenizer.json", "model.safetensors"],
};

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

/// Our batch-inference model: a small quantized (GGUF) Llama-architecture LLM.
/// Default is Llama-3.2-1B-Instruct (Q4_K_M). Qwen2.5-0.5B-Instruct is an
/// accepted alternate (also llama-arch GGUF, supported by candle).
pub fn llama_gguf_spec(model_ref: &str) -> ModelSpec {
    let r = model_ref.to_ascii_lowercase();
    if r.contains("qwen") {
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
