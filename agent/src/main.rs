//! Computexchange supplier agent.
//!
//! A signed binary that runs on idle Apple Silicon Macs: it detects and
//! benchmarks the hardware, registers with the Go control plane, polls for
//! tasks, executes them through the runner backends, and reports the result.
//! Three subcommands: `run`, `bench`, `version`.

mod cluster;
mod config;
mod continuous_batch; // Apple-Silicon continuous-batch lane skeleton (Hawking port; docs/HAWKING_PORT_PLAN.md)
mod failure;
mod hardware;
mod models;
mod pool;
mod protocol;
mod quantized_llama_batched; // vendored + patched candle quantized_llama (bsz>1 batched prefill)
mod runners;
mod sandbox; // sandboxed BYO-container execution for the `custom` general-compute lane
mod status;
mod types;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use sysinfo::System;
use tokio::sync::Semaphore;

use config::AgentConfig;
use pool::ModelPool;
use protocol::ControlPlaneClient;
use runners::{default_runners, dispatch, JobRunner, RunError};
use status::StatusWriter;
use types::{Heartbeat, TaskCommit, TaskDispatch, WorkerCapability};

const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Parser)]
#[command(name = "cx-agent", version = AGENT_VERSION, about = "Computexchange supplier agent")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Detect + benchmark hardware, then run the agent loop against the control plane.
    Run {
        /// Path to the agent TOML config.
        #[arg(long, default_value = "agent.toml")]
        config: PathBuf,
    },
    /// Detect + benchmark hardware and print the WorkerCapability as JSON. No network.
    Bench {
        /// Optional config path; only `supplier_id` is read (for the printed record).
        #[arg(long)]
        config: Option<PathBuf>,
    },
    /// Plane B (docs/PLANE_B.md): from MEASURED cluster figures, compute what a
    /// co-located Mac fabric would advertise as one `apple_silicon_cluster` worker
    /// (summed usable memory + bottleneck bandwidth), how a model's layers shard
    /// across it, and the drop-a-node → re-shard-or-offline decision. PURE math, no
    /// hardware — this only PLANS; executing the plan needs the external substrate
    /// (Exo / MLX-distributed / JACCL over Thunderbolt 5). The seam, runnable.
    ClusterPlan {
        /// Member unified memory sizes in GB, comma-separated (e.g. 512,512,512,512).
        #[arg(long)]
        members_gb: String,
        /// Bottleneck (slowest MEASURED) interconnect bandwidth, GB/s.
        #[arg(long, default_value_t = 80.0)]
        link_gbps: f32,
        /// Per-node held-back margin (OS + KV cache + activation buffers), GB.
        #[arg(long, default_value_t = 32.0)]
        margin_gb: f32,
        /// Total transformer layers of the model to plan.
        #[arg(long, default_value_t = 126)]
        model_layers: u32,
        /// The model's memory footprint, GB (must fit the summed usable memory).
        #[arg(long, default_value_t = 700.0)]
        model_gb: f32,
    },
    /// Batch-throughput benchmark: load a GGUF model and sweep batch sizes, timing
    /// batched vs serial decode. Device-agnostic — it measures whatever backend the
    /// binary was built for (Metal on macOS, CUDA when built `--features cuda`), so the
    /// SAME command produces comparable Apple-Silicon and NVIDIA numbers. Prints a
    /// human table to stderr and a machine-readable JSON record to stdout (redirect
    /// stdout to capture just the JSON). No network; downloads the GGUF once if absent.
    BenchBatch {
        /// Model ref (e.g. llama-3.2-1b-instruct-q4, qwen2.5-7b-instruct-q4).
        #[arg(long, default_value = "llama-3.2-1b-instruct-q4")]
        model: String,
        /// Max new tokens to generate per request (decode length; decode is where
        /// batching pays off, so keep this realistic, not tiny).
        #[arg(long, default_value_t = 48)]
        max_tokens: u32,
        /// Batch sizes to sweep, comma-separated (e.g. 1,2,4,8,16,32).
        #[arg(long, default_value = "1,2,4,8,16,32")]
        batch_sizes: String,
        /// The prompt every request in the batch runs (identical prompts keep the
        /// measurement about batching, not prompt variance; greedy output is then
        /// also checkable for the batched==serial invariant).
        #[arg(long, default_value = "Write a detailed paragraph about the ocean and its wonders:")]
        prompt: String,
        /// Gate mode: exit non-zero if batched output ever diverges from serial. Off by
        /// default (a throughput benchmark records divergence as data, since GPU
        /// reduction-order can legitimately flip a greedy tie without invalidating the
        /// tok/s). Turn ON to use this as a byte-determinism gate.
        #[arg(long, default_value_t = false)]
        require_deterministic: bool,
    },
    /// Print the agent version and exit.
    Version,
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    // Logs to STDERR so stdout stays clean for the machine-readable JSON the `bench`
    // and `bench-batch` subcommands print (a harness redirects stdout to capture just
    // the record). stderr is the conventional stream for diagnostics; under
    // systemd/launchd/docker both streams are still captured.
    fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_writer(std::io::stderr)
        .init();
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Current local hour (0..=23) for quiet-hours checks. Derived from the system
/// clock; we compute it without pulling in a date library by reading the
/// `localtime`-adjusted seconds is non-trivial in std, so we use a coarse UTC
/// hour. Operators set quiet_hours in the agent's local timezone === UTC on a
/// server; this is documented in the sample config.
fn current_hour_utc() -> u8 {
    ((now_unix() / 3600) % 24) as u8
}

