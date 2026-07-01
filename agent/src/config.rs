//! Agent configuration — loaded from a TOML file, with environment overrides.
//!
//! Real logic: parse a TOML file at the path given by `--config`, apply
//! `CX_CONTROL_URL` / `CX_WORKER_TOKEN` overrides, and decide eligibility from
//! the user's resource policy (quiet hours, battery).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;
use uuid::Uuid;

use crate::hardware::MemorySnapshot;

/// Default GB of memory reserved for the supplier's own use. Conservative on
/// purpose: a provider should never feel the agent. Operators lower it on a
/// dedicated box. Used when `agent.toml` omits `memory_headroom_gb`.
fn default_memory_headroom_gb() -> f32 {
    8.0
}

/// Default ceiling on physical-memory utilization (%). At/above this the agent
/// stops claiming new work regardless of headroom, so a consumer Mac is never
/// driven into swap. Used when `agent.toml` omits `max_memory_pct`.
fn default_max_memory_pct() -> f32 {
    85.0
}

/// User-facing operator policy for this machine. Deserialized straight from TOML.
///
/// Some fields (`max_cpu_pct`, `min_payout_usd_per_hr`, `data_dir`) are part of
/// the operator config contract and are consumed by Phase 2 (CPU throttling,
/// reservation-price reporting, model cache placement); they are parsed and
/// validated today even though the honest-stub loop does not yet act on them.
/// Which on-device runtime serves generative LLM jobs. `candle` (default) is the
/// wired path; the others opt into serving-lane SEAMs that surface an honest
/// boundary until their runtime is wired:
/// - `mlx`     — Apple MLX (mlx-rs / Metal FFI), `runners::MlxRunner`.
/// - `vllm`    — pinned vLLM OpenAI-compatible CUDA server, `runners::VllmRunner`
///   (PERF_AND_CAPABILITY_AUDIT Wave 2; docs/VLLM_LANE.md). The CUDA serving lane.
/// - `hawking` — Apple-Silicon continuous-batch lane ported from the founder's
///   Hawking engine, `continuous_batch` module (docs/HAWKING_PORT_PLAN.md). Apple-only.
/// Set in agent.toml: `inference_backend = "vllm"` (etc).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InferenceBackend {
    #[default]
    Candle,
    Mlx,
    Vllm,
    Hawking,
}

