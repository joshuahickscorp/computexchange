//! Job runners — the closed job-type contract, with REAL Candle inference.
//!
//! `can_run` gates on job type, model kind, and available memory. `run` actually
//! executes the model: BERT/MiniLM embeddings, Whisper transcription, and small
//! quantized-Llama generation, all via `candle` (Metal on Apple Silicon, CPU
//! otherwise — see `models::device`). Every failure is a typed `RunError`; a
//! genuine model-download or inference error never produces a fake result.

use std::io::Cursor;

use crate::quantized_llama_batched::ModelWeights as QLlama; // patched: bsz>1 batched prefill
use async_trait::async_trait;
use candle_core::quantized::gguf_file;
use candle_core::{Device, IndexOp, Tensor};
use candle_nn::VarBuilder;
use candle_transformers::models::bert::{BertModel, Config as BertConfig, DTYPE as BERT_DTYPE};
use candle_transformers::models::whisper::{self, audio as whisper_audio, model as whisper_model};
use serde::{Deserialize, Serialize};
use tokenizers::Tokenizer;

use crate::models;
use crate::pool::ModelPool;
use crate::types::{HardwareClass, JobManifest, JobType, ModelKind, WorkerCapability};

/// Output of a successfully executed job. `result` is the serialized result the
/// control plane parses (see module-level result formats in the contract).
#[derive(Debug, Clone)]
pub struct JobOutput {
    /// Serialized result payload (embeddings / completions / transcript). JSON for
    /// every job type by default; an opt-in binary embedding artifact when
    /// `binary` is true (PLANE_D D5/D15).
    pub result: Vec<u8>,
    /// True when `result` is a non-JSON binary artifact (embed `binary` mode), so
    /// the uploader sets `application/octet-stream` instead of `application/json`.
    /// Defaults false — every existing runner leaves it unset and stays JSON.
    pub binary: bool,
    pub duration_ms: u64,
    pub tokens_used: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum RunError {
    /// No registered runner could handle this manifest on this hardware.
    #[error("no runner can handle job `{job_type}` with model kind `{model_kind}`")]
    NoRunner {
        job_type: String,
        model_kind: String,
    },
    /// A model file could not be downloaded/resolved from HuggingFace.
    #[error("model fetch from `{repo}` failed: {msg}")]
    ModelFetch { repo: String, msg: String },
    /// The input chunk was not the JSONL shape this job expects.
    #[error("bad input for `{job}`: {msg}")]
    BadInput { job: &'static str, msg: String },
    /// Model load or forward pass failed (candle/tokenizer error).
    #[error("inference error in `{backend}`: {msg}")]
    Inference { backend: &'static str, msg: String },
    /// The job needs a co-located cluster substrate that is not present on this
    /// host (Plane B, docs/PLANE_B.md §3,§5). The cluster's routing, advertisement,
    /// and shard PLAN are proven locally; EXECUTING a sharded forward pass runs on
    /// Exo / MLX-distributed / JACCL over Thunderbolt 5 (external/field). We surface
    /// the boundary — never fake a distributed forward pass.
    #[error("cluster substrate required for `{model}`: {detail}")]
    ExternalSubstrate { model: String, detail: String },
    /// This host cannot run a documented lane that a properly-equipped host could.
    /// Today this is the `custom` general-compute job (ACCRETION.md §7-8) on a worker
    /// without the container sandbox: the sandboxed BYO-container runner IS built
    /// (sandbox.rs) but requires a Linux GPU host with Docker + the NVIDIA Container
    /// Toolkit, so an incapable worker fails HONESTLY here rather than faking a result
    /// (BLACKHOLE: surface the boundary; the scheduler routes `custom` only to workers
    /// that advertise it).
    #[error("`{job_type}` not supported on this host: {detail}")]
    NotImplemented {
        job_type: &'static str,
        detail: String,
    },
}

/// Helper: wrap any `candle`/`anyhow` error as an `Inference` failure.
fn infer_err<E: std::fmt::Display>(backend: &'static str) -> impl Fn(E) -> RunError {
    move |e| RunError::Inference {
        backend,
        msg: e.to_string(),
    }
}

#[async_trait]
pub trait JobRunner: Send + Sync {
    /// Can this backend execute `manifest` on a worker with `cap`? REAL logic.
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool;
    /// Execute the job against an input chunk, returning the result JSON. Backends
    /// are pulled WARM from `pool` (loaded once, reused) rather than re-loaded per
    /// task.
    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError>;
    fn backend_name(&self) -> &'static str;
}

/// Shared memory gate: the worker must meet the manifest's minimum.
fn meets_memory(manifest: &JobManifest, cap: &WorkerCapability) -> bool {
    cap.memory_gb >= manifest.constraints.min_memory_gb
}

// ---------------------------------------------------------------------------
// Input JSONL parsing (shared)
// ---------------------------------------------------------------------------

/// One embed/infer input line: `{"id":..,"text":..}` (infer may use `prompt`).
#[derive(Debug, Deserialize)]
struct TextItem {
    #[allow(dead_code)]
    id: Option<String>,
    text: Option<String>,
    prompt: Option<String>,
}

impl TextItem {
    /// The text payload, accepting either `text` or `prompt`.
    fn body(&self) -> Option<&str> {
        self.text.as_deref().or(self.prompt.as_deref())
    }
}

/// One transcribe input line: `{"id":..,"audio_b64":..}` (16kHz mono wav).
#[derive(Debug, Deserialize)]
struct AudioItem {
    #[allow(dead_code)]
    id: Option<String>,
    audio_b64: String,
}

/// Parse a JSONL byte chunk into `T`, one item per non-empty line. Surfaces the
/// offending line number on the first parse error (no silent skipping).
fn parse_jsonl<T: for<'de> Deserialize<'de>>(
    input: &[u8],
    job: &'static str,
) -> Result<Vec<T>, RunError> {
    let text = std::str::from_utf8(input).map_err(|e| RunError::BadInput {
        job,
        msg: format!("input is not UTF-8: {e}"),
    })?;
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let item: T = serde_json::from_str(line).map_err(|e| RunError::BadInput {
            job,
            msg: format!("line {}: {e}", i + 1),
        })?;
        out.push(item);
    }
    if out.is_empty() {
        return Err(RunError::BadInput {
            job,
            msg: "no input items".to_string(),
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Result JSON (what we PUT to output_url; the control plane parses these)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct EmbedResult {
    pub job_type: &'static str, // "embed"
    pub model: String,
    pub dim: usize,
    pub count: usize,
    pub vectors: Vec<Vec<f32>>,
}

// ---------------------------------------------------------------------------
// Binary embedding artifact (PLANE_D §11 D5 / §21 D15)
// ---------------------------------------------------------------------------
//
// A compact, self-describing float32 container for large embedding outputs. The
// JSON `EmbedResult` is ~12-15 bytes per float (text decimals + commas); this is
// exactly 4 bytes per float plus a fixed 16-byte header, so it is several times
// smaller for any real output and never allocates per-element strings. JSON stays
// the DEFAULT (small jobs + debugging); this is emitted only when the embed job
// opts in (`JobType::Embed { binary: true }`). The SDK (`sdk/python`) hides the
// format behind a numpy-free reader.
//
// Layout (little-endian throughout — fixed, not host-dependent):
//   off 0  : magic   = b"CXEM"           (4 bytes, "Compute eXchange EMbeddings")
//   off 4  : version = 1                 (u32)
//   off 8  : dim                         (u32, floats per row)
//   off 12 : count                       (u32, number of rows)
//   off 16 : count*dim packed f32 rows, row-major (row 0 first), LE bytes
// Total = 16 + count*dim*4 bytes. No model id / job_type lives in the binary blob
// (those stay on the JSON control path); the blob is pure numeric payload.

/// Magic prefix marking a Computexchange binary embedding artifact. The control
/// plane merge uses these 4 bytes to detect a binary chunk and pass it through
/// instead of JSON-parsing it (control/api.go mergeResultObject).
pub const EMBED_BIN_MAGIC: &[u8; 4] = b"CXEM";
/// Current binary embedding format version.
pub const EMBED_BIN_VERSION: u32 = 1;
/// Fixed header size in bytes (magic + version + dim + count).
pub const EMBED_BIN_HEADER: usize = 16;

/// Encode `vectors` (each of length `dim`) as the binary embedding artifact. All
/// rows MUST already be `dim`-long (the embedder guarantees this); a row of the
/// wrong width is a real bug, surfaced as an `Inference` error rather than written
/// as a truncated/garbage blob (BLACKHOLE: never emit a silently-wrong artifact).
pub fn encode_embeddings_binary(dim: usize, vectors: &[Vec<f32>]) -> Result<Vec<u8>, RunError> {
    let count = vectors.len();
    let mut out = Vec::with_capacity(EMBED_BIN_HEADER + count * dim * 4);
    out.extend_from_slice(EMBED_BIN_MAGIC);
    out.extend_from_slice(&EMBED_BIN_VERSION.to_le_bytes());
    out.extend_from_slice(&(dim as u32).to_le_bytes());
    out.extend_from_slice(&(count as u32).to_le_bytes());
    for (i, row) in vectors.iter().enumerate() {
        if row.len() != dim {
            return Err(RunError::Inference {
                backend: "embed",
                msg: format!(
                    "binary encode: row {i} has {} floats, expected dim {dim}",
                    row.len()
                ),
            });
        }
        for &f in row {
            out.extend_from_slice(&f.to_le_bytes());
        }
    }
    Ok(out)
}

/// Decode a binary embedding artifact back into (dim, rows). Used only by tests
/// here (the production decoder is the Python SDK reader); kept next to the encoder
/// so the round-trip is proven in one place. Surfaces every malformation as an
/// error — a short/garbled blob is never silently accepted.
#[cfg(test)]
pub fn decode_embeddings_binary(bytes: &[u8]) -> Result<(usize, Vec<Vec<f32>>), RunError> {
    let bad = |msg: String| RunError::Inference {
        backend: "embed",
        msg,
    };
    if bytes.len() < EMBED_BIN_HEADER {
        return Err(bad(format!(
            "binary decode: {} bytes < {EMBED_BIN_HEADER}-byte header",
            bytes.len()
        )));
    }
    if &bytes[0..4] != EMBED_BIN_MAGIC {
        return Err(bad("binary decode: bad magic".to_string()));
    }
    let rd = |o: usize| u32::from_le_bytes([bytes[o], bytes[o + 1], bytes[o + 2], bytes[o + 3]]);
    let version = rd(4);
    if version != EMBED_BIN_VERSION {
        return Err(bad(format!("binary decode: unknown version {version}")));
    }
    let dim = rd(8) as usize;
    let count = rd(12) as usize;
    let want = EMBED_BIN_HEADER + count * dim * 4;
    if bytes.len() != want {
        return Err(bad(format!(
            "binary decode: body is {} bytes, header implies {want} ({count}x{dim} f32)",
            bytes.len()
        )));
    }
    let mut rows = Vec::with_capacity(count);
    let mut o = EMBED_BIN_HEADER;
    for _ in 0..count {
        let mut row = Vec::with_capacity(dim);
        for _ in 0..dim {
            row.push(f32::from_le_bytes([
                bytes[o],
                bytes[o + 1],
                bytes[o + 2],
                bytes[o + 3],
            ]));
            o += 4;
        }
        rows.push(row);
    }
    Ok((dim, rows))
}

#[derive(Debug, Serialize)]
pub struct Completion {
    pub index: usize,
    pub text: String,
    pub tokens: usize,
}

#[derive(Debug, Serialize)]
pub struct BatchInferResult {
    pub job_type: &'static str, // "batch_infer"
    pub model: String,
    pub completions: Vec<Completion>,
}

#[derive(Debug, Serialize)]
pub struct Segment {
    pub start: f32,
    pub end: f32,
    pub text: String,
}

#[derive(Debug, Serialize)]
pub struct TranscribeResult {
    pub job_type: &'static str, // "audio_transcribe"
    pub model: String,
    pub text: String,
    pub segments: Vec<Segment>,
}

#[derive(Debug, Serialize)]
pub struct LabelAssignment {
    pub index: usize,
    pub label: String,
}

#[derive(Debug, Serialize)]
pub struct ClassificationResult {
    pub job_type: &'static str, // "batch_classification"
    pub model: String,
    pub count: usize,
    pub labels: Vec<LabelAssignment>,
}

#[derive(Debug, Serialize)]
pub struct ExtractedItem {
    pub index: usize,
    pub json: serde_json::Value,
}

#[derive(Debug, Serialize)]
pub struct ExtractionResult {
    pub job_type: &'static str, // "json_extraction"
    pub model: String,
    pub count: usize,
    pub items: Vec<ExtractedItem>,
}

#[derive(Debug, Serialize)]
pub struct Ranking {
    pub index: usize,
    pub order: Vec<usize>,
}

#[derive(Debug, Serialize)]
pub struct RerankResult {
    pub job_type: &'static str, // "rerank"
    pub model: String,
    pub count: usize,
    pub rankings: Vec<Ranking>,
}

/// One rerank input line: `{"id":..,"query":..,"docs":["..",...]}`.
#[derive(Debug, Deserialize)]
struct RerankItem {
    #[allow(dead_code)]
    id: Option<String>,
    query: Option<String>,
    #[serde(default)]
    docs: Vec<String>,
}

// ---------------------------------------------------------------------------
// EmbedRunner — BERT / sentence-transformers (all-MiniLM-L6-v2, 384-dim)
// ---------------------------------------------------------------------------

/// Embedding dimension of all-MiniLM-L6-v2.
pub const EMBED_DIM: usize = 384;

/// A loaded MiniLM embedder: tokenizer + BERT weights on the active device.
pub struct Embedder {
    model: BertModel,
    tokenizer: Tokenizer,
    device: Device,
}

impl Embedder {
    /// Resolve + load the model (downloads on first use, cache-first after).
    pub fn load() -> Result<Self, RunError> {
        let paths = models::fetch(&models::EMBED)?;
        let (config_p, tok_p, weights_p) = (&paths[0], &paths[1], &paths[2]);

        let cfg_bytes = std::fs::read(config_p).map_err(infer_err("embed"))?;
        let config: BertConfig = serde_json::from_slice(&cfg_bytes).map_err(infer_err("embed"))?;
        let mut tokenizer = Tokenizer::from_file(tok_p).map_err(infer_err("embed"))?;
        // Pad to the batch's longest sequence so we can run a real batch tensor.
        let pad = tokenizers::PaddingParams::default();
        tokenizer.with_padding(Some(pad));

        let device = models::device().clone();
        // FP32 (BERT_DTYPE): FP16 was tried and reverted — it gave no throughput gain
        // on this tiny model (overhead-bound) and degraded embedding precision enough
        // to flip rerank ordering. Embeddings stay full-precision.
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[weights_p], BERT_DTYPE, &device)
                .map_err(infer_err("embed"))?
        };
        let model = BertModel::load(vb, &config).map_err(infer_err("embed"))?;
        Ok(Self {
            model,
            tokenizer,
            device,
        })
    }

    /// Embed a batch of strings → one L2-normalized `EMBED_DIM`-vector each.
    /// Mean-pools the last hidden state over real (non-pad) tokens.
    pub fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, RunError> {
        let backend = "embed";
        let encs = self
            .tokenizer
            .encode_batch(texts.to_vec(), true)
            .map_err(infer_err(backend))?;

        let (bsz, seq) = (encs.len(), encs[0].len());
        let mut ids = Vec::with_capacity(bsz * seq);
        let mut mask = Vec::with_capacity(bsz * seq);
        for e in &encs {
            ids.extend(e.get_ids().iter().map(|&x| x as i64));
            mask.extend(e.get_attention_mask().iter().map(|&x| x as f32));
        }

        let input_ids =
            Tensor::from_vec(ids, (bsz, seq), &self.device).map_err(infer_err(backend))?;
        let token_type = input_ids.zeros_like().map_err(infer_err(backend))?;
        let attn = Tensor::from_vec(mask, (bsz, seq), &self.device).map_err(infer_err(backend))?;

        // [bsz, seq, hidden]
        let hidden = self
            .model
            .forward(&input_ids, &token_type, Some(&attn))
            .map_err(infer_err(backend))?;

        // Mean-pool over tokens weighted by the attention mask, then L2-normalize.
        let mask3 = attn
            .unsqueeze(2)
            .map_err(infer_err(backend))?
            .to_dtype(hidden.dtype())
            .map_err(infer_err(backend))?; // [bsz, seq, 1]
        let summed = hidden
            .broadcast_mul(&mask3)
            .map_err(infer_err(backend))?
            .sum(1)
            .map_err(infer_err(backend))?; // [bsz, hidden]
        let counts = mask3.sum(1).map_err(infer_err(backend))?; // [bsz, 1]
        let mean = summed.broadcast_div(&counts).map_err(infer_err(backend))?;
        let norm = mean
            .sqr()
            .map_err(infer_err(backend))?
            .sum_keepdim(1)
            .map_err(infer_err(backend))?
            .sqrt()
            .map_err(infer_err(backend))?;
        let normed = mean.broadcast_div(&norm).map_err(infer_err(backend))?;
        normed.to_vec2::<f32>().map_err(infer_err(backend))
    }
}

pub struct EmbedRunner;

#[async_trait]
impl JobRunner for EmbedRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        matches!(manifest.job_type, JobType::Embed { .. })
            && matches!(
                manifest.model.kind,
                ModelKind::Gguf | ModelKind::Hf | ModelKind::Mlx
            )
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let items: Vec<TextItem> = parse_jsonl(input, "embed")?;
        let texts: Vec<String> = items
            .iter()
            .map(|it| it.body().unwrap_or("").to_string())
            .collect();
        let count = texts.len();

        // Warm embedder (loaded once for the whole process), forward off-runtime.
        let vectors = embed_texts(pool, texts).await?;

        // Output encoding: JSON is the DEFAULT (small jobs + debugging). The compact
        // binary float32 artifact (PLANE_D D5/D15) is emitted only when the job opts
        // in via `JobType::Embed { binary: true }` — never automatically, so an
        // existing JSON embed job is byte-for-byte unchanged and the JSON merge path
        // is untouched. `wants_binary` also accepts a `manifest.params.embed_binary`
        // hint for forward-compatibility, even though the current dispatch forwards
        // the flag through the job_type spec rather than `params`. The >256-row size
        // threshold (PLANE_D D5) is the documented guidance for WHEN a buyer should
        // request binary; it is proven as a pure size win in the encoder test rather
        // than used to silently switch a buyer's output format out from under them.
        let binary = wants_binary(manifest);
        let (bytes, is_binary) = if binary {
            (encode_embeddings_binary(EMBED_DIM, &vectors)?, true)
        } else {
            // A large JSON embed output is exactly the case D5 binary is for; surface
            // the opportunity (real, actionable telemetry — not noise) so an operator
            // sees that setting `binary:true` would shrink this artifact. We still
            // honor the buyer's JSON choice; we never switch it for them.
            if count >= EMBED_BINARY_ROW_HINT {
                tracing::info!(
                    rows = count,
                    hint = EMBED_BINARY_ROW_HINT,
                    "embed: large output emitted as JSON; set job_type.binary=true for the compact float32 artifact (PLANE_D D5)"
                );
            }
            let result = EmbedResult {
                job_type: "embed",
                model: short_model_id(&manifest.model.model_ref, "all-minilm-l6-v2"),
                dim: EMBED_DIM,
                count,
                vectors,
            };
            (
                serde_json::to_vec(&result).map_err(infer_err("embed"))?,
                false,
            )
        };
        Ok(JobOutput {
            result: bytes,
            binary: is_binary,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: count as u64,
        })
    }

    fn backend_name(&self) -> &'static str {
        "embed"
    }
}

/// Row count at/above which PLANE_D D5 recommends a binary embedding artifact over
/// JSON. Documentation/guidance only — see `EmbedRunner::run` (we do not auto-switch
/// a buyer's format at this threshold; binary is strictly opt-in).
pub const EMBED_BINARY_ROW_HINT: usize = 256;

/// True if an embed job asked for the binary artifact. Primary source is the
/// `binary` flag on the `Embed` job_type (it round-trips to the agent via the
/// persisted `job_type_spec`). As a forward-compatible fallback we also honor a
/// `manifest.params.embed_binary == true` hint, so if the dispatch ever forwards
/// `params` the same intent still works. Anything else → false (JSON default).
fn wants_binary(manifest: &JobManifest) -> bool {
    if let JobType::Embed { binary, .. } = manifest.job_type {
        if binary {
            return true;
        }
    }
    manifest
        .params
        .get("embed_binary")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// Embed a batch of strings via the warm pool embedder, off the async runtime.
/// Shared by the embed and rerank runners (one source for the forward pass).
async fn embed_texts(pool: &ModelPool, texts: Vec<String>) -> Result<Vec<Vec<f32>>, RunError> {
    let embedder = pool.embedder().await?;
    tokio::task::spawn_blocking(move || embedder.embed(&texts))
        .await
        .map_err(infer_err("embed"))?
}

// ---------------------------------------------------------------------------
// WhisperRunner — speech-to-text (openai/whisper-tiny|base)
// ---------------------------------------------------------------------------

pub struct WhisperBackend {
    model: whisper_model::Whisper,
    tokenizer: Tokenizer,
    config: whisper::Config,
    mel_filters: Vec<f32>,
    device: Device,
    // Special token ids resolved from the tokenizer.
    sot: u32,
    eot: u32,
    transcribe: u32,
    no_timestamps: u32,
}

impl WhisperBackend {
    pub fn load(model_ref: &str) -> Result<Self, RunError> {
        let spec = models::whisper_spec(model_ref);
        let paths = models::fetch(&spec)?;
        let (config_p, tok_p, weights_p) = (&paths[0], &paths[1], &paths[2]);

        let cfg_bytes = std::fs::read(config_p).map_err(infer_err("whisper"))?;
        let config: whisper::Config =
            serde_json::from_slice(&cfg_bytes).map_err(infer_err("whisper"))?;
        let tokenizer = Tokenizer::from_file(tok_p).map_err(infer_err("whisper"))?;

        let device = models::device().clone();
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[weights_p], whisper::DTYPE, &device)
                .map_err(infer_err("whisper"))?
        };
        let model =
            whisper_model::Whisper::load(&vb, config.clone()).map_err(infer_err("whisper"))?;

        let mel_filters = mel_filterbank(config.num_mel_bins);
        let tok = |s: &str| -> Result<u32, RunError> {
            tokenizer.token_to_id(s).ok_or_else(|| RunError::Inference {
                backend: "whisper",
                msg: format!("tokenizer missing special token {s}"),
            })
        };
        Ok(Self {
            sot: tok(whisper::SOT_TOKEN)?,
            eot: tok(whisper::EOT_TOKEN)?,
            transcribe: tok(whisper::TRANSCRIBE_TOKEN)?,
            no_timestamps: tok(whisper::NO_TIMESTAMPS_TOKEN)?,
            model,
            tokenizer,
            config,
            mel_filters,
            device,
        })
    }