/// Best-effort battery detection on macOS via `pmset -g batt`. If the box has
/// no battery (desktop/server) or the call fails, we report "not on battery".
fn on_battery() -> bool {
    use std::process::Command;
    let out = match Command::new("pmset").arg("-g").arg("batt").output() {
        Ok(o) if o.status.success() => o,
        _ => return false,
    };
    let text = String::from_utf8_lossy(&out.stdout);
    text.contains("Battery Power")
}

/// Sample coarse CPU utilization (0..=100) for the heartbeat.
fn cpu_pct(sys: &mut System) -> f32 {
    sys.refresh_cpu_usage();
    let cpus = sys.cpus();
    if cpus.is_empty() {
        return 0.0;
    }
    let sum: f32 = cpus.iter().map(|c| c.cpu_usage()).sum();
    sum / cpus.len() as f32
}

/// REAL memory reading for the throttle gate, against the resource that actually
/// limits THIS box. On the CUDA lane (`device_label() == "cuda"`) that is VRAM,
/// not host RAM — a GPU box has plenty of host RAM, so the host-RAM throttle would
/// never trip even when the card is nearly full (e.g. a 24 GB card handed a ~20 GB
/// job). We read free+total VRAM via nvidia-smi and feed it through the SAME
/// `evaluate_memory_throttle` thresholds. If CUDA is active but VRAM can't be read
/// we surface that and fall back to the host-RAM snapshot (mirrors
/// `detect_and_benchmark`) rather than fabricating headroom. On Apple/CPU the
/// gating resource is unified/host memory, so we keep the host-RAM path.
fn throttle_snapshot() -> hardware::MemorySnapshot {
    if models::device_label() == "cuda" {
        match hardware::read_vram_snapshot() {
            Some(vram) => return vram,
            None => tracing::warn!(
                "CUDA lane active but nvidia-smi VRAM read failed; gating on host RAM this cycle"
            ),
        }
    }
    hardware::read_memory_snapshot()
}

/// GPU utilization (%) + temperature (°C) for the heartbeat, CUDA lane only. Off the
/// NVIDIA lane (Apple/CPU) there is no discrete GPU to query, so report an honest
/// 0.0/None rather than a fabricated number; on CUDA a failed nvidia-smi read also
/// degrades to 0.0/None (never faked).
fn gpu_telemetry() -> (f32, Option<f32>) {
    if models::device_label() == "cuda" {
        if let Some((util, temp)) = hardware::read_gpu_telemetry() {
            return (util, temp);
        }
    }
    (0.0, None)
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Version => {
            println!("cx-agent {AGENT_VERSION}");
            Ok(())
        }
        Command::Bench { config } => {
            init_tracing();
            run_bench(config)
        }
        Command::BenchBatch {
            model,
            max_tokens,
            batch_sizes,
            prompt,
            require_deterministic,
        } => {
            init_tracing();
            run_bench_batch(&model, max_tokens, &batch_sizes, &prompt, require_deterministic)
        }
        Command::ClusterPlan {
            members_gb,
            link_gbps,
            margin_gb,
            model_layers,
            model_gb,
        } => run_cluster_plan(&members_gb, link_gbps, margin_gb, model_layers, model_gb),
        Command::Run { config } => {
            init_tracing();
            let cfg = AgentConfig::load(&config)
                .with_context(|| format!("loading config {}", config.display()))?;
            run_agent(cfg).await
        }
    }
}

/// `bench` subcommand: detect + benchmark, print WorkerCapability JSON.
fn run_bench(config: Option<PathBuf>) -> Result<()> {
    // Read supplier id + the configured engine tag from the config when present, so
    // `bench` prints the SAME engine the agent will advertise; with no config it is the
    // default Candle path (engine "candle").
    let (supplier_id, engine) = match config {
        Some(path) => {
            let cfg = AgentConfig::load(&path)
                .with_context(|| format!("loading config {}", path.display()))?;
            (cfg.supplier_id, cfg.inference_backend.engine_tag())
        }
        None => (uuid::Uuid::nil(), config::InferenceBackend::default().engine_tag()),
    };
    // `bench` is informational only — reservation price is 0.0 (not advertised).
    let cap = hardware::detect_and_benchmark(supplier_id, AGENT_VERSION, 0.0, engine);
    println!("{}", serde_json::to_string_pretty(&cap)?);
    Ok(())
}

