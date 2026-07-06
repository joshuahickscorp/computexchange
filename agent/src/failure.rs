//! failure.rs — map a `RunError` to the SHARED failure-taxonomy class string and
//! build the `FailReport` the agent POSTs to `/v1/worker/task/{id}/fail` (Plane C/D
//! D0, docs/PLANE_C_ERRATA.md C-Errata-1/2). The class strings MUST stay in
//! lockstep with `control/failure.go` `failureClasses` — that one vocabulary is
//! what lets the control plane requeue-or-fail and explain to the buyer.
//!
//! Reporting a typed failure is what turns a 30-minute stale wait into a
//! seconds-fast requeue. We never fabricate the memory snapshot — it is a real
//! reading taken at the moment of failure (the whole point of OOM diagnosis).

use crate::hardware::MemorySnapshot;
use crate::runners::RunError;
use crate::types::{FailMemory, FailReport};

/// Classify a `RunError` into a taxonomy class (matches control/failure.go).
/// `low_memory` lets an inference failure taken under near-zero effective memory
/// be reported as a true `oom` rather than a generic `internal_error`.
pub fn classify(err: &RunError, low_memory: bool) -> &'static str {
    match err {
        RunError::NoRunner { .. } => "unsupported_job_type",
        RunError::ModelFetch { .. } => "model_load_failed",
        RunError::BadInput { .. } => "bad_input",
        // A cluster substrate that this single host cannot provide — retry/elsewhere
        // is the control plane's call, so report it as a non-buyer system failure.
        RunError::ExternalSubstrate { .. } => "internal_error",
        // A lane THIS host can't serve (today: a `custom` container job on a worker
        // with no Docker/NVIDIA sandbox). Terminal for this worker, not retryable on
        // it — the control plane re-dispatches to a worker that advertises the lane.
        // `unsupported_job_type` is the taxonomy's "can't be served as specified" class.
        RunError::NotImplemented { .. } => "unsupported_job_type",
        RunError::Inference { msg, .. } => {
            let m = msg.to_ascii_lowercase();
            if m.contains("input_url") || m.contains("output_url") || m.contains("presigned") {
                "object_store_error"
            } else if low_memory || m.contains("out of memory") || m.contains("oom") {
                "oom"
            } else {
                "internal_error"
            }
        }
        // A real mid-job memory-pressure preemption (docs/internal/
        // CREED_AND_PATH_TO_TEN.md, "Memory management & dynamic throttling
        // internals" 7→8) — same wire class as any other OOM signal, already
        // retryable/not-buyer-fault in control/failure.go, so the control plane
        // requeues the remaining rows to a worker with room with no taxonomy change.
        RunError::OomPreempt { .. } => "oom",
    }
}

/// Build the `FailReport` for a failed task: the classified class, a short message,
/// the backend + model, the run duration, and the REAL memory snapshot at failure.
/// `headroom_gb` is the operator's reserved headroom (so effective = available −
/// headroom, matching the throttle math).
pub fn build_report(
    err: &RunError,
    backend: &str,
    model: &str,
    duration_ms: u64,
    snap: &MemorySnapshot,
    headroom_gb: f32,
) -> FailReport {
    let effective = (snap.available_gb - headroom_gb).max(0.0);
    // "Low memory" when the allocatable pool is essentially gone — the signal that
    // an inference failure here was almost certainly an OOM.
    let low = effective <= 0.5 || snap.available_gb <= headroom_gb;
    FailReport {
        class: classify(err, low).to_string(),
        message: err.to_string(),
        duration_ms,
        backend: backend.to_string(),
        model: model.to_string(),
        memory: Some(FailMemory {
            total_gb: snap.total_gb,
            available_gb: snap.available_gb,
            effective_gb: effective,
            reserved_headroom_gb: headroom_gb,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_run_errors_to_shared_taxonomy() {
        assert_eq!(
            classify(
                &RunError::BadInput {
                    job: "embed",
                    msg: "x".into()
                },
                false
            ),
            "bad_input"
        );
        assert_eq!(
            classify(
                &RunError::ModelFetch {
                    repo: "r".into(),
                    msg: "x".into()
                },
                false
            ),
            "model_load_failed"
        );
        // S3 errors surface as object_store_error (retryable infra).
        assert_eq!(
            classify(
                &RunError::Inference {
                    backend: "embed",
                    msg: "fetching input_url: timeout".into()
                },
                false
            ),
            "object_store_error"
        );
        // An inference failure under low memory is reported as a true OOM.
        assert_eq!(
            classify(
                &RunError::Inference {
                    backend: "batch_infer",
                    msg: "metal alloc failed".into()
                },
                true
            ),
            "oom"
        );
        // …but a generic inference failure with memory to spare is internal_error.
        assert_eq!(
            classify(
                &RunError::Inference {
                    backend: "batch_infer",
                    msg: "tokenizer error".into()
                },
                false
            ),
            "internal_error"
        );
        // A `custom` container job on a worker without the sandbox is terminal
        // (unsupported_job_type), not a retryable internal_error.
        assert_eq!(
            classify(
                &RunError::NotImplemented {
                    job_type: "custom",
                    detail: "custom requires a Docker + NVIDIA GPU host".into(),
                },
                false
            ),
            "unsupported_job_type"
        );
    }

    #[test]
    fn build_report_carries_real_memory_and_oom_signal() {
        let snap = MemorySnapshot {
            total_gb: 64.0,
            available_gb: 1.0,
        };
        let err = RunError::Inference {
            backend: "batch_infer",
            msg: "alloc".into(),
        };
        let r = build_report(
            &err,
            "batch_infer",
            "llama-3.2-1b-instruct-q4",
            1200,
            &snap,
            8.0,
        );
        // available (1) ≤ headroom (8) ⇒ low memory ⇒ oom.
        assert_eq!(r.class, "oom");
        let m = r.memory.unwrap();
        assert_eq!(m.total_gb, 64.0);
        assert_eq!(m.effective_gb, 0.0); // max(1 - 8, 0)
        assert_eq!(m.reserved_headroom_gb, 8.0);
    }
}