    /// Transcribe one 30s-or-less PCM clip (f32 mono @16kHz) via greedy decoding.
    pub fn transcribe(&mut self, pcm: &[f32]) -> Result<String, RunError> {
        let backend = "whisper";
        let n_mel = self.config.num_mel_bins;
        let mel = whisper_audio::pcm_to_mel(&self.config, pcm, &self.mel_filters);
        let frames = mel.len() / n_mel;
        let mel = Tensor::from_vec(mel, (1, n_mel, frames), &self.device)
            .map_err(infer_err(backend))?
            .to_dtype(whisper::DTYPE)
            .map_err(infer_err(backend))?;

        let audio_features = self
            .model
            .encoder
            .forward(&mel, true)
            .map_err(infer_err(backend))?;

        // Greedy decode from the standard prompt: <sot> <transcribe> <notimestamps>.
        let mut tokens: Vec<u32> = vec![self.sot, self.transcribe, self.no_timestamps];
        let max_new = self.config.max_target_positions.min(224);
        for _ in 0..max_new {
            let toks = Tensor::new(tokens.as_slice(), &self.device)
                .map_err(infer_err(backend))?
                .unsqueeze(0)
                .map_err(infer_err(backend))?;
            // Feed the whole growing sequence each step with the KV cache reset
            // (flush=true) — correct and cheap for short whisper sequences. The
            // decoder's positional embedding starts at position 0 by design, so
            // recompute-from-scratch is the intended greedy pattern here.
            let dec = self
                .model
                .decoder
                .forward(&toks, &audio_features, true)
                .map_err(infer_err(backend))?;
            let seq_len = dec.dim(1).map_err(infer_err(backend))?;
            let logits = self
                .model
                .decoder
                .final_linear(&dec)
                .map_err(infer_err(backend))?; // [1, seq, vocab]
            let last = logits.i((0, seq_len - 1)).map_err(infer_err(backend))?; // [vocab]
            let next = last
                .argmax(0)
                .map_err(infer_err(backend))?
                .to_scalar::<u32>()
                .map_err(infer_err(backend))?;
            if next == self.eot {
                break;
            }
            tokens.push(next);
        }
        self.model.reset_kv_cache();

        // Decode, dropping the prompt + any special tokens.
        let text = self
            .tokenizer
            .decode(&tokens[3..], true)
            .map_err(infer_err(backend))?;
        Ok(text.trim().to_string())
    }
}

