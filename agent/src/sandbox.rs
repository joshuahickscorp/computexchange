//! sandbox.rs — sandboxed execution of an opaque BYO-container compute job (the
//! `custom` JobType; ACCRETION.md §7-8, the metered NVIDIA GPU-second lane). The
//! buyer supplies an OCI image + command; the agent runs it ON THE GPU inside a
//! locked-down container, pipes the job input to the container's STDIN, and captures
//! its STDOUT as the result. The input/output contract is deliberately simple and
//! mount-free (no host paths exposed): stdin = input bytes, stdout = result bytes.
//!
//! This is UNTRUSTED code, so the defaults are strict and non-negotiable: no network,
//! read-only rootfs, every Linux capability dropped, no privilege escalation, runs as
//! `nobody`, memory + pid caps, and a hard wall-clock timeout (coreutils `timeout`).
//!
//! Linux + Docker + the NVIDIA Container Toolkit only (the CUDA lane). On a host
//! without them, `run_sandboxed` returns an honest typed error — NEVER a fake result.
//! Verified end-to-end on a real GPU host via scripts/prove-cuda.sh; the arg builder
//! is unit-tested here without Docker.

use std::io::Write;
use std::process::{Command, Stdio};

use crate::runners::RunError;

/// Resource + time bounds for one sandboxed job.
pub struct SandboxLimits {
    /// Container memory cap (host RAM, GiB). Swap is pinned equal so the job can't
    /// spill to disk.
    pub memory_gb: u32,
    /// Hard wall-clock kill (seconds). Exceeding it is a terminal failure, not a hang.
    pub timeout_secs: u32,
    /// Max process count (fork-bomb guard).
    pub pids: u32,
}

impl Default for SandboxLimits {
    fn default() -> Self {
        Self {
            memory_gb: 16,
            timeout_secs: 3600,
            pids: 512,
        }
    }
}

/// Build the argv for `timeout … docker run …` with the locked-down sandbox flags.
/// PURE (no I/O) so the hardening is unit-tested without Docker present. `image` and
/// `command` are the buyer's; every other flag is the agent's non-negotiable sandbox.
pub fn sandbox_argv(image: &str, command: &[String], limits: &SandboxLimits) -> Vec<String> {
    let mem = format!("{}g", limits.memory_gb);
    let mut a: Vec<String> = Vec::new();
    // Hard wall-clock kill via coreutils `timeout`: SIGTERM at the deadline, SIGKILL
    // 10s later if the container ignores it. `timeout` exit code 124 == timed out.
    a.extend(["timeout", "--kill-after=10"].map(String::from));
    a.push(limits.timeout_secs.to_string());
    // Docker: ephemeral (--rm), interactive stdin (-i), all GPUs.
    a.extend(["docker", "run", "--rm", "-i", "--gpus", "all"].map(String::from));
    // Sandbox hardening for untrusted code.
    a.extend(
        [
            "--network",
            "none",        // no network: no exfiltration, no SSRF, no lateral movement
            "--read-only", // immutable root filesystem
            "--tmpfs",
            "/tmp:rw,size=2g,noexec", // the only writable surface, non-executable
            "--cap-drop",
            "ALL", // drop every Linux capability
            "--security-opt",
            "no-new-privileges", // block setuid privilege escalation
            "--user",
            "65534:65534", // run as `nobody`, never root
            "--pids-limit",
        ]
        .map(String::from),
    );
    a.push(limits.pids.to_string());
    a.extend(["--memory", &mem, "--memory-swap", &mem].map(String::from)); // cap RAM, no swap spill
                                                                           // The buyer's image + command (the only untrusted inputs).
    a.push(image.to_string());
    a.extend(command.iter().cloned());
    a
}