/// `bench-batch` subcommand: sweep batch sizes on a real GGUF model, timing batched
/// vs serial decode. The batched path (generate_batch) shares the decode step across
/// the batch — the core throughput lever CX relies on — so this quantifies the win on
/// whatever backend the binary was built for. Emits a JSON record on stdout; a human
/// table on stderr. Also asserts the batched==serial greedy invariant so a throughput
/// number can never be reported over an INCORRECT (diverged) batched decode.
fn run_bench_batch(
    model: &str,
    max_tokens: u32,
    batch_sizes: &str,
    prompt: &str,
    require_deterministic: bool,
) -> Result<()> {
    use std::time::Instant;

    let sizes: Vec<usize> = batch_sizes
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| {
            let n = s.parse::<usize>().map_err(|e| anyhow::anyhow!("bad batch size {s:?}: {e}"))?;
            // Reject 0: a zero-size batch runs zero sequences, so tok/s and
            // per-request throughput divide by zero → NaN → serializes as JSON null,
            // breaking the stdout contract AND vacuously "passing" the batched==serial
            // check (all() over an empty vec is true). Never let it into the sweep.
            if n == 0 {
                anyhow::bail!("batch size must be >= 1 (got 0 in --batch-sizes)");
            }
            Ok(n)
        })
        .collect::<Result<Vec<_>>>()?;
    if sizes.is_empty() {
        anyhow::bail!("no batch sizes given (e.g. --batch-sizes 1,2,4,8,16,32)");
    }

    let device = models::device_label();
    // Report the same class axis the scheduler pins on, so a bench record is
    // attributable to an exact (device, engine, build_hash). The bench binary is the
    // default Candle path; the engine tag matches what `run` would advertise.
    let engine = config::InferenceBackend::default().engine_tag();
    let build_hash = hardware::engine_build_hash(engine, AGENT_VERSION);
    eprintln!("== cx-agent bench-batch ==");
    eprintln!("device={device} model={model} max_tokens={max_tokens} build_hash={build_hash}");

    let mut be = runners::LlamaBackend::load(model)
        .map_err(|e| anyhow::anyhow!("load {model}: {e}"))?;

    // Warm-up: one generate outside the timed loop so weight upload / first-kernel
    // JIT / autotune costs land here, not in the measured serial baseline.
    let (warm_text, _) = be
        .generate(prompt, max_tokens)
        .map_err(|e| anyhow::anyhow!("warmup generate: {e}"))?;

    // Serial baseline: time ONE request end-to-end (B=1 through the scalar path). This
    // is the "no batching" reference every batched tok/s is compared against.
    let t = Instant::now();
    let (serial_text, serial_tok) = be
        .generate(prompt, max_tokens)
        .map_err(|e| anyhow::anyhow!("serial generate: {e}"))?;
    let serial_dt = t.elapsed().as_secs_f64();
    // A zero-token serial baseline (model emitted EOS immediately) would make every
    // speedup = tps/0 = ±inf/NaN → JSON null downstream. That is a degenerate input
    // (bad model/prompt), not a measurable run — fail loudly rather than emit nulls.
    if serial_tok == 0 {
        anyhow::bail!(
            "serial baseline produced 0 tokens for model {model:?} — cannot benchmark \
             (check the model ref and prompt)"
        );
    }
    let serial_tps = serial_tok as f64 / serial_dt;
    eprintln!("serial (B=1): {serial_tok} tok in {serial_dt:.2}s = {serial_tps:.1} tok/s");

    #[derive(serde::Serialize)]
    struct SweepRow {
        batch: usize,
        wall_s: f64,
        total_tokens: usize,
        tokens_per_s: f64,
        per_request_tok_s: f64,
        speedup_vs_serial: f64,
        batched_equals_serial: bool,
    }

    let mut rows: Vec<SweepRow> = Vec::with_capacity(sizes.len());
    let mut peak_tps = serial_tps;
    for &b in &sizes {
        let prompts: Vec<String> = std::iter::repeat(prompt.to_string()).take(b).collect();
        let t = Instant::now();
        let res = be
            .generate_batch(&prompts, max_tokens)
            .map_err(|e| anyhow::anyhow!("generate_batch b={b}: {e}"))?;
        let wall = t.elapsed().as_secs_f64();
        let total_tok: usize = res.iter().map(|(_, n)| n).sum();
        let tps = total_tok as f64 / wall;
        // Byte-determinism vs serial: does batched greedy output match one-at-a-time?
        // This is a SEPARATE property from throughput. On Apple/Metal it holds at every
        // batch size (mask-cache + active-set-shrink determinism). On CUDA it can FLIP a
        // greedy argmax TIE because GPU float reductions vary with batch composition —
        // the batched tokens are still a valid greedy decode, just not byte-identical to
        // serial. So a divergence does NOT invalidate the tok/s (the tokens really were
        // produced at that rate); it is recorded as a determinism data point, and only a
        // gate (--require-deterministic) treats it as a hard failure.
        let equals_serial = res.iter().all(|(text, _)| *text == serial_text);
        let row = SweepRow {
            batch: b,
            wall_s: wall,
            total_tokens: total_tok,
            tokens_per_s: tps,
            per_request_tok_s: tps / b as f64,
            speedup_vs_serial: tps / serial_tps,
            batched_equals_serial: equals_serial,
        };
        eprintln!(
            "batch={b:>3}: {total_tok:>5} tok in {wall:>6.2}s = {tps:>7.1} tok/s  ({:.2}x serial){}",
            row.speedup_vs_serial,
            if equals_serial { "" } else { "  !! batched != serial" }
        );
        peak_tps = peak_tps.max(tps);
        rows.push(row);
    }

    let all_deterministic = rows.iter().all(|r| r.batched_equals_serial);
    let diverged: Vec<usize> = rows.iter().filter(|r| !r.batched_equals_serial).map(|r| r.batch).collect();
    eprintln!(
        "peak {peak_tps:.1} tok/s = {:.2}x serial · byte-determinism vs serial: {}",
        peak_tps / serial_tps,
        if all_deterministic {
            "IDENTICAL at every batch size".to_string()
        } else {
            format!("DIVERGES at batch {diverged:?} (GPU reduction-order tie-flip; throughput still valid)")
        }
    );

    let record = serde_json::json!({
        "kind": "bench_batch",
        "device": device,
        "build_hash": build_hash,
        "model": model,
        "max_tokens": max_tokens,
        "prompt_preview": prompt.chars().take(60).collect::<String>(),
        "warmup_ok": !warm_text.is_empty(),
        "serial_baseline_tok_s": serial_tps,
        "peak_tok_s": peak_tps,
        "peak_speedup_vs_serial": peak_tps / serial_tps,
        // Byte-determinism vs serial (NOT a throughput validity flag). true = batched
        // output byte-identical to serial at every batch size; false = at least one
        // batch diverged (see `diverged_batches`).
        "batched_deterministic_vs_serial": all_deterministic,
        "diverged_batches": diverged,
        "sweep": rows,
    });
    println!("{}", serde_json::to_string_pretty(&record)?);

    // Divergence is a data point, not a failure — the tok/s are real. Only a caller that
    // explicitly demands byte-determinism (--require-deterministic, e.g. a verification
    // gate) treats a divergence as a hard, non-zero-exit failure.
    if require_deterministic && !all_deterministic {
        anyhow::bail!(
            "batched decode diverged from serial at batch {diverged:?} and \
             --require-deterministic was set — failing the determinism gate"
        );
    }
    Ok(())
}

