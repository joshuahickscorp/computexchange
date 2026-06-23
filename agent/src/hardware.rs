//! Hardware detection and REAL on-box measurement.
//!
//! Everything here is a genuine measurement of the machine the agent runs on:
//! - chip identity via `sysctl machdep.cpu.brand_string`
//! - physical memory via `sysctl hw.memsize` (cross-checked with `sysinfo`)
//! - a real pure-Rust memory-bandwidth microbenchmark (no fabricated numbers)
//!
//! Per-model token/embedding throughput is REAL too: `detect_and_benchmark`
//! runs each available runner on a tiny fixed workload and measures eps/tps,
//! p99 latency, and a sustained-load thermal proxy (see `runners::run_benchmarks`).

use std::process::Command;
use std::time::Instant;

use sysinfo::System;
use uuid::Uuid;

use crate::types::{HardwareClass, WorkerCapability};

/// Bytes touched per streaming pass of the bandwidth benchmark (~256 MiB).
const BENCH_BYTES: usize = 256 * 1024 * 1024;
/// Number of streaming passes timed; we keep the best (peak) GB/s.
const BENCH_PASSES: usize = 5;

/// Run `sysctl -n <key>` and return trimmed stdout, or `None` on any failure.
/// macOS-only path; on other platforms `sysctl` is absent and this returns None.
fn sysctl(key: &str) -> Option<String> {
    let out = Command::new("sysctl").arg("-n").arg(key).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// Map the CPU brand string to a `HardwareClass`.
///
/// Apple strings look like `"Apple M3 Max"`, `"Apple M2 Pro"`, `"Apple M1"`,
/// `"Apple M2 Ultra"`. Anything we don't recognize as Apple Silicon falls back
/// to `Cpu`.
fn classify(brand: &str) -> HardwareClass {
    let b = brand.to_ascii_lowercase();
    let is_apple = b.contains("apple") && b.contains(" m");
    if !is_apple {
        return HardwareClass::Cpu;
    }
    if b.contains("ultra") {
        HardwareClass::AppleSiliconUltra
    } else if b.contains("max") {
        HardwareClass::AppleSiliconMax
    } else if b.contains("pro") {
        HardwareClass::AppleSiliconPro
    } else {
        HardwareClass::AppleSiliconBase
    }
}

/// Query `nvidia-smi` for the first GPU's name and total VRAM (GB). Returns None
/// when nvidia-smi is absent or fails — i.e., not an NVIDIA host. A REAL reading,
/// never fabricated (BLACKHOLE: surface every failure).
fn nvidia_gpu() -> Option<(String, f32)> {
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let first = stdout.lines().next()?.trim(); // e.g. "NVIDIA A100-SXM4-80GB, 81920"
    let (name, mib) = first.split_once(',')?;
    let mib: f64 = mib.trim().parse().ok()?;
    Some((name.trim().to_string(), (mib / 1024.0) as f32)) // MiB → GiB (~GB)
}

/// Map NVIDIA VRAM (GB) to a VRAM-tiered `HardwareClass`. VRAM is the gating
/// resource on NVIDIA (what decides which models fit), as unified memory is on
/// Apple. The top tier is a catch-all for frontier / multi-GPU cards (H200/B200) so
/// a larger card is never mislabeled into a smaller tier.
fn classify_nvidia(vram_gb: f32) -> HardwareClass {
    if vram_gb <= 24.0 {
        HardwareClass::Nvidia24g
    } else if vram_gb <= 48.0 {
        HardwareClass::Nvidia48g
    } else if vram_gb <= 80.0 {
        HardwareClass::Nvidia80g
    } else {
        HardwareClass::Nvidia180g
    }
}

/// Read the OS version string for the capability record.
/// Uses `sw_vers -productVersion` on macOS, falling back to `sysinfo`.
fn os_version() -> String {
    if let Some(v) = Command::new("sw_vers")
        .arg("-productVersion")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
    {
        return format!("macOS {v}");
    }
    System::long_os_version().unwrap_or_else(|| "unknown".to_string())
}

/// REAL streaming-read memory-bandwidth microbenchmark, in pure Rust.
///
/// Allocates a ~256 MiB buffer and runs several timed passes that read and
/// lightly transform every 8-byte word, accumulating into a checksum the
/// compiler cannot elide. We report the *peak* throughput across passes as
/// GB/s (decimal gigabytes, to match how memory bandwidth is usually quoted).
///
/// This measures achievable single-thread streaming read bandwidth, which is a
/// genuine and reproducible figure — not a spec-sheet number.
pub fn measure_memory_bandwidth_gbps() -> f32 {
    let len = BENCH_BYTES / std::mem::size_of::<u64>();
    let mut buf: Vec<u64> = (0..len as u64).collect();
    // Touch once to ensure pages are resident before timing.
    let mut warm: u64 = 0;
    for &x in &buf {
        warm = warm.wrapping_add(x);
    }
    std::hint::black_box(warm);

    let mut best_gbps = 0.0f32;
    for pass in 0..BENCH_PASSES {
        let salt = pass as u64;
        let start = Instant::now();
        let mut acc: u64 = 0;
        for v in buf.iter_mut() {
            // Read + cheap transform + write-back: exercises the bus both ways.
            let x = v.wrapping_mul(6364136223846793005).wrapping_add(salt);
            acc = acc.wrapping_add(x);
            *v = x;
        }
        let elapsed = start.elapsed().as_secs_f64();
        std::hint::black_box(acc);
        if elapsed > 0.0 {
            // Count both the read and the write-back traffic.
            let bytes = (BENCH_BYTES as f64) * 2.0;
            let gbps = (bytes / 1e9) / elapsed;
            best_gbps = best_gbps.max(gbps as f32);
        }
    }
    best_gbps
}

/// A REAL, point-in-time reading of physical memory (bytes → decimal GB).
///
/// `available_gb` is the kernel's estimate of memory obtainable *without*
/// swapping (free + reclaimable), not just free RAM — that is the figure that
/// decides whether the box can take another job without being pushed into swap.
/// Total memory alone is never enough (a 64 GB Mac with 2 GB free must NOT be
/// handed a 4 GB job), which is the whole point of dynamic throttling.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MemorySnapshot {
    pub total_gb: f32,
    pub available_gb: f32,
}

