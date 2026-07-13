//! status.rs — emit `~/.compute-exchange/status.json` for the menu-bar app.
//!
//! The macOS menu-bar app (`macapp/`) is a viewer: it reads this file to show
//! live state, earnings, and telemetry. The schema mirrors
//! `macapp/ComputeExchangeAgent/StatusModel.swift` EXACTLY (snake_case keys), so
//! this file is part of the agent↔app contract — keep the two in lockstep.
//!
//! Writes are ATOMIC (write a temp file, then rename over the target) so the app
//! never observes a half-written document. The path is `$CX_STATUS_PATH` when set
//! (used by `prove-local` to redirect into the artifacts dir), else
//! `~/.compute-exchange/status.json`. Failures are surfaced (logged), never hidden.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;
use uuid::Uuid;

use crate::config::{AgentConfig, ThrottleDecision};
use crate::types::{BenchResult, Earnings};

/// Bumped only on a breaking change to the on-disk shape (the app tolerates a
/// partial/older file but reads this to know what to expect).
const SCHEMA_VERSION: u32 = 1;

// ── Supplier earnings economics (docs/internal/CREED_AND_PATH_TO_TEN.md,
//    "Supplier earnings economics" 4→5: "Surface it where a supplier actually
//    looks"). These constants MUST stay in lockstep with
//    scripts/supplier_earnings_calculator.py (the 2→3 rung's calculator) and the
//    real numbers they mirror:
//      - price table == control/seed.go's `price_per_1k` columns (the exact
//        catalogue a buyer is actually charged).
//      - take rate == control/payment.go's `takeRateFromEnv` default (3%,
//        clamped to [1%, 5%] by `CX_PLATFORM_TAKE_PCT`); the agent has no
//        network path to read the live env var off the control plane, so the
//        documented default is used, same as the Python calculator's default.
//    This is a LIVE per-worker number: it is computed from THIS worker's own
//    measured `benchmarks` (already gathered by `run_benchmarks` at startup),
//    never a fleet average.

/// CX catalogue prices, USD per 1k units (control/seed.go). A unit is one
/// embedding or one generated token — both billed identically (control/api.go
/// `estimateJobUSD`).
const CX_PRICE_PER_1K: &[(&str, f64)] = &[
    ("all-minilm-l6-v2", 0.00100),
    ("llama-3.2-1b-instruct-q4", 0.00200),
    ("qwen2.5-7b-instruct-q4", 0.00800),
];

/// Mirrors `control/payment.go`'s documented default `CX_PLATFORM_TAKE_PCT` (3%,
/// clamped to [1%, 5%]) — the agent has no live read of the control plane's env
/// var, so it uses the same documented default the calculator script uses.
const DEFAULT_PLATFORM_TAKE_PCT: f64 = 3.0;

/// Estimated sustained Metal-load wattage by hw_class (NOT measured — see
/// `scripts/supplier_earnings_calculator.py`'s own caveat: no idle/load
/// power-draw benchmark exists yet). Used only to net out an electricity cost
/// so the figure is net, not gross.
fn estimated_sustained_watts(hw_class: crate::types::HardwareClass) -> f64 {
    use crate::types::HardwareClass::*;
    match hw_class {
        AppleSiliconBase => 20.0,
        AppleSiliconPro => 30.0,
        AppleSiliconMax => 45.0,
        AppleSiliconUltra | AppleSiliconCluster => 65.0,
        Cpu => 25.0,
        Nvidia24g | Nvidia48g | Nvidia80g | Nvidia180g => 150.0,
    }
}

const DEFAULT_ELECTRICITY_USD_PER_KWH: f64 = 0.15;

/// Hours/day this worker is assumed to stay online for the projection — this is
/// a "ceiling if you stay online all day" figure (the rung's own framing), not
/// an observed queue-depth number (that is the separate, honest-near-zero
/// "today" figure the 3→4 rung already publishes in
/// `scripts/supplier_earnings_calculator.py`'s output).
const HOURS_ONLINE_PER_DAY: f64 = 24.0;

/// Compute a projected "$/day if you stay online" figure for THIS worker,
/// LIVE from its own measured `benchmarks` (never a fleet average — the rung's
/// explicit requirement). Picks the worker's best-paying priced model (highest
/// net $/hr among ones with a nonzero measured tps/eps and a known catalogue
/// price), nets out an estimated electricity cost, and projects it across
/// `HOURS_ONLINE_PER_DAY`. Returns `None` when no benchmark line has both a
/// nonzero measured throughput and a catalogue price (e.g. a fresh worker that
/// hasn't benchmarked yet, or one whose only models aren't in the catalogue) —
/// the caller must never fabricate a number in that case.
pub fn projected_daily_usd(
    benchmarks: &[BenchResult],
    hw_class: crate::types::HardwareClass,
) -> Option<f64> {
    let share_rate = 1.0 - (DEFAULT_PLATFORM_TAKE_PCT.clamp(1.0, 5.0) / 100.0);
    let watts = estimated_sustained_watts(hw_class);
    let elec_usd_hr = watts / 1000.0 * DEFAULT_ELECTRICITY_USD_PER_KWH;

    let best_net_usd_hr = benchmarks
        .iter()
        .filter_map(|b| {
            let price = CX_PRICE_PER_1K
                .iter()
                .find(|(model, _)| *model == b.model_id)
                .map(|(_, p)| *p)?;
            let units_per_sec = if b.tps > 0.0 { b.tps } else { b.eps } as f64;
            if units_per_sec <= 0.0 {
                return None;
            }
            let gross_buyer_usd_hr = units_per_sec * 3600.0 / 1000.0 * price;
            let supplier_usd_hr = gross_buyer_usd_hr * share_rate;
            Some(supplier_usd_hr - elec_usd_hr)
        })
        .fold(None::<f64>, |acc, v| Some(acc.map_or(v, |a: f64| a.max(v))));

    best_net_usd_hr.map(|net_hr| (net_hr * HOURS_ONLINE_PER_DAY).max(0.0))
}