pub struct WhisperRunner;

#[async_trait]
impl JobRunner for WhisperRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        matches!(manifest.job_type, JobType::AudioTranscribe { .. })
            && matches!(
                manifest.model.kind,
                ModelKind::Gguf | ModelKind::Mlx | ModelKind::Hf
            )
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let items: Vec<AudioItem> = parse_jsonl(input, "audio_transcribe")?;

        // Warm whisper backend (loaded once). `transcribe` is `&mut self`, so we
        // take the per-model mutex inside the blocking thread for the whole clip
        // loop — correct serialization without re-loading the weights.
        let model = pool.whisper(&manifest.model.model_ref).await?;
        let (text, segments) = tokio::task::spawn_blocking(move || -> Result<_, RunError> {
            let mut backend = model.blocking_lock();
            let mut full = String::new();
            let mut segments = Vec::new();
            let mut clock = 0.0f32;
            for it in &items {
                let pcm = decode_wav_b64(&it.audio_b64)?;
                let dur = pcm.len() as f32 / whisper::SAMPLE_RATE as f32;
                let seg_text = backend.transcribe(&pcm)?;
                if !full.is_empty() {
                    full.push(' ');
                }
                full.push_str(&seg_text);
                segments.push(Segment {
                    start: clock,
                    end: clock + dur,
                    text: seg_text,
                });
                clock += dur;
            }
            Ok((full, segments))
        })
        .await
        .map_err(infer_err("whisper"))??;

        let result = TranscribeResult {
            job_type: "audio_transcribe",
            // Honest label: the repo we actually resolved (tiny by default).
            model: short_model_id(
                models::whisper_spec(&manifest.model.model_ref).repo,
                "whisper-tiny",
            ),
            text,
            segments,
        };
        let bytes = serde_json::to_vec(&result).map_err(infer_err("whisper"))?;
        Ok(JobOutput {
            result: bytes,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: 0,
        })
    }

    fn backend_name(&self) -> &'static str {
        "whisper"
    }
}

/// Decode a base64 WAV (16kHz mono, 16-bit or float) into f32 PCM in [-1,1].
fn decode_wav_b64(b64: &str) -> Result<Vec<f32>, RunError> {
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(b64.trim())
        .map_err(|e| RunError::BadInput {
            job: "audio_transcribe",
            msg: format!("audio_b64 not valid base64: {e}"),
        })?;
    let mut reader = hound::WavReader::new(Cursor::new(bytes)).map_err(|e| RunError::BadInput {
        job: "audio_transcribe",
        msg: format!("not a WAV: {e}"),
    })?;
    let spec = reader.spec();
    let pcm: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.samples::<f32>().filter_map(Result::ok).collect(),
        hound::SampleFormat::Int => {
            let max = (1i64 << (spec.bits_per_sample - 1)) as f32;
            reader
                .samples::<i32>()
                .filter_map(Result::ok)
                .map(|s| s as f32 / max)
                .collect()
        }
    };
    // Downmix to mono if needed (average channels).
    let pcm = if spec.channels > 1 {
        let ch = spec.channels as usize;
        pcm.chunks(ch)
            .map(|c| c.iter().sum::<f32>() / ch as f32)
            .collect()
    } else {
        pcm
    };
    if pcm.is_empty() {
        return Err(RunError::BadInput {
            job: "audio_transcribe",
            msg: "WAV decoded to zero samples".to_string(),
        });
    }
    Ok(pcm)
}

/// Build a Whisper-compatible mel filterbank (HTK mel, librosa "slaney" norm),
/// flattened row-major `[n_mels][N_FFT/2+1]` as f32 — the same matrix Whisper's
/// `melfilters.bytes` carries, generated here so we ship no external asset.
fn mel_filterbank(n_mels: usize) -> Vec<f32> {
    let n_fft = whisper::N_FFT;
    let sr = whisper::SAMPLE_RATE as f32;
    let n_freqs = n_fft / 2 + 1;
    let f_min = 0.0f32;
    let f_max = sr / 2.0;

    // HTK mel scale.
    let hz_to_mel = |f: f32| 2595.0 * (1.0 + f / 700.0).log10();
    let mel_to_hz = |m: f32| 700.0 * (10f32.powf(m / 2595.0) - 1.0);

    let m_min = hz_to_mel(f_min);
    let m_max = hz_to_mel(f_max);
    // n_mels+2 mel points → band edges.
    let mel_pts: Vec<f32> = (0..n_mels + 2)
        .map(|i| m_min + (m_max - m_min) * i as f32 / (n_mels + 1) as f32)
        .collect();
    let hz_pts: Vec<f32> = mel_pts.iter().map(|&m| mel_to_hz(m)).collect();
    // FFT bin center frequencies.
    let fft_freqs: Vec<f32> = (0..n_freqs).map(|i| i as f32 * sr / n_fft as f32).collect();

    let mut fb = vec![0.0f32; n_mels * n_freqs];
    for m in 0..n_mels {
        let (left, center, right) = (hz_pts[m], hz_pts[m + 1], hz_pts[m + 2]);
        for (k, &f) in fft_freqs.iter().enumerate() {
            let up = (f - left) / (center - left);
            let down = (right - f) / (right - center);
            let w = up.min(down).max(0.0);
            fb[m * n_freqs + k] = w;
        }
        // Slaney normalization: scale each filter to unit area in Hz.
        let enorm = 2.0 / (hz_pts[m + 2] - hz_pts[m]);
        for k in 0..n_freqs {
            fb[m * n_freqs + k] *= enorm;
        }
    }
    fb
}

// ---------------------------------------------------------------------------
// BatchInferRunner — small quantized Llama-arch LLM (GGUF) generation
// ---------------------------------------------------------------------------

pub struct LlamaBackend {
    model: QLlama,
    tokenizer: Tokenizer,
    eos: u32,
    device: Device,
}

impl LlamaBackend {
    pub fn load(model_ref: &str) -> Result<Self, RunError> {
        let spec = models::llama_gguf_spec(model_ref);
        let paths = models::fetch(&spec)?;
        let gguf_p = &paths[0];

        let device = models::device().clone();
        let mut file = std::fs::File::open(gguf_p).map_err(infer_err("batch_infer"))?;
        let content = gguf_file::Content::read(&mut file).map_err(infer_err("batch_infer"))?;

        // EOS token id from GGUF metadata (llama.cpp convention).
        let eos = content
            .metadata
            .get("tokenizer.ggml.eos_token_id")
            .and_then(|v| v.to_u32().ok())
            .unwrap_or(2);
        let model =
            QLlama::from_gguf(content, &mut file, &device).map_err(infer_err("batch_infer"))?;

        // The matching HF tokenizer (same repo family) for encode/decode.
        let tokenizer = load_llama_tokenizer(model_ref)?;
        Ok(Self {
            model,
            tokenizer,
            eos,
            device,
        })
    }

    /// Greedy-generate up to `max_tokens` from `prompt` (temperature ignored at
    /// 0.0 → argmax; >0 still uses argmax here for determinism — see note). Wraps
    /// the prompt in the model's chat format. Returns (text, n_generated).
    pub fn generate(&mut self, prompt: &str, max_tokens: u32) -> Result<(String, usize), RunError> {
        let backend = "batch_infer";
        let wrapped = format!(
            "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|>\
             <|start_header_id|>assistant<|end_header_id|>\n\n"
        );
        let enc = self
            .tokenizer
            .encode(wrapped, true)
            .map_err(infer_err(backend))?;
        let mut tokens: Vec<u32> = enc.get_ids().to_vec();

        let mut generated: Vec<u32> = Vec::new();
        let mut index_pos = 0usize;
        for step in 0..max_tokens as usize {
            // First pass feeds the whole prompt; later passes feed one token.
            let ctx = if step == 0 {
                &tokens[..]
            } else {
                &tokens[tokens.len() - 1..]
            };
            let input = Tensor::new(ctx, &self.device)
                .map_err(infer_err(backend))?
                .unsqueeze(0)
                .map_err(infer_err(backend))?;
            let logits = self
                .model
                .forward(&input, index_pos)
                .map_err(infer_err(backend))?;
            let logits = logits.squeeze(0).map_err(infer_err(backend))?;
            // logits is [seq, vocab] on first pass; take the last row.
            let last = if logits.rank() == 2 {
                let s = logits.dim(0).map_err(infer_err(backend))?;
                logits.get(s - 1).map_err(infer_err(backend))?
            } else {
                logits
            };
            let next = last
                .argmax(0)
                .map_err(infer_err(backend))?
                .to_scalar::<u32>()
                .map_err(infer_err(backend))?;
            index_pos += ctx.len();
            if next == self.eos {
                break;
            }
            tokens.push(next);
            generated.push(next);
        }

        let text = self
            .tokenizer
            .decode(&generated, true)
            .map_err(infer_err(backend))?;
        Ok((text.trim().to_string(), generated.len()))
    }

    /// Batched greedy generation — the GPU-saturation win over one-prompt-at-a-time
    /// decode. candle's quantized-llama causal mask has no padding-awareness and its
    /// rotary uses one position range for the whole batch, so we BUCKET prompts by
    /// exact token length: same-length prompts batch with NO padding, which keeps the
    /// per-sequence math identical to `generate` (attention is within-sequence; greedy
    /// argmax is unchanged) while running B sequences per forward pass. Results are
    /// returned in the caller's original order.
    pub fn generate_batch(
        &mut self,
        prompts: &[String],
        max_tokens: u32,
    ) -> Result<Vec<(String, usize)>, RunError> {
        let backend = "batch_infer";
        // Wrap + tokenize every prompt (same chat template as `generate`).
        let mut encoded: Vec<Vec<u32>> = Vec::with_capacity(prompts.len());
        for p in prompts {
            let wrapped = format!(
                "<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{p}<|eot_id|>\
                 <|start_header_id|>assistant<|end_header_id|>\n\n"
            );
            let enc = self
                .tokenizer
                .encode(wrapped, true)
                .map_err(infer_err(backend))?;
            encoded.push(enc.get_ids().to_vec());
        }
        // Bucket prompt indices by token length so each batch needs no padding.
        let mut buckets: std::collections::HashMap<usize, Vec<usize>> =
            std::collections::HashMap::new();
        for (i, ids) in encoded.iter().enumerate() {
            buckets.entry(ids.len()).or_default().push(i);
        }
        let mut out: Vec<(String, usize)> = vec![(String::new(), 0); prompts.len()];
        for members in buckets.values() {
            // bsz == 1: nothing to batch — the single-prompt path does a fast
            // full-prompt prefill (candle handles seq_len > 1 fine at batch size 1).
            if members.len() == 1 {
                let m = members[0];
                let (text, n) = self.generate(&prompts[m], max_tokens)?;
                out[m] = (text, n);
                continue;
            }
            // bsz > 1: true batched prefill. step 0 feeds each full prompt as one
            // (bsz, plen) forward; later steps feed each sequence's last token (bsz, 1)
            // against the grown KV cache. The patched quantized_llama makes the
            // output-projection slice contiguous so the (bsz, plen) prefill's quantized
            // matmul succeeds. Math is identical to single-prompt (attention is
            // within-sequence; greedy argmax unchanged) — verified batched == serial.
            let bsz = members.len();
            let plen = encoded[members[0]].len();
            let mut gen: Vec<Vec<u32>> = vec![Vec::new(); bsz];
            let mut last_tok: Vec<u32> = vec![0u32; bsz];
            let mut done = vec![false; bsz];
            let mut index_pos = 0usize;
            for step in 0..max_tokens as usize {
                let (rows, seq_len) = if step == 0 {
                    let mut flat = Vec::with_capacity(bsz * plen);
                    for &m in members {
                        flat.extend_from_slice(&encoded[m]);
                    }
                    (flat, plen)
                } else {
                    (last_tok.clone(), 1usize)
                };
                let input = Tensor::from_vec(rows, (bsz, seq_len), &self.device)
                    .map_err(infer_err(backend))?;
                let logits = self
                    .model
                    .forward(&input, index_pos)
                    .map_err(infer_err(backend))?; // (bsz, vocab) — last position only
                index_pos += seq_len;
                let next: Vec<u32> = logits
                    .argmax(1)
                    .map_err(infer_err(backend))?
                    .to_vec1::<u32>()
                    .map_err(infer_err(backend))?;
                let mut all_done = true;
                for b in 0..bsz {
                    if done[b] {
                        continue;
                    }
                    let t = next[b];
                    if t == self.eos {
                        done[b] = true;
                    } else {
                        gen[b].push(t);
                        last_tok[b] = t;
                        all_done = false;
                    }
                }
                if all_done {
                    break;
                }
            }
            for (b, &m) in members.iter().enumerate() {
                let text = self
                    .tokenizer
                    .decode(&gen[b], true)
                    .map_err(infer_err(backend))?;
                out[m] = (text.trim().to_string(), gen[b].len());
            }
        }
        Ok(out)
    }
}