impl MemorySnapshot {
    /// Used fraction of physical memory, in percent (0..=100). Pure.
    pub fn used_pct(&self) -> f32 {
        if self.total_gb <= 0.0 {
            return 0.0;
        }
        (((self.total_gb - self.available_gb) / self.total_gb) * 100.0).clamp(0.0, 100.0)
    }
}

/// Take a REAL memory reading from the OS via `sysinfo` (host statistics on
/// macOS). Self-contained — refreshes only memory on a throwaway `System` — so it
/// is cheap enough to call on every poll/heartbeat and never fabricates a number.
pub fn read_memory_snapshot() -> MemorySnapshot {
    let mut sys = System::new();
    sys.refresh_memory();
    // sysinfo 0.31 reports memory in BYTES (matches `hw.memsize` used above).
    MemorySnapshot {
        total_gb: (sys.total_memory() as f64 / 1e9) as f32,
        available_gb: (sys.available_memory() as f64 / 1e9) as f32,
    }
}

/// A REAL, point-in-time reading of the first NVIDIA GPU's VRAM via `nvidia-smi`,
/// shaped as the same `MemorySnapshot` the host-RAM throttle consumes so the
/// gating logic is identical against VRAM. `total_gb` = total VRAM, `available_gb`
/// = free VRAM (`memory.free`), both in decimal GB.
///
/// On a GPU box the gating resource is VRAM, not host RAM: a 24 GB card with most
/// of its VRAM already resident must NOT be handed a ~20 GB-VRAM job, exactly as a
/// pressured Mac must not take a job that would push it into swap. Returns `None`
/// when nvidia-smi is absent or fails — i.e. no readable NVIDIA GPU — so the caller
/// surfaces the failure honestly (BLACKHOLE) rather than fabricating headroom.
pub fn read_vram_snapshot() -> Option<MemorySnapshot> {
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=memory.free,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let first = stdout.lines().next()?.trim(); // e.g. "11096, 24576" (MiB, nounits)
    let (free_mib, total_mib) = first.split_once(',')?;
    let free_mib: f64 = free_mib.trim().parse().ok()?;
    let total_mib: f64 = total_mib.trim().parse().ok()?;
    if total_mib <= 0.0 {
        return None; // a zero/garbage total is not a usable reading
    }
    // MiB → decimal GB, matching read_memory_snapshot's units so the throttle
    // thresholds (headroom_gb, max_memory_pct) carry over unchanged.
    let mib_to_gb = |mib: f64| (mib * 1024.0 * 1024.0 / 1e9) as f32;
    Some(MemorySnapshot {
        total_gb: mib_to_gb(total_mib),
        available_gb: mib_to_gb(free_mib),
    })
}

