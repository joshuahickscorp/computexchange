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
        Ok(cfg)
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
}
