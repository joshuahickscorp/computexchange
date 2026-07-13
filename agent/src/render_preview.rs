//! Experimental, operator-pinned render-speculation preview executor.
//!
//! This is intentionally **not** a production or billing lane.  The local
//! `cx-agent spec-render-preview --input REQUEST.json` command gives the agent
//! one real execution seam for the existing Python `SpeculativeEngine`
//! without changing `TaskCommit`, receipts, pricing, or control-plane economics:
//!
//! * the local CLI never contacts control;
//! * absent `CX_SPEC_RENDER_PREVIEW_DRIVER` => the local command refuses to run;
//! * the driver must be an absolute, executable file whose SHA-256 matches the
//!   operator-provided pin;
//! * the optional polling seam claims only the explicit
//!   `render_speculative_preview` contract whose hashes and render parameters
//!   round-trip through `job_type_spec`; it is not advertised by the production
//!   runtime matrix and therefore cannot be submitted/billed today;
//! * stdin/stdout, wall time, and subprocess-tree lifetime are bounded;
//! * the returned envelope must say preview-only, non-billable, not production
//!   ready, no measured baseline/speedup, and no artifact attestation.
//!
//! The first-party driver is `scripts/spec-lab/cx_agent_render_preview_driver.py`.
//! It imports the existing Python generalized controller and an independently
//! SHA-256-pinned operator backend.  A buyer can supply render units, but cannot
//! choose code, a container image, a command, or a Python module.

use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::pool::ModelPool;
use crate::runners::{JobOutput, JobRunner, RunError};
use crate::types::{JobManifest, JobType, ServiceTier, WorkerCapability};

const DRIVER_ENV: &str = "CX_SPEC_RENDER_PREVIEW_DRIVER";
const DRIVER_SHA256_ENV: &str = "CX_SPEC_RENDER_PREVIEW_DRIVER_SHA256";
const TIMEOUT_ENV: &str = "CX_SPEC_RENDER_PREVIEW_TIMEOUT_SECS";
const BACKEND_SHA256_ENV: &str = "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256";
const CORE_SHA256_ENV: &str = "CX_SPEC_RENDER_PREVIEW_CORE_SHA256";
const ADAPTER_SHA256_ENV: &str = "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256";
const BLENDER_SHA256_ENV: &str = "CX_SPEC_RENDER_CYCLES_BLENDER_SHA256";

const DEFAULT_TIMEOUT_SECS: u64 = 120;
const MAX_TIMEOUT_SECS: u64 = 3_600;
const MAX_INPUT_BYTES: usize = 16 << 20;
const MAX_OUTPUT_BYTES: usize = 32 << 20;
const MAX_STDERR_BYTES: usize = 64 << 10;
const MAX_UNITS: usize = 4_096;
const MIN_DIMENSION: u32 = 16;
const MAX_DIMENSION: u32 = 4_096;
const MAX_PIXELS: u64 = 4_194_304;
const MAX_FRAME: u32 = 1_000_000;
const MAX_LOW_SAMPLES: u32 = 64;
const MAX_HIGH_SAMPLES: u32 = 4_096;
const RESULT_KIND: &str = "cx_spec_render_preview_result";
const RECEIPT_TRUST: &str = "local_experiment_unattested";
const BRANCH_ID: &str = "agent-render-preview-v1";

#[derive(Debug, Clone)]
struct LiveCyclesPins {
    backend_sha256: String,
    controller_core_sha256: String,
    controller_adapter_sha256: String,
    blender_sha256: String,
}

/// Optional runner created only from an explicit, content-addressed operator
/// opt-in.  There is deliberately no config default and no automatic discovery.
#[derive(Debug, Clone)]
pub struct SpecRenderPreviewRunner {
    driver: PathBuf,
    expected_sha256: String,
    timeout: Duration,
    live_cycles_pins: Option<LiveCyclesPins>,
}

impl SpecRenderPreviewRunner {
    /// Resolve the opt-in once at preview-command startup.  A partially configured opt-in
    /// is a hard error: silently falling back after an operator tried to enable an
    /// experimental code path would make the active execution surface ambiguous.
    pub fn from_env() -> Result<Option<Self>, String> {
        let driver = std::env::var_os(DRIVER_ENV);
        let digest = std::env::var(DRIVER_SHA256_ENV).ok();
        let timeout = std::env::var(TIMEOUT_ENV).ok();

        let Some(driver) = driver else {
            if digest.is_some() || timeout.is_some() {
                return Err(format!(
                    "{DRIVER_ENV} is required when {DRIVER_SHA256_ENV} or {TIMEOUT_ENV} is set"
                ));
            }
            return Ok(None);
        };
        let digest = digest
            .ok_or_else(|| format!("{DRIVER_SHA256_ENV} is required when {DRIVER_ENV} is set"))?;
        let timeout_secs = match timeout {
            Some(raw) => raw.parse::<u64>().map_err(|_| {
                format!("{TIMEOUT_ENV} must be an integer in [1,{MAX_TIMEOUT_SECS}], got {raw:?}")
            })?,
            None => DEFAULT_TIMEOUT_SECS,
        };
        Self::new(PathBuf::from(driver), &digest, timeout_secs).map(Some)
    }

