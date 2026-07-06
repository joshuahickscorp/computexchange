//! Wire types — the SHARED CONTRACT (the project "horizon").
//!
//! These mirror the canonical JSON used by the Go control plane EXACTLY:
//! snake_case fields, snake_case string enums, tagged `JobType`. This file is
//! the single source of truth for the wire shape on the Rust side; do not let
//! the representation drift from the control plane.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Hardware capability class. Wire: snake_case strings
/// (`apple_silicon_base`, `apple_silicon_pro`, `apple_silicon_max`,
/// `apple_silicon_ultra`, `apple_silicon_cluster`, `nvidia_24g`, `nvidia_48g`,
/// `nvidia_80g`, `nvidia_180g`, `cpu`).
///
/// `AppleSiliconCluster` (Plane B, docs/PLANE_B.md) is a co-located group of Macs
/// that registers as ONE worker whose advertised `memory_gb` is the SUMMED usable
/// unified memory of its members (minus per-node margin). The rest of the system
/// treats it as a single high-memory worker — no model-sharding awareness needed.
///
/// The `nvidia_*` classes are the NVIDIA/CUDA lane, tiered by VRAM (the gating
/// resource on NVIDIA, as unified memory is on Apple). They are a DISTINCT class
/// family from Apple so within-class verification never compares results across
/// architectures — floating-point kernels differ Metal↔CUDA.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HardwareClass {
    AppleSiliconBase,
    AppleSiliconPro,
    AppleSiliconMax,
    AppleSiliconUltra,
    AppleSiliconCluster,
    #[serde(rename = "nvidia_24g")]
    Nvidia24g,
    #[serde(rename = "nvidia_48g")]
    Nvidia48g,
    #[serde(rename = "nvidia_80g")]
    Nvidia80g,
    #[serde(rename = "nvidia_180g")]
    Nvidia180g,
    Cpu,
}

/// Service tier. Wire: `batch | priority | trusted`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ServiceTier {
    Batch,
    Priority,
    Trusted,
}

/// Job type — internally tagged enum.
///
/// Wire examples:
/// `{"type":"embed","batch_size":64}`,
/// `{"type":"batch_infer","max_tokens":512,"temperature":0.0}`.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum JobType {
    // Execution-hint fields default so a reconstructed dispatch manifest that
    // carries only the discriminant (e.g. `{"type":"embed"}`) still decodes —
    // the control plane stores the job_type tag, not every hint, and the real
    // work is driven by the runner + presigned input_url.
    Embed {
        #[serde(default)]
        batch_size: usize,
        /// Opt-in compact output (PLANE_D §11 D5 / §21 D15): when true the runner
        /// emits a BINARY float32 artifact (magic header + packed little-endian
        /// rows) instead of the JSON `vectors` array, saving real bytes on large
        /// embedding outputs. `default` (false) keeps JSON the default + decodable
        /// against an older peer that never sends the field. This hint round-trips
        /// to the agent via the persisted `job_type_spec`, unlike `manifest.params`.
        #[serde(default)]
        binary: bool,
    },
    BatchInfer {
        #[serde(default)]
        max_tokens: u32,
        #[serde(default)]
        temperature: f32,
    },
    AudioTranscribe {
        #[serde(default)]
        language: Option<String>,
        #[serde(default)]
        timestamps: bool,
    },
    ImageGen {
        #[serde(default)]
        resolution: (u32, u32),
        #[serde(default)]
        steps: u32,
    },
    Eval {
        #[serde(default)]
        rubric: serde_json::Value,
    },
    LoraFinetune {
        #[serde(default)]
        epochs: u32,
        #[serde(default)]
        lr: f32,
        #[serde(default)]
        checkpoint_every: u32,
    },
    /// Assign each input text exactly one label from `labels` (warm Llama, top-1).
    BatchClassification {
        #[serde(default)]
        labels: Vec<String>,
    },
    /// Extract a JSON object per input text conforming to `schema` (warm Llama).
    JsonExtraction {
        #[serde(default)]
        schema: serde_json::Value,
    },
    /// Re-order each query's candidate docs by relevance (warm MiniLM, cosine).
    Rerank {
        #[serde(default)]
        top_k: u32,
    },
    /// GENERAL-COMPUTE LANE (ACCRETION.md §7-8): an opaque bring-your-own-container
    /// compute job for the metered NVIDIA GPU-second market (simulation / render /
    /// HPC / training / ZK). `image` is the OCI container reference to run in (None
    /// when only `command` is given); `command` is the argv the sandbox executes
    /// inside it (empty = the image's own entrypoint). The agent runs it ON THE GPU
    /// inside a locked-down sandbox (agent/src/sandbox.rs: no network, read-only
    /// rootfs, all caps dropped, non-root, memory/pids capped, hard wall-clock
    /// timeout), piping the job input to the container's stdin and capturing stdout
    /// as the result; a worker without the sandbox returns an HONEST typed error,
    /// never a fabricated result. Unlike the verified AI catalogue (a known answer,
    /// honeypot/redundancy-checked), arbitrary compute has no known answer, so this
    /// lane is metered per GPU-second, never output-checked.
    Custom {
        #[serde(default)]
        image: Option<String>,
        #[serde(default)]
        command: Vec<String>,
    },
}