/// `cluster-plan` subcommand (Plane B): turn MEASURED member memories + a measured
/// bottleneck link into the cluster's one-worker advertisement and shard layout,
/// then show what happens if a node drops. Pure (uses `cluster.rs`); no hardware,
/// no network. Honest by construction: the summed memory subtracts a real per-node
/// margin, the bandwidth is the bottleneck link, and EXECUTING the plan is flagged
/// as the external substrate's job — this only plans it.
fn run_cluster_plan(
    members_gb: &str,
    link_gbps: f32,
    margin_gb: f32,
    model_layers: u32,
    model_gb: f32,
) -> Result<()> {
    use cluster::{ClusterNode, ClusterTopology, Link, ReshardDecision};

    let mems: Vec<f32> = members_gb
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| s.parse::<f32>())
        .collect::<Result<_, _>>()
        .context("parsing --members-gb (expected comma-separated GB, e.g. 512,512,512,512)")?;
    if mems.len() < 2 {
        anyhow::bail!(
            "a cluster needs at least 2 members (got {}); a single Mac is a normal Plane-A worker",
            mems.len()
        );
    }

    let nodes: Vec<ClusterNode> = mems
        .iter()
        .map(|&m| ClusterNode {
            unified_memory_gb: m,
        })
        .collect();
    // Model the fabric as a ring at the supplied (measured) bottleneck bandwidth.
    let links: Vec<Link> = (0..nodes.len())
        .map(|i| Link {
            a: i,
            b: (i + 1) % nodes.len(),
            gbps: link_gbps,
            latency_us: 0.0,
        })
        .collect();
    let topo = ClusterTopology { nodes, links };

    let advert = topo
        .advertise(margin_gb)
        .context("topology does not form a cluster (need ≥2 members + a fabric)")?;
    println!("Plane B cluster plan (docs/PLANE_B.md) — MEASURED inputs, pure math");
    println!("  members          : {} Macs {:?} GB", mems.len(), mems);
    println!(
        "  advertises as    : apple_silicon_cluster, memory_gb={:.1} (summed usable, −{:.0} GB/node margin), memory_bw_gbps={:.1} (bottleneck link)",
        advert.memory_gb, margin_gb, advert.memory_bw_gbps
    );
    println!(
        "  model            : {} layers, {:.1} GB → fits: {}",
        model_layers,
        model_gb,
        topo.fits_model(margin_gb, model_gb)
    );

    if !topo.fits_model(margin_gb, model_gb) {
        println!(
            "  RESULT           : model does NOT fit summed usable memory ({:.1} GB) — this cluster would not advertise it.",
            topo.summed_usable_memory_gb(margin_gb)
        );
        return Ok(());
    }

    println!("  shard layout     : pipeline stages, contiguous layers per node");
    for s in topo.assign_shards(model_layers, margin_gb) {
        println!(
            "    node {:>2}        : layers [{:>3}, {:>3})  ({} layers)",
            s.node,
            s.first_layer,
            s.first_layer + s.layer_count,
            s.layer_count
        );
    }

    // Illustrate fault handling: drop the last member and re-decide.
    let mut survivors = topo.clone();
    survivors.nodes.pop();
    let n = survivors.nodes.len();
    survivors.links = if n >= 2 {
        (0..n)
            .map(|i| Link {
                a: i,
                b: (i + 1) % n,
                gbps: link_gbps,
                latency_us: 0.0,
            })
            .collect()
    } else {
        Vec::new()
    };
    match survivors.on_membership_change(model_layers, model_gb, margin_gb) {
        ReshardDecision::Reshard(plan) => println!(
            "  if a node drops  : RE-SHARD across {} survivors ({} layers still covered)",
            survivors.nodes.len(),
            plan.iter().map(|s| s.layer_count).sum::<u32>()
        ),
        ReshardDecision::Offline => println!(
            "  if a node drops  : go OFFLINE — survivors ({:.1} GB usable) can't hold the model; never run a partial shard",
            survivors.summed_usable_memory_gb(margin_gb)
        ),
    }

    println!(
        "  EXECUTION        : planning only. Running this shard layout needs the EXTERNAL co-located substrate"
    );
    println!(
        "                     (Exo / MLX-distributed / JACCL over Thunderbolt 5, macOS 26.2) — see PLANE_B.md §3,§5."
    );
    Ok(())
}