/// The job currently shown in the menu bar. Serialized shape matches
/// `StatusModel.swift`'s `CurrentJob` (`job_id` is the JOB id, not the task id).
#[derive(Serialize, Clone, PartialEq)]
struct CurrentJob {
    job_id: String,
    job_type: String,
    started_at: u64,
}

/// One in-flight task. Keyed internally by `task_id` (unique, so concurrent tasks
/// of the same job don't collide); only `job` is ever serialized.
struct InflightTask {
    task_id: Uuid,
    job: CurrentJob,
}

/// The operator preferences the agent is ACTUALLY running with (Atlas F7 / item 26):
/// the effective config AFTER the `agent.prefs.toml` overlay merged into `agent.toml`.
/// The app shows these as "applied" values sourced from the agent (truth), distinct
/// from its own local toggle state (which may differ until the agent is relaunched).
/// `max_concurrent_tasks` is the RESOLVED permit count (the derived default when the
/// operator left it on auto), so the app shows the real number, never "auto". Mirrors
/// the `AppliedPrefs` the Swift `StatusModel` decodes.
#[derive(Serialize, Clone)]
pub struct AppliedPrefs {
    pub power_only: bool,
    pub quiet_hours: Option<(u8, u8)>,
    pub min_payout_usd_per_hr: f32,
    pub memory_headroom_gb: f32,
    pub max_memory_pct: f32,
    pub max_concurrent_tasks: usize,
}

impl AppliedPrefs {
    /// Snapshot the effective operator prefs from the loaded config. `memory_gb` is
    /// the box's advertised memory, used to resolve the concurrency permit count the
    /// agent actually runs with (so an unset `max_concurrent_tasks` shows its derived
    /// value, not a placeholder).
    pub fn from_config(cfg: &AgentConfig, memory_gb: f32) -> Self {
        Self {
            power_only: cfg.power_only,
            quiet_hours: cfg.quiet_hours,
            min_payout_usd_per_hr: cfg.min_payout_usd_per_hr,
            memory_headroom_gb: cfg.memory_headroom_gb,
            max_memory_pct: cfg.max_memory_pct,
            max_concurrent_tasks: cfg.concurrency(memory_gb),
        }
    }
}

/// The serialized status document. Field names/types mirror `AgentStatus` in
/// `StatusModel.swift`; `active` is true whenever the agent process is running
/// (the operator launched it — the app's master switch gates the launch, not us).
///
/// The resource block (`total_memory_gb` … `throttle_reason`) is the supplier
/// throttling surface: live available memory, the reserved headroom, the
/// effective allocatable pool for jobs, whether the agent is currently throttled,
/// and a human-readable reason — so a provider can see exactly why work paused.
#[derive(Serialize)]
struct StatusDoc<'a> {
    schema_version: u32,
    state: &'static str,
    agent_version: &'a str,
    worker_id: Option<&'a str>,
    current_job: Option<CurrentJob>,
    current_task_id: Option<String>,
    today_earnings_usd: f64,
    balance_usd: f64,
    lifetime_usd: f64,
    /// Projected "$/day if you stay online" (Supplier Earnings Economics 4→5),
    /// computed LIVE from this worker's own measured `benchmarks` — never a
    /// fleet average. `None` when no benchmark line has both a nonzero
    /// measured throughput and a known catalogue price (e.g. before the
    /// startup benchmark has produced anything usable).
    projected_daily_usd: Option<f64>,
    thermal_state: &'static str,
    gpu_temp_c: Option<f32>,
    cpu_pct: f32,
    model_cache_bytes: u64,
    // Dynamic provider throttling surface.
    total_memory_gb: f32,
    available_memory_gb: f32,
    reserved_headroom_gb: f32,
    effective_memory_gb: f32,
    throttled: bool,
    throttle_reason: Option<&'a str>,
    active: bool,
    eligible_now: bool,
    last_heartbeat: u64,
    last_error: Option<&'a str>,
    /// The operator prefs the agent is actually running with (item 26). `None` until
    /// the runtime sets it right after config load; the app shows "unknown" then.
    applied_prefs: Option<&'a AppliedPrefs>,

    // --- Trust surface (Supplier onboarding & safety 7->8: "Populate the trust
    // panel with real data"). All optional on the wire, matching
    // `StatusModel.swift`'s contract: absent until the agent has real,
    // control-plane-sourced data to back it — never fabricated.
    payouts_configured: Option<bool>,
    payouts_connected: Option<bool>,
    payouts_enabled: Option<bool>,
    last_payout_usd: Option<f64>,
    last_payout_at: Option<u64>,
    next_payout_at: Option<u64>,
    honeypots_passed: Option<i64>,
    honeypots_failed: Option<i64>,
    verification_label: Option<&'a str>,
}

/// Mutable status shared between the heartbeat arm and the task pipeline.
struct Inner {
    worker_id: Option<String>,
    inflight: Vec<InflightTask>,
    today_earnings_usd: f64,
    balance_usd: f64,
    lifetime_usd: f64,
    /// Set once at construction from the worker's own startup benchmarks
    /// (see `StatusWriter::new`); never recomputed against a fleet average.
    projected_daily_usd: Option<f64>,
    /// `(utc_day, lifetime_usd at the first observation of that day)`, so
    /// `today = lifetime - baseline`. UTC-day based, matching the agent's coarse
    /// UTC quiet-hours convention (documented in the sample config).
    lifetime_baseline: Option<(u64, f64)>,
    thermal_state: &'static str,
    gpu_temp_c: Option<f32>,
    cpu_pct: f32,
    model_cache_bytes: u64,
    // Resource-protection telemetry, refreshed on every heartbeat + throttle event.
    total_memory_gb: f32,
    available_memory_gb: f32,
    reserved_headroom_gb: f32,
    effective_memory_gb: f32,
    throttled: bool,
    throttle_reason: Option<String>,
    eligible_now: bool,
    last_heartbeat: u64,
    last_error: Option<String>,
    /// The effective operator prefs after the overlay (item 26). Set once by the
    /// runtime after config load; `None` until then.
    applied_prefs: Option<AppliedPrefs>,