impl JobType {
    /// Stable tag string, matching the serde `type` discriminant. Used to build
    /// the `supported_jobs` list and `BenchResult.job_type` without re-serializing.
    pub fn tag(&self) -> &'static str {
        match self {
            JobType::Embed { .. } => "embed",
            JobType::BatchInfer { .. } => "batch_infer",
            JobType::AudioTranscribe { .. } => "audio_transcribe",
            JobType::ImageGen { .. } => "image_gen",
            JobType::Eval { .. } => "eval",
            JobType::LoraFinetune { .. } => "lora_finetune",
            JobType::BatchClassification { .. } => "batch_classification",
            JobType::JsonExtraction { .. } => "json_extraction",
            JobType::Rerank { .. } => "rerank",
            JobType::Custom { .. } => "custom",
        }
    }
}

/// Model reference. Wire: `{ "kind": "gguf"|"hf"|"mlx", "ref": "..." }`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelRef {
    pub kind: ModelKind,
    /// Wire field is `ref` (a Rust keyword), so it is renamed.
    #[serde(rename = "ref")]
    pub model_ref: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelKind {
    Gguf,
    Hf,
    Mlx,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InputRef {
    pub url: String,
    pub bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OutputRef {
    pub url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobConstraints {
    pub min_memory_gb: f32,
    /// `None` = any hardware class.
    pub hw_classes: Option<Vec<HardwareClass>>,
    pub max_duration_secs: u32,
    /// e.g. `["CA", "US"]` to restrict to those countries; `None` = unrestricted.
    pub data_residency: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerificationPolicy {
    pub redundancy_frac: f32,
    pub honeypot_frac: f32,
    pub payout_hold_secs: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobManifest {
    pub id: Uuid,
    pub job_type: JobType,
    pub model: ModelRef,
    pub inputs: Vec<InputRef>,
    pub output: OutputRef,
    pub params: serde_json::Value,
    pub constraints: JobConstraints,
    pub verification: VerificationPolicy,
    pub tier: ServiceTier,
}

/// A single benchmark line for one (model, job_type) pair.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BenchResult {
    pub model_id: String,
    pub job_type: String,
    /// Tokens per second.
    pub tps: f32,
    /// Embeddings per second.
    pub eps: f32,
    pub p99_ms: u32,
    pub thermal_ok: bool,
    /// Wall-clock milliseconds the COLD load of this model took (docs/
    /// CREED_AND_PATH_TO_TEN.md, "Warm model pool" 6.5→7 — "cold-load latency is
    /// completely unmeasured... the quote path can only say a cold load is
    /// possible, never how many seconds it costs"). Measured once, at the
    /// unavoidably-cold first load every fresh agent process does during its
    /// startup benchmark — a real number instead of a category. `#[serde(default)]`
    /// so an older agent's echoed/cached record (which never sent this field)
    /// still deserializes as 0, not a parse failure.
    #[serde(default)]
    pub load_ms: u64,
}

/// Default engine tag for a `WorkerCapability` deserialized without one (an older
/// agent or the register echo from a server that does not round-trip the field):
/// the wired Candle path, which is what such a worker is running. Keeps a
/// single-engine Candle fleet's behavior unchanged.
fn default_engine() -> String {
    "candle".to_string()
}

/// Default build hash for a `WorkerCapability` deserialized without one (an older
/// agent, or the register echo from a server that does not round-trip the field):
/// an empty string, which the control plane treats as "unknown build". An unknown
/// build is NEVER drawn as a byte-exact redundancy peer or auto-docked on a pure
/// byte mismatch — it falls back to provisional trust (mirrors the missing-third-
/// worker pattern), so an older agent that does not advertise the finer class is
/// safe by default, never wrongly quarantined. See docs/DETERMINISM_CLASS.md.
fn default_build_hash() -> String {
    String::new()
}

/// What this worker advertises to the control plane on registration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerCapability {
    pub worker_id: Uuid,
    pub supplier_id: Uuid,
    pub hw_class: HardwareClass,
    /// On-device inference ENGINE this worker runs (`candle` default, plus the
    /// `mlx`/`vllm`/`hawking` lanes). It is the second axis of the verification
    /// class: the control plane draws byte-exact redundancy peers and seeds
    /// honeypots from the SAME (hw_class, engine) class, because two engines'
    /// FP kernels differ even on identical hardware. `default` (deserializes to
    /// "candle" via the field default below — see `default_engine`) keeps the
    /// registration echo decodable against a server that does not round-trip it,
    /// and a single-engine Candle fleet behaves exactly as before.
    #[serde(default = "default_engine")]
    pub engine: String,
    /// Finer verification-class axis BELOW (hw_class, engine): a stable hash of the
    /// byte-output-determining BUILD inputs (engine + agent build + device backend +
    /// catalogue quant — see `hardware::engine_build_hash`). The control plane draws
    /// byte-exact redundancy peers and seeds honeypots from the SAME (hw_class, engine,
    /// build_hash), because a kernel/codegen change between agent builds can shift bytes
    /// even on identical hardware running the same engine (Hawking's own research proves
    /// token-level determinism is impossible across heterogeneous Apple-Silicon
    /// generations). `default` (deserializes to "" — see `default_build_hash`) keeps the
    /// registration echo decodable against a server that does not round-trip it, and an
    /// empty build hash is treated as "unknown" → provisional trust, never an auto-dock.
    #[serde(default = "default_build_hash")]
    pub build_hash: String,
    pub memory_gb: f32,
    pub memory_bw_gbps: f32,
    pub supported_jobs: Vec<String>,
    pub supported_models: Vec<String>,
    pub benchmarks: Vec<BenchResult>,
    pub agent_version: String,
    pub os_version: String,
    /// Operator reservation price (USD/hr): the control plane must not dispatch
    /// work below this. Contract delta; `default` keeps the registration echo
    /// decodable against a server that does not round-trip it yet.
    #[serde(default)]
    pub min_payout_usd_hr: f32,
}

/// A task handed out by the control plane in response to a poll.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskDispatch {
    pub task_id: Uuid,
    pub job_id: Uuid,
    pub manifest: JobManifest,
    pub input_url: String,
    pub output_url: String,
    /// Object key to PUT the result to / echo back in the commit. Contract delta
    /// agreed with the control plane; `default` keeps us robust to older servers
    /// that don't send it yet (we then derive a key from the job/task ids).
    #[serde(default)]
    pub result_key: String,
    /// Presigned PUT URL for the intra-task partial checkpoint object — the
    /// task's `result_key` + ".partial", same expiry as `output_url`. The control
    /// plane sends it ONLY for the generative job types (batch_infer,
    /// batch_classification, json_extraction); `None` from an older control plane
    /// or for any other job. When present the agent MAY periodically PUT the
    /// final result shape plus a top-level `"partial": true` marker to it, so a
    /// killed stuck job still yields mid-chunk progress. Partial objects are
    /// NEVER merged into the verified artifact and NEVER affect payment
    /// (unverified work is not paid). Additive contract delta; `default` keeps
    /// the dispatch decodable against a control plane that never sends it.
    #[serde(default)]
    pub partial_put_url: Option<String>,
    pub deadline: u64,
    /// Pay rate the control plane is offering for this task (USD/hr). Contract
    /// delta; `default` (0.0) means "not advertised" → the min-payout gate treats
    /// 0.0 as "no rate sent" and does not block.
    #[serde(default)]
    pub offered_rate_usd_hr: f32,
}

/// Result submission after a task completes.
///
/// `result_sha256` (Control Plane Hot Path 8->9, docs/internal/
/// CREED_AND_PATH_TO_TEN.md "Get result-commit off the S3 critical path") is the
/// lowercase-hex SHA-256 of the exact bytes just PUT to `output_url`, computed
/// AFTER a successful upload. It lets the control plane trust a hash-to-hash
/// redundancy compare for byte-exact job types instead of re-downloading this
/// same object synchronously inside the commit request. `#[serde(default)]` so
/// an older control plane build's TaskCommit decode (were the field ever
/// reordered/renamed) — and any caller that doesn't set it — still round-trips;
/// the control plane treats an empty string exactly like an absent field and
/// always falls back to a real GetObject, so this is purely additive.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskCommit {
    pub task_id: Uuid,
    pub result_key: String,
    pub duration_ms: u64,
    pub tokens_used: u64,
    #[serde(default)]
    pub result_sha256: String,
    pub hardware_temp_c: Option<f32>,
}

/// Periodic liveness + telemetry signal (every ~30s).
///
/// The resource-protection fields (`available_memory_gb` … `throttled`) are the
/// supplier-throttling contract delta: the control plane's safe-dispatch filter
/// reads `effective_memory_gb` + `throttled` so it never hands a task to a worker
/// that has paused for memory pressure. They `default` so an older control plane
/// (or a heartbeat predating the fields) still decodes.
///
/// `loaded_models` is the warm-routing delta (docs/PLANE_D.md §9 D3): the canonical
/// ids of models currently WARM in the agent's pool. The control plane upserts a
/// worker_model_state row per id and the scheduler gives a small re-rank bonus to a
/// worker that already has the job's model warm (the fastest task avoids a load). It
/// `default`s to empty so an older control plane / a beat predating the field still
/// decodes, and the agent reports REAL ids only — never a fabricated warm set.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Heartbeat {
    pub worker_id: Uuid,
    pub timestamp: u64,
    pub cpu_pct: f32,
    pub gpu_pct: f32,
    pub gpu_temp_c: Option<f32>,
    pub current_task: Option<Uuid>,
    /// Live available (free + reclaimable) memory, GB.
    #[serde(default)]
    pub available_memory_gb: f32,
    /// Effective allocatable memory for jobs = available − reserved headroom, GB.
    #[serde(default)]
    pub effective_memory_gb: f32,
    /// Headroom the operator reserves for their own use, GB.
    #[serde(default)]
    pub reserved_headroom_gb: f32,
    /// True when the agent is currently pausing new claims for memory pressure.
    #[serde(default)]
    pub throttled: bool,
    /// Canonical ids of models currently warm in the pool (warm-routing, D3). Empty
    /// when nothing is loaded yet; real ids only.
    #[serde(default)]
    pub loaded_models: Vec<String>,
}