impl InferenceBackend {
    /// Stable wire string the worker advertises as its `engine` discriminator
    /// (control/types.go WorkerCapability.Engine). The control plane draws
    /// byte-exact redundancy peers and seeds honeypots from the SAME
    /// (hw_class, engine) class, so two workers running DIFFERENT engines (whose
    /// FP kernels differ) are never byte-compared. `candle` is the default and the
    /// only wired path today; `mlx`, `vllm`, and `hawking` are serving-lane seams,
    /// each with its OWN tag so its (FP-distinct) output is only ever compared
    /// within its own engine class. Matches the lowercase serde rename so the
    /// advertised string is the same token the config accepts.
    pub fn engine_tag(&self) -> &'static str {
        match self {
            InferenceBackend::Candle => "candle",
            InferenceBackend::Mlx => "mlx",
            InferenceBackend::Vllm => "vllm",
            InferenceBackend::Hawking => "hawking",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct AgentConfig {
    /// Base URL of the control plane, e.g. `https://control.computeexchange.dev`.
    pub control_url: String,
    /// Opaque worker auth token, sent as `X-Worker-Token`.
    pub worker_token: String,
    /// Stable identity of the supplier (the human/org operating this fleet).
    pub supplier_id: Uuid,
    /// Ceiling on CPU usage this agent will drive the box to (0.0..=100.0).
    pub max_cpu_pct: f32,
    /// Inclusive-start, exclusive-end local-hour window to stay idle, e.g.
    /// `(9, 17)` = refuse work 09:00–16:59. `None` = always allowed by hours.
    pub quiet_hours: Option<(u8, u8)>,
    /// If true, refuse to run while on battery power.
    pub power_only: bool,
    /// Floor on acceptable pay rate; the control plane is told this so it does
    /// not dispatch work below the operator's reservation price.
    pub min_payout_usd_per_hr: f32,
    /// GB of memory reserved for the supplier's own use. Effective allocatable
    /// memory for jobs is `available - memory_headroom_gb`; once that floor would
    /// be breached the agent pauses new claims (dynamic provider throttling).
    /// Defaults via `default_memory_headroom_gb` when omitted from the TOML.
    #[serde(default = "default_memory_headroom_gb")]
    pub memory_headroom_gb: f32,
    /// Ceiling on physical-memory utilization (%). At/above it the agent stops
    /// claiming new work even if headroom remains, so the box never swaps.
    /// Defaults via `default_max_memory_pct` when omitted.
    #[serde(default = "default_max_memory_pct")]
    pub max_memory_pct: f32,
    /// Where the agent stores model caches / scratch / results.
    pub data_dir: PathBuf,
    /// Max tasks executed concurrently (the work pipeline's permit count). `None`
    /// → derive a sensible default from cores + memory at startup. Heavy GPU
    /// compute still serializes behind each model's mutex; concurrency overlaps
    /// S3 I/O with compute and lets distinct models run in parallel.
    #[serde(default)]
    pub max_concurrent_tasks: Option<usize>,
    /// On-device inference runtime for generative LLM jobs (`candle` default, or
    /// `mlx` to route them to the MLX serving-lane seam). See `InferenceBackend`.
    #[serde(default)]
    pub inference_backend: InferenceBackend,
}

/// Operator-preferences overlay (Atlas F7 / backlog item 25). A PARTIAL TOML the
/// menu-bar app writes as `agent.prefs.toml` carrying ONLY the operator-tunable
/// knobs. Every field is optional: a present value OVERRIDES the base `agent.toml`,
/// an absent one leaves the base untouched. This is what makes the Mac app's menu
/// toggles real instead of decorative — the launched agent reads this overlay (via
/// `CX_AGENT_PREFS` or the conventional sidecar next to the config) and merges it.
/// Unknown keys are ignored on purpose, so a newer app writing a key an older agent
/// build doesn't know never breaks the launch.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct OperatorPrefs {
    pub power_only: Option<bool>,
    pub min_payout_usd_per_hr: Option<f32>,
    pub memory_headroom_gb: Option<f32>,
    pub max_memory_pct: Option<f32>,
    pub max_cpu_pct: Option<f32>,
    pub quiet_hours: Option<(u8, u8)>,
    pub max_concurrent_tasks: Option<usize>,
}

impl OperatorPrefs {
    /// Parse a prefs TOML file. A malformed file is a surfaced error, never silently
    /// dropped — a decorative toggle is worse than an absent one (F7).
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading prefs file {}", path.display()))?;
        toml::from_str(&text).with_context(|| format!("parsing prefs TOML {}", path.display()))
    }
}

/// The outcome of a memory-throttle evaluation. Pure data carried into the
/// status surface (menu bar) and the control-plane heartbeat. `reason` is set
/// iff `throttled`; every figure is GB except `used_pct`.
#[derive(Debug, Clone, PartialEq)]
pub struct ThrottleDecision {
    pub throttled: bool,
    pub reason: Option<String>,
    pub total_gb: f32,
    pub available_gb: f32,
    pub reserved_headroom_gb: f32,
    /// Effective allocatable memory for jobs = available − headroom (floored at 0).
    pub effective_gb: f32,
    pub used_pct: f32,
}