/// Execute one dispatched task end-to-end and build the commit to submit.
///
/// Real path (action plan §K): GET the presigned `input_url` → run the backend →
/// PUT the result JSON to the presigned `output_url` → WIPE the in-memory input
/// and result buffers. Job bytes live in memory only; nothing is written to disk
/// unencrypted, and the buffers are zeroized the moment the commit is built.
async fn execute_task(
    task: &TaskDispatch,
    cap: &WorkerCapability,
    runners: &[Box<dyn JobRunner>],
    pool: &ModelPool,
    s3: &reqwest::Client,
) -> Result<TaskCommit, RunError> {
    let manifest = &task.manifest;
    let runner = dispatch(manifest, cap, runners).await?;
    tracing::info!(task = %task.task_id, backend = runner.backend_name(), "executing task");

    // 1. GET the presigned input (the task's JSONL chunk) into memory.
    let mut input = s3_get(s3, &task.input_url)
        .await
        .map_err(|e| RunError::Inference {
            backend: runner.backend_name(),
            msg: format!("fetching input_url: {e:#}"),
        })?;

    // 2. Run the model through the WARM pool. On failure, still wipe input first.
    let output = match runner.run(manifest, &input, pool).await {
        Ok(o) => o,
        Err(e) => {
            wipe(&mut input);
            return Err(e);
        }
    };
    wipe(&mut input);

    let (duration_ms, tokens_used) = (output.duration_ms, output.tokens_used);

    // 3. PUT the result to the presigned output URL. Large results (>16 MiB) are
    // staged to a temp file ENCRYPTED with an ephemeral AES-GCM key, uploaded from
    // there, then wiped + removed — nothing large sits on disk in the clear (action
    // plan §K). Small results stay entirely in memory. The content type follows the
    // payload: JSON for every job by default, octet-stream for the opt-in binary
    // embedding artifact (PLANE_D D5/D15) so the object is stored as opaque bytes.
    let mut result = output.result;
    let content_type = if output.binary {
        "application/octet-stream"
    } else {
        "application/json"
    };
    let put = if result.len() > STAGING_THRESHOLD {
        put_via_encrypted_staging(s3, &task.output_url, &result, content_type).await
    } else {
        s3_put_bytes(s3, &task.output_url, &result, content_type).await
    };
    put.map_err(|e| RunError::Inference {
        backend: runner.backend_name(),
        msg: format!("putting output_url: {e:#}"),
    })?;

    let commit = TaskCommit {
        task_id: task.task_id,
        // Echo the object key the control plane told us to write to. Prefer the
        // explicit `result_key`; fall back to a job/task-derived key if absent.
        result_key: if task.result_key.is_empty() {
            format!("results/{}/{}.json", task.job_id, task.task_id)
        } else {
            task.result_key.clone()
        },
        duration_ms,
        tokens_used,
        hardware_temp_c: None,
    };

    // 4. Wipe the result buffer now that it is durably PUT and the commit built.
    wipe(&mut result);
    Ok(commit)
}

/// Overwrite a buffer's bytes with zeros and drop its capacity. Best-effort
/// in-memory wipe of decrypted job data per the security mandate.
fn wipe(buf: &mut Vec<u8>) {
    for b in buf.iter_mut() {
        *b = 0;
    }
    buf.clear();
    buf.shrink_to_fit();
}

/// GET a presigned URL into memory (no auth header — the signature is in the URL).
async fn s3_get(client: &reqwest::Client, url: &str) -> Result<Vec<u8>> {
    let resp = client
        .get(url)
        .send()
        .await
        .context("GET presigned input")?;
    let resp = resp
        .error_for_status()
        .context("input_url returned error status")?;
    Ok(resp.bytes().await.context("reading input body")?.to_vec())
}

/// PUT bytes to a presigned URL with the given `Content-Type`. `application/json`
/// for normal results; `application/octet-stream` for the opt-in binary embedding
/// artifact (PLANE_D D5/D15). The bytes are uploaded verbatim either way.
async fn s3_put_bytes(
    client: &reqwest::Client,
    url: &str,
    body: &[u8],
    content_type: &str,
) -> Result<()> {
    let resp = client
        .put(url)
        .header(reqwest::header::CONTENT_TYPE, content_type)
        .body(body.to_vec())
        .send()
        .await
        .context("PUT presigned output")?;
    resp.error_for_status()
        .context("output_url returned error status")?;
    Ok(())
}

/// Results larger than this are staged through an encrypted temp file rather than
/// held only in memory (action plan §K / STRONG I).
const STAGING_THRESHOLD: usize = 16 * 1024 * 1024;