/// REAL memory snapshot at the moment of a task failure (GB). Sent with a
/// `FailReport` so the control plane can diagnose OOM and feed quote risk — never
/// fabricated (mirrors control/failure.go `FailureMemory`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FailMemory {
    pub total_gb: f32,
    pub available_gb: f32,
    pub effective_gb: f32,
    pub reserved_headroom_gb: f32,
}

/// Body of `POST /v1/worker/task/{id}/fail` (Plane C/D D0): the agent's immediate
/// typed failure report, so a doomed task is requeued in seconds instead of waiting
/// out the 30-min stale reaper. `class` is the shared taxonomy (control/failure.go).
/// Mirrors control/failure.go `FailureReport`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FailReport {
    pub class: String,
    pub message: String,
    pub duration_ms: u64,
    pub backend: String,
    pub model: String,
    pub memory: Option<FailMemory>,
}

/// Earnings summary returned by `GET /v1/worker/earnings`. Consumed by the
/// heartbeat to populate the menu-bar status file (see status.rs).
/// `last_payout_*`/`next_payout_at` are Supplier onboarding & safety 7->8
/// (docs/internal/CREED_AND_PATH_TO_TEN.md, "Populate the trust panel with real
/// data"): real payout proof sourced from this supplier's own ledger rows on the
/// control plane, never fabricated — absent (`None`) when there is no such row
/// yet, matching the control plane's own `omitempty` pointer fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Earnings {
    pub balance_usd: f64,
    pub lifetime_usd: f64,
    #[serde(default)]
    pub last_payout_usd: Option<f64>,
    #[serde(default)]
    pub last_payout_at: Option<u64>,
    #[serde(default)]
    pub next_payout_at: Option<u64>,
}