    fn new(driver: PathBuf, expected_sha256: &str, timeout_secs: u64) -> Result<Self, String> {
        if !driver.is_absolute() {
            return Err(format!("{DRIVER_ENV} must be an absolute path"));
        }
        if !(1..=MAX_TIMEOUT_SECS).contains(&timeout_secs) {
            return Err(format!(
                "{TIMEOUT_ENV} must be in [1,{MAX_TIMEOUT_SECS}], got {timeout_secs}"
            ));
        }
        validate_sha256(expected_sha256, DRIVER_SHA256_ENV)?;

        let canonical = driver
            .canonicalize()
            .map_err(|e| format!("resolving {DRIVER_ENV} {}: {e}", driver.display()))?;
        let metadata = canonical
            .metadata()
            .map_err(|e| format!("stat {}: {e}", canonical.display()))?;
        if !metadata.is_file() {
            return Err(format!("{DRIVER_ENV} must name a regular file"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o111 == 0 {
                return Err(format!(
                    "{DRIVER_ENV} {} is not executable",
                    canonical.display()
                ));
            }
        }
        let actual =
            sha256_file(&canonical).map_err(|e| format!("hashing {}: {e}", canonical.display()))?;
        if actual != expected_sha256 {
            return Err(format!(
                "{DRIVER_SHA256_ENV} mismatch for {}: expected {expected_sha256}, got {actual}",
                canonical.display()
            ));
        }
        Ok(Self {
            driver: canonical,
            expected_sha256: expected_sha256.to_string(),
            timeout: Duration::from_secs(timeout_secs),
            live_cycles_pins: None,
        })
    }

    /// Build the optional polling runner.  The local CLI supports any pinned
    /// protocol-v1 backend, but live dispatch is deliberately narrower: it
    /// requires all Cycles/controller hashes up front and binds each dispatch to
    /// those exact locally configured bytes.  Any partial opt-in is a startup
    /// error rather than a silent fallback to an ambiguous runner set.
    pub(crate) fn for_live_cycles_from_env() -> Result<Option<Self>, String> {
        let live_names = [
            BACKEND_SHA256_ENV,
            CORE_SHA256_ENV,
            ADAPTER_SHA256_ENV,
            BLENDER_SHA256_ENV,
        ];
        let any_live = live_names
            .iter()
            .any(|name| std::env::var_os(name).is_some());
        let Some(mut runner) = Self::from_env()? else {
            if any_live {
                return Err(format!(
                    "{DRIVER_ENV} and {DRIVER_SHA256_ENV} are required when a live Cycles preview pin is set"
                ));
            }
            return Ok(None);
        };

        let required = |name: &'static str| -> Result<String, String> {
            let value = std::env::var(name)
                .map_err(|_| format!("{name} is required for live Cycles preview dispatch"))?;
            validate_sha256(&value, name)?;
            Ok(value)
        };
        runner.live_cycles_pins = Some(LiveCyclesPins {
            backend_sha256: required(BACKEND_SHA256_ENV)?,
            controller_core_sha256: required(CORE_SHA256_ENV)?,
            controller_adapter_sha256: required(ADAPTER_SHA256_ENV)?,
            blender_sha256: required(BLENDER_SHA256_ENV)?,
        });
        Ok(Some(runner))
    }

    pub(crate) async fn execute_preview(&self, input: &[u8]) -> Result<Vec<u8>, RunError> {
        if input.is_empty() {
            return Err(RunError::BadInput {
                job: "spec_render_preview",
                msg: "preview request is empty".into(),
            });
        }
        if input.len() > MAX_INPUT_BYTES {
            return Err(RunError::BadInput {
                job: "spec_render_preview",
                msg: format!(
                    "preview request is {} bytes; limit is {MAX_INPUT_BYTES}",
                    input.len()
                ),
            });
        }
        let driver = self.driver.clone();
        let expected_sha256 = self.expected_sha256.clone();
        let timeout = self.timeout;
        let input = input.to_vec();
        tokio::task::spawn_blocking(move || {
            // Revalidate at execution time, not only at startup: replacing the
            // file after `from_env()` must not bypass the operator's content pin.
            // There remains a narrow local-filesystem hash->exec race between
            // this check and Command::spawn.  Portable fd-backed execution is not
            // reliable for shebang scripts across macOS/Linux; exploiting that
            // residual race requires local write access to an operator-trusted
            // executable path, outside the buyer-controlled request surface.
            revalidate_driver_for_spawn(&driver, &expected_sha256)?;
            run_driver(&driver, &input, timeout)
        })
        .await
        .map_err(|e| RunError::Inference {
            backend: "spec_render_preview",
            msg: format!("preview driver task join failed: {e}"),
        })?
    }

    /// Local-CLI entry point that enforces the request cap while reading, before
    /// an attacker-controlled/surprising file size can be materialized in memory.
    pub(crate) async fn execute_preview_file(&self, path: &Path) -> Result<Vec<u8>, RunError> {
        let path = path.to_path_buf();
        let input = tokio::task::spawn_blocking(move || read_file_capped(&path, MAX_INPUT_BYTES))
            .await
            .map_err(|e| RunError::Inference {
                backend: "spec_render_preview",
                msg: format!("preview request reader task join failed: {e}"),
            })??;
        self.execute_preview(&input).await
    }
}

#[async_trait]
impl JobRunner for SpecRenderPreviewRunner {
    async fn can_run(&self, manifest: &JobManifest, cap: &WorkerCapability) -> bool {
        self.live_contract(manifest).is_ok() && cap.memory_gb >= manifest.constraints.min_memory_gb
    }