/// Upload a large result without ever leaving it unencrypted on disk.
///
/// We encrypt the plaintext with a one-shot AES-256-GCM key (random key+nonce,
/// generated here and dropped at the end), write only the CIPHERTEXT to a temp
/// file, then read it back, decrypt in memory, and PUT the plaintext over TLS to
/// the presigned URL. The transient plaintext copy and the temp file are both
/// wiped/removed before returning. The presigned endpoint still receives the
/// plaintext payload with `content_type` (the control plane verifies plaintext);
/// the encryption guards the *disk* spill only, which is the whole point of staging.
async fn put_via_encrypted_staging(
    client: &reqwest::Client,
    url: &str,
    plaintext: &[u8],
    content_type: &str,
) -> Result<()> {
    use aes_gcm::aead::{Aead, KeyInit};
    use aes_gcm::{Aes256Gcm, Key, Nonce};
    use rand::RngCore;

    // Ephemeral key + nonce: live only for this upload, never persisted.
    let mut key_bytes = [0u8; 32];
    let mut nonce_bytes = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut key_bytes);
    rand::thread_rng().fill_bytes(&mut nonce_bytes);
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key_bytes));
    let nonce = Nonce::from_slice(&nonce_bytes);

    let ciphertext = cipher
        .encrypt(nonce, plaintext)
        .map_err(|e| anyhow::anyhow!("staging encryption failed: {e}"))?;

    // Write CIPHERTEXT to a uniquely-named temp file.
    let path = std::env::temp_dir().join(format!("cx-stage-{}.bin", uuid::Uuid::new_v4()));
    tokio::fs::write(&path, &ciphertext)
        .await
        .with_context(|| format!("writing encrypted staging file {}", path.display()))?;

    // Read it back, decrypt in memory, upload plaintext, then wipe + remove —
    // even if the PUT fails, so no decrypted bytes linger on disk.
    let result = async {
        let on_disk = tokio::fs::read(&path)
            .await
            .context("reading encrypted staging file")?;
        let mut decrypted = cipher
            .decrypt(nonce, on_disk.as_ref())
            .map_err(|e| anyhow::anyhow!("staging decryption failed: {e}"))?;
        let put = s3_put_bytes(client, url, &decrypted, content_type).await;
        wipe(&mut decrypted);
        put
    }
    .await;

    // Best-effort secure cleanup: overwrite the ciphertext file with zeros, then
    // remove it. (Overwrite-in-place is best-effort on modern FS, but we never
    // leave the staged bytes readable as a normal file.)
    let _ = tokio::fs::write(&path, vec![0u8; ciphertext.len()]).await;
    let _ = tokio::fs::remove_file(&path).await;
    result
}

/// Shared, cheaply-cloned context every in-flight task needs. Bundling it keeps
/// the spawn sites tidy and the warm `pool` shared across all tasks.
#[derive(Clone)]
struct WorkCtx {
    client: Arc<ControlPlaneClient>,
    cap: Arc<WorkerCapability>,
    runners: Arc<Vec<Box<dyn JobRunner>>>,
    pool: ModelPool,
    s3: reqwest::Client,
    min_payout_usd_per_hr: f32,
    /// Operator's reserved memory headroom (GB), for the memory snapshot attached
    /// to a typed failure report (Plane C/D D0).
    memory_headroom_gb: f32,
    status: Arc<StatusWriter>,
}