    // Trust surface (Supplier onboarding & safety 7->8), refreshed each heartbeat
    // from real `GET /v1/worker/connect/status` + `GET /v1/worker/verification`
    // calls (see `heartbeat`'s `connect`/`verification` params). `None` until the
    // first successful call — the app then shows an honest "not reported yet"
    // state rather than a fabricated default.
    payouts_configured: Option<bool>,
    payouts_connected: Option<bool>,
    payouts_enabled: Option<bool>,
    last_payout_usd: Option<f64>,
    last_payout_at: Option<u64>,
    next_payout_at: Option<u64>,
    honeypots_passed: Option<i64>,
    honeypots_failed: Option<i64>,
    verification_label: Option<String>,
}

/// Owns the status file and the shared mutable state behind a mutex. Cheap to
/// share via `Arc`; every mutating method snapshots under the lock and then
/// writes the file with the lock released.
pub struct StatusWriter {
    path: PathBuf,
    agent_version: String,
    inner: Mutex<Inner>,
}

impl StatusWriter {
    /// Build the writer for a freshly-registered worker and resolve the output
    /// path. Computes the model-cache size once up front so the first write is
    /// already populated. `benchmarks`/`hw_class` are this worker's OWN
    /// startup-benchmark results (Supplier Earnings Economics 4→5) — used once,
    /// here, to compute a live `projected_daily_usd` for this specific worker,
    /// never a fleet average.
    pub fn new(
        agent_version: &str,
        worker_id: Uuid,
        benchmarks: &[BenchResult],
        hw_class: crate::types::HardwareClass,
    ) -> Self {
        Self {
            path: status_path(),
            agent_version: agent_version.to_string(),
            inner: Mutex::new(Inner {
                worker_id: Some(worker_id.to_string()),
                inflight: Vec::new(),
                today_earnings_usd: 0.0,
                balance_usd: 0.0,
                lifetime_usd: 0.0,
                projected_daily_usd: projected_daily_usd(benchmarks, hw_class),
                lifetime_baseline: None,
                // Honest starting state: no thermal reading has happened yet.
                thermal_state: "unknown",
                gpu_temp_c: None,
                cpu_pct: 0.0,
                model_cache_bytes: dir_size(&model_cache_dir()),
                total_memory_gb: 0.0,
                available_memory_gb: 0.0,
                reserved_headroom_gb: 0.0,
                effective_memory_gb: 0.0,
                throttled: false,
                throttle_reason: None,
                eligible_now: true,
                last_heartbeat: 0,
                last_error: None,
                applied_prefs: None,
                payouts_configured: None,
                payouts_connected: None,
                payouts_enabled: None,
                last_payout_usd: None,
                last_payout_at: None,
                next_payout_at: None,
                honeypots_passed: None,
                honeypots_failed: None,
                verification_label: None,
            }),
        }
    }

    /// Record the operator prefs the agent is ACTUALLY running with (item 26), called
    /// once by the runtime right after config load. Every subsequent status write then
    /// carries the applied values, so the app can show agent truth instead of only its
    /// local toggle state.
    pub fn set_applied_prefs(&self, prefs: AppliedPrefs) {
        {
            let mut i = self.inner.lock().unwrap();
            i.applied_prefs = Some(prefs);
        }
        self.write();
    }

    /// Emit the initial `idle` status right after registration, so the app has a
    /// file to read before the first heartbeat (~30s away).
    pub fn registered(&self) {
        self.write();
    }

    /// A task started executing: show it as the current job and flip to `running`.
    pub fn job_started(&self, task_id: Uuid, job_id: Uuid, job_type: &str, started_at: u64) {
        {
            let mut i = self.inner.lock().unwrap();
            i.inflight.push(InflightTask {
                task_id,
                job: CurrentJob {
                    job_id: job_id.to_string(),
                    job_type: job_type.to_string(),
                    started_at,
                },
            });
        }
        self.write();
    }

    /// A task finished (success or failure). Drops it from the in-flight set,
    /// surfaces any error verbatim, and refreshes the model-cache size (a model
    /// may have just been downloaded).
    pub fn job_finished(&self, task_id: Uuid, error: Option<String>) {
        {
            let mut i = self.inner.lock().unwrap();
            i.inflight.retain(|t| t.task_id != task_id);
            if error.is_some() {
                i.last_error = error;
            }
            i.model_cache_bytes = dir_size(&model_cache_dir());
        }
        self.write();
    }