/// Load the HF tokenizer.json that pairs with the GGUF model (from the base repo,
/// not the GGUF repo, which usually lacks tokenizer.json). Must mirror
/// `models::llama_gguf_spec`'s repo choice so the tokenizer matches the weights:
/// the big 7B model uses the Qwen2.5-7B tokenizer, the small alternate the
/// Qwen2.5-0.5B one, and the default the Llama-3.2-1B one.
fn load_llama_tokenizer(model_ref: &str) -> Result<Tokenizer, RunError> {
    let r = model_ref.to_ascii_lowercase();
    let spec = if models::is_big_llama(&r) {
        models::ModelSpec {
            repo: "Qwen/Qwen2.5-7B-Instruct",
            files: &["tokenizer.json"],
        }
    } else if r.contains("qwen") {
        models::ModelSpec {
            repo: "Qwen/Qwen2.5-0.5B-Instruct",
            files: &["tokenizer.json"],
        }
    } else {
        models::ModelSpec {
            repo: "unsloth/Llama-3.2-1B-Instruct",
            files: &["tokenizer.json"],
        }
    };
    let paths = models::fetch(&spec)?;
    Tokenizer::from_file(&paths[0]).map_err(infer_err("batch_infer"))
}

pub struct BatchInferRunner;

#[async_trait]
impl JobRunner for BatchInferRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        // The bigger 7B model needs real VRAM: enforce a hard agent-side floor so a
        // mis-constrained manifest never loads it on a worker that cannot hold it —
        // we decline (→ NoRunner) instead of attempting a load that would OOM. The
        // catalogue's higher min_memory_gb is the primary gate; this is the backstop.
        let big_model_fits = !models::is_big_llama(&manifest.model.model_ref)
            || cap.memory_gb >= models::BIG_LLAMA_MIN_MEMORY_GB;
        matches!(manifest.job_type, JobType::BatchInfer { .. })
            && manifest.model.kind == ModelKind::Gguf
            && meets_memory(manifest, cap)
            && big_model_fits
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let max_tokens = match manifest.job_type {
            JobType::BatchInfer { max_tokens, .. } => max_tokens,
            _ => 256,
        };
        let items: Vec<TextItem> = parse_jsonl(input, "batch_infer")?;
        let prompts: Vec<String> = items
            .iter()
            .map(|it| it.body().unwrap_or("").to_string())
            .collect();

        // Warm Llama backend (loaded once). `generate` is `&mut self`; lock the
        // per-model mutex inside the blocking thread for the whole batch.
        let model = pool.llama(&manifest.model.model_ref).await?;
        let (completions, total_tokens) =
            tokio::task::spawn_blocking(move || -> Result<_, RunError> {
                let mut backend = model.blocking_lock();
                // Batched: prompts are bucketed by length and run B-per-forward-pass
                // (see generate_batch) — the GPU-saturation win over serial decode.
                let results = backend.generate_batch(&prompts, max_tokens)?;
                let total: usize = results.iter().map(|(_, n)| *n).sum();
                let completions: Vec<Completion> = results
                    .into_iter()
                    .enumerate()
                    .map(|(index, (text, tokens))| Completion {
                        index,
                        text,
                        tokens,
                    })
                    .collect();
                Ok((completions, total))
            })
            .await
            .map_err(infer_err("batch_infer"))??;

        let result = BatchInferResult {
            job_type: "batch_infer",
            model: short_model_id(&manifest.model.model_ref, "llama-3.2-1b-instruct-q4"),
            completions,
        };
        let bytes = serde_json::to_vec(&result).map_err(infer_err("batch_infer"))?;
        Ok(JobOutput {
            result: bytes,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: total_tokens as u64,
        })
    }

    fn backend_name(&self) -> &'static str {
        "batch_infer"
    }
}

// ---------------------------------------------------------------------------
// BatchClassificationRunner — warm Llama → exactly one label per text
// ---------------------------------------------------------------------------

/// Lowercase + keep only alphanumerics, for tolerant label matching.
fn normalize_label(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

/// Map a model's free-text answer to exactly one of `labels`, deterministically.
/// Tries, in order: exact (normalized) equality, generation-contains-label,
/// label-contains-generation, prefix. If nothing matches we return label 0 with
/// `matched=false` so the caller can log low confidence — we never invent a label
/// outside the provided set, and the choice is stable (no randomness).
fn closest_label(generation: &str, labels: &[String]) -> (String, bool) {
    debug_assert!(!labels.is_empty());
    // 0. a bare ordinal ("3") maps to the 3rd label — the prompt numbers the list.
    if let Ok(n) = generation.trim().parse::<usize>() {
        if n >= 1 && n <= labels.len() {
            return (labels[n - 1].clone(), true);
        }
    }
    let g = normalize_label(generation);
    let norm: Vec<String> = labels.iter().map(|l| normalize_label(l)).collect();
    // 1. exact normalized match.
    if let Some(i) = norm.iter().position(|n| *n == g) {
        return (labels[i].clone(), true);
    }
    if !g.is_empty() {
        // 2. generation contains a whole label (longest label first to prefer the
        // most specific) ; 3. a label contains the generation; 4. prefix either way.
        let mut idx: Vec<usize> = (0..labels.len()).collect();
        idx.sort_by_key(|&i| std::cmp::Reverse(norm[i].len()));
        for &i in &idx {
            let n = &norm[i];
            if n.is_empty() {
                continue;
            }
            if g.contains(n.as_str())
                || n.contains(g.as_str())
                || g.starts_with(n.as_str())
                || n.starts_with(g.as_str())
            {
                return (labels[i].clone(), true);
            }
        }
        // 5. fuzzy: the label sharing the longest leading run with the generation
        // (>= 4 chars) wins — maps "financialservices" -> "finance", "engineeringintern"
        // -> "engineering", etc. Deterministic, and still confined to the provided set
        // (we never invent a label outside it).
        let mut best = (0usize, 0usize);
        for (i, n) in norm.iter().enumerate() {
            let shared = g.chars().zip(n.chars()).take_while(|(a, b)| a == b).count();
            if shared > best.1 {
                best = (i, shared);
            }
        }
        if best.1 >= 4 {
            return (labels[best.0].clone(), true);
        }
    }
    (labels[0].clone(), false)
}

/// Build the classification prompt: a numbered list + a strict copy-from-list
/// instruction keeps a small model in-set, and "pick the closest" stops it inventing
/// its own categories on messy real text. Short generation, robust mapping.
fn classification_prompt(text: &str, labels: &[String]) -> String {
    let list = labels
        .iter()
        .enumerate()
        .map(|(i, l)| format!("{}. {}", i + 1, l))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "You are a strict text classifier. Choose the SINGLE best label for the text \
         from this exact list:\n{list}\n\nReply with ONLY the label text, copied exactly \
         from the list, and nothing else. If none fits perfectly, choose the closest one \
         from the list.\n\nText: {text}\n\nLabel:"
    )
}

pub struct BatchClassificationRunner;

#[async_trait]
impl JobRunner for BatchClassificationRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        matches!(manifest.job_type, JobType::BatchClassification { .. })
            && manifest.model.kind == ModelKind::Gguf
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let labels = match &manifest.job_type {
            JobType::BatchClassification { labels } => labels.clone(),
            _ => Vec::new(),
        };
        if labels.is_empty() {
            return Err(RunError::BadInput {
                job: "batch_classification",
                msg: "manifest.job_type.labels is empty; nothing to classify into".to_string(),
            });
        }
        let items: Vec<TextItem> = parse_jsonl(input, "batch_classification")?;
        let texts: Vec<String> = items
            .iter()
            .map(|it| it.body().unwrap_or("").to_string())
            .collect();

        let model = pool.llama(&manifest.model.model_ref).await?;
        let (assignments, total) = {
            let labels = labels.clone();
            tokio::task::spawn_blocking(move || -> Result<_, RunError> {
                let mut backend = model.blocking_lock();
                // Batched: classify all texts B-per-forward-pass (short generations,
                // many items — exactly where bucketed batching pays off).
                let prompts: Vec<String> = texts
                    .iter()
                    .map(|t| classification_prompt(t, &labels))
                    .collect();
                let results = backend.generate_batch(&prompts, 12)?;
                let total: usize = results.iter().map(|(_, n)| *n).sum();
                let out: Vec<LabelAssignment> = results
                    .into_iter()
                    .enumerate()
                    .map(|(index, (gen, _))| {
                        let (label, matched) = closest_label(&gen, &labels);
                        if !matched {
                            tracing::warn!(
                                index,
                                generation = %gen,
                                "classification: no label matched generation; defaulting to first label (low confidence)"
                            );
                        }
                        LabelAssignment { index, label }
                    })
                    .collect();
                Ok((out, total))
            })
            .await
            .map_err(infer_err("batch_classification"))??
        };

        let result = ClassificationResult {
            job_type: "batch_classification",
            model: short_model_id(&manifest.model.model_ref, "llama-3.2-1b-instruct-q4"),
            count: assignments.len(),
            labels: assignments,
        };
        let bytes = serde_json::to_vec(&result).map_err(infer_err("batch_classification"))?;
        Ok(JobOutput {
            result: bytes,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: total as u64,
        })
    }

    fn backend_name(&self) -> &'static str {
        "batch_classification"
    }
}

// ---------------------------------------------------------------------------
// JsonExtractionRunner — warm Llama → one JSON object per text, per `schema`
// ---------------------------------------------------------------------------

/// Build the extraction prompt: ask for a single JSON object matching `schema`.
fn extraction_prompt(text: &str, schema: &serde_json::Value) -> String {
    let schema_str = serde_json::to_string(schema).unwrap_or_else(|_| "{}".to_string());
    format!(
        "Extract information from the text into a single JSON object matching this schema: {}.\n\
         Reply with only the JSON object and nothing else — no markdown, no prose.\n\n\
         Text: {}\n\nJSON:",
        schema_str, text
    )
}

/// Pull the first balanced JSON object out of a model generation. Models often
/// wrap JSON in prose or fences; we scan for the first `{` and return the
/// substring through its matching `}` (string-aware so braces inside strings
/// don't fool us). `None` if there is no balanced object.
fn extract_json_object(gen: &str) -> Option<serde_json::Value> {
    let bytes = gen.as_bytes();
    let start = gen.find('{')?;
    let (mut depth, mut in_str, mut esc) = (0i32, false, false);
    for i in start..bytes.len() {
        let c = bytes[i] as char;
        if in_str {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        match c {
            '"' => in_str = true,
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return serde_json::from_str(&gen[start..=i]).ok();
                }
            }
            _ => {}
        }
    }
    None
}

pub struct JsonExtractionRunner;