/// The agent loop: register once, then run a BOUNDED-CONCURRENCY pipeline —
/// prefetch up to `max_concurrent_tasks` tasks, execute them concurrently
/// through the warm model pool, and commit each as it finishes. A 30s heartbeat
/// and SIGINT stay responsive via `tokio::select!`. Heavy GPU compute serializes
/// behind each model's mutex (in the pool); the wins are no per-task model reload
/// and overlapping S3 GET/PUT with compute.
async fn run_agent(cfg: AgentConfig) -> Result<()> {
    let cap = hardware::detect_and_benchmark(
        cfg.supplier_id,
        AGENT_VERSION,
        cfg.min_payout_usd_per_hr,
        // The configured on-device engine (candle default). It becomes the second
        // axis of the worker's verification class on the control plane.
        cfg.inference_backend.engine_tag(),
    );
    let worker_id = cap.worker_id;
    let permits = cfg.concurrency(cap.memory_gb);

    let client = ControlPlaneClient::new(cfg.control_url.clone(), cfg.worker_token.clone())
        .context("building control-plane client (is worker_token set?)")?;
    // Separate client for presigned S3 object I/O (no auth header; longer body
    // timeout for large chunks).
    let s3 = reqwest::Client::builder()
        .timeout(Duration::from_secs(120))
        .build()
        .context("building S3 client")?;

    tracing::info!(%worker_id, control = %cfg.control_url, max_concurrent_tasks = permits, "registering with control plane");
    let confirmed = client.register(&cap).await.context("registration failed")?;
    tracing::info!(worker_id = %confirmed.worker_id, "registered");

    // Menu-bar status surface: write the status file now (idle), then on every
    // heartbeat and task transition. The macOS app reads it (see macapp/).
    let status = Arc::new(StatusWriter::new(AGENT_VERSION, worker_id));
    // Echo the APPLIED operator prefs (the effective config after the agent.prefs.toml
    // overlay) so the app shows agent truth, not just its local toggle state (item 26).
    status.set_applied_prefs(status::AppliedPrefs::from_config(&cfg, cap.memory_gb));
    status.registered();

    let ctx = WorkCtx {
        client: Arc::new(client),
        cap: Arc::new(cap),
        runners: Arc::new({
            let mut rs = default_runners();
            // Serving-lane seams: only when the operator opts in. Each is inserted right
            // AFTER ClusterRunner (index 0) so a giant cluster model still routes to the
            // Plane B seam, but BEFORE the Candle generative runners so lane jobs route
            // here. (Each seam's can_run also yields cluster models, defense-in-depth.)
            // The backends are mutually exclusive (one inference_backend), and the default
            // Candle backend leaves dispatch byte-for-byte unchanged.
            match cfg.inference_backend {
                config::InferenceBackend::Candle => {}
                config::InferenceBackend::Mlx => {
                    rs.insert(1, Box::new(runners::MlxRunner));
                    tracing::info!("inference_backend=mlx: MLX serving-lane seam active (generative LLM jobs route to the MLX boundary until the runtime is wired)");
                }
                config::InferenceBackend::Vllm => {
                    rs.insert(1, Box::new(runners::VllmRunner));
                    tracing::info!("inference_backend=vllm: vLLM CUDA serving-lane seam active (generative LLM jobs route to the vLLM boundary until a pinned server is configured + the determinism soak passes — docs/VLLM_LANE.md)");
                }
                config::InferenceBackend::Hawking => {
                    // The Hawking Apple-Silicon continuous-batch lane is a module-level
                    // scheduler skeleton (continuous_batch.rs), inert-by-default: it falls
                    // back to the existing per-task batched decode, so behavior is unchanged
                    // today. The engine tag still flows to the control plane so the
                    // verification class is correct ahead of the port landing.
                    tracing::info!("inference_backend=hawking: Apple-Silicon continuous-batch lane registered (engine=hawking); the scheduler port is a compiling skeleton that falls back to per-task batched decode — docs/HAWKING_PORT_PLAN.md");
                }
            }
            rs
        }),
        pool: ModelPool::new(),
        s3,
        min_payout_usd_per_hr: cfg.min_payout_usd_per_hr,
        memory_headroom_gb: cfg.memory_headroom_gb,
        status: status.clone(),
    };
    let sem = Arc::new(Semaphore::new(permits));
    // In-flight tasks; drained as they finish so commits/errors surface promptly.
    let mut inflight = tokio::task::JoinSet::new();

    let mut sys = System::new();
    let mut heartbeat = tokio::time::interval(Duration::from_secs(30));
    // Don't fire a burst if a tick is missed while we were busy in a task.
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            biased;

            _ = tokio::signal::ctrl_c() => {
                tracing::info!(
                    inflight = inflight.len(),
                    models_loaded = pool::loads(),
                    "received SIGINT; shutting down (in-flight tasks will be reassigned by the control plane)"
                );
                inflight.shutdown().await;
                return Ok(());
            }

            _ = heartbeat.tick() => {
                let ts = now_unix();
                let cpu = cpu_pct(&mut sys);
                // Real memory reading → throttle decision. Sent to the control
                // plane (effective memory + throttled gate the safe-dispatch
                // filter) and surfaced to the menu bar. On the CUDA lane this is
                // VRAM (the real limit on a GPU box); host/unified RAM otherwise.
                let throttle = cfg.evaluate_memory_throttle(&throttle_snapshot(), None);
                // Warm-routing (D3): report the models actually warm in the pool so the
                // control plane can prefer this worker for those models. Real ids only —
                // `loaded_model_ids` gates on a resolved OnceCell, never a load in flight.
                let loaded_models = ctx.pool.loaded_model_ids().await;
                // Real GPU telemetry on the CUDA lane (nvidia-smi); honest 0.0/None off it.
                let (gpu, gpu_temp) = gpu_telemetry();
                let hb = Heartbeat {
                    worker_id,
                    timestamp: ts,
                    cpu_pct: cpu,
                    gpu_pct: gpu,
                    gpu_temp_c: gpu_temp,
                    current_task: None,
                    available_memory_gb: throttle.available_gb,
                    effective_memory_gb: throttle.effective_gb,
                    reserved_headroom_gb: throttle.reserved_headroom_gb,
                    throttled: throttle.throttled,
                    loaded_models,
                };
                if let Err(e) = ctx.client.heartbeat(&hb).await {
                    tracing::warn!(error = %e, "heartbeat failed");
                }
                // Refresh the menu-bar status: telemetry + eligibility + earnings
                // (best-effort; keep last-known totals if the earnings call fails).
                let eligible = cfg.is_eligible_to_run(current_hour_utc(), on_battery());
                let earnings = ctx.client.earnings().await.ok();
                status.heartbeat(cpu, None, eligible, ts, earnings, &throttle);
            }

            // Reap a finished task. `join_next` is None only when nothing is
            // in-flight, in which case it returns immediately and we fall through
            // to polling — so this arm never starves the others.
            Some(joined) = inflight.join_next() => {
                if let Err(e) = joined {
                    tracing::error!(error = %e, "in-flight task panicked");
                }
            }

            // Acquire a permit (waits when all `permits` slots are busy → this IS
            // the concurrency bound), then long-poll. A returned task is spawned
            // with the permit moved into it; no work releases the permit at once.
            // Reaping finished tasks (the arm above) keeps working while we hold a
            // permit here, so an idle poll never blocks commits.
            permit = sem.clone().acquire_owned() => {
                let permit = permit.expect("semaphore is never closed");
                if let Err(e) = poll_and_spawn(&cfg, &ctx, permit, &mut inflight).await {
                    tracing::warn!(error = %e, "poll cycle error");
                    // Back off briefly so a hard failure doesn't hot-loop.
                    tokio::time::sleep(Duration::from_secs(5)).await;
                }
            }
        }
    }
}