/// Per-supplier honeypot verification aggregate returned by
/// `GET /v1/worker/verification` (Supplier onboarding & safety 7->8) — the
/// trust-panel data source, distinct from the buyer-facing per-job
/// `Verification` receipt on the control plane. `label` reuses the control
/// plane's own derived vocabulary ("verified" | "honeypot-checked" |
/// "no-independent-peer" | "cross-class-skip" | "unverified").
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SupplierVerification {
    pub honeypots_passed: i64,
    pub honeypots_failed: i64,
    pub verification_label: String,
}

/// Payout readiness returned by `GET /v1/worker/connect/status` (Supplier
/// onboarding & safety 7->8). Mirrors `handleWorkerConnectStatus`'s wire shape
/// exactly: `configured` is true once the control plane has a Stripe key at
/// all, `connected` once this supplier has linked an Express account, `enabled`
/// once Stripe reports `payouts_enabled` on that account.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConnectStatus {
    pub configured: bool,
    pub connected: bool,
    #[serde(rename = "payouts_enabled")]
    pub enabled: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Horizon round-trip for the Plane B class: `apple_silicon_cluster` decodes to
    /// `AppleSiliconCluster` and serializes back to the same wire string, in
    /// lockstep with control/types.go validHWClasses + proto/manifest.schema.json.
    #[test]
    fn hardware_class_cluster_roundtrips() {
        let c: HardwareClass = serde_json::from_str(r#""apple_silicon_cluster""#).unwrap();
        assert_eq!(c, HardwareClass::AppleSiliconCluster);
        assert_eq!(
            serde_json::to_string(&HardwareClass::AppleSiliconCluster).unwrap(),
            r#""apple_silicon_cluster""#
        );
        // The existing classes still round-trip (no drift).
        assert_eq!(
            serde_json::to_string(&HardwareClass::AppleSiliconUltra).unwrap(),
            r#""apple_silicon_ultra""#
        );
    }

    /// Regression (found in the first live end-to-end run): the control plane
    /// returns a *partial* manifest in the poll response — only the job_type
    /// discriminant, model, verification, tier, and an empty `inputs` — because
    /// the real input travels via the presigned `input_url`. The agent must still
    /// decode it. This guards the `#[serde(default)]` on the JobType hint fields
    /// and the dispatch contract (model.kind present, inputs `[]` not `null`).
    #[test]
    fn minimal_poll_dispatch_decodes() {
        let json = r#"{
            "task_id":"00000000-0000-0000-0000-000000000001",
            "job_id":"00000000-0000-0000-0000-000000000002",
            "manifest":{
                "id":"00000000-0000-0000-0000-000000000002",
                "job_type":{"type":"embed"},
                "model":{"kind":"gguf","ref":"all-minilm-l6-v2"},
                "inputs":[],
                "output":{"url":""},
                "params":null,
                "constraints":{"min_memory_gb":0.0,"hw_classes":null,"max_duration_secs":0,"data_residency":null},
                "verification":{"redundancy_frac":0.0,"honeypot_frac":0.0,"payout_hold_secs":5},
                "tier":"batch"
            },
            "input_url":"http://example/in",
            "output_url":"http://example/out",
            "result_key":"jobs/x/tasks/0/result.json",
            "deadline":0
        }"#;
        let d: TaskDispatch = serde_json::from_str(json).expect("minimal dispatch must decode");
        assert!(matches!(d.manifest.job_type, JobType::Embed { .. }));
        assert_eq!(d.manifest.model.kind, ModelKind::Gguf);
        assert_eq!(d.manifest.model.model_ref, "all-minilm-l6-v2");
        assert!(d.manifest.inputs.is_empty());
        assert_eq!(d.result_key, "jobs/x/tasks/0/result.json");
        // No `partial_put_url` on the wire (older control plane) → None, never an
        // error (the intra-task checkpointing delta is strictly additive).
        assert!(d.partial_put_url.is_none());
    }

    /// Intra-task checkpointing contract delta: a dispatch CARRYING
    /// `partial_put_url` decodes it, and the same dispatch without the field (an
    /// older control plane) still decodes to `None` — additive both ways.
    #[test]
    fn dispatch_partial_put_url_decodes_present_and_absent() {
        let base = r#"{
            "task_id":"00000000-0000-0000-0000-000000000001",
            "job_id":"00000000-0000-0000-0000-000000000002",
            "manifest":{
                "id":"00000000-0000-0000-0000-000000000002",
                "job_type":{"type":"batch_infer"},
                "model":{"kind":"gguf","ref":"llama-3.2-1b-instruct-q4"},
                "inputs":[],
                "output":{"url":""},
                "params":null,
                "constraints":{"min_memory_gb":0.0,"hw_classes":null,"max_duration_secs":0,"data_residency":null},
                "verification":{"redundancy_frac":0.0,"honeypot_frac":0.0,"payout_hold_secs":5},
                "tier":"batch"
            },
            "input_url":"http://example/in",
            "output_url":"http://example/out",
            "result_key":"jobs/x/tasks/0/result.json",
            "deadline":0"#;
        let with = format!("{base},\n\"partial_put_url\":\"http://example/out.partial?sig=abc\"}}");
        let d: TaskDispatch = serde_json::from_str(&with).expect("dispatch with partial URL");
        assert_eq!(
            d.partial_put_url.as_deref(),
            Some("http://example/out.partial?sig=abc")
        );

        let without = format!("{base}}}");
        let d: TaskDispatch = serde_json::from_str(&without).expect("dispatch without partial URL");
        assert!(d.partial_put_url.is_none());
    }

    /// A job_type carrying only its discriminant must decode (hint fields default).
    #[test]
    fn jobtype_without_hint_fields_decodes() {
        assert!(matches!(
            serde_json::from_str::<JobType>(r#"{"type":"embed"}"#).unwrap(),
            JobType::Embed {
                batch_size: 0,
                binary: false
            }
        ));
        assert!(matches!(
            serde_json::from_str::<JobType>(r#"{"type":"batch_infer"}"#).unwrap(),
            JobType::BatchInfer { .. }
        ));
    }

    /// The new job types carry their params through the poll dispatch (contract
    /// #5): labels / schema / top_k arrive on the wire and decode into the enum.
    #[test]
    fn new_jobtypes_carry_params() {
        let c: JobType =
            serde_json::from_str(r#"{"type":"batch_classification","labels":["pos","neg"]}"#)
                .unwrap();
        assert_eq!(c.tag(), "batch_classification");
        match c {
            JobType::BatchClassification { labels } => assert_eq!(labels, ["pos", "neg"]),
            _ => panic!("wrong variant"),
        }

        let e: JobType =
            serde_json::from_str(r#"{"type":"json_extraction","schema":{"name":"string"}}"#)
                .unwrap();
        assert_eq!(e.tag(), "json_extraction");
        match e {
            JobType::JsonExtraction { schema } => assert_eq!(schema["name"], "string"),
            _ => panic!("wrong variant"),
        }

        let r: JobType = serde_json::from_str(r#"{"type":"rerank","top_k":5}"#).unwrap();
        assert_eq!(r.tag(), "rerank");
        match r {
            JobType::Rerank { top_k } => assert_eq!(top_k, 5),
            _ => panic!("wrong variant"),
        }
        // Bare discriminants still decode (hint fields default).
        assert!(matches!(
            serde_json::from_str::<JobType>(r#"{"type":"rerank"}"#).unwrap(),
            JobType::Rerank { top_k: 0 }
        ));
    }

    /// The general-compute SEAM (ACCRETION.md §7-8): a `custom` job decodes its
    /// opaque `image` + `command` payload, round-trips the tag, and — like every
    /// other variant — still decodes from a bare discriminant (fields default to
    /// None / empty). This guards the contract shape shared with control/types.go
    /// (Image *string, Command []string) and proto/manifest.schema.json.
    #[test]
    fn custom_jobtype_carries_image_and_command() {
        let c: JobType = serde_json::from_str(
            r#"{"type":"custom","image":"docker.io/org/sim:tag","command":["python","sim.py"]}"#,
        )
        .unwrap();
        assert_eq!(c.tag(), "custom");
        match c {
            JobType::Custom { image, command } => {
                assert_eq!(image.as_deref(), Some("docker.io/org/sim:tag"));
                assert_eq!(command, ["python", "sim.py"]);
            }
            _ => panic!("wrong variant"),
        }
        // A null image (command-only) round-trips to None.
        let c: JobType =
            serde_json::from_str(r#"{"type":"custom","image":null,"command":["./run"]}"#).unwrap();
        match c {
            JobType::Custom { image, command } => {
                assert!(image.is_none());
                assert_eq!(command, ["./run"]);
            }
            _ => panic!("wrong variant"),
        }
        // Bare discriminant decodes (fields default).
        assert!(matches!(
            serde_json::from_str::<JobType>(r#"{"type":"custom"}"#).unwrap(),
            JobType::Custom {
                image: None,
                command,
            } if command.is_empty()
        ));
    }
}