/// Detect hardware class and take real measurements, producing the
/// `WorkerCapability` advertised at registration.
///
/// `worker_id` is freshly minted per process start; `supplier_id` comes from
/// config. `min_payout_usd_hr` is the operator reservation price (0.0 from
/// `bench`). The advertised `supported_models`/`supported_jobs` are the CANONICAL
/// catalogue (contract #4), not whatever happened to benchmark — the control
/// plane's hard model filter dispatches against these exact ids.
pub fn detect_and_benchmark(
    supplier_id: Uuid,
    agent_version: &str,
    min_payout_usd_hr: f32,
) -> WorkerCapability {
    let mut sys = System::new();
    sys.refresh_memory();

    // Physical (host) memory: prefer sysctl hw.memsize (exact bytes), fall back to sysinfo.
    let mem_bytes = sysctl("hw.memsize")
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or_else(|| sys.total_memory());
    let host_mem_gb = (mem_bytes as f64 / 1e9) as f32;

    // Class + the gating-memory figure the scheduler filters on. On the CUDA lane the
    // gating resource is VRAM and the class is nvidia_* — NOT host RAM / an Apple
    // class; we detect it via nvidia-smi. Off the CUDA lane it's the Apple/CPU class
    // + host unified memory. device_label() reflects the actually-selected backend.
    let brand = sysctl("machdep.cpu.brand_string").unwrap_or_else(|| "unknown".to_string());
    let (hw_class, memory_gb) = if crate::models::device_label() == "cuda" {
        match nvidia_gpu() {
            Some((name, vram_gb)) => {
                let class = classify_nvidia(vram_gb);
                tracing::info!(gpu = %name, ?class, vram_gb, "detected NVIDIA GPU (CUDA lane)");
                (class, vram_gb)
            }
            None => {
                tracing::warn!("CUDA device active but nvidia-smi unavailable; advertising `cpu`");
                (HardwareClass::Cpu, host_mem_gb)
            }
        }
    } else {
        let class = classify(&brand);
        if class == HardwareClass::Cpu {
            tracing::warn!(cpu = %brand, "could not map to an Apple Silicon class; advertising as `cpu`");
        } else {
            tracing::info!(cpu = %brand, ?class, memory_gb = host_mem_gb, "detected hardware");
        }
        (class, host_mem_gb)
    };

    tracing::info!("running memory-bandwidth microbenchmark (~256MiB streaming)...");
    let memory_bw_gbps = measure_memory_bandwidth_gbps();
    tracing::info!(memory_bw_gbps, "measured streaming memory bandwidth");

    // REAL per-model benchmarks: load each backend and measure it on a tiny
    // workload. Models that fail to load (e.g. HF unreachable) are skipped with
    // a warning inside `run_benchmarks` — never replaced by fabricated numbers.
    tracing::info!(
        device = crate::models::device_label(),
        "running real model benchmarks (embed eps + llama tps, ~20s each)…"
    );
    let benchmarks = crate::runners::run_benchmarks();
    for b in &benchmarks {
        tracing::info!(
            model = %b.model_id, eps = b.eps, tps = b.tps, p99_ms = b.p99_ms,
            thermal_ok = b.thermal_ok, "benchmark"
        );
    }
    // Advertise the CANONICAL catalogue ids (contract #4), regardless of which
    // benchmarks happened to load. These must match prove-local's submitted refs
    // and the control-plane models table, or the hard model filter never
    // dispatches work. `benchmarks` still carries only what we actually measured.
    let supported_models: Vec<String> = [
        "all-minilm-l6-v2",
        "llama-3.2-1b-instruct-q4",
        "whisper-tiny",
        "whisper-base",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();

    // BYO-container general-compute (`custom`, ACCRETION.md §7-8) runs only on a
    // container-capable GPU host. Advertise it only on an NVIDIA worker with a
    // reachable Docker daemon, so the scheduler never routes an opaque container job
    // to a worker that can't sandbox it; run() still errors honestly if the daemon
    // dies between detection and dispatch. (matches! borrows hw_class — no move.)
    let runs_custom = matches!(
        hw_class,
        HardwareClass::Nvidia24g
            | HardwareClass::Nvidia48g
            | HardwareClass::Nvidia80g
            | HardwareClass::Nvidia180g
    ) && container_sandbox_available();

    WorkerCapability {
        worker_id: Uuid::new_v4(),
        supplier_id,
        hw_class,
        memory_gb,
        memory_bw_gbps,
        // Job types this agent will accept dispatch for. `can_run` in runners.rs
        // is the real per-task guard; this is the advertised superset (every
        // runner we registered, including the new workloads).
        supported_jobs: {
            let mut jobs: Vec<String> = [
                "embed",
                "batch_infer",
                "audio_transcribe",
                "batch_classification",
                "json_extraction",
                "rerank",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect();
            if runs_custom {
                jobs.push("custom".to_string());
            }
            jobs
        },
        supported_models,
        benchmarks,
        agent_version: agent_version.to_string(),
        os_version: os_version(),
        min_payout_usd_hr,
    }
}

/// Sample the active GPU's utilization (%) and temperature (°C) via nvidia-smi for the
/// heartbeat. `Some((util_pct, temp_c))` on the NVIDIA lane; `None` if nvidia-smi is
/// absent or fails — the caller then reports an honest 0.0/None, never a fabricated
/// load. Mirrors read_vram_snapshot's nvidia-smi parsing.
pub fn read_gpu_telemetry() -> Option<(f32, Option<f32>)> {
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let first = stdout.lines().next()?.trim(); // e.g. "37, 58" (%, °C)
    let (util, temp) = first.split_once(',')?;
    let util_pct: f32 = util.trim().parse().ok()?;
    // Temperature reads "N/A" on some virtualized GPUs — keep utilization, drop temp.
    let temp_c: Option<f32> = temp.trim().parse().ok();
    Some((util_pct, temp_c))
}

/// True when this host can run the BYO-container `custom` lane: a reachable Docker
/// daemon. Checked once at capability build so the worker never advertises a lane it
/// cannot execute (the NVIDIA Container Toolkit is assumed on a GPU supplier box and
/// surfaces honestly at run() time if absent). `docker version --format {{.Server...}}`
/// prints the SERVER version only when the daemon answers, so a missing binary or a
/// dead daemon both yield false — never a fabricated capability.
fn container_sandbox_available() -> bool {
    std::process::Command::new("docker")
        .args(["version", "--format", "{{.Server.Version}}"])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_apple_variants() {
        assert_eq!(classify("Apple M3 Max"), HardwareClass::AppleSiliconMax);
        assert_eq!(classify("Apple M2 Pro"), HardwareClass::AppleSiliconPro);
        assert_eq!(classify("Apple M2 Ultra"), HardwareClass::AppleSiliconUltra);
        assert_eq!(classify("Apple M1"), HardwareClass::AppleSiliconBase);
        assert_eq!(classify("Apple M4"), HardwareClass::AppleSiliconBase);
        assert_eq!(classify("Intel(R) Core(TM) i9-9980HK"), HardwareClass::Cpu);
    }

    #[test]
    fn classify_nvidia_tiers() {
        assert_eq!(classify_nvidia(16.0), HardwareClass::Nvidia24g); // RTX 4090 etc.
        assert_eq!(classify_nvidia(24.0), HardwareClass::Nvidia24g);
        assert_eq!(classify_nvidia(48.0), HardwareClass::Nvidia48g); // A6000/L40
        assert_eq!(classify_nvidia(80.0), HardwareClass::Nvidia80g); // A100/H100 80G
        assert_eq!(classify_nvidia(141.0), HardwareClass::Nvidia180g); // H200 / frontier
    }

    #[test]
    fn bandwidth_is_positive() {
        // A real run on any machine must report > 0 GB/s.
        assert!(measure_memory_bandwidth_gbps() > 0.0);
    }

    #[test]
    fn used_pct_is_total_minus_available() {
        let s = MemorySnapshot {
            total_gb: 16.0,
            available_gb: 4.0,
        };
        assert!((s.used_pct() - 75.0).abs() < 0.01);
        // Degenerate total → 0%, never NaN/divide-by-zero.
        assert_eq!(
            MemorySnapshot {
                total_gb: 0.0,
                available_gb: 0.0
            }
            .used_pct(),
            0.0
        );
    }

    #[test]
    fn read_memory_snapshot_is_real() {
        // A real reading on any machine: positive total, available ≤ total.
        let s = read_memory_snapshot();
        assert!(s.total_gb > 0.0, "total memory must be positive");
        assert!(
            s.available_gb >= 0.0 && s.available_gb <= s.total_gb,
            "available ({}) must be within [0, total ({})]",
            s.available_gb,
            s.total_gb
        );
    }
}