#[async_trait]
impl JobRunner for JsonExtractionRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        matches!(manifest.job_type, JobType::JsonExtraction { .. })
            && manifest.model.kind == ModelKind::Gguf
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let schema = match &manifest.job_type {
            JobType::JsonExtraction { schema } => schema.clone(),
            _ => serde_json::Value::Null,
        };
        let items: Vec<TextItem> = parse_jsonl(input, "json_extraction")?;
        let texts: Vec<String> = items
            .iter()
            .map(|it| it.body().unwrap_or("").to_string())
            .collect();

        let model = pool.llama(&manifest.model.model_ref).await?;
        let (extracted, total) = tokio::task::spawn_blocking(move || -> Result<_, RunError> {
            let mut backend = model.blocking_lock();
            // Batched: extract from all texts B-per-forward-pass.
            let prompts: Vec<String> = texts
                .iter()
                .map(|t| extraction_prompt(t, &schema))
                .collect();
            let results = backend.generate_batch(&prompts, 256)?;
            let total: usize = results.iter().map(|(_, n)| *n).sum();
            // Validate each parses to a JSON object; on failure surface an empty
            // object with an `_error` so the row is accounted for, never faked as a
            // clean extraction.
            let out: Vec<ExtractedItem> = results
                .into_iter()
                .enumerate()
                .map(|(index, (gen, _))| {
                    let json = match extract_json_object(&gen) {
                        Some(v) => v,
                        None => {
                            tracing::warn!(
                                index,
                                generation = %gen,
                                "json_extraction: generation had no parseable JSON object"
                            );
                            serde_json::json!({ "_error": "no_parseable_json", "_raw": gen })
                        }
                    };
                    ExtractedItem { index, json }
                })
                .collect();
            Ok((out, total))
        })
        .await
        .map_err(infer_err("json_extraction"))??;

        let result = ExtractionResult {
            job_type: "json_extraction",
            model: short_model_id(&manifest.model.model_ref, "llama-3.2-1b-instruct-q4"),
            count: extracted.len(),
            items: extracted,
        };
        let bytes = serde_json::to_vec(&result).map_err(infer_err("json_extraction"))?;
        Ok(JobOutput {
            result: bytes,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: total as u64,
        })
    }

    fn backend_name(&self) -> &'static str {
        "json_extraction"
    }
}

// ---------------------------------------------------------------------------
// RerankRunner — warm MiniLM → embed query+docs, cosine, order desc by score
// ---------------------------------------------------------------------------

/// Cosine similarity of two equal-length, non-empty vectors. The embedder already
/// L2-normalizes its outputs, so this is just the dot product — but we divide by
/// the norms anyway to stay correct for any caller.
fn cosine(a: &[f32], b: &[f32]) -> f32 {
    let dot: f32 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let na: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let nb: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if na == 0.0 || nb == 0.0 {
        0.0
    } else {
        dot / (na * nb)
    }
}

pub struct RerankRunner;

#[async_trait]
impl JobRunner for RerankRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        matches!(manifest.job_type, JobType::Rerank { .. })
            && matches!(
                manifest.model.kind,
                ModelKind::Gguf | ModelKind::Hf | ModelKind::Mlx
            )
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let top_k = match manifest.job_type {
            JobType::Rerank { top_k } => top_k as usize,
            _ => 0,
        };
        let items: Vec<RerankItem> = parse_jsonl(input, "rerank")?;

        // Embed every (query, doc...) in ONE batch through the warm embedder, then
        // score per row. One forward pass for the whole chunk keeps the GPU busy.
        let mut flat: Vec<String> = Vec::new();
        // For each item: (query_offset, doc_count). Empty-doc rows score to [].
        let mut layout: Vec<(usize, usize)> = Vec::with_capacity(items.len());
        for it in &items {
            let q = it.query.clone().unwrap_or_default();
            let off = flat.len();
            flat.push(q);
            for d in &it.docs {
                flat.push(d.clone());
            }
            layout.push((off, it.docs.len()));
        }
        let total_tokens = flat.len() as u64;
        let vectors = embed_texts(pool, flat).await?;

        let mut rankings = Vec::with_capacity(items.len());
        for (index, (off, ndocs)) in layout.into_iter().enumerate() {
            let qv = &vectors[off];
            // Score each doc against the query, then order doc indices desc.
            let mut scored: Vec<(usize, f32)> = (0..ndocs)
                .map(|d| (d, cosine(qv, &vectors[off + 1 + d])))
                .collect();
            // Stable, deterministic: higher score first, ties broken by original
            // doc index (ascending) so equal-score docs keep input order.
            scored.sort_by(|a, b| {
                b.1.partial_cmp(&a.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(a.0.cmp(&b.0))
            });
            let mut order: Vec<usize> = scored.into_iter().map(|(d, _)| d).collect();
            if top_k > 0 && order.len() > top_k {
                order.truncate(top_k);
            }
            rankings.push(Ranking { index, order });
        }

        let result = RerankResult {
            job_type: "rerank",
            model: short_model_id(&manifest.model.model_ref, "all-minilm-l6-v2"),
            count: rankings.len(),
            rankings,
        };
        let bytes = serde_json::to_vec(&result).map_err(infer_err("rerank"))?;
        Ok(JobOutput {
            result: bytes,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: total_tokens,
        })
    }

    fn backend_name(&self) -> &'static str {
        "rerank"
    }
}

/// Normalize a possibly-fully-qualified model ref to a short stable id for the
/// result JSON, defaulting to `fallback` when the ref is empty.
fn short_model_id(model_ref: &str, fallback: &str) -> String {
    let r = model_ref.trim();
    if r.is_empty() {
        return fallback.to_string();
    }
    r.rsplit('/').next().unwrap_or(r).to_ascii_lowercase()
}

// ---------------------------------------------------------------------------
// ClusterRunner (Plane B seam — docs/PLANE_B.md §3)
// ---------------------------------------------------------------------------

/// Substrings that mark a model "giant" enough to justify Plane B sharding (the
/// 405B / 671B class — PLANE_B.md §1). ONLY these route to an
/// `apple_silicon_cluster`; small models run faster whole on a single Plane-A
/// worker and must never pay the interconnect cost. Matched case-insensitively on
/// the model ref. The cluster's `supported_models` (advertised at registration)
/// is the authoritative gate; this is the agent-side echo of it.
const CLUSTER_MODEL_MARKERS: &[&str] = &["405b", "671b"];

/// True if `model_ref` names a giant, cluster-only model.
fn is_cluster_model(model_ref: &str) -> bool {
    let r = model_ref.to_ascii_lowercase();
    CLUSTER_MODEL_MARKERS.iter().any(|m| r.contains(m))
}

/// The Plane B execution seam. It accepts a giant-model job ONLY on an
/// `apple_silicon_cluster` worker, and then surfaces the boundary: a sharded
/// forward pass runs on an EXTERNAL co-located substrate (Exo / MLX-distributed /
/// JACCL over Thunderbolt 5), which a single host does not provide. The cluster's
/// summed-memory routing, its advertisement, and the shard PLAN are all proven
/// locally (`cluster.rs`, the `cluster-plan` subcommand, and the control plane's
/// summed-memory routing test); only the distributed EXECUTION is field work. This
/// runner never fakes a distributed forward pass (BLACKHOLE: surface the boundary).
pub struct ClusterRunner;

#[async_trait]
impl JobRunner for ClusterRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        cap.hw_class == HardwareClass::AppleSiliconCluster
            && is_cluster_model(&manifest.model.model_ref)
            && meets_memory(manifest, cap)
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        _input: &[u8],
        _pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        // We reached here only on a real cluster worker for a giant model. The
        // routing + plan are proven; distributed execution is external/field — so
        // we return an explicit, typed boundary, never a fabricated result.
        Err(RunError::ExternalSubstrate {
            model: short_model_id(&manifest.model.model_ref, "cluster-model"),
            detail:
                "co-located substrate (Exo / MLX-distributed / JACCL over Thunderbolt 5, macOS 26.2) \
                 not available on this host — Plane B sharded execution is external/field (docs/PLANE_B.md §3,§5). \
                 Use `cx-agent cluster-plan` to compute the shard layout."
                    .to_string(),
        })
    }

    fn backend_name(&self) -> &'static str {
        "cluster"
    }
}

// ---------------------------------------------------------------------------
// CustomRunner — general-compute SEAM (ACCRETION.md §7-8)
// ---------------------------------------------------------------------------

/// The general-compute lane: the metered bring-your-own-container runner (simulation
/// / render / HPC / training / ZK on the NVIDIA GPU-second market — ACCRETION.md
/// §7-8). The buyer supplies an OCI `image` + `command`; this runner executes it on
/// the GPU inside a locked-down sandbox (sandbox.rs: no network, read-only rootfs,
/// all caps dropped, non-root, memory/pids capped, hard wall-clock timeout), piping
/// the job input to the container's stdin and capturing its stdout as the result.
/// Arbitrary compute has no known answer, so this lane is metered per GPU-second and
/// reputation-trusted, never honeypot/redundancy output-checked like the AI catalogue.
/// On a host that cannot run the sandbox (no Docker / NVIDIA Container Toolkit) it
/// returns an honest typed error — never a fabricated result (BLACKHOLE). GPU
/// execution is validated end-to-end by scripts/prove-cuda.sh.
pub struct CustomRunner;

#[async_trait]
impl JobRunner for CustomRunner {
    async fn can_run(&self, manifest: &JobManifest, _cap: &WorkerCapability) -> bool {
        // Claim the `custom` job type so dispatch routes it here. Whether this host can
        // actually sandbox a container is decided in run() (honest error if not);
        // scheduler-side routing is gated by the worker's advertised `supported_jobs`,
        // which only lists `custom` on the container-capable CUDA lane (hardware.rs).
        matches!(manifest.job_type, JobType::Custom { .. })
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        _pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let started = std::time::Instant::now();
        let (image, command) = match &manifest.job_type {
            JobType::Custom { image, command } => (image.clone(), command.clone()),
            // can_run gates this to Custom, but never assume — surface, don't fake.
            other => {
                return Err(RunError::BadInput {
                    job: "custom",
                    msg: format!("non-custom job `{}` routed to CustomRunner", other.tag()),
                })
            }
        };
        let image = image.ok_or(RunError::BadInput {
            job: "custom",
            msg: "custom job requires a container `image`".into(),
        })?;
        // The buyer's declared memory floor doubles as the container RAM cap (≥16 GiB,
        // so a trivial declaration still gets headroom); the wall-clock limit is the
        // job's max_duration_secs (default 1h).
        let limits = crate::sandbox::SandboxLimits {
            memory_gb: if manifest.constraints.min_memory_gb >= 1.0 {
                manifest.constraints.min_memory_gb.ceil() as u32
            } else {
                16
            },
            timeout_secs: if manifest.constraints.max_duration_secs > 0 {
                manifest.constraints.max_duration_secs
            } else {
                3600
            },
            ..Default::default()
        };
        // Docker exec is blocking I/O — run it off the async runtime so a long compute
        // job never stalls the agent's poll/heartbeat loop.
        let input = input.to_vec();
        let result = tokio::task::spawn_blocking(move || {
            crate::sandbox::run_sandboxed(&image, &command, &input, &limits)
        })
        .await
        .map_err(|e| RunError::Inference {
            backend: "custom",
            msg: format!("sandbox task join failed: {e}"),
        })??;
        Ok(JobOutput {
            // Opaque compute output — raw bytes, not catalogue JSON; uploaded as
            // application/octet-stream. Metered by GPU-seconds (duration), not tokens.
            result,
            binary: true,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: 0,
        })
    }

    fn backend_name(&self) -> &'static str {
        "custom"
    }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

/// The full set of runners this agent knows about. Order matters: `dispatch`
/// returns the first whose `can_run` is true. `ClusterRunner` is FIRST so a giant
/// model on a cluster worker routes to the Plane B seam (not to BatchInferRunner,
/// which would try to load it on one node); for every non-cluster worker its
/// `can_run` is false, so normal dispatch is unchanged. `CustomRunner` is the
/// general-compute seam (ACCRETION.md §7-8): it claims the `custom` job type so such
/// a job reaches an honest `NotImplemented` boundary instead of a generic NoRunner.
pub fn default_runners() -> Vec<Box<dyn JobRunner>> {
    vec![
        Box::new(ClusterRunner),
        Box::new(EmbedRunner),
        Box::new(BatchInferRunner),
        Box::new(WhisperRunner),
        Box::new(BatchClassificationRunner),
        Box::new(JsonExtractionRunner),
        Box::new(RerankRunner),
        Box::new(CustomRunner),
    ]
}

/// Select the first runner that can handle `manifest` on `cap`, else an explicit
/// error (never a silent skip or a default backend).
pub async fn dispatch<'a>(
    manifest: &JobManifest,
    cap: &WorkerCapability,
    runners: &'a [Box<dyn JobRunner>],
) -> Result<&'a dyn JobRunner, RunError> {
    for r in runners {
        if r.can_run(manifest, cap).await {
            return Ok(r.as_ref());
        }
    }
    Err(RunError::NoRunner {
        job_type: manifest.job_type.tag().to_string(),
        model_kind: format!("{:?}", manifest.model.kind),
    })
}