/// Run the buyer's container sandboxed: pipe `input` to its stdin, capture stdout as
/// the result. Honest typed errors — a missing sandbox is `NotImplemented` (this host
/// can't run the lane), a non-zero/timed-out container is `Inference`. Never fakes a
/// result.
pub fn run_sandboxed(
    image: &str,
    command: &[String],
    input: &[u8],
    limits: &SandboxLimits,
) -> Result<Vec<u8>, RunError> {
    if image.trim().is_empty() {
        return Err(RunError::BadInput {
            job: "custom",
            msg: "custom job has no container image".into(),
        });
    }
    let argv = sandbox_argv(image, command, limits);
    let mut child = match Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        // No `timeout`/`docker` on PATH → this host cannot run the BYO-container lane.
        // Surface the boundary honestly; the scheduler should only route `custom` to
        // capable (Linux + Docker + NVIDIA Container Toolkit) workers.
        Err(e) => {
            return Err(RunError::NotImplemented {
                job_type: "custom",
                detail: format!(
                    "the sandboxed BYO-container runner needs Docker + the NVIDIA \
                     Container Toolkit on a Linux GPU host; could not launch it ({e})"
                ),
            })
        }
    };
    // Feed the job input on stdin from a SEPARATE THREAD while wait_with_output drains
    // stdout/stderr below. Writing all input before reading output would deadlock the
    // moment the container's stdout fills the OS pipe buffer (~64 KiB) — both sides
    // would block on a full pipe. Dropping the handle at the end closes stdin (EOF).
    let stdin = child.stdin.take();
    let input_owned = input.to_vec();
    let writer = std::thread::spawn(move || {
        if let Some(mut s) = stdin {
            let _ = s.write_all(&input_owned); // a container that ignores stdin is fine
        }
    });
    let out = child.wait_with_output().map_err(|e| RunError::Inference {
        backend: "custom",
        msg: format!("waiting on sandbox: {e}"),
    })?;
    let _ = writer.join();
    match out.status.code() {
        Some(0) => Ok(out.stdout),
        Some(124) => Err(RunError::Inference {
            backend: "custom",
            msg: format!(
                "custom job exceeded its {}s time limit and was killed",
                limits.timeout_secs
            ),
        }),
        code => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            let tail: Vec<&str> = stderr.lines().rev().take(5).collect();
            let tail: String = tail.into_iter().rev().collect::<Vec<_>>().join("\n");
            Err(RunError::Inference {
                backend: "custom",
                msg: format!("custom container exited with {code:?}: {}", tail.trim()),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sandbox_argv_locks_down_untrusted_code() {
        let limits = SandboxLimits {
            memory_gb: 24,
            timeout_secs: 600,
            pids: 256,
        };
        let argv = sandbox_argv(
            "ghcr.io/acme/sim:1.2",
            &["./run".into(), "--iters".into(), "1000".into()],
            &limits,
        );
        let joined = argv.join(" ");
        // Hard timeout wraps docker.
        assert_eq!(argv[0], "timeout");
        assert!(joined.contains("--kill-after=10 600 docker run --rm -i --gpus all"));
        // Every hardening flag is present.
        for flag in [
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--user 65534:65534",
            "--pids-limit 256",
            "--memory 24g",
            "--memory-swap 24g",
        ] {
            assert!(joined.contains(flag), "missing sandbox flag: {flag}");
        }
        // The buyer's image + command come LAST (after all agent-controlled flags).
        let img = argv
            .iter()
            .position(|s| s == "ghcr.io/acme/sim:1.2")
            .unwrap();
        assert!(argv[img + 1] == "./run" && argv[img + 2] == "--iters" && argv[img + 3] == "1000");
        // No GPU job ever gets a network.
        assert!(!joined.contains("--network host") && !joined.contains("--network bridge"));
    }

    #[test]
    fn empty_image_is_rejected_not_run() {
        let err = run_sandboxed("", &[], b"", &SandboxLimits::default()).unwrap_err();
        assert!(matches!(err, RunError::BadInput { .. }));
    }
}