    async fn run(
        &self,
        manifest: &JobManifest,
        input: &[u8],
        _pool: &ModelPool,
    ) -> Result<JobOutput, RunError> {
        let contract = self.live_contract(manifest).map_err(preview_bad_input)?;
        validate_cycles_request(input, &contract)?;
        let started = Instant::now();
        let result = self.execute_preview(input).await?;
        Ok(JobOutput {
            // The validated envelope explicitly remains preview-only,
            // non-billable, synthetic, and unattested. The production runtime
            // matrix does not advertise this job type; this trait result exists
            // for the typed polling scaffold and cannot currently enter the
            // standard buyer/economic path.
            result,
            binary: false,
            duration_ms: started.elapsed().as_millis() as u64,
            tokens_used: 0,
        })
    }

    fn backend_name(&self) -> &'static str {
        "spec_render_preview"
    }
}

#[derive(Debug, Clone, Copy)]
struct LiveRenderContract<'a> {
    scene_path: &'a str,
    scene_sha256: &'a str,
    width: u32,
    height: u32,
    frame: u32,
    draft_samples: u32,
    verify_samples: u32,
    repair_samples: u32,
}

impl SpecRenderPreviewRunner {
    fn live_contract<'a>(
        &self,
        manifest: &'a JobManifest,
    ) -> Result<LiveRenderContract<'a>, String> {
        let Some(pins) = &self.live_cycles_pins else {
            return Err("live Cycles preview pins are not configured".into());
        };
        let JobType::RenderSpeculativePreview {
            schema_version,
            preview_only,
            billing_eligible,
            production_ready,
            receipt_trust,
            driver_sha256,
            backend_sha256,
            controller_core_sha256,
            controller_adapter_sha256,
            blender_sha256,
            scene_path,
            scene_sha256,
            width,
            height,
            frame,
            draft_samples,
            verify_samples,
            repair_samples,
        } = &manifest.job_type
        else {
            return Err("job type is not render_speculative_preview".into());
        };

        for (value, name) in [
            (driver_sha256.as_str(), "driver_sha256"),
            (backend_sha256.as_str(), "backend_sha256"),
            (controller_core_sha256.as_str(), "controller_core_sha256"),
            (
                controller_adapter_sha256.as_str(),
                "controller_adapter_sha256",
            ),
            (blender_sha256.as_str(), "blender_sha256"),
            (scene_sha256.as_str(), "scene_sha256"),
        ] {
            validate_sha256(value, name)?;
        }
        if *schema_version != 1
            || !*preview_only
            || *billing_eligible
            || *production_ready
            || receipt_trust != RECEIPT_TRUST
        {
            return Err(
                "preview job tried to escape preview_only/nonbillable/unattested contract".into(),
            );
        }
        if driver_sha256 != &self.expected_sha256
            || backend_sha256 != &pins.backend_sha256
            || controller_core_sha256 != &pins.controller_core_sha256
            || controller_adapter_sha256 != &pins.controller_adapter_sha256
            || blender_sha256 != &pins.blender_sha256
        {
            return Err("preview job pins do not match the operator-configured live runner".into());
        }
        validate_scene_path(scene_path)?;
        validate_render_bounds(
            *width,
            *height,
            *frame,
            *draft_samples,
            *verify_samples,
            *repair_samples,
        )?;
        if manifest.tier != ServiceTier::Batch
            || manifest.verification.redundancy_frac != 0.0
            || manifest.verification.honeypot_frac != 0.0
            || manifest.verification.payout_hold_secs != 0
        {
            return Err(
                "preview jobs require batch tier and an explicit zero-verification policy".into(),
            );
        }
        if !(manifest.params.is_null()
            || manifest
                .params
                .as_object()
                .is_some_and(serde_json::Map::is_empty))
        {
            return Err("preview execution parameters must live only in job_type_spec".into());
        }
        Ok(LiveRenderContract {
            scene_path,
            scene_sha256,
            width: *width,
            height: *height,
            frame: *frame,
            draft_samples: *draft_samples,
            verify_samples: *verify_samples,
            repair_samples: *repair_samples,
        })
    }
}

fn preview_bad_input(msg: String) -> RunError {
    RunError::BadInput {
        job: "spec_render_preview",
        msg,
    }
}

fn validate_scene_path(value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 1_024 || !value.ends_with(".blend") {
        return Err(
            "scene_path must be a nonempty relative lowercase .blend path <=1024 bytes".into(),
        );
    }
    if value.starts_with('/')
        || value.contains('\\')
        || value.contains('\0')
        || value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err("scene_path must use strict relative POSIX components".into());
    }
    Ok(())
}