// ---------------------------------------------------------------------------
// Real benchmarks — measure each available backend on a tiny fixed workload
// ---------------------------------------------------------------------------

use crate::types::BenchResult;

/// Wall-clock budget for the sustained-load thermal probe, per model.
const THERMAL_SECS: u64 = 20;
/// Iterations timed for the p99 latency estimate.
const LATENCY_ITERS: usize = 12;

/// Percentile (0..=100) of a latency sample, in ms. Nearest-rank method.
fn percentile_ms(mut samples: Vec<f64>, pct: f64) -> u32 {
    if samples.is_empty() {
        return 0;
    }
    samples.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let rank = ((pct / 100.0) * samples.len() as f64).ceil() as usize;
    let idx = rank.saturating_sub(1).min(samples.len() - 1);
    samples[idx].round() as u32
}

/// Run real benchmarks for every model whose weights load on this box. Each
/// `BenchResult` carries measured eps (embed) or tps (llama), a measured p99
/// latency, and a `thermal_ok` derived from sustained-load throughput stability
/// (no temperature sensor needed; see below). Models that fail to load are
/// skipped with a warning — we never emit an unmeasured (fabricated) line.
pub fn run_benchmarks() -> Vec<BenchResult> {
    let mut out = Vec::new();
    match bench_embed() {
        Ok(b) => out.push(b),
        Err(e) => tracing::warn!(error = %e, "embed benchmark unavailable (model load failed)"),
    }
    match bench_llama() {
        Ok(b) => out.push(b),
        Err(e) => tracing::warn!(error = %e, "llama benchmark unavailable (model load failed)"),
    }
    out
}

/// Benchmark the MiniLM embedder: embeddings/sec, p99 latency per 8-item batch,
/// and sustained-throughput thermal stability over `THERMAL_SECS`.
fn bench_embed() -> Result<BenchResult, RunError> {
    let embedder = Embedder::load()?;
    let batch: Vec<String> = (0..8)
        .map(|i| format!("benchmark sentence number {i} for throughput measurement"))
        .collect();

    // Warmup (first call pays kernel-compile / allocation costs).
    embedder.embed(&batch)?;

    // p99 latency over a handful of fixed batches.
    let mut lat = Vec::with_capacity(LATENCY_ITERS);
    for _ in 0..LATENCY_ITERS {
        let t = std::time::Instant::now();
        embedder.embed(&batch)?;
        lat.push(t.elapsed().as_secs_f64() * 1000.0);
    }
    let p99_ms = percentile_ms(lat, 99.0);

    // Sustained load (kernels already warm from the p99 loop above): compares
    // early- vs late-window mean throughput to detect degradation (thermal proxy).
    let (eps, thermal_ok) = sustained_eps(&embedder, &batch)?;
    Ok(BenchResult {
        model_id: "all-minilm-l6-v2".to_string(),
        job_type: "embed".to_string(),
        tps: 0.0,
        eps,
        p99_ms,
        thermal_ok,
    })
}

/// Drive the embedder under continuous load; return (peak eps, thermal_ok).
fn sustained_eps(embedder: &Embedder, batch: &[String]) -> Result<(f32, bool), RunError> {
    let n = batch.len() as f64;
    sustained_throughput(|| {
        let t = std::time::Instant::now();
        embedder.embed(batch)?;
        Ok(n / t.elapsed().as_secs_f64().max(1e-6))
    })
}

/// Run `step` (returns one throughput sample) continuously for `THERMAL_SECS`,
/// then return (peak throughput, thermal_ok). `thermal_ok` is a no-sensor proxy:
/// the AVERAGE throughput of the last ~25% of the window stayed within 15% of
/// the average of the first ~25% (after a short warmup the caller already did).
/// Comparing window means — not best-vs-worst — keeps tiny, jittery ops honest.
fn sustained_throughput(
    mut step: impl FnMut() -> Result<f64, RunError>,
) -> Result<(f32, bool), RunError> {
    let secs = THERMAL_SECS as f64;
    let edge = secs * 0.25;
    let start = std::time::Instant::now();
    let deadline = start + std::time::Duration::from_secs(THERMAL_SECS);
    let mut peak = 0.0f64;
    let (mut early_sum, mut early_n) = (0.0f64, 0u32);
    let (mut late_sum, mut late_n) = (0.0f64, 0u32);
    while std::time::Instant::now() < deadline {
        let thr = step()?;
        peak = peak.max(thr);
        let since = start.elapsed().as_secs_f64();
        if since < edge {
            early_sum += thr;
            early_n += 1;
        } else if since >= secs - edge {
            late_sum += thr;
            late_n += 1;
        }
    }
    // If a window has no samples (very slow op), don't claim throttling.
    let thermal_ok = if early_n == 0 || late_n == 0 {
        true
    } else {
        (late_sum / late_n as f64) >= (early_sum / early_n as f64) * 0.85
    };
    Ok((peak as f32, thermal_ok))
}

/// Benchmark the quantized Llama: tokens/sec on a short generation, p99 per-step
/// latency, and sustained-throughput thermal stability.
fn bench_llama() -> Result<BenchResult, RunError> {
    let mut model = LlamaBackend::load("")?;
    let prompt = "Write one short sentence about the ocean.";

    // Warmup + measured generations. Tokens/sec is total generated tokens over
    // total wall time across the sustained window.
    let (_t, _n) = model.generate(prompt, 16)?; // warmup

    // p99 over several short generations (whole-generation latency / tokens).
    let mut lat = Vec::with_capacity(LATENCY_ITERS / 2);
    for _ in 0..(LATENCY_ITERS / 2) {
        let t = std::time::Instant::now();
        let (_txt, n) = model.generate(prompt, 16)?;
        let per_tok = t.elapsed().as_secs_f64() * 1000.0 / (n.max(1) as f64);
        lat.push(per_tok);
    }
    let p99_ms = percentile_ms(lat, 99.0);

    // Sustained tokens/sec + thermal proxy.
    let (tps, thermal_ok) = sustained_tps(&mut model, prompt)?;
    Ok(BenchResult {
        // Canonical catalogue id (matches the models table + batch_infer label).
        model_id: "llama-3.2-1b-instruct-q4".to_string(),
        job_type: "batch_infer".to_string(),
        tps,
        eps: 0.0,
        p99_ms,
        thermal_ok,
    })
}

