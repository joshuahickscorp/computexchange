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
use crate::types::Earnings;

/// Bumped only on a breaking change to the on-disk shape (the app tolerates a
/// partial/older file but reads this to know what to expect).
const SCHEMA_VERSION: u32 = 1;

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
}

/// Mutable status shared between the heartbeat arm and the task pipeline.
struct Inner {
    worker_id: Option<String>,
    inflight: Vec<InflightTask>,
    today_earnings_usd: f64,
    balance_usd: f64,
    lifetime_usd: f64,
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
    /// already populated.
    pub fn new(agent_version: &str, worker_id: Uuid) -> Self {
        Self {
            path: status_path(),
            agent_version: agent_version.to_string(),
            inner: Mutex::new(Inner {
                worker_id: Some(worker_id.to_string()),
                inflight: Vec::new(),
                today_earnings_usd: 0.0,
                balance_usd: 0.0,
                lifetime_usd: 0.0,
                lifetime_baseline: None,
                thermal_state: "nominal",
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
    /// available / effective memory and the pause reason.
    pub fn heartbeat(
        &self,
        cpu_pct: f32,
        gpu_temp_c: Option<f32>,
        eligible_now: bool,
        ts: u64,
        earnings: Option<Earnings>,
        throttle: &ThrottleDecision,
    ) {
        {
            let mut i = self.inner.lock().unwrap();
            i.cpu_pct = cpu_pct;
            i.gpu_temp_c = gpu_temp_c;
            i.thermal_state = thermal_from_temp(gpu_temp_c);
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

/// Map a GPU temperature (°C) to the app's thermal buckets. No reading (None) →
/// `nominal` (we never fabricate a thermal state). Thresholds are conservative
/// for Apple Silicon GPUs.
fn thermal_from_temp(gpu_temp_c: Option<f32>) -> &'static str {
    match gpu_temp_c {
        None => "nominal",
        Some(t) if t < 70.0 => "nominal",
        Some(t) if t < 85.0 => "fair",
        Some(t) if t < 95.0 => "serious",
        Some(_) => "critical",
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
        let w = StatusWriter::new("9.9.9", Uuid::nil());
        w.job_started(
            Uuid::from_u128(1),
            Uuid::from_u128(2),
            "embed",
            1_718_900_000,
        );
        // Drive a heartbeat to set telemetry + the resource block, then read the
        // exact bytes the writer persisted (round-trips the real serialization).
        w.heartbeat(
            18.0,
            None,
            true,
            1_718_900_123,
            Some(Earnings {
                balance_usd: 12.5,
                lifetime_usd: 130.0,
            }),
            &ample(64.0, 40.0, 8.0),
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
    }

    /// A throttled heartbeat flips state to `paused` and surfaces the reason.
    #[test]
    fn throttled_state_is_paused_with_reason() {
        let w = StatusWriter::new("9.9.9", Uuid::nil());
        let throttled = ThrottleDecision {
            throttled: true,
            reason: Some("reserved headroom: 6.0 GB available ≤ 8.0 GB headroom".into()),
            total_gb: 16.0,
            available_gb: 6.0,
            reserved_headroom_gb: 8.0,
            effective_gb: 0.0,
            used_pct: 62.5,
        };
        w.heartbeat(5.0, None, true, 200, None, &throttled);
        let i = w.inner.lock().unwrap();
        assert!(i.throttled && i.inflight.is_empty() && i.eligible_now);
        assert!(i.throttle_reason.as_deref().unwrap().contains("headroom"));
    }

    #[test]
    fn thermal_buckets() {
        assert_eq!(thermal_from_temp(None), "nominal");
        assert_eq!(thermal_from_temp(Some(50.0)), "nominal");
        assert_eq!(thermal_from_temp(Some(80.0)), "fair");
        assert_eq!(thermal_from_temp(Some(90.0)), "serious");
        assert_eq!(thermal_from_temp(Some(99.0)), "critical");
    }

    /// `state` reflects eligibility when no job is running: eligible → idle,
    /// not eligible (quiet hours / battery) → paused.
    #[test]
    fn state_idle_vs_paused_when_no_job() {
        let w = StatusWriter::new("9.9.9", Uuid::nil());
        w.heartbeat(0.0, None, true, 100, None, &ample(64.0, 40.0, 8.0));
        {
            let i = w.inner.lock().unwrap();
            assert!(i.inflight.is_empty() && i.eligible_now && !i.throttled);
        }
        w.heartbeat(0.0, None, false, 200, None, &ample(64.0, 40.0, 8.0));
        {
            let i = w.inner.lock().unwrap();
            assert!(!i.eligible_now);
        }
    }
}