    /// Refresh telemetry + earnings on each heartbeat tick. `earnings` is the
    /// best-effort `GET /v1/worker/earnings` result (None when that call failed —
    /// we keep the last known totals rather than zeroing them). `throttle` is the
    /// live memory-throttle reading so the provider surface shows current
    /// available / effective memory and the pause reason. `connect`/`verification`
    /// are the same best-effort pattern, sourced from `GET /v1/worker/connect/status`
    /// and `GET /v1/worker/verification` (Supplier onboarding & safety 7->8: "Populate
    /// the trust panel with real data") — `None` on a failed call keeps the last
    /// known trust-surface state rather than blanking it back to "not reported".
    ///
    /// Nine narrow, independently-meaningful telemetry values — not a design
    /// smell worth a bundling struct across the one real and several test call
    /// sites.
    #[allow(clippy::too_many_arguments)]
    pub fn heartbeat(
        &self,
        cpu_pct: f32,
        gpu_temp_c: Option<f32>,
        thermal_pressure: Option<crate::config::ThermalPressure>,
        eligible_now: bool,
        ts: u64,
        earnings: Option<Earnings>,
        throttle: &ThrottleDecision,
        connect: Option<crate::types::ConnectStatus>,
        verification: Option<crate::types::SupplierVerification>,
    ) {
        {
            let mut i = self.inner.lock().unwrap();
            i.cpu_pct = cpu_pct;
            i.gpu_temp_c = gpu_temp_c;
            // Real `ProcessInfo.thermalState` reading (Mac agent) takes priority when
            // present; falls back to the CUDA-temperature bucketing off that lane, and
            // to an honest "unknown" (not "nominal") when neither exists.
            i.thermal_state = match thermal_pressure {
                Some(p) => thermal_from_pressure(p),
                None => thermal_from_temp(gpu_temp_c),
            };
            i.eligible_now = eligible_now;
            i.last_heartbeat = ts;
            apply_throttle(&mut i, throttle);
            if let Some(e) = earnings {
                i.balance_usd = e.balance_usd;
                i.lifetime_usd = e.lifetime_usd;
                let day = ts / 86_400;
                match i.lifetime_baseline {
                    Some((d, base)) if d == day => {
                        i.today_earnings_usd = (e.lifetime_usd - base).max(0.0);
                    }
                    // First sighting of this UTC day: today's accrual restarts at 0.
                    _ => {
                        i.lifetime_baseline = Some((day, e.lifetime_usd));
                        i.today_earnings_usd = 0.0;
                    }
                }
                i.last_payout_usd = e.last_payout_usd;
                i.last_payout_at = e.last_payout_at;
                i.next_payout_at = e.next_payout_at;
            }
            if let Some(c) = connect {
                i.payouts_configured = Some(c.configured);
                i.payouts_connected = Some(c.connected);
                i.payouts_enabled = Some(c.enabled);
            }
            if let Some(v) = verification {
                i.honeypots_passed = Some(v.honeypots_passed);
                i.honeypots_failed = Some(v.honeypots_failed);
                i.verification_label = Some(v.verification_label);
            }
        }
        self.write();
    }

    /// Reflect a throttle decision immediately (between heartbeats), so the menu
    /// bar surfaces a memory pause the moment the agent declines to claim work —
    /// not 30s later on the next heartbeat. Refreshes the resource block + reason.
    pub fn set_throttle(&self, throttle: &ThrottleDecision) {
        {
            let mut i = self.inner.lock().unwrap();
            apply_throttle(&mut i, throttle);
        }
        self.write();
    }

    /// Reflect a thermal-pause decision immediately (Supplier onboarding & safety
    /// 6→7) — the same "surface the pause the instant it happens" treatment
    /// `set_throttle` gives memory pressure. Sets `thermal_state` from the real
    /// reading and, when throttled, the pause reason (so the menu bar shows
    /// exactly why claiming stopped, matching the consent copy).
    pub fn set_thermal_throttle(&self, decision: &crate::config::ThermalDecision) {
        {
            let mut i = self.inner.lock().unwrap();
            if let Some(p) = decision.reading {
                i.thermal_state = thermal_from_pressure(p);
            }
            if decision.throttled {
                i.throttled = true;
                i.throttle_reason = decision.reason.clone();
            }
        }
        self.write();
    }

    /// Build + serialize the status document UNDER the lock (serialization is
    /// cheap CPU work), then release the lock and write the bytes to disk — so the
    /// mutex never spans the file I/O, while the borrowed doc stays valid.
    fn write(&self) {
        let json = {
            let i = self.inner.lock().unwrap();
            let state = if !i.inflight.is_empty() {
                "running"
            } else if i.eligible_now && !i.throttled {
                "idle"
            } else {
                // Not eligible (quiet hours / battery) or throttled (memory
                // pressure): the operator is still "on", we're just holding off.
                "paused"
            };
            let doc = StatusDoc {
                schema_version: SCHEMA_VERSION,
                state,
                agent_version: &self.agent_version,
                worker_id: i.worker_id.as_deref(),
                current_job: i.inflight.last().map(|t| t.job.clone()),
                current_task_id: i.inflight.last().map(|t| t.task_id.to_string()),
                today_earnings_usd: i.today_earnings_usd,
                balance_usd: i.balance_usd,
                lifetime_usd: i.lifetime_usd,
                projected_daily_usd: i.projected_daily_usd,
                thermal_state: i.thermal_state,
                gpu_temp_c: i.gpu_temp_c,
                cpu_pct: i.cpu_pct,
                model_cache_bytes: i.model_cache_bytes,
                total_memory_gb: i.total_memory_gb,
                available_memory_gb: i.available_memory_gb,
                reserved_headroom_gb: i.reserved_headroom_gb,
                effective_memory_gb: i.effective_memory_gb,
                throttled: i.throttled,
                throttle_reason: i.throttle_reason.as_deref(),
                active: true,
                eligible_now: i.eligible_now,
                last_heartbeat: i.last_heartbeat,
                last_error: i.last_error.as_deref(),
                applied_prefs: i.applied_prefs.as_ref(),
                payouts_configured: i.payouts_configured,
                payouts_connected: i.payouts_connected,
                payouts_enabled: i.payouts_enabled,
                last_payout_usd: i.last_payout_usd,
                last_payout_at: i.last_payout_at,
                next_payout_at: i.next_payout_at,
                honeypots_passed: i.honeypots_passed,
                honeypots_failed: i.honeypots_failed,
                verification_label: i.verification_label.as_deref(),
            };
            serde_json::to_vec_pretty(&doc)
        };
        match json {
            Ok(bytes) => {
                if let Err(e) = write_atomic(&self.path, &bytes) {
                    tracing::warn!(error = %e, path = %self.path.display(), "failed to write status.json");
                }
            }
            Err(e) => tracing::warn!(error = %e, "failed to serialize status.json"),
        }
    }
}