fn validate_render_bounds(
    width: u32,
    height: u32,
    frame: u32,
    draft_samples: u32,
    verify_samples: u32,
    repair_samples: u32,
) -> Result<(), String> {
    if !(MIN_DIMENSION..=MAX_DIMENSION).contains(&width)
        || !(MIN_DIMENSION..=MAX_DIMENSION).contains(&height)
        || u64::from(width) * u64::from(height) > MAX_PIXELS
    {
        return Err(format!(
            "render dimensions must each be in [{MIN_DIMENSION},{MAX_DIMENSION}] and total at most {MAX_PIXELS} pixels"
        ));
    }
    if frame > MAX_FRAME {
        return Err(format!("frame must be <= {MAX_FRAME}"));
    }
    if !(1..=MAX_LOW_SAMPLES).contains(&draft_samples)
        || !(1..=MAX_LOW_SAMPLES).contains(&verify_samples)
        || !(2..=MAX_HIGH_SAMPLES).contains(&repair_samples)
        || repair_samples <= draft_samples.max(verify_samples)
    {
        return Err(format!(
            "draft/verify samples must be 1..={MAX_LOW_SAMPLES}; repair must be 2..={MAX_HIGH_SAMPLES} and greater than both"
        ));
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CyclesPreviewRequest {
    schema_version: u32,
    kind: String,
    units: Vec<CyclesPreviewUnit>,
    #[serde(default)]
    meta: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CyclesPreviewUnit {
    unit_id: String,
    payload: CyclesPreviewPayload,
    meta: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CyclesPreviewPayload {
    scene_path: String,
    scene_sha256: String,
    width: u32,
    height: u32,
    frame: u32,
    draft_samples: u32,
    verify_samples: u32,
    repair_samples: u32,
}

fn validate_cycles_request(
    input: &[u8],
    contract: &LiveRenderContract<'_>,
) -> Result<(), RunError> {
    let request: CyclesPreviewRequest = serde_json::from_slice(input).map_err(|e| {
        preview_bad_input(format!(
            "live Cycles request is not the closed protocol-v1 shape: {e}"
        ))
    })?;
    if request.schema_version != 1
        || request.kind != "cx_spec_render_preview_request"
        || request.units.len() != 1
        || !request.meta.is_object()
    {
        return Err(preview_bad_input(
            "live Cycles request must be protocol v1 with exactly one unit and object meta".into(),
        ));
    }
    let unit = &request.units[0];
    if unit.unit_id.is_empty() || unit.unit_id.len() > 256 || !unit.meta.is_object() {
        return Err(preview_bad_input(
            "live Cycles unit_id/meta are malformed or oversized".into(),
        ));
    }
    let p = &unit.payload;
    validate_scene_path(&p.scene_path).map_err(preview_bad_input)?;
    validate_sha256(&p.scene_sha256, "scene_sha256").map_err(preview_bad_input)?;
    validate_render_bounds(
        p.width,
        p.height,
        p.frame,
        p.draft_samples,
        p.verify_samples,
        p.repair_samples,
    )
    .map_err(preview_bad_input)?;
    if p.scene_path != contract.scene_path
        || p.scene_sha256 != contract.scene_sha256
        || p.width != contract.width
        || p.height != contract.height
        || p.frame != contract.frame
        || p.draft_samples != contract.draft_samples
        || p.verify_samples != contract.verify_samples
        || p.repair_samples != contract.repair_samples
    {
        return Err(preview_bad_input(
            "live Cycles request does not match its persisted job_type_spec binding".into(),
        ));
    }
    Ok(())
}

fn validate_sha256(value: &str, env_name: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(format!(
            "{env_name} must be exactly 64 lowercase hexadecimal characters"
        ));
    }
    Ok(())
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut chunk = [0u8; 64 << 10];
    loop {
        let n = file.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        hasher.update(&chunk[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn read_file_capped(path: &Path, cap: usize) -> Result<Vec<u8>, RunError> {
    let file = File::open(path).map_err(|e| RunError::BadInput {
        job: "spec_render_preview",
        msg: format!("opening preview request {}: {e}", path.display()),
    })?;
    let mut bytes = Vec::new();
    file.take((cap as u64).saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|e| RunError::BadInput {
            job: "spec_render_preview",
            msg: format!("reading preview request {}: {e}", path.display()),
        })?;
    if bytes.len() > cap {
        return Err(RunError::BadInput {
            job: "spec_render_preview",
            msg: format!("preview request exceeds the {cap}-byte limit"),
        });
    }
    Ok(bytes)
}

/// Repeat every identity property immediately before spawn.  Startup validation
/// gives early feedback; this check is the security boundary against post-start
/// replacement of the pinned driver.
fn revalidate_driver_for_spawn(path: &Path, expected_sha256: &str) -> Result<(), RunError> {
    let fail = |msg: String| RunError::Inference {
        backend: "spec_render_preview",
        msg,
    };
    let metadata = path.metadata().map_err(|e| {
        fail(format!(
            "stat pinned preview driver {}: {e}",
            path.display()
        ))
    })?;
    if !metadata.is_file() {
        return Err(fail(format!(
            "pinned preview driver {} is no longer a regular file",
            path.display()
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return Err(fail(format!(
                "pinned preview driver {} is no longer executable",
                path.display()
            )));
        }
    }
    let actual = sha256_file(path).map_err(|e| {
        fail(format!(
            "rehashing pinned preview driver {}: {e}",
            path.display()
        ))
    })?;
    if actual != expected_sha256 {
        return Err(fail(format!(
            "pinned preview driver {} changed after startup: expected {expected_sha256}, got {actual}",
            path.display()
        )));
    }
    Ok(())
}

fn read_capped<R: Read>(
    mut reader: R,
    cap: usize,
    exceeded: Arc<AtomicBool>,
) -> std::io::Result<Vec<u8>> {
    let mut kept = Vec::new();
    let mut chunk = [0u8; 16 << 10];
    loop {
        let n = reader.read(&mut chunk)?;
        if n == 0 {
            return Ok(kept);
        }
        let room = cap.saturating_sub(kept.len());
        kept.extend_from_slice(&chunk[..n.min(room)]);
        if n > room {
            exceeded.store(true, Ordering::Release);
        }
        // Continue draining even after the cap so the child cannot deadlock on a
        // full pipe while the supervisor notices the flag and kills its group.
    }
}

fn kill_child_tree(child: &mut std::process::Child) {
    #[cfg(unix)]
    unsafe {
        // The child is placed in its own process group below.  Kill the group so
        // a Blender/renderer grandchild cannot outlive a timed-out driver.
        let _ = libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.kill();
}

fn stderr_tail(stderr: &[u8]) -> String {
    const TAIL: usize = 4 << 10;
    let start = stderr.len().saturating_sub(TAIL);
    String::from_utf8_lossy(&stderr[start..]).trim().to_string()
}

fn run_driver(driver: &Path, input: &[u8], timeout: Duration) -> Result<Vec<u8>, RunError> {
    let mut command = Command::new(driver);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| RunError::NotImplemented {
            job_type: "custom",
            detail: format!("launching pinned spec-render preview driver: {e}"),
        })?;

    let mut stdin = child.stdin.take().expect("piped child stdin");
    let input = input.to_vec();
    let writer = std::thread::spawn(move || {
        let _ = stdin.write_all(&input);
        // Drop closes stdin and gives the driver a deterministic EOF.
    });

    let stdout = child.stdout.take().expect("piped child stdout");
    let stderr = child.stderr.take().expect("piped child stderr");
    let stdout_exceeded = Arc::new(AtomicBool::new(false));
    let stderr_exceeded = Arc::new(AtomicBool::new(false));
    let stdout_flag = stdout_exceeded.clone();
    let stderr_flag = stderr_exceeded.clone();
    let stdout_reader =
        std::thread::spawn(move || read_capped(stdout, MAX_OUTPUT_BYTES, stdout_flag));
    let stderr_reader =
        std::thread::spawn(move || read_capped(stderr, MAX_STDERR_BYTES, stderr_flag));

    let started = Instant::now();
    let status = loop {
        if stdout_exceeded.load(Ordering::Acquire) || stderr_exceeded.load(Ordering::Acquire) {
            kill_child_tree(&mut child);
            let _ = child.wait();
            let _ = writer.join();
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            return Err(RunError::Inference {
                backend: "spec_render_preview",
                msg: format!(
                    "preview driver exceeded its bounded output (stdout {MAX_OUTPUT_BYTES} bytes, stderr {MAX_STDERR_BYTES} bytes)"
                ),
            });
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < timeout => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                kill_child_tree(&mut child);
                let _ = child.wait();
                let _ = writer.join();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(RunError::Inference {
                    backend: "spec_render_preview",
                    msg: format!(
                        "preview driver exceeded its {}s wall-clock limit and was killed",
                        timeout.as_secs()
                    ),
                });
            }
            Err(e) => {
                kill_child_tree(&mut child);
                let _ = child.wait();
                let _ = writer.join();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(RunError::Inference {
                    backend: "spec_render_preview",
                    msg: format!("waiting for preview driver: {e}"),
                });
            }
        }
    };

    let _ = writer.join();
    let stdout = stdout_reader
        .join()
        .map_err(|_| RunError::Inference {
            backend: "spec_render_preview",
            msg: "preview stdout reader panicked".into(),
        })?
        .map_err(|e| RunError::Inference {
            backend: "spec_render_preview",
            msg: format!("reading preview stdout: {e}"),
        })?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| RunError::Inference {
            backend: "spec_render_preview",
            msg: "preview stderr reader panicked".into(),
        })?
        .map_err(|e| RunError::Inference {
            backend: "spec_render_preview",
            msg: format!("reading preview stderr: {e}"),
        })?;

    // The child may exit between `try_wait` and a reader setting its flag.  Check
    // again after both pipes reach EOF so a fast process cannot outrun the cap.
    if stdout_exceeded.load(Ordering::Acquire) || stderr_exceeded.load(Ordering::Acquire) {
        return Err(RunError::Inference {
            backend: "spec_render_preview",
            msg: format!(
                "preview driver exceeded its bounded output (stdout {MAX_OUTPUT_BYTES} bytes, stderr {MAX_STDERR_BYTES} bytes)"
            ),
        });
    }

    if !status.success() {
        return Err(RunError::Inference {
            backend: "spec_render_preview",
            msg: format!(
                "preview driver exited with {:?}: {}",
                status.code(),
                stderr_tail(&stderr)
            ),
        });
    }
    validate_envelope(&stdout)
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PreviewEnvelope {
    schema_version: u32,
    kind: String,
    preview_only: bool,
    billing_eligible: bool,
    production_ready: bool,
    receipt_trust: String,
    outputs: Vec<Value>,
    receipt: PreviewReceipt,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PreviewReceipt {
    schema_version: u32,
    draft_cost_s: f64,
    verify_cost_s: f64,
    accepted_fraction: f64,
    repair_cost_s: f64,
    overhead_cost_s: f64,
    total_product_time_s: f64,
    quality_tier: String,
    speedup_vs_baseline: Option<f64>,
    exact: bool,
    modality: String,
    branch_id: String,
    units: usize,
    accepted_units: usize,
    repaired_units: usize,
    repaired_fraction: f64,
    baseline_total_time_s: f64,
    baseline_source: String,
    quality_gate: bool,
    artifact_verified: bool,
    evidence: String,
    global_ssim: Option<f64>,
    worst_tile_ssim: Option<f64>,
    p5_ssim: Option<f64>,
    claim_scope: String,
    details: Value,
}

fn validate_envelope(bytes: &[u8]) -> Result<Vec<u8>, RunError> {
    let envelope: PreviewEnvelope =
        serde_json::from_slice(bytes).map_err(|e| RunError::Inference {
            backend: "spec_render_preview",
            msg: format!("preview driver returned an invalid/unknown-field envelope: {e}"),
        })?;
    let receipt = &envelope.receipt;
    let fail = |msg: String| RunError::Inference {
        backend: "spec_render_preview",
        msg,
    };

    if envelope.schema_version != 1
        || envelope.kind != RESULT_KIND
        || !envelope.preview_only
        || envelope.billing_eligible
        || envelope.production_ready
        || envelope.receipt_trust != RECEIPT_TRUST
    {
        return Err(fail(
            "preview envelope tried to escape the non-billable experimental contract".into(),
        ));
    }
    if receipt.schema_version != 1
        || receipt.modality != "render"
        || receipt.branch_id != BRANCH_ID
        || receipt.exact
        || receipt.artifact_verified
        || receipt.evidence != "synthetic"
        || receipt.baseline_source != "absent"
        || receipt.baseline_total_time_s != 0.0
        || receipt.speedup_vs_baseline.is_some()
        || receipt.global_ssim.is_some()
        || receipt.worst_tile_ssim.is_some()
        || receipt.p5_ssim.is_some()
    {
        return Err(fail(
            "preview receipt claimed attestation, exactness, measured evidence, quality scores, or a speedup".into(),
        ));
    }
    if !(1..=MAX_UNITS).contains(&receipt.units)
        || envelope.outputs.len() != receipt.units
        || receipt.accepted_units > receipt.units
        || receipt.repaired_units > receipt.units - receipt.accepted_units
    {
        return Err(fail("preview receipt unit counts are inconsistent".into()));
    }
    let finite_nonnegative = |v: f64| v.is_finite() && v >= 0.0;
    if ![
        receipt.draft_cost_s,
        receipt.verify_cost_s,
        receipt.repair_cost_s,
        receipt.overhead_cost_s,
        receipt.total_product_time_s,
        receipt.accepted_fraction,
        receipt.repaired_fraction,
    ]
    .into_iter()
    .all(finite_nonnegative)
        || receipt.total_product_time_s <= 0.0
        || receipt.accepted_fraction > 1.0
        || receipt.repaired_fraction > 1.0
    {
        return Err(fail(
            "preview receipt contains invalid scalar values".into(),
        ));
    }
    let phase_sum = receipt.draft_cost_s
        + receipt.verify_cost_s
        + receipt.repair_cost_s
        + receipt.overhead_cost_s;
    if (phase_sum - receipt.total_product_time_s).abs() > 5e-5 {
        return Err(fail(
            "preview receipt total_product_time_s contradicts its charged phase sum".into(),
        ));
    }
    let expected_accept = receipt.accepted_units as f64 / receipt.units as f64;
    let expected_repaired = receipt.repaired_units as f64 / receipt.units as f64;
    if (receipt.accepted_fraction - expected_accept).abs() > 1e-6
        || (receipt.repaired_fraction - expected_repaired).abs() > 1e-6
    {
        return Err(fail(
            "preview receipt fractions contradict unit counts".into(),
        ));
    }
    match (receipt.quality_gate, receipt.quality_tier.as_str()) {
        (true, "preview") | (false, "fail") => {}
        _ => {
            return Err(fail(
                "preview receipt quality gate must map only to preview|fail".into(),
            ))
        }
    }
    if !receipt.details.is_object() || receipt.claim_scope.len() > 4_096 {
        return Err(fail(
            "preview receipt metadata is malformed or oversized".into(),
        ));
    }

    // Re-serialize the validated closed struct.  The uploaded result is therefore
    // exactly the schema we checked, never an unchecked byte stream with a valid
    // JSON prefix followed by another document.
    serde_json::to_vec(&envelope).map_err(|e| fail(format!("serializing preview envelope: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{
        JobConstraints, ModelKind, ModelRef, OutputRef, ServiceTier, VerificationPolicy,
    };
    use uuid::Uuid;

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    struct RestoreEnv(Vec<(&'static str, Option<std::ffi::OsString>)>);

    impl RestoreEnv {
        fn set(values: &[(&'static str, String)]) -> Self {
            let old = values
                .iter()
                .map(|(name, value)| {
                    let previous = std::env::var_os(name);
                    std::env::set_var(name, value);
                    (*name, previous)
                })
                .collect();
            Self(old)
        }
    }

    impl Drop for RestoreEnv {
        fn drop(&mut self) {
            for (name, previous) in self.0.drain(..) {
                match previous {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }

    const DRIVER_PIN: &str = "1111111111111111111111111111111111111111111111111111111111111111";
    const BACKEND_PIN: &str = "2222222222222222222222222222222222222222222222222222222222222222";
    const CORE_PIN: &str = "3333333333333333333333333333333333333333333333333333333333333333";
    const ADAPTER_PIN: &str = "4444444444444444444444444444444444444444444444444444444444444444";
    const BLENDER_PIN: &str = "5555555555555555555555555555555555555555555555555555555555555555";
    const SCENE_PIN: &str = "6666666666666666666666666666666666666666666666666666666666666666";

    fn live_job_type() -> JobType {
        JobType::RenderSpeculativePreview {
            schema_version: 1,
            preview_only: true,
            billing_eligible: false,
            production_ready: false,
            receipt_trust: RECEIPT_TRUST.into(),
            driver_sha256: DRIVER_PIN.into(),
            backend_sha256: BACKEND_PIN.into(),
            controller_core_sha256: CORE_PIN.into(),
            controller_adapter_sha256: ADAPTER_PIN.into(),
            blender_sha256: BLENDER_PIN.into(),
            scene_path: "scenes/pinned.blend".into(),
            scene_sha256: SCENE_PIN.into(),
            width: 640,
            height: 360,
            frame: 7,
            draft_samples: 8,
            verify_samples: 8,
            repair_samples: 64,
        }
    }

    fn live_runner() -> SpecRenderPreviewRunner {
        SpecRenderPreviewRunner {
            driver: PathBuf::from("/operator/pinned-driver"),
            expected_sha256: DRIVER_PIN.into(),
            timeout: Duration::from_secs(60),
            live_cycles_pins: Some(LiveCyclesPins {
                backend_sha256: BACKEND_PIN.into(),
                controller_core_sha256: CORE_PIN.into(),
                controller_adapter_sha256: ADAPTER_PIN.into(),
                blender_sha256: BLENDER_PIN.into(),
            }),
        }
    }

    fn manifest(job_type: JobType) -> JobManifest {
        JobManifest {
            id: Uuid::nil(),
            job_type,
            model: ModelRef {
                kind: ModelKind::Hf,
                model_ref: "render-preview".into(),
            },
            inputs: Vec::new(),
            output: OutputRef { url: String::new() },
            params: Value::Null,
            constraints: JobConstraints {
                min_memory_gb: 0.0,
                hw_classes: None,
                max_duration_secs: 60,
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

    fn cycles_request() -> Value {
        serde_json::json!({
            "schema_version": 1,
            "kind": "cx_spec_render_preview_request",
            "units": [{
                "unit_id": "frame-7",
                "payload": {
                    "scene_path": "scenes/pinned.blend",
                    "scene_sha256": SCENE_PIN,
                    "width": 640,
                    "height": 360,
                    "frame": 7,
                    "draft_samples": 8,
                    "verify_samples": 8,
                    "repair_samples": 64
                },
                "meta": {}
            }],
            "meta": {}
        })
    }

    fn valid_envelope() -> Value {
        serde_json::json!({
            "schema_version": 1,
            "kind": RESULT_KIND,
            "preview_only": true,
            "billing_eligible": false,
            "production_ready": false,
            "receipt_trust": RECEIPT_TRUST,
            "outputs": [{"artifact_b64": "AA=="}],
            "receipt": {
                "schema_version": 1,
                "draft_cost_s": 0.1,
                "verify_cost_s": 0.2,
                "accepted_fraction": 1.0,
                "repair_cost_s": 0.0,
                "overhead_cost_s": 0.01,
                "total_product_time_s": 0.31,
                "quality_tier": "preview",
                "speedup_vs_baseline": null,
                "exact": false,
                "modality": "render",
                "branch_id": BRANCH_ID,
                "units": 1,
                "accepted_units": 1,
                "repaired_units": 0,
                "repaired_fraction": 0.0,
                "baseline_total_time_s": 0.0,
                "baseline_source": "absent",
                "quality_gate": true,
                "artifact_verified": false,
                "evidence": "synthetic",
                "global_ssim": null,
                "worst_tile_ssim": null,
                "p5_ssim": null,
                "claim_scope": "SYNTHETIC; no baseline or speedup is claimed",
                "details": {"execution_path": "agent-preview"}
            }
        })
    }

    #[test]
    fn live_manifest_gate_binds_honesty_flags_pins_and_request_params() {
        let runner = live_runner();
        let good = manifest(live_job_type());
        let contract = runner.live_contract(&good).expect("closed live contract");
        let request = serde_json::to_vec(&cycles_request()).unwrap();
        validate_cycles_request(&request, &contract).expect("request matches job_type_spec");

        let mut billed = live_job_type();
        if let JobType::RenderSpeculativePreview {
            billing_eligible, ..
        } = &mut billed
        {
            *billing_eligible = true;
        }
        assert!(runner.live_contract(&manifest(billed)).is_err());

        let mut wrong_pin = live_job_type();
        if let JobType::RenderSpeculativePreview { backend_sha256, .. } = &mut wrong_pin {
            *backend_sha256 = "0".repeat(64);
        }
        assert!(runner.live_contract(&manifest(wrong_pin)).is_err());

        let mut wrong_request = cycles_request();
        wrong_request["units"][0]["payload"]["width"] = serde_json::json!(641);
        assert!(
            validate_cycles_request(&serde_json::to_vec(&wrong_request).unwrap(), &contract)
                .is_err()
        );
    }

    #[test]
    fn envelope_accepts_only_unattested_preview_receipts() {
        let good = serde_json::to_vec(&valid_envelope()).unwrap();
        let normalized = validate_envelope(&good).expect("safe preview envelope");
        let parsed: Value = serde_json::from_slice(&normalized).unwrap();
        assert_eq!(parsed["receipt"]["quality_tier"], "preview");

        for (field, value) in [
            ("artifact_verified", Value::Bool(true)),
            ("exact", Value::Bool(true)),
            ("evidence", Value::String("measured".into())),
            ("baseline_source", Value::String("measured".into())),
        ] {
            let mut bad = valid_envelope();
            bad["receipt"][field] = value;
            assert!(validate_envelope(&serde_json::to_vec(&bad).unwrap()).is_err());
        }
        let mut billed = valid_envelope();
        billed["billing_eligible"] = Value::Bool(true);
        assert!(validate_envelope(&serde_json::to_vec(&billed).unwrap()).is_err());
        let mut delivery = valid_envelope();
        delivery["receipt"]["quality_tier"] = Value::String("delivery".into());
        assert!(validate_envelope(&serde_json::to_vec(&delivery).unwrap()).is_err());
    }

    #[test]
    fn driver_pin_is_content_addressed_and_timeout_is_bounded() {
        let path =
            std::env::temp_dir().join(format!("cx-render-preview-driver-{}", Uuid::new_v4()));
        std::fs::write(&path, b"#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let digest = sha256_file(&path).unwrap();
        assert!(SpecRenderPreviewRunner::new(path.clone(), &digest, 1).is_ok());
        assert!(SpecRenderPreviewRunner::new(path.clone(), &"0".repeat(64), 1).is_err());
        assert!(SpecRenderPreviewRunner::new(path.clone(), &digest, 0).is_err());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn local_request_file_is_bounded_while_reading() {
        let path =
            std::env::temp_dir().join(format!("cx-render-preview-request-cap-{}", Uuid::new_v4()));
        std::fs::write(&path, b"12345").unwrap();
        assert_eq!(read_file_capped(&path, 5).unwrap(), b"12345");
        let err = read_file_capped(&path, 4).expect_err("fifth byte must trip cap");
        assert!(err.to_string().contains("4-byte limit"));
        let _ = std::fs::remove_file(path);
    }

    #[tokio::test]
    async fn driver_replacement_after_startup_is_refused_before_spawn() {
        let path = std::env::temp_dir().join(format!(
            "cx-render-preview-driver-mutate-{}",
            Uuid::new_v4()
        ));
        std::fs::write(&path, b"#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let digest = sha256_file(&path).unwrap();
        let runner = SpecRenderPreviewRunner::new(path.clone(), &digest, 1).unwrap();
        // Same path and still executable, different bytes after construction.
        std::fs::write(&path, b"#!/bin/sh\necho should-not-run\n").unwrap();
        let err = runner
            .execute_preview(b"{}")
            .await
            .expect_err("post-start replacement must fail the content pin");
        assert!(err.to_string().contains("changed after startup"));
        let _ = std::fs::remove_file(path);
    }

    #[cfg(unix)]
    #[tokio::test]
    #[allow(clippy::await_holding_lock)]
    async fn first_party_python_controller_runs_end_to_end_through_agent_executor() {
        // Environment is process-global; serialize this one integration test and
        // restore every value even if later assertions fail.
        let _lock = ENV_LOCK.lock().unwrap();
        let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("agent has repository parent");
        let spec_lab = repo.join("scripts/spec-lab");
        let driver = spec_lab.join("cx_agent_render_preview_driver.py");
        let core = spec_lab.join("cx_speculative_core.py");
        let adapter = spec_lab.join("cx_render_spec_adapter.py");
        let backend =
            std::env::temp_dir().join(format!("cx-render-preview-backend-{}.py", Uuid::new_v4()));
        std::fs::write(
            &backend,
            br#"from cx_speculative_core import DraftProposal, RepairResult, Verification
PROTOCOL_VERSION = 1
MODALITY = "render"
def baseline(unit): return unit.payload["truth"]
def draft(unit): return DraftProposal(unit, unit.payload["draft"])
def verify(proposal):
    truth = proposal.unit.payload["truth"]
    return Verification(proposal.draft == truth, truth)
def repair(_proposal, verification): return RepairResult(verification.truth)
"#,
        )
        .unwrap();

        let values = [
            (DRIVER_ENV, driver.display().to_string()),
            (DRIVER_SHA256_ENV, sha256_file(&driver).unwrap()),
            (
                "CX_SPEC_RENDER_PREVIEW_BACKEND",
                backend.display().to_string(),
            ),
            (
                "CX_SPEC_RENDER_PREVIEW_BACKEND_SHA256",
                sha256_file(&backend).unwrap(),
            ),
            (
                "CX_SPEC_RENDER_PREVIEW_CORE_SHA256",
                sha256_file(&core).unwrap(),
            ),
            (
                "CX_SPEC_RENDER_PREVIEW_ADAPTER_SHA256",
                sha256_file(&adapter).unwrap(),
            ),
        ];
        let _restore = RestoreEnv::set(&values);
        let runner = SpecRenderPreviewRunner::from_env()
            .unwrap()
            .expect("explicit preview runner");
        let request = serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "kind": "cx_spec_render_preview_request",
            "units": [
                {"unit_id": "tile-0", "payload": {"draft": "A", "truth": "A"}, "meta": {}},
                {"unit_id": "tile-1", "payload": {"draft": "bad", "truth": "B"}, "meta": {}}
            ],
            "meta": {"test": true}
        }))
        .unwrap();
        let output = runner.execute_preview(&request).await.unwrap();
        let parsed: Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(parsed["outputs"], serde_json::json!(["A", "B"]));
        assert_eq!(parsed["receipt"]["quality_tier"], "preview");
        assert_eq!(parsed["receipt"]["artifact_verified"], false);
        assert_eq!(parsed["receipt"]["speedup_vs_baseline"], Value::Null);
        let _ = std::fs::remove_file(backend);
    }

    #[test]
    fn envelope_rejects_unknown_fields_and_contradictory_accounting() {
        let mut extra = valid_envelope();
        extra["surprise"] = Value::Bool(true);
        assert!(validate_envelope(&serde_json::to_vec(&extra).unwrap()).is_err());

        let mut total = valid_envelope();
        total["receipt"]["total_product_time_s"] = serde_json::json!(99.0);
        assert!(validate_envelope(&serde_json::to_vec(&total).unwrap()).is_err());

        let mut outputs = valid_envelope();
        outputs["outputs"] = Value::Array(Vec::new());
        assert!(validate_envelope(&serde_json::to_vec(&outputs).unwrap()).is_err());
    }
}