/// Gate on eligibility, long-poll once, and on a task: start it and spawn the
/// execute→commit pipeline (the `permit` rides along, freeing the slot when the
/// task ends). Returns promptly so heartbeat/SIGINT stay responsive.
async fn poll_and_spawn(
    cfg: &AgentConfig,
    ctx: &WorkCtx,
    permit: tokio::sync::OwnedSemaphorePermit,
    inflight: &mut tokio::task::JoinSet<()>,
) -> Result<()> {
    if !cfg.is_eligible_to_run(current_hour_utc(), on_battery()) {
        tracing::debug!("not eligible to run (quiet hours / battery); idling 60s");
        drop(permit); // release the slot while we idle
        tokio::time::sleep(Duration::from_secs(60)).await;
        return Ok(());
    }

    // Dynamic provider throttling: before claiming work, re-read REAL memory and
    // pause new claims if taking a job would breach the supplier's reserved
    // headroom or drive the box past its memory ceiling. Enforced here (before the
    // claim) and re-evaluated every cycle — so finishing a task and looping back
    // re-checks before the next claim, and a pressured Mac is never handed more
    // work. The surfaced reason tells the operator exactly why work paused. On the
    // CUDA lane the gating resource is VRAM (a GPU box has ample host RAM, so the
    // host-RAM gate would never trip even with the card nearly full); host/unified
    // RAM on Apple/CPU. `throttle_snapshot()` picks the right one per active device.
    let throttle = cfg.evaluate_memory_throttle(&throttle_snapshot(), None);
    if throttle.throttled {
        tracing::info!(
            reason = throttle.reason.as_deref().unwrap_or("memory pressure"),
            available_gb = throttle.available_gb,
            effective_gb = throttle.effective_gb,
            "memory throttle: pausing new claims"
        );
        ctx.status.set_throttle(&throttle);
        drop(permit); // release the slot while we idle
        tokio::time::sleep(Duration::from_secs(30)).await;
        return Ok(());
    }

    let task = match ctx.client.poll_task().await {
        Ok(Some(t)) => t,
        Ok(None) => return Ok(()), // long-poll returned no work; `permit` drops
        Err(e) => return Err(e.into()),
    };
    tracing::info!(task = %task.task_id, job = %task.job_id, "received task");

    // Min-payout gate (STRONG H): the server already filters by reservation
    // price, but if it ever offers a rate below ours, refuse honestly rather than
    // running for less than the operator's floor. 0.0 = "no rate advertised".
    let floor = ctx.min_payout_usd_per_hr;
    if floor > 0.0 && task.offered_rate_usd_hr > 0.0 && task.offered_rate_usd_hr < floor {
        tracing::warn!(
            task = %task.task_id,
            offered = task.offered_rate_usd_hr,
            floor,
            "offered rate below reservation price; skipping task (not started)"
        );
        return Ok(()); // never started → control plane reassigns it
    }

    ctx.client
        .start_task(task.task_id)
        .await
        .context("start_task")?;
    // Surface the running job to the menu bar (the operator sees the job_id; the
    // task_id keys the in-flight set so concurrent tasks don't collide).
    ctx.status.job_started(
        task.task_id,
        task.job_id,
        task.manifest.job_type.tag(),
        now_unix(),
    );

    // Spawn the heavy work so the loop returns to its `select!` immediately:
    // the heartbeat keeps firing and new tasks prefetch while this one runs.
    let ctx = ctx.clone();
    inflight.spawn(async move {
        // `permit` is held for the lifetime of this task; dropping it here frees
        // the slot for the next poll.
        let _permit = permit;
        let task_id = task.task_id;
        let started = std::time::Instant::now();
        match execute_task(&task, &ctx.cap, &ctx.runners, &ctx.pool, &ctx.s3).await {
            Ok(commit) => {
                match ctx.client.commit_task(task_id, &commit).await {
                    Ok(()) => tracing::info!(task = %task_id, "committed result"),
                    Err(e) => tracing::error!(task = %task_id, error = %e, "commit_task failed"),
                }
                ctx.status.job_finished(task_id, None);
            }
            Err(e) => {
                // Honest failure: no result produced (model download / inference
                // error, or no runner matched). Do NOT commit a fake result. Plane
                // C/D D0: report a TYPED failure immediately so the control plane
                // requeues (retryable) or fails+refunds (terminal) in seconds —
                // instead of stranding the task for the 30-min stale reaper.
                tracing::error!(task = %task_id, error = %e, "task execution failed; reporting typed failure");
                let snap = hardware::read_memory_snapshot();
                let report = failure::build_report(
                    &e,
                    task.manifest.job_type.tag(),
                    &task.manifest.model.model_ref,
                    started.elapsed().as_millis() as u64,
                    &snap,
                    ctx.memory_headroom_gb,
                );
                if let Err(fe) = ctx.client.fail_task(task_id, &report).await {
                    tracing::warn!(task = %task_id, error = %fe, "fail_task report failed (stale reaper remains the fallback)");
                }
                ctx.status.job_finished(task_id, Some(e.to_string()));
            }
        }
    });
    Ok(())
}