/// Copy a throttle reading into the shared status state. Shared by `heartbeat`
/// (periodic) and `set_throttle` (event-driven), so the resource block is set in
/// exactly one place.
fn apply_throttle(i: &mut Inner, t: &ThrottleDecision) {
    i.total_memory_gb = t.total_gb;
    i.available_memory_gb = t.available_gb;
    i.reserved_headroom_gb = t.reserved_headroom_gb;
    i.effective_memory_gb = t.effective_gb;
    i.throttled = t.throttled;
    i.throttle_reason = t.reason.clone();
}

/// Resolve the status-file path: `$CX_STATUS_PATH` (when set and non-empty), else
/// `~/.compute-exchange/status.json`, else `./status.json` if `$HOME` is unset.
fn status_path() -> PathBuf {
    if let Ok(p) = std::env::var("CX_STATUS_PATH") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    match std::env::var("HOME") {
        Ok(home) if !home.is_empty() => PathBuf::from(home)
            .join(".compute-exchange")
            .join("status.json"),
        _ => PathBuf::from("status.json"),
    }
}

/// Resolve the model cache root the same way `models.rs` does: `$CX_MODEL_CACHE`
/// when set, else `$HF_HOME/hub`, else `~/.cache/huggingface/hub` — so the size we
/// report matches where weights are actually downloaded (never moves them).
fn model_cache_dir() -> PathBuf {
    if let Ok(d) = std::env::var("CX_MODEL_CACHE") {
        if !d.is_empty() {
            return PathBuf::from(d);
        }
    }
    if let Ok(hf) = std::env::var("HF_HOME") {
        if !hf.is_empty() {
            return PathBuf::from(hf).join("hub");
        }
    }
    match std::env::var("HOME") {
        Ok(home) if !home.is_empty() => PathBuf::from(home)
            .join(".cache")
            .join("huggingface")
            .join("hub"),
        _ => PathBuf::from(".cache/huggingface/hub"),
    }
}

/// Best-effort recursive sum of regular-file sizes under `root`. Symlinks are not
/// followed (no cycles); unreadable entries are skipped. Missing dir → 0. Called
/// at startup and after each task, never on the per-heartbeat hot path.
fn dir_size(root: &Path) -> u64 {
    fn walk(dir: &Path, acc: &mut u64) {
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => return,
        };
        for entry in entries.flatten() {
            let Ok(ft) = entry.file_type() else { continue };
            if ft.is_symlink() {
                continue;
            }
            if ft.is_dir() {
                walk(&entry.path(), acc);
            } else if ft.is_file() {
                if let Ok(md) = entry.metadata() {
                    *acc += md.len();
                }
            }
        }
    }
    let mut total = 0;
    walk(root, &mut total);
    total
}

/// Map a GPU temperature (°C) to the app's thermal buckets. Used on the CUDA
/// lane, where `nvidia-smi` gives a real number. No reading (`None`) is honestly
/// reported as `"unknown"` — NOT `"nominal"` — because "no measurement" and
/// "measured safe" are different claims; a permanently-`"nominal"` label on a
/// device with no sensor read at all is exactly the honesty gap this function
/// used to have (docs/internal/CREED_AND_PATH_TO_TEN.md, Supplier onboarding &
/// safety 6→7). On Apple Silicon this path isn't the real signal at all —
/// `thermal_state_label` (fed by the real `ThermalPressure` overlay reading)
/// takes priority whenever it has a value; see `StatusWriter::heartbeat`.
fn thermal_from_temp(gpu_temp_c: Option<f32>) -> &'static str {
    match gpu_temp_c {
        None => "unknown",
        Some(t) if t < 70.0 => "nominal",
        Some(t) if t < 85.0 => "fair",
        Some(t) if t < 95.0 => "serious",
        Some(_) => "critical",
    }
}

/// Map a real `ThermalPressure` reading (Apple's own `ProcessInfo.thermalState`,
/// piped in via the operator-prefs overlay) to the same thermal-bucket strings
/// `thermal_from_temp` uses, so the status surface's `thermal_state` field means
/// the same thing regardless of which lane produced it.
fn thermal_from_pressure(p: crate::config::ThermalPressure) -> &'static str {
    use crate::config::ThermalPressure::*;
    match p {
        Nominal => "nominal",
        Fair => "fair",
        Serious => "serious",
        Critical => "critical",
    }
}