impl AgentConfig {
    /// Decide whether memory pressure should pause NEW work. PURE — fully
    /// determined by the snapshot + policy (+ an optional next-task estimate) with
    /// no I/O, so it is unit-tested without ever touching the OS or risking a real
    /// OOM. Effective allocatable memory is `available − memory_headroom_gb`; we
    /// refuse new work when (a) utilization has reached `max_memory_pct`, (b) the
    /// reserved headroom would be breached (available ≤ headroom), or (c) a known
    /// next-task estimate exceeds the effective pool — so the supplier's Mac is
    /// never pushed into swap/OOM by work the agent chose to take. Each gate (a)/(b)
    /// is active only when its knob is positive; with BOTH `max_memory_pct` and
    /// `memory_headroom_gb` at 0 the governor is off (real figures still reported,
    /// never throttles).
    pub fn evaluate_memory_throttle(
        &self,
        snap: &MemorySnapshot,
        next_task_gb: Option<f32>,
    ) -> ThrottleDecision {
        let headroom = self.memory_headroom_gb.max(0.0);
        let effective = (snap.available_gb - headroom).max(0.0);
        let used_pct = snap.used_pct();

        // Governor OFF when BOTH knobs are non-positive: no memory protection
        // configured (a dedicated box opts out). We still report the real figures
        // — we just never pause. Each active check is guarded by its own knob
        // being positive (symmetric), so headroom=0 disables the headroom gate
        // and max_memory_pct=0 disables the utilization gate.
        let reason = if self.max_memory_pct > 0.0 && used_pct >= self.max_memory_pct {
            Some(format!(
                "memory pressure: {used_pct:.0}% used ≥ {:.0}% ceiling",
                self.max_memory_pct
            ))
        } else if headroom > 0.0 && snap.available_gb <= headroom {
            Some(format!(
                "reserved headroom: {:.1} GB available ≤ {headroom:.1} GB headroom",
                snap.available_gb
            ))
        } else if next_task_gb.is_some_and(|need| need > effective) {
            let need = next_task_gb.unwrap();
            Some(format!(
                "next task needs {need:.1} GB but only {effective:.1} GB allocatable after headroom"
            ))
        } else {
            None
        };

        ThrottleDecision {
            throttled: reason.is_some(),
            reason,
            total_gb: snap.total_gb,
            available_gb: snap.available_gb,
            reserved_headroom_gb: headroom,
            effective_gb: effective,
            used_pct,
        }
    }

    /// Load config from a TOML file, then apply env overrides.
    ///
    /// `CX_CONTROL_URL` and `CX_WORKER_TOKEN`, when set and non-empty, replace
    /// the corresponding file value. This lets the token stay out of the file.
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config file {}", path.display()))?;
        let mut cfg: AgentConfig =
            toml::from_str(&text).with_context(|| format!("parsing TOML {}", path.display()))?;

        if let Ok(url) = std::env::var("CX_CONTROL_URL") {
            if !url.is_empty() {
                cfg.control_url = url;
            }
        }
        if let Ok(token) = std::env::var("CX_WORKER_TOKEN") {
            if !token.is_empty() {
                cfg.worker_token = token;
            }
        }

        // Operator-prefs overlay (Atlas F7 / item 25). Source, in precedence order:
        //   1. CX_AGENT_PREFS — an explicit path (the app sets it on launch), else
        //   2. the conventional `agent.prefs.toml` sidecar next to the config file —
        //      the exact path the menu-bar app writes — so its toggles apply with no
        //      extra wiring. A present pref value overrides the base; absent leaves it.
        // An explicit path that won't read, or an existing sidecar that won't parse, is
        // a HARD error (surfaced) — silently-ignored prefs ARE the decorative-toggle bug.
        let prefs_path: Option<PathBuf> = match std::env::var("CX_AGENT_PREFS") {
            Ok(p) if !p.is_empty() => Some(PathBuf::from(p)),
            _ => {
                let sidecar = path.with_file_name("agent.prefs.toml");
                if sidecar.exists() {
                    Some(sidecar)
                } else {
                    None
                }
            }
        };
        if let Some(pp) = prefs_path {
            cfg.apply_prefs(&OperatorPrefs::load(&pp)?);
        }