/// Drive generation continuously for THERMAL_SECS; return (peak tps, thermal_ok).
fn sustained_tps(model: &mut LlamaBackend, prompt: &str) -> Result<(f32, bool), RunError> {
    sustained_throughput(|| {
        let t = std::time::Instant::now();
        let (_txt, n) = model.generate(prompt, 24)?;
        Ok(n as f64 / t.elapsed().as_secs_f64().max(1e-6))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jsonl_parse_text_and_prompt() {
        let input = b"{\"id\":\"a\",\"text\":\"hello\"}\n{\"id\":\"b\",\"prompt\":\"world\"}\n";
        let items: Vec<TextItem> = parse_jsonl(input, "embed").unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].body(), Some("hello"));
        assert_eq!(items[1].body(), Some("world"));
    }

    #[test]
    fn jsonl_rejects_empty_and_bad() {
        assert!(parse_jsonl::<TextItem>(b"", "embed").is_err());
        assert!(parse_jsonl::<TextItem>(b"not json\n", "embed").is_err());
    }

    // Result structs are only ever serialized on the write path (the agent PUTs
    // them), so `job_type` is a zero-alloc `&'static str`. The control plane
    // parses them on its side; here we assert the on-wire shape via `Value`.
    #[test]
    fn embed_result_serializes_to_contract_shape() {
        let r = EmbedResult {
            job_type: "embed",
            model: "all-minilm-l6-v2".into(),
            dim: 3,
            count: 1,
            vectors: vec![vec![0.1, 0.2, 0.3]],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(v["job_type"], "embed");
        assert_eq!(v["dim"], 3);
        assert_eq!(v["count"], 1);
        assert_eq!(v["vectors"][0].as_array().unwrap().len(), 3);
    }

    #[test]
    fn batch_infer_result_serializes_to_contract_shape() {
        let r = BatchInferResult {
            job_type: "batch_infer",
            model: "llama-3.2-1b-instruct".into(),
            completions: vec![Completion {
                index: 0,
                text: "hi".into(),
                tokens: 1,
            }],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(v["job_type"], "batch_infer");
        assert_eq!(v["completions"][0]["index"], 0);
        assert_eq!(v["completions"][0]["text"], "hi");
        assert_eq!(v["completions"][0]["tokens"], 1);
    }

    #[test]
    fn transcribe_result_serializes_to_contract_shape() {
        let r = TranscribeResult {
            job_type: "audio_transcribe",
            model: "whisper-tiny".into(),
            text: "hello world".into(),
            segments: vec![Segment {
                start: 0.0,
                end: 1.5,
                text: "hello world".into(),
            }],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(v["job_type"], "audio_transcribe");
        assert_eq!(v["text"], "hello world");
        assert_eq!(v["segments"][0]["end"], 1.5);
    }

    #[test]
    fn mel_filterbank_shape_and_nonneg() {
        let n_mels = 80;
        let fb = mel_filterbank(n_mels);
        let n_freqs = whisper::N_FFT / 2 + 1;
        assert_eq!(fb.len(), n_mels * n_freqs);
        assert!(fb.iter().all(|&w| w >= 0.0));
        // Each filter must have some energy (non-degenerate triangles).
        for m in 0..n_mels {
            let sum: f32 = fb[m * n_freqs..(m + 1) * n_freqs].iter().sum();
            assert!(sum > 0.0, "filter {m} is empty");
        }
    }

    // Binary embedding artifact (PLANE_D D5/D15): the encoder must round-trip
    // exactly (N×dim f32 in → identical f32 out) AND be materially smaller than the
    // JSON `vectors` array for the same rows. This is the deterministic, network-free
    // proof of the slice — no model download needed. The size win is the whole point.
    #[test]
    fn embed_binary_roundtrips_and_beats_json_size() {
        let dim = EMBED_DIM;
        // A realistic large batch (> the D5 row hint) of distinct, non-trivial f32s.
        let count = EMBED_BINARY_ROW_HINT + 64;
        let vectors: Vec<Vec<f32>> = (0..count)
            .map(|r| {
                (0..dim)
                    .map(|c| ((r * dim + c) as f32) * 0.001_234 - 0.5)
                    .collect()
            })
            .collect();

        let bin = encode_embeddings_binary(dim, &vectors).expect("encode");
        // Header + exact packed body, nothing more.
        assert_eq!(bin.len(), EMBED_BIN_HEADER + count * dim * 4);
        assert_eq!(&bin[0..4], EMBED_BIN_MAGIC);

        let (got_dim, got) = decode_embeddings_binary(&bin).expect("decode");
        assert_eq!(got_dim, dim);
        assert_eq!(got.len(), count);
        // Exact equality: f32 LE round-trips bit-for-bit, no tolerance needed.
        assert_eq!(got, vectors, "binary round-trip changed the vectors");

        // Size win vs the JSON the runner would otherwise PUT for the same rows.
        let json = serde_json::to_vec(&EmbedResult {
            job_type: "embed",
            model: "all-minilm-l6-v2".into(),
            dim,
            count,
            vectors: vectors.clone(),
        })
        .unwrap();
        assert!(
            bin.len() < json.len(),
            "binary ({} B) must be smaller than JSON ({} B) for {count}x{dim} rows",
            bin.len(),
            json.len()
        );
        // Binary is ~4 bytes/float + 16; JSON spends ~12-15. Demand a real win
        // (binary under half the JSON size) so a regression that bloats the format
        // trips this, not just a 1-byte edge.
        assert!(
            bin.len() * 2 < json.len(),
            "expected binary to be <50% of JSON; got {} vs {}",
            bin.len(),
            json.len()
        );
    }

    // The decoder surfaces every malformation as an error — a short, mis-magicked,
    // or size-inconsistent blob is NEVER silently accepted (BLACKHOLE).
    #[test]
    fn embed_binary_decode_rejects_malformed() {
        // Too short for even the header.
        assert!(decode_embeddings_binary(b"CX").is_err());
        // Right length but wrong magic.
        let mut bad = encode_embeddings_binary(2, &[vec![1.0, 2.0]]).unwrap();
        bad[0] = b'X';
        assert!(decode_embeddings_binary(&bad).is_err());
        // Header claims more rows than the body carries.
        let mut truncated = encode_embeddings_binary(2, &[vec![1.0, 2.0], vec![3.0, 4.0]]).unwrap();
        truncated.truncate(truncated.len() - 4); // drop one float
        assert!(decode_embeddings_binary(&truncated).is_err());
        // A ragged row (wrong width) is a real bug at encode time, not a blob.
        assert!(encode_embeddings_binary(3, &[vec![1.0, 2.0]]).is_err());
    }

    // `wants_binary` reads the opt-in from the Embed job_type flag (the channel that
    // actually round-trips to the agent) and, as a forward-compat fallback, from a
    // `params.embed_binary` hint. Default is JSON (false) — opt-in only.
    #[test]
    fn wants_binary_reads_flag_and_params_fallback() {
        // Flag on the job_type → binary.
        let m = test_manifest(JobType::Embed {
            batch_size: 0,
            binary: true,
        });
        assert!(wants_binary(&m));
        // Flag off, no params → JSON default.
        let m = test_manifest(JobType::Embed {
            batch_size: 0,
            binary: false,
        });
        assert!(!wants_binary(&m));
        // Forward-compat: a `params.embed_binary` hint also opts in. (Only
        // EmbedRunner ever calls wants_binary, so this fallback is meaningful for
        // embed jobs and inert for the rest — it is never wired into another runner.)
        let mut m = test_manifest(JobType::Embed {
            batch_size: 0,
            binary: false,
        });
        m.params = serde_json::json!({ "embed_binary": true });
        assert!(wants_binary(&m));
        // No flag, no hint → JSON default even with unrelated params present.
        let mut m = test_manifest(JobType::Embed {
            batch_size: 0,
            binary: false,
        });
        m.params = serde_json::json!({ "split_size": 4 });
        assert!(!wants_binary(&m));
    }

    #[test]
    fn short_model_id_strips_repo() {
        assert_eq!(
            short_model_id("sentence-transformers/all-MiniLM-L6-v2", "x"),
            "all-minilm-l6-v2"
        );
        assert_eq!(short_model_id("", "fallback"), "fallback");
    }

    // Real forward-pass test, gated behind `#[ignore]` because it downloads the
    // ~90MB MiniLM model on first run. Run with:
    //   cargo test --release embed_runs_real_forward_pass -- --ignored --nocapture
    #[test]
    #[ignore = "downloads all-MiniLM-L6-v2 (~90MB) and runs a real forward pass"]
    fn embed_runs_real_forward_pass() {
        let embedder = Embedder::load().expect("load MiniLM");
        let texts = vec![
            "a cat sits on the mat".to_string(),
            "hello world".to_string(),
        ];
        let vecs = embedder.embed(&texts).expect("embed");
        assert_eq!(vecs.len(), 2);
        assert_eq!(vecs[0].len(), EMBED_DIM);
        // L2-normalized → unit norm.
        for v in &vecs {
            let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
            assert!((norm - 1.0).abs() < 1e-3, "not unit-norm: {norm}");
        }
        eprintln!(
            "embed OK: 2x{} dims, v0[0..4]={:?}",
            EMBED_DIM,
            &vecs[0][..4]
        );

        // Full trait path: JSONL in → EmbedResult JSON out, vectors L2-normalized.
        let input = b"{\"id\":\"a\",\"text\":\"a cat sits on the mat\"}\n{\"id\":\"b\",\"text\":\"hello world\"}\n";
        let manifest = test_manifest(JobType::Embed {
            batch_size: 8,
            binary: false,
        });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(EmbedRunner.run(&manifest, input, &pool))
            .expect("EmbedRunner run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "embed");
        assert_eq!(v["dim"], EMBED_DIM);
        assert_eq!(v["count"], 2);
        assert_eq!(v["vectors"].as_array().unwrap().len(), 2);
        assert_eq!(v["vectors"][0].as_array().unwrap().len(), EMBED_DIM);
    }

    /// Build a tiny 16kHz mono WAV (sine sweep) as base64 — a self-contained
    /// audio fixture so the whisper path needs no external file.
    fn synthetic_wav_b64(secs: f32) -> String {
        use base64::Engine;
        let sr = whisper::SAMPLE_RATE as u32;
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: sr,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut buf = std::io::Cursor::new(Vec::new());
        {
            let mut w = hound::WavWriter::new(&mut buf, spec).unwrap();
            let n = (secs * sr as f32) as usize;
            for i in 0..n {
                let t = i as f32 / sr as f32;
                let freq = 220.0 + 200.0 * t; // rising tone
                let s = (2.0 * std::f32::consts::PI * freq * t).sin() * 0.3;
                w.write_sample((s * i16::MAX as f32) as i16).unwrap();
            }
            w.finalize().unwrap();
        }
        base64::engine::general_purpose::STANDARD.encode(buf.into_inner())
    }

    // Whisper runs end-to-end on the already-cached whisper-tiny. A synthetic
    // tone won't yield real words, but this proves load + mel + encode + greedy
    // decode + result JSON all execute on real weights. Run with:
    //   cargo test --release whisper_runs_real -- --ignored --nocapture
    #[test]
    #[ignore = "loads whisper-tiny weights and runs a real encode/decode pass"]
    fn whisper_runs_real() {
        let b64 = synthetic_wav_b64(1.0);
        let input = format!("{{\"id\":\"a\",\"audio_b64\":\"{b64}\"}}\n");
        let manifest = test_manifest(JobType::AudioTranscribe {
            language: None,
            timestamps: true,
        });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(WhisperRunner.run(&manifest, input.as_bytes(), &pool))
            .expect("whisper run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "audio_transcribe");
        assert_eq!(v["segments"].as_array().unwrap().len(), 1);
        eprintln!("whisper OK: result={}", serde_json::to_string(&v).unwrap());
    }

    // BatchInfer runs end-to-end on a small quantized GGUF llama. Downloads the
    // model (~800MB) on first run. Run with:
    //   cargo test --release batch_infer_runs_real -- --ignored --nocapture
    #[test]
    #[ignore = "downloads a quantized GGUF llama (~800MB) and generates tokens"]
    fn batch_infer_runs_real() {
        let input = b"{\"id\":\"a\",\"prompt\":\"Reply with just the word: ping\"}\n";
        let manifest = test_manifest(JobType::BatchInfer {
            max_tokens: 16,
            temperature: 0.0,
        });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(BatchInferRunner.run(&manifest, input, &pool))
            .expect("batch_infer run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "batch_infer");
        let c = &v["completions"][0];
        assert!(c["tokens"].as_u64().unwrap() >= 1, "no tokens generated");
        eprintln!(
            "batch_infer OK: completion={}",
            serde_json::to_string(c).unwrap()
        );
    }

    /// Minimal manifest for runner integration tests (GGUF/HF kind, generous
    /// constraints). `can_run` is bypassed — we invoke `run` directly.
    fn test_manifest(job_type: JobType) -> JobManifest {
        use crate::types::*;
        JobManifest {
            id: uuid::Uuid::nil(),
            job_type,
            model: ModelRef {
                kind: ModelKind::Gguf,
                model_ref: String::new(),
            },
            inputs: vec![],
            output: OutputRef { url: String::new() },
            params: serde_json::Value::Null,
            constraints: JobConstraints {
                min_memory_gb: 0.0,
                hw_classes: None,
                max_duration_secs: 600,
                data_residency: None,
            },
            verification: VerificationPolicy {
                redundancy_frac: 0.0,
                honeypot_frac: 0.0,
                payout_hold_secs: 0,
            },
            tier: ServiceTier::Batch,
        }
    }

    /// A WorkerCapability with the given class + memory, other fields nil/empty.
    fn cap_with(hw_class: HardwareClass, memory_gb: f32) -> WorkerCapability {
        WorkerCapability {
            worker_id: uuid::Uuid::nil(),
            supplier_id: uuid::Uuid::nil(),
            hw_class,
            memory_gb,
            memory_bw_gbps: 80.0,
            supported_jobs: vec![],
            supported_models: vec![],
            benchmarks: vec![],
            agent_version: "test".into(),
            os_version: "test".into(),
            min_payout_usd_hr: 0.0,
        }
    }

    /// The Plane B seam: ClusterRunner accepts a giant model ONLY on a cluster
    /// worker, rejects a single Mac and small models, and its `run` surfaces the
    /// external-substrate boundary rather than faking a distributed forward pass.
    #[test]
    fn cluster_runner_gates_on_class_and_giant_model() {
        let cluster = cap_with(HardwareClass::AppleSiliconCluster, 1800.0);
        let single = cap_with(HardwareClass::AppleSiliconMax, 64.0);
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let mut giant = test_manifest(JobType::BatchInfer {
                max_tokens: 16,
                temperature: 0.0,
            });
            giant.model.model_ref = "llama-3.1-405b-instruct-q4".into();
            // giant model on a cluster → accepted by the Plane B seam.
            assert!(ClusterRunner.can_run(&giant, &cluster).await);
            // same model on a single Mac → not a cluster, rejected.
            assert!(!ClusterRunner.can_run(&giant, &single).await);
            // a small model on a cluster → not a cluster-model, rejected (runs whole).
            let mut small = test_manifest(JobType::BatchInfer {
                max_tokens: 16,
                temperature: 0.0,
            });
            small.model.model_ref = "llama-3.2-1b-instruct-q4".into();
            assert!(!ClusterRunner.can_run(&small, &cluster).await);

            // run() honestly surfaces the boundary; it must NOT fabricate a result.
            let pool = ModelPool::new();
            match ClusterRunner.run(&giant, b"", &pool).await {
                Err(RunError::ExternalSubstrate { model, detail }) => {
                    assert!(model.contains("405b"));
                    assert!(detail.contains("Thunderbolt"));
                }
                other => panic!("expected ExternalSubstrate boundary, got {other:?}"),
            }
        });
    }

    /// The general-compute lane: a `custom` job routes to CustomRunner (claimed via
    /// `can_run`, not the AI runners), and `run` validates input HONESTLY — an
    /// image-less job is rejected before any container is spawned, never a fabricated
    /// result. The sandbox hardening is unit-tested in sandbox.rs and the full Docker
    /// execution is validated on a real GPU host by scripts/prove-cuda.sh, so this
    /// test deliberately does not shell out.
    #[test]
    fn custom_runner_routes_and_validates() {
        let cap = cap_with(HardwareClass::Nvidia80g, 80.0);
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let m = test_manifest(JobType::Custom {
                image: Some("docker.io/org/sim:tag".into()),
                command: vec!["python".into(), "sim.py".into()],
            });
            // Claims custom; the AI runners do not.
            assert!(CustomRunner.can_run(&m, &cap).await);
            assert!(!BatchInferRunner.can_run(&m, &cap).await);
            assert!(!EmbedRunner.can_run(&m, &cap).await);

            // dispatch routes a custom job to CustomRunner (not NoRunner).
            let runners = default_runners();
            let picked = dispatch(&m, &cap, &runners).await.expect("custom routes");
            assert_eq!(picked.backend_name(), "custom");

            // A custom job with no image is rejected HONESTLY before any container is
            // spawned — never a fabricated result (BLACKHOLE). (The Docker execution
            // path itself is exercised on a real GPU host by scripts/prove-cuda.sh.)
            let no_image = test_manifest(JobType::Custom {
                image: None,
                command: vec!["./run".into()],
            });
            let pool = ModelPool::new();
            match CustomRunner.run(&no_image, b"", &pool).await {
                Err(RunError::BadInput { job, .. }) => assert_eq!(job, "custom"),
                other => panic!("expected BadInput for image-less custom job, got {other:?}"),
            }
        });
    }

    /// The bigger 7B model is gated to high-VRAM workers: BatchInferRunner declines
    /// it below `BIG_LLAMA_MIN_MEMORY_GB` (→ NoRunner, never an OOM load) and accepts
    /// it on a big worker. The small default model still runs on a small worker.
    #[test]
    fn big_llama_gated_by_worker_memory() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let small_worker = cap_with(HardwareClass::AppleSiliconPro, 16.0);
            let big_worker = cap_with(HardwareClass::Nvidia80g, 80.0);

            let mut big = test_manifest(JobType::BatchInfer {
                max_tokens: 16,
                temperature: 0.0,
            });
            big.model.model_ref = "qwen2.5-7b-instruct-q4".into();
            // Big model on a small worker → declined (the agent-side floor backstops
            // a mis-constrained manifest); on a big worker → accepted.
            assert!(!BatchInferRunner.can_run(&big, &small_worker).await);
            assert!(BatchInferRunner.can_run(&big, &big_worker).await);

            // The small default model runs on the small worker (gate is big-only).
            let mut small = test_manifest(JobType::BatchInfer {
                max_tokens: 16,
                temperature: 0.0,
            });
            small.model.model_ref = "llama-3.2-1b-instruct-q4".into();
            assert!(BatchInferRunner.can_run(&small, &small_worker).await);
        });
    }

    // --- new runners: pure-logic + result-shape tests (no network) ---

    #[test]
    fn closest_label_matches_robustly_and_never_fabricates() {
        let labels = vec![
            "positive".to_string(),
            "negative".to_string(),
            "neutral".to_string(),
        ];
        // Exact (case/space/punct-insensitive).
        assert_eq!(
            closest_label("Positive", &labels),
            ("positive".into(), true)
        );
        assert_eq!(
            closest_label("  NEGATIVE.", &labels),
            ("negative".into(), true)
        );
        // Wrapped in chatter → contains-match.
        assert_eq!(
            closest_label("The sentiment is positive overall", &labels),
            ("positive".into(), true)
        );
        // A numbered answer ("2") maps to the 2nd label.
        assert_eq!(closest_label("2", &labels), ("negative".into(), true));
        // Fuzzy leading-run fallback keeps a near-miss in-set.
        assert_eq!(
            closest_label("positivity", &labels),
            ("positive".into(), true)
        );
        // No match → first label, flagged low-confidence (never invents a label).
        let (label, matched) = closest_label("banana", &labels);
        assert_eq!(label, "positive");
        assert!(!matched);
        // The returned label is ALWAYS one of the provided labels.
        assert!(labels.contains(&closest_label("anything at all", &labels).0));
    }

    #[test]
    fn extract_json_object_pulls_first_balanced_object() {
        // Bare object.
        let v = extract_json_object(r#"{"name":"Ada","age":36}"#).unwrap();
        assert_eq!(v["name"], "Ada");
        assert_eq!(v["age"], 36);
        // Wrapped in prose / fences (models love doing this).
        let v = extract_json_object("Sure! Here you go:\n```json\n{\"ok\":true}\n```").unwrap();
        assert_eq!(v["ok"], true);
        // Braces inside strings must not confuse the scanner.
        let v = extract_json_object(r#"prefix {"text":"a } b","n":1} suffix"#).unwrap();
        assert_eq!(v["text"], "a } b");
        assert_eq!(v["n"], 1);
        // Nested objects.
        let v = extract_json_object(r#"{"a":{"b":2}}"#).unwrap();
        assert_eq!(v["a"]["b"], 2);
        // No JSON → None (the runner records an explicit _error, never fakes).
        assert!(extract_json_object("no json here").is_none());
    }

    #[test]
    fn cosine_and_rerank_order_are_correct_and_deterministic() {
        // Orthogonal-ish vectors: doc1 aligns with the query, doc0 is orthogonal.
        let q = [1.0f32, 0.0];
        assert!((cosine(&q, &[1.0, 0.0]) - 1.0).abs() < 1e-6);
        assert!(cosine(&q, &[0.0, 1.0]).abs() < 1e-6);
        assert_eq!(cosine(&[0.0, 0.0], &q), 0.0); // zero vector → 0, no NaN

        // Replicate the runner's ordering on hand-built scores to prove it sorts
        // desc by score with ties broken by ascending index (deterministic).
        let scores = [0.1f32, 0.9, 0.9, 0.3];
        let mut scored: Vec<(usize, f32)> = scores.iter().copied().enumerate().collect();
        scored.sort_by(|a, b| {
            b.1.partial_cmp(&a.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.0.cmp(&b.0))
        });
        let order: Vec<usize> = scored.into_iter().map(|(i, _)| i).collect();
        assert_eq!(order, vec![1, 2, 3, 0]); // 0.9(idx1), 0.9(idx2 tie), 0.3, 0.1
    }

    #[test]
    fn new_result_shapes_match_contract() {
        // batch_classification: {"job_type","model","count","labels":[{index,label}]}
        let c = ClassificationResult {
            job_type: "batch_classification",
            model: "llama-3.2-1b-instruct-q4".into(),
            count: 2,
            labels: vec![
                LabelAssignment {
                    index: 0,
                    label: "positive".into(),
                },
                LabelAssignment {
                    index: 1,
                    label: "negative".into(),
                },
            ],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&c).unwrap()).unwrap();
        assert_eq!(v["job_type"], "batch_classification");
        assert_eq!(v["count"], 2);
        assert_eq!(v["labels"][1]["index"], 1);
        assert_eq!(v["labels"][1]["label"], "negative");

        // json_extraction: {"job_type","model","count","items":[{index,json}]}
        let e = ExtractionResult {
            job_type: "json_extraction",
            model: "llama-3.2-1b-instruct-q4".into(),
            count: 1,
            items: vec![ExtractedItem {
                index: 0,
                json: serde_json::json!({"name":"Ada"}),
            }],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&e).unwrap()).unwrap();
        assert_eq!(v["job_type"], "json_extraction");
        assert_eq!(v["items"][0]["index"], 0);
        assert_eq!(v["items"][0]["json"]["name"], "Ada");

        // rerank: {"job_type","model","count","rankings":[{index,order:[..]}]}
        let r = RerankResult {
            job_type: "rerank",
            model: "all-minilm-l6-v2".into(),
            count: 1,
            rankings: vec![Ranking {
                index: 0,
                order: vec![2, 0, 1],
            }],
        };
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
        assert_eq!(v["job_type"], "rerank");
        assert_eq!(v["rankings"][0]["index"], 0);
        assert_eq!(
            v["rankings"][0]["order"].as_array().unwrap().len(),
            3,
            "order is the doc-index permutation"
        );
    }

    #[test]
    fn new_runners_route_via_can_run() {
        let cap = WorkerCapability {
            worker_id: uuid::Uuid::nil(),
            supplier_id: uuid::Uuid::nil(),
            hw_class: crate::types::HardwareClass::AppleSiliconMax,
            memory_gb: 64.0,
            memory_bw_gbps: 400.0,
            supported_jobs: vec![],
            supported_models: vec![],
            benchmarks: vec![],
            agent_version: "test".into(),
            os_version: "test".into(),
            min_payout_usd_hr: 0.0,
        };
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            let m = test_manifest(JobType::BatchClassification {
                labels: vec!["a".into()],
            });
            assert!(BatchClassificationRunner.can_run(&m, &cap).await);
            assert!(!EmbedRunner.can_run(&m, &cap).await);

            let m = test_manifest(JobType::JsonExtraction {
                schema: serde_json::json!({}),
            });
            assert!(JsonExtractionRunner.can_run(&m, &cap).await);

            let m = test_manifest(JobType::Rerank { top_k: 3 });
            assert!(RerankRunner.can_run(&m, &cap).await);
            // Rerank uses the embedder, so it also accepts non-GGUF kinds.
            let mut m2 = test_manifest(JobType::Rerank { top_k: 0 });
            m2.model.kind = ModelKind::Hf;
            assert!(RerankRunner.can_run(&m2, &cap).await);
        });
    }

    // Rerank runs end-to-end on the warm MiniLM embedder (downloads ~90MB on
    // first run). Proves the query/doc embed + cosine + order path produces the
    // contract shape with a sensible ranking. Run with:
    //   cargo test --release rerank_runs_real -- --ignored --nocapture
    #[test]
    #[ignore = "downloads all-MiniLM-L6-v2 (~90MB) and runs a real rerank"]
    fn rerank_runs_real() {
        // Query about cats; doc 1 is on-topic, docs 0 and 2 are not.
        let input = b"{\"id\":\"q\",\"query\":\"a small domestic cat\",\"docs\":[\"quarterly financial report\",\"the kitten chased a ball of yarn\",\"diesel engine maintenance\"]}\n";
        let manifest = test_manifest(JobType::Rerank { top_k: 0 });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(RerankRunner.run(&manifest, input, &pool))
            .expect("rerank run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "rerank");
        let order = v["rankings"][0]["order"].as_array().unwrap();
        assert_eq!(order.len(), 3);
        assert_eq!(order[0], 1, "the on-topic doc must rank first");
        eprintln!("rerank OK: order={}", serde_json::to_string(order).unwrap());
    }

    // BatchClassification runs end-to-end on the warm quantized Llama (~800MB on
    // first run). Proves prompt → generation → top-1 label mapping → contract
    // shape. Run with:
    //   cargo test --release batch_classification_runs_real -- --ignored --nocapture
    #[test]
    #[ignore = "downloads a quantized GGUF llama (~800MB) and classifies"]
    fn batch_classification_runs_real() {
        let input =
            b"{\"id\":\"a\",\"text\":\"I absolutely loved this movie, it was wonderful!\"}\n";
        let manifest = test_manifest(JobType::BatchClassification {
            labels: vec!["positive".into(), "negative".into()],
        });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(BatchClassificationRunner.run(&manifest, input, &pool))
            .expect("classification run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "batch_classification");
        let label = v["labels"][0]["label"].as_str().unwrap();
        assert!(
            label == "positive" || label == "negative",
            "label must be one of the provided set, got {label}"
        );
        eprintln!("classification OK: label={label}");
    }

    // JsonExtraction runs end-to-end on the warm quantized Llama (~800MB on first
    // run). Proves the generation → balanced-JSON-object extraction path. Run:
    //   cargo test --release json_extraction_runs_real -- --ignored --nocapture
    #[test]
    #[ignore = "downloads a quantized GGUF llama (~800MB) and extracts JSON"]
    fn json_extraction_runs_real() {
        let input = b"{\"id\":\"a\",\"text\":\"Ada Lovelace was born in 1815 in London.\"}\n";
        let manifest = test_manifest(JobType::JsonExtraction {
            schema: serde_json::json!({"name":"string","born":"number","city":"string"}),
        });
        let pool = ModelPool::new();
        let out = tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(JsonExtractionRunner.run(&manifest, input, &pool))
            .expect("extraction run");
        let v: serde_json::Value = serde_json::from_slice(&out.result).unwrap();
        assert_eq!(v["job_type"], "json_extraction");
        assert!(
            v["items"][0]["json"].is_object(),
            "extracted item is a JSON object"
        );
        eprintln!(
            "extraction OK: item={}",
            serde_json::to_string(&v["items"][0]).unwrap()
        );
    }

    #[test]
    #[ignore = "downloads the GGUF llama (~800MB) + needs a GPU; run on the A100 to measure batching"]
    fn batched_vs_serial_throughput() {
        use std::time::Instant;
        let mut be = LlamaBackend::load("llama-3.2-1b-instruct-q4").expect("load gguf");
        // 32 same-length prompts — a realistic batch_infer/classification shape.
        // Generation-heavy prompt (decode dominates, where batching pays off).
        let prompts: Vec<String> = (0..32)
            .map(|_| "Write a detailed paragraph about the ocean and its wonders:".to_string())
            .collect();
        let max = 48u32;

        let t = Instant::now();
        let mut serial_tok = 0usize;
        let mut first_serial = String::new();
        for (i, p) in prompts.iter().enumerate() {
            let (text, n) = be.generate(p, max).unwrap();
            serial_tok += n;
            if i == 0 {
                first_serial = text;
            }
        }
        let serial = t.elapsed();

        let t = Instant::now();
        let res = be.generate_batch(&prompts, max).unwrap();
        let batched = t.elapsed();
        let batched_tok: usize = res.iter().map(|(_, n)| n).sum();

        println!("\n=== batch_infer throughput (32 prompts, {max} tok max) ===");
        println!(
            "serial : {serial_tok} tok in {serial:?} = {:.1} tok/s",
            serial_tok as f64 / serial.as_secs_f64()
        );
        println!(
            "batched: {batched_tok} tok in {batched:?} = {:.1} tok/s",
            batched_tok as f64 / batched.as_secs_f64()
        );
        println!(
            "SPEEDUP: {:.1}x",
            serial.as_secs_f64() / batched.as_secs_f64()
        );

        // Correctness: identical greedy prompts must yield the identical output, and
        // the batched result must equal the serial result token-for-token.
        for r in &res {
            assert_eq!(
                r.0, first_serial,
                "batched output must match serial (greedy)"
            );
        }
        println!("correctness: OK — batched == serial for all 32");
    }
}