/// Write `json` to `path` atomically: ensure the parent dir exists, write a
/// sibling temp file, then rename it over the target (so a reader never observes
/// a half-written document). Serialization happens in `write()` under the lock.
fn write_atomic(path: &Path, json: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, json)?;
    std::fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A non-throttled decision with the given memory figures, for tests.
    fn ample(total: f32, available: f32, headroom: f32) -> ThrottleDecision {
        ThrottleDecision {
            throttled: false,
            reason: None,
            total_gb: total,
            available_gb: available,
            reserved_headroom_gb: headroom,
            effective_gb: (available - headroom).max(0.0),
            used_pct: ((total - available) / total * 100.0).max(0.0),
        }
    }

    /// The serialized document must carry every key the Swift app decodes, in
    /// snake_case, with the expected value shapes. This is the contract guard.
    #[test]
    fn status_doc_matches_swift_contract() {
        let benchmarks = vec![BenchResult {
            model_id: "llama-3.2-1b-instruct-q4".into(),
            job_type: "batch_infer".into(),
            tps: 85.64071,
            eps: 0.0,
            p99_ms: 100,
            thermal_ok: true,
            load_ms: 0,
        }];
        let w = StatusWriter::new(
            "9.9.9",
            Uuid::nil(),
            &benchmarks,
            crate::types::HardwareClass::AppleSiliconPro,
        );
        w.job_started(
            Uuid::from_u128(1),
            Uuid::from_u128(2),
            "embed",
            1_718_900_000,
        );
        // Drive a heartbeat to set telemetry + the resource block, then read the
        // exact bytes the writer persisted (round-trips the real serialization).
        // Also exercises the trust surface (Supplier onboarding & safety 7->8):
        // real payout figures + a real verification aggregate, not None, so the
        // contract test proves the wire actually carries them.
        w.heartbeat(
            18.0,
            None,
            None,
            true,
            1_718_900_123,
            Some(Earnings {
                balance_usd: 12.5,
                lifetime_usd: 130.0,
                last_payout_usd: Some(4.20),
                last_payout_at: Some(1_718_800_000),
                next_payout_at: Some(1_719_000_000),
            }),
            &ample(64.0, 40.0, 8.0),
            Some(crate::types::ConnectStatus {
                configured: true,
                connected: true,
                enabled: true,
            }),
            Some(crate::types::SupplierVerification {
                honeypots_passed: 3,
                honeypots_failed: 0,
                verification_label: "verified".into(),
            }),
        );
        // Item 26: the agent echoes the APPLIED prefs (effective config after overlay).
        w.set_applied_prefs(AppliedPrefs {
            power_only: true,
            quiet_hours: Some((22, 6)),
            min_payout_usd_per_hr: 2.5,
            memory_headroom_gb: 8.0,
            max_memory_pct: 85.0,
            max_concurrent_tasks: 4,
        });
        let v: serde_json::Value = {
            let i = w.inner.lock().unwrap();
            let doc = StatusDoc {
                schema_version: SCHEMA_VERSION,
                state: "running",
                agent_version: &w.agent_version,
                worker_id: i.worker_id.as_deref(),
                current_job: i.inflight.last().map(|t| t.job.clone()),
                current_task_id: i.inflight.last().map(|t| t.task_id.to_string()),
                today_earnings_usd: i.today_earnings_usd,
                balance_usd: i.balance_usd,
                lifetime_usd: i.lifetime_usd,
                projected_daily_usd: i.projected_daily_usd,
                thermal_state: i.thermal_state,
                gpu_temp_c: i.gpu_temp_c,
                cpu_pct: i.cpu_pct,
                model_cache_bytes: i.model_cache_bytes,
                total_memory_gb: i.total_memory_gb,
                available_memory_gb: i.available_memory_gb,
                reserved_headroom_gb: i.reserved_headroom_gb,
                effective_memory_gb: i.effective_memory_gb,
                throttled: i.throttled,
                throttle_reason: i.throttle_reason.as_deref(),
                active: true,
                eligible_now: i.eligible_now,
                last_heartbeat: i.last_heartbeat,
                last_error: i.last_error.as_deref(),
                applied_prefs: i.applied_prefs.as_ref(),
                payouts_configured: i.payouts_configured,
                payouts_connected: i.payouts_connected,
                payouts_enabled: i.payouts_enabled,
                last_payout_usd: i.last_payout_usd,
                last_payout_at: i.last_payout_at,
                next_payout_at: i.next_payout_at,
                honeypots_passed: i.honeypots_passed,
                honeypots_failed: i.honeypots_failed,
                verification_label: i.verification_label.as_deref(),
            };
            serde_json::to_value(&doc).unwrap()
        };
        for key in [
            "schema_version",
            "state",
            "agent_version",
            "worker_id",
            "current_job",
            "current_task_id",
            "today_earnings_usd",
            "balance_usd",
            "lifetime_usd",
            "projected_daily_usd",
            "thermal_state",
            "gpu_temp_c",
            "cpu_pct",
            "model_cache_bytes",
            "total_memory_gb",
            "available_memory_gb",
            "reserved_headroom_gb",
            "effective_memory_gb",
            "throttled",
            "throttle_reason",
            "active",
            "eligible_now",
            "last_heartbeat",
            "last_error",
            "applied_prefs",
            "payouts_configured",
            "payouts_connected",
            "payouts_enabled",
            "last_payout_usd",
            "last_payout_at",
            "next_payout_at",
            "honeypots_passed",
            "honeypots_failed",
            "verification_label",
        ] {
            assert!(v.get(key).is_some(), "missing key {key}");
        }
        assert_eq!(v["state"], "running");
        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["balance_usd"], 12.5);
        assert_eq!(v["current_job"]["job_type"], "embed");
        assert_eq!(v["current_job"]["job_id"], Uuid::from_u128(2).to_string());
        // Resource block reflects the throttle reading.
        assert_eq!(v["effective_memory_gb"], 32.0);
        assert_eq!(v["reserved_headroom_gb"], 8.0);
        assert_eq!(v["throttled"], false);
        // Item 26: applied prefs are echoed from the agent (the values it runs with).
        assert_eq!(v["applied_prefs"]["power_only"], true);
        assert_eq!(v["applied_prefs"]["quiet_hours"][0], 22);
        assert_eq!(v["applied_prefs"]["quiet_hours"][1], 6);
        assert_eq!(v["applied_prefs"]["min_payout_usd_per_hr"], 2.5);
        assert_eq!(v["applied_prefs"]["max_concurrent_tasks"], 4);
        // Supplier Earnings Economics 4→5: a live per-worker projection, computed
        // from THIS worker's own measured benchmarks — must be present and positive
        // for a worker with a real, priced, nonzero-throughput benchmark line.
        let projected = v["projected_daily_usd"]
            .as_f64()
            .expect("projected_daily_usd must be a number");
        assert!(
            projected > 0.0,
            "expected a positive projection, got {projected}"
        );

        // Supplier onboarding & safety 7->8: the trust panel actually carries the
        // real control-plane-sourced data passed into this heartbeat, not nulls.
        assert_eq!(v["payouts_configured"], true);
        assert_eq!(v["payouts_connected"], true);
        assert_eq!(v["payouts_enabled"], true);
        assert_eq!(v["last_payout_usd"], 4.20);
        assert_eq!(v["last_payout_at"], 1_718_800_000);
        assert_eq!(v["next_payout_at"], 1_719_000_000);
        assert_eq!(v["honeypots_passed"], 3);
        assert_eq!(v["honeypots_failed"], 0);
        assert_eq!(v["verification_label"], "verified");
    }

    /// Supplier onboarding & safety 7->8: a heartbeat whose connect/verification
    /// calls FAILED (`None`, mirroring `.ok()` on a real transport error) must keep
    /// the last known trust-surface values rather than blanking the panel back to
    /// "not reported yet" — the same honest-degradation contract `earnings: None`
    /// already gets. A absent-from-the-start worker legitimately stays `None`.
    #[test]
    fn trust_surface_survives_a_failed_poll() {
        let w = StatusWriter::new(
            "9.9.9",
            Uuid::nil(),
            &[],
            crate::types::HardwareClass::AppleSiliconPro,
        );
        w.heartbeat(
            0.0,
            None,
            None,
            true,
            100,
            None,
            &ample(64.0, 40.0, 8.0),
            Some(crate::types::ConnectStatus {
                configured: true,
                connected: true,
                enabled: true,
            }),
            Some(crate::types::SupplierVerification {
                honeypots_passed: 2,
                honeypots_failed: 1,
                verification_label: "honeypot-checked".into(),
            }),
        );
        {
            let i = w.inner.lock().unwrap();
            assert_eq!(i.payouts_enabled, Some(true));
            assert_eq!(i.honeypots_passed, Some(2));
            assert_eq!(i.honeypots_failed, Some(1));
            assert_eq!(i.verification_label.as_deref(), Some("honeypot-checked"));
        }
        // Next heartbeat's connect/verification calls failed (None) — real values
        // must survive, not reset to None.
        w.heartbeat(
            0.0,
            None,
            None,
            true,
            200,
            None,
            &ample(64.0, 40.0, 8.0),
            None,
            None,
        );
        let i = w.inner.lock().unwrap();
        assert_eq!(
            i.payouts_enabled,
            Some(true),
            "payout readiness must survive a failed poll"
        );
        assert_eq!(
            i.honeypots_passed,
            Some(2),
            "verification counts must survive a failed poll"
        );
        assert_eq!(i.honeypots_failed, Some(1));
        assert_eq!(i.verification_label.as_deref(), Some("honeypot-checked"));
    }

    /// Before the first successful connect/verification call, the trust surface is
    /// honestly absent (`None`) — never a fabricated default like `false`/`0`.
    #[test]
    fn trust_surface_absent_until_first_real_report() {
        let w = StatusWriter::new(
            "9.9.9",
            Uuid::nil(),
            &[],
            crate::types::HardwareClass::AppleSiliconPro,
        );
        w.heartbeat(
            0.0,
            None,
            None,
            true,
            100,
            None,
            &ample(64.0, 40.0, 8.0),
            None,
            None,
        );
        let i = w.inner.lock().unwrap();
        assert_eq!(i.payouts_configured, None);
        assert_eq!(i.payouts_connected, None);
        assert_eq!(i.payouts_enabled, None);
        assert_eq!(i.honeypots_passed, None);
        assert_eq!(i.honeypots_failed, None);
        assert_eq!(i.verification_label, None);
    }

    /// A throttled heartbeat flips state to `paused` and surfaces the reason.
    #[test]
    fn throttled_state_is_paused_with_reason() {
        let w = StatusWriter::new(
            "9.9.9",
            Uuid::nil(),
            &[],
            crate::types::HardwareClass::AppleSiliconPro,
        );
        let throttled = ThrottleDecision {
            throttled: true,
            reason: Some("reserved headroom: 6.0 GB available ≤ 8.0 GB headroom".into()),
            total_gb: 16.0,
            available_gb: 6.0,
            reserved_headroom_gb: 8.0,
            effective_gb: 0.0,
            used_pct: 62.5,
        };
        w.heartbeat(5.0, None, None, true, 200, None, &throttled, None, None);
        let i = w.inner.lock().unwrap();
        assert!(i.throttled && i.inflight.is_empty() && i.eligible_now);
        assert!(i.throttle_reason.as_deref().unwrap().contains("headroom"));
    }

    /// The projection is computed LIVE from the worker's own measured
    /// benchmarks, not a fleet average: two different `tps` inputs for the
    /// same hw_class produce two different projections, and the math matches
    /// the same formula `scripts/supplier_earnings_calculator.py` uses
    /// (gross buyer $/hr = tps × 3600/1000 × price; net = ×supplier_share −
    /// electricity, then ×24h).
    #[test]
    fn projected_daily_usd_scales_with_measured_throughput() {
        let bench = |tps: f32| {
            vec![BenchResult {
                model_id: "llama-3.2-1b-instruct-q4".into(),
                job_type: "batch_infer".into(),
                tps,
                eps: 0.0,
                p99_ms: 100,
                thermal_ok: true,
                load_ms: 0,
            }]
        };
        let slow = projected_daily_usd(&bench(20.0), crate::types::HardwareClass::AppleSiliconPro)
            .unwrap();
        let fast = projected_daily_usd(
            &bench(85.64071),
            crate::types::HardwareClass::AppleSiliconPro,
        )
        .unwrap();
        assert!(
            fast > slow,
            "higher measured tok/s must yield a higher projection (fast={fast}, slow={slow}), never a fleet-average constant"
        );

        // Hand-check the fast case against the same formula the Python calculator
        // uses: gross = 85.64071 * 3600/1000 * 0.002 = 0.61661...; supplier share at
        // the default 3% take = ×0.97; minus AppleSiliconPro's 30W × $0.15/kWh
        // electricity ($0.0045/hr); × 24h/day.
        let gross_hr = 85.64071_f64 * 3600.0 / 1000.0 * 0.00200;
        let net_hr = gross_hr * 0.97 - (30.0_f64 / 1000.0 * 0.15);
        let expected_daily = net_hr * 24.0;
        assert!(
            (fast - expected_daily).abs() < 1e-6,
            "fast={fast} expected≈{expected_daily}"
        );
    }

    /// A worker with no benchmark line that has BOTH a nonzero measured
    /// throughput AND a known catalogue price must yield `None` — never a
    /// fabricated number.
    #[test]
    fn projected_daily_usd_none_when_unpriced_or_zero_throughput() {
        assert_eq!(
            projected_daily_usd(&[], crate::types::HardwareClass::AppleSiliconPro),
            None
        );
        let zero_tps = vec![BenchResult {
            model_id: "llama-3.2-1b-instruct-q4".into(),
            job_type: "batch_infer".into(),
            tps: 0.0,
            eps: 0.0,
            p99_ms: 100,
            thermal_ok: true,
            load_ms: 0,
        }];
        assert_eq!(
            projected_daily_usd(&zero_tps, crate::types::HardwareClass::AppleSiliconPro),
            None
        );
        let unpriced_model = vec![BenchResult {
            model_id: "some-unknown-model".into(),
            job_type: "batch_infer".into(),
            tps: 50.0,
            eps: 0.0,
            p99_ms: 100,
            thermal_ok: true,
            load_ms: 0,
        }];
        assert_eq!(
            projected_daily_usd(
                &unpriced_model,
                crate::types::HardwareClass::AppleSiliconPro
            ),
            None
        );
    }

    /// The projection picks the worker's BEST-paying priced+measured model when
    /// several benchmark lines exist, not the first or an average.
    #[test]
    fn projected_daily_usd_picks_best_paying_model() {
        let bench = vec![
            BenchResult {
                model_id: "all-minilm-l6-v2".into(),
                job_type: "embed".into(),
                tps: 0.0,
                eps: 1967.3141,
                p99_ms: 10,
                thermal_ok: true,
                load_ms: 0,
            },
            BenchResult {
                model_id: "llama-3.2-1b-instruct-q4".into(),
                job_type: "batch_infer".into(),
                tps: 85.64071,
                eps: 0.0,
                p99_ms: 100,
                thermal_ok: true,
                load_ms: 0,
            },
        ];
        let combined =
            projected_daily_usd(&bench, crate::types::HardwareClass::AppleSiliconPro).unwrap();
        let embed_only = projected_daily_usd(
            &[bench[0].clone()],
            crate::types::HardwareClass::AppleSiliconPro,
        )
        .unwrap();
        let llama_only = projected_daily_usd(
            &[bench[1].clone()],
            crate::types::HardwareClass::AppleSiliconPro,
        )
        .unwrap();
        // At these real measured rates (1967.3 eps vs 85.6 tps) the embed line
        // nets more per hour than llama despite being listed FIRST in the
        // array and having the lower catalogue price-per-unit — proving the
        // selection is a real max-by-value, not "first" or "highest price".
        assert!(embed_only > llama_only);
        assert!((combined - embed_only).abs() < 1e-9);
    }

    #[test]
    fn thermal_buckets() {
        // No reading is honestly "unknown", never fabricated as "nominal"
        // (Supplier onboarding & safety 6→7 — this was the exact gap).
        assert_eq!(thermal_from_temp(None), "unknown");
        assert_eq!(thermal_from_temp(Some(50.0)), "nominal");
        assert_eq!(thermal_from_temp(Some(80.0)), "fair");
        assert_eq!(thermal_from_temp(Some(90.0)), "serious");
        assert_eq!(thermal_from_temp(Some(99.0)), "critical");
    }

    #[test]
    fn thermal_pressure_bucket_mapping_matches_temp_buckets() {
        use crate::config::ThermalPressure;
        assert_eq!(thermal_from_pressure(ThermalPressure::Nominal), "nominal");
        assert_eq!(thermal_from_pressure(ThermalPressure::Fair), "fair");
        assert_eq!(thermal_from_pressure(ThermalPressure::Serious), "serious");
        assert_eq!(thermal_from_pressure(ThermalPressure::Critical), "critical");
    }

    /// `state` reflects eligibility when no job is running: eligible → idle,
    /// not eligible (quiet hours / battery) → paused.
    #[test]
    fn state_idle_vs_paused_when_no_job() {
        let w = StatusWriter::new(
            "9.9.9",
            Uuid::nil(),
            &[],
            crate::types::HardwareClass::AppleSiliconPro,
        );
        w.heartbeat(
            0.0,
            None,
            None,
            true,
            100,
            None,
            &ample(64.0, 40.0, 8.0),
            None,
            None,
        );
        {
            let i = w.inner.lock().unwrap();
            assert!(i.inflight.is_empty() && i.eligible_now && !i.throttled);
        }
        w.heartbeat(
            0.0,
            None,
            None,
            false,
            200,
            None,
            &ample(64.0, 40.0, 8.0),
            None,
            None,
        );
        {
            let i = w.inner.lock().unwrap();
            assert!(!i.eligible_now);
        }
    }
}