        Ok(cfg)
    }

    /// Merge an operator-prefs overlay over this config (Atlas F7 / item 25). Each
    /// PRESENT pref field replaces the base value (prefs win); an ABSENT field is left
    /// untouched. `quiet_hours` is set when present; the app omits the key entirely
    /// when quiet hours are disabled, so an absent `quiet_hours` leaves the base value
    /// rather than clearing it. Pure — unit-tested without I/O.
    pub fn apply_prefs(&mut self, prefs: &OperatorPrefs) {
        if let Some(v) = prefs.power_only {
            self.power_only = v;
        }
        if let Some(v) = prefs.min_payout_usd_per_hr {
            self.min_payout_usd_per_hr = v;
        }
        if let Some(v) = prefs.memory_headroom_gb {
            self.memory_headroom_gb = v;
        }
        if let Some(v) = prefs.max_memory_pct {
            self.max_memory_pct = v;
        }
        if let Some(v) = prefs.max_cpu_pct {
            self.max_cpu_pct = v;
        }
        if let Some(v) = prefs.quiet_hours {
            self.quiet_hours = Some(v);
        }
        if let Some(v) = prefs.max_concurrent_tasks {
            // The app writes 0 to mean "derive from cores+RAM" (its sidecar comment), so
            // map 0 to None (the derive sentinel) rather than pinning concurrency to 1.
            self.max_concurrent_tasks = if v == 0 { None } else { Some(v) };
        }
    }

    /// Decide whether the agent may pick up work right now.
    ///
    /// Rules (any failing rule means "no"):
    /// - During quiet hours, refuse. The window `(start, end)` is treated on a
    ///   24h clock; if `start <= end` it is the plain `[start, end)` interval,
    ///   if `start > end` it wraps past midnight (e.g. `(22, 6)` = 22:00–05:59).
    /// - On battery, refuse when `power_only` is set.
    pub fn is_eligible_to_run(&self, now_hour: u8, on_battery: bool) -> bool {
        if self.power_only && on_battery {
            return false;
        }
        if let Some((start, end)) = self.quiet_hours {
            let in_quiet = if start <= end {
                now_hour >= start && now_hour < end
            } else {
                now_hour >= start || now_hour < end
            };
            if in_quiet {
                return false;
            }
        }
        true
    }

    /// Resolve the work pipeline's permit count. Honors an explicit
    /// `max_concurrent_tasks`; otherwise derives a small, memory-aware default:
    /// roughly one slot per 8 GiB of RAM, clamped to `[2, 4]` (the warm models
    /// are the memory cost, and heavy compute serializes behind their mutexes, so
    /// a wide pool buys nothing but RAM pressure). Always at least 1.
    pub fn concurrency(&self, memory_gb: f32) -> usize {
        match self.max_concurrent_tasks {
            Some(n) => n.max(1),
            None => ((memory_gb / 8.0) as usize).clamp(2, 4),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(quiet_hours: Option<(u8, u8)>, power_only: bool) -> AgentConfig {
        AgentConfig {
            control_url: "http://localhost".into(),
            worker_token: "t".into(),
            supplier_id: Uuid::nil(),
            max_cpu_pct: 80.0,
            quiet_hours,
            power_only,
            min_payout_usd_per_hr: 0.0,
            memory_headroom_gb: 8.0,
            max_memory_pct: 85.0,
            data_dir: PathBuf::from("/tmp"),
            max_concurrent_tasks: None,
            inference_backend: InferenceBackend::default(),
        }
    }

    fn snap(total: f32, available: f32) -> MemorySnapshot {
        MemorySnapshot {
            total_gb: total,
            available_gb: available,
        }
    }

    #[test]
    fn throttle_clears_when_memory_is_ample() {
        // 64 GB box, 40 GB free, 8 GB headroom → 32 GB effective, used 37.5% < 85%.
        let d = cfg(None, false).evaluate_memory_throttle(&snap(64.0, 40.0), None);
        assert!(!d.throttled, "reason: {:?}", d.reason);
        assert_eq!(d.reserved_headroom_gb, 8.0);
        assert!((d.effective_gb - 32.0).abs() < 0.01);
    }

    #[test]
    fn throttle_trips_when_headroom_would_be_breached() {
        // Only 6 GB available, headroom 8 GB → effective 0, paused with a reason.
        let d = cfg(None, false).evaluate_memory_throttle(&snap(16.0, 6.0), None);
        assert!(d.throttled);
        assert_eq!(d.effective_gb, 0.0);
        assert!(d.reason.unwrap().contains("headroom"));
    }

    #[test]
    fn throttle_trips_on_utilization_ceiling() {
        // 90% used ≥ 85% ceiling, even though 10 GB > 8 GB headroom would pass (b).
        let mut c = cfg(None, false);
        c.memory_headroom_gb = 1.0; // headroom alone would NOT trip
        let d = c.evaluate_memory_throttle(&snap(100.0, 10.0), None);
        assert!(d.throttled);
        assert!(d.reason.unwrap().contains("pressure"));
    }

    #[test]
    fn governor_off_when_both_knobs_zero() {
        // headroom 0 AND max_memory_pct 0 ⇒ no memory protection configured: never
        // throttle, even at literally 0 GB available (a dedicated box opting out).
        let mut c = cfg(None, false);
        c.memory_headroom_gb = 0.0;
        c.max_memory_pct = 0.0;
        assert!(!c.evaluate_memory_throttle(&snap(16.0, 0.0), None).throttled);
        assert!(
            !c.evaluate_memory_throttle(&snap(16.0, 16.0), None)
                .throttled
        );
        // A single knob still governs: headroom 0 but a utilization ceiling set.
        c.max_memory_pct = 90.0;
        assert!(c.evaluate_memory_throttle(&snap(16.0, 0.5), None).throttled);
    }

    #[test]
    fn throttle_respects_known_next_task_estimate() {
        // 20 GB available, 8 GB headroom → 12 GB effective. A 16 GB task can't fit.
        let c = cfg(None, false);
        assert!(
            c.evaluate_memory_throttle(&snap(32.0, 20.0), Some(16.0))
                .throttled
        );
        // …but a 4 GB task fits and does not trip.
        assert!(
            !c.evaluate_memory_throttle(&snap(32.0, 20.0), Some(4.0))
                .throttled
        );
    }

    #[test]
    fn vram_tier_gating_uses_same_throttle_against_vram() {
        // CUDA-lane gating: the SAME `evaluate_memory_throttle` is fed a
        // VRAM-shaped snapshot (total = total VRAM, available = free VRAM). This is
        // the exact bug from the task: a 24 GB card already holding ~20 GB (≈3.5 GB
        // free) handed a ~16 GB-VRAM job must THROTTLE, where a GPU box's ample host
        // RAM never would. We model a dedicated GPU box (headroom 0) so only the
        // utilization ceiling + next-task estimate govern — VRAM, not host RAM.
        let mut c = cfg(None, false);
        c.memory_headroom_gb = 0.0; // dedicated GPU box: no host-RAM-style reserve
        c.max_memory_pct = 90.0; // pause once VRAM is ≥90% resident

        // 24 GB card, ~3.5 GB free → 85.4% used (< 90% ceiling) but a 16 GB job
        // cannot fit the 3.5 GB of free VRAM → throttled on the next-task estimate.
        let near_full = snap(24.0, 3.5);
        let d = c.evaluate_memory_throttle(&near_full, Some(16.0));
        assert!(
            d.throttled,
            "20GB-resident 24GB card must throttle a 16GB job"
        );
        assert!(d.reason.unwrap().contains("16.0 GB"));

        // Same card, but now 22 GB resident → 91.7% used ≥ 90% ceiling: throttle
        // even with no known next-task estimate (pure utilization gate on VRAM).
        let d = c.evaluate_memory_throttle(&snap(24.0, 2.0), None);
        assert!(d.throttled);
        assert!(d.reason.unwrap().contains("pressure"));

        // Same card freshly idle (22 GB free): a 16 GB job fits and 8.3% used is
        // well under the ceiling → NOT throttled. Proves we gate on real VRAM
        // headroom, not blanket-refuse on the CUDA lane.
        let d = c.evaluate_memory_throttle(&snap(24.0, 22.0), Some(16.0));
        assert!(
            !d.throttled,
            "idle 24GB card must accept a 16GB job: {:?}",
            d.reason
        );
        assert!((d.effective_gb - 22.0).abs() < 0.01);
    }

    #[test]
    fn battery_blocks_only_when_power_only() {
        assert!(!cfg(None, true).is_eligible_to_run(12, true));
        assert!(cfg(None, true).is_eligible_to_run(12, false));
        assert!(cfg(None, false).is_eligible_to_run(12, true));
    }

    #[test]
    fn quiet_hours_same_day_window() {
        let c = cfg(Some((9, 17)), false);
        assert!(!c.is_eligible_to_run(9, false));
        assert!(!c.is_eligible_to_run(16, false));
        assert!(c.is_eligible_to_run(17, false));
        assert!(c.is_eligible_to_run(8, false));
    }

    #[test]
    fn concurrency_default_is_memory_aware_and_clamped() {
        let mut c = cfg(None, false);
        // Derived path: ~1 slot per 8 GiB, clamped to [2, 4].
        assert_eq!(c.concurrency(4.0), 2); // tiny box still gets 2
        assert_eq!(c.concurrency(16.0), 2);
        assert_eq!(c.concurrency(64.0), 4); // big box caps at 4
                                            // Explicit override wins and is floored at 1.
        c.max_concurrent_tasks = Some(8);
        assert_eq!(c.concurrency(8.0), 8);
        c.max_concurrent_tasks = Some(0);
        assert_eq!(c.concurrency(8.0), 1);
    }

    #[test]
    fn quiet_hours_wraps_midnight() {
        let c = cfg(Some((22, 6)), false);
        assert!(!c.is_eligible_to_run(23, false));
        assert!(!c.is_eligible_to_run(0, false));
        assert!(!c.is_eligible_to_run(5, false));
        assert!(c.is_eligible_to_run(6, false));
        assert!(c.is_eligible_to_run(12, false));
    }

    #[test]
    fn operator_prefs_overlay_overrides_present_fields_only() {
        let mut c = cfg(None, false); // base: no quiet hours, not power-only, headroom 8
        c.min_payout_usd_per_hr = 1.0;
        let prefs = OperatorPrefs {
            power_only: Some(true),
            quiet_hours: Some((22, 6)),
            memory_headroom_gb: Some(2.0),
            min_payout_usd_per_hr: None, // absent → base value (1.0) survives
            ..Default::default()
        };
        c.apply_prefs(&prefs);
        assert!(c.power_only, "present pref overrides");
        assert_eq!(c.quiet_hours, Some((22, 6)));
        assert_eq!(c.memory_headroom_gb, 2.0);
        assert_eq!(
            c.min_payout_usd_per_hr, 1.0,
            "absent pref leaves the base value untouched"
        );
    }

    #[test]
    fn operator_prefs_actually_control_eligibility() {
        // F7's whole point: a toggled pref must CHANGE agent behavior, not be
        // decorative. The base config is always eligible at 02:00; applying a
        // quiet-hours pref makes the agent refuse work in the window, and a power-only
        // pref makes it refuse on battery — proving the overlay reaches the governor.
        let mut c = cfg(None, false);
        assert!(c.is_eligible_to_run(2, false), "base: eligible at 02:00");
        c.apply_prefs(&OperatorPrefs {
            quiet_hours: Some((22, 6)),
            ..Default::default()
        });
        assert!(
            !c.is_eligible_to_run(2, false),
            "after prefs: quiet hours refuse 02:00"
        );
        c.apply_prefs(&OperatorPrefs {
            power_only: Some(true),
            ..Default::default()
        });
        assert!(
            !c.is_eligible_to_run(12, true),
            "after prefs: power-only refuses on battery"
        );
    }

    #[test]
    fn operator_prefs_parse_partial_toml_ignoring_unknown_keys() {
        // The app writes a PARTIAL toml (only the toggled knobs). Absent keys parse as
        // None, present ones as Some, and an unknown future key is ignored (forward
        // compat: an older agent never breaks on a newer app's key).
        let toml = "power_only = true\nmin_payout_usd_per_hr = 3.5\nquiet_hours = [22, 6]\nunknown_future_key = 7\n";
        let prefs: OperatorPrefs = toml::from_str(toml).unwrap();
        assert_eq!(prefs.power_only, Some(true));
        assert_eq!(prefs.min_payout_usd_per_hr, Some(3.5));
        assert_eq!(prefs.quiet_hours, Some((22, 6)));
        assert_eq!(prefs.memory_headroom_gb, None, "absent key → None");
    }

    #[test]
    fn operator_prefs_zero_concurrency_means_derive() {
        // The app writes `max_concurrent_tasks = 0` to mean "derive from cores+RAM".
        // The overlay must map 0 → None (derive), NOT pin concurrency to 1.
        let mut c = cfg(None, false);
        c.max_concurrent_tasks = Some(7); // base had an explicit pin
        c.apply_prefs(&OperatorPrefs {
            max_concurrent_tasks: Some(0),
            ..Default::default()
        });
        assert_eq!(c.max_concurrent_tasks, None, "0 from the app means derive");
        assert_eq!(c.concurrency(64.0), 4, "derive path: big box caps at 4");
        // A positive pref still pins.
        c.apply_prefs(&OperatorPrefs {
            max_concurrent_tasks: Some(3),
            ..Default::default()
        });
        assert_eq!(c.max_concurrent_tasks, Some(3));
    }
}
