//! Host-local resource planning and admission for the supplier agent.
//!
//! The control plane decides which worker and how many fleet chunks should run.
//! This module makes the second, local decision: how much CPU and memory one
//! claimed chunk may reserve, and whether it is safe to overlap with other work.
//! It deliberately does not change model math or speculative decoding.

use std::sync::Arc;

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::models;
use crate::pool;
use crate::types::{JobManifest, JobType};

const MEMORY_QUANTUM_GB: f32 = 0.25;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionMode {
    /// Independent CPU/I/O work may overlap until its reservations fill the host.
    Parallel,
    /// Work shares a resident/model runtime and queues at that runtime's safe
    /// serialization or batching boundary.
    Stacked,
    /// Buyer-selected or machine-saturating work gets the full CPU admission
    /// budget. Memory remains bounded by the same reservation ledger.
    Exclusive,
}

impl ExecutionMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Parallel => "parallel",
            Self::Stacked => "stacked",
            Self::Exclusive => "exclusive",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ResourcePlan {
    pub mode: ExecutionMode,
    pub cpu_units: u32,
    pub memory_units: u32,
    pub memory_gb: f32,
    pub runtime_key: String,
    pub reason: &'static str,
}

/// Pure planner inputs fixed for one running agent. Live pressure remains a
/// separate, stricter gate in `AgentConfig::evaluate_memory_throttle`; this
/// capacity is the allocatable pool after operator headroom.
#[derive(Debug, Clone)]
pub struct ResourceGovernor {
    cpu_units: u32,
    memory_units: u32,
    accelerator: bool,
}

impl ResourceGovernor {
    pub fn new(
        logical_cpus: usize,
        max_cpu_pct: f32,
        allocatable_memory_gb: f32,
        accelerator: bool,
    ) -> Self {
        let pct = max_cpu_pct.clamp(1.0, 100.0) / 100.0;
        let cpu_units = ((logical_cpus.max(1) as f32 * pct).floor() as u32).max(1);
        let memory_units = gb_to_units(allocatable_memory_gb.max(MEMORY_QUANTUM_GB));
        Self {
            cpu_units,
            memory_units,
            accelerator,
        }
    }

    pub fn cpu_units(&self) -> u32 {
        self.cpu_units
    }

    pub fn allocatable_memory_gb(&self) -> f32 {
        self.memory_units as f32 * MEMORY_QUANTUM_GB
    }

    /// Queue enough work to hide object I/O and cold-load latency without
    /// allowing an unbounded claimed-task backlog on one worker.
    pub fn queue_depth(&self) -> usize {
        ((self.cpu_units as usize).saturating_mul(2))
            .min(self.memory_units as usize)
            .clamp(2, 64)
    }

    pub fn plan(&self, manifest: &JobManifest, warm_models: &[String]) -> ResourcePlan {
        let declared = manifest.constraints.min_memory_gb.max(0.0);
        let (mode, cpu, heuristic_gb, runtime_key, reason) = match &manifest.job_type {
            JobType::Embed { .. } | JobType::Rerank { .. } => {
                let key = canonical_embed_key(&manifest.model.model_ref);
                let warm = warm_models.iter().any(|id| id == &key);
                (
                    ExecutionMode::Parallel,
                    half(self.cpu_units),
                    if warm { 0.5 } else { measured_or(&key, 1.5) },
                    key,
                    "independent embedding/rerank work can overlap",
                )
            }
            JobType::BatchInfer { .. }
            | JobType::BatchClassification { .. }
            | JobType::JsonExtraction { .. } => {
                let key = canonical_llama_key(&manifest.model.model_ref);
                let warm = warm_models.iter().any(|id| id == &key);
                let cold = if models::is_big_llama(&manifest.model.model_ref) {
                    7.0
                } else {
                    2.5
                };
                (
                    ExecutionMode::Stacked,
                    if self.accelerator { 1 } else { self.cpu_units },
                    if warm { 1.0 } else { measured_or(&key, cold) },
                    key,
                    "resident generative work stacks at the model runtime",
                )
            }
            JobType::AudioTranscribe { .. } => {
                let key = canonical_whisper_key(&manifest.model.model_ref);
                let warm = warm_models.iter().any(|id| id == &key);
                (
                    ExecutionMode::Parallel,
                    half(self.cpu_units),
                    if warm { 0.75 } else { measured_or(&key, 2.0) },
                    key,
                    "audio work uses a bounded independent runtime",
                )
            }
            JobType::Eval { .. } => (
                ExecutionMode::Parallel,
                half(self.cpu_units),
                1.0,
                "eval".into(),
                "evaluation can overlap within the host budget",
            ),
            JobType::ImageGen { .. }
            | JobType::LoraFinetune { .. }
            | JobType::Custom { .. }
            | JobType::RenderSpeculativePreview { .. } => (
                ExecutionMode::Exclusive,
                self.cpu_units,
                4.0,
                manifest.job_type.tag().into(),
                "machine-saturating work receives the full CPU budget",
            ),
        };

        let memory_gb = declared.max(heuristic_gb).min(self.allocatable_memory_gb());
        ResourcePlan {
            mode,
            cpu_units: cpu.clamp(1, self.cpu_units),
            memory_units: gb_to_units(memory_gb).clamp(1, self.memory_units),
            memory_gb,
            runtime_key,
            reason,
        }
    }
}

fn half(n: u32) -> u32 {
    n.div_ceil(2).max(1)
}

fn gb_to_units(gb: f32) -> u32 {
    (gb / MEMORY_QUANTUM_GB).ceil().max(1.0) as u32
}

fn measured_or(key: &str, fallback_gb: f32) -> f32 {
    pool::residency_snapshot()
        .get(key)
        .map(|m| (m.rss_delta_bytes.max(0) as f32 / 1_073_741_824.0).max(MEMORY_QUANTUM_GB))
        .unwrap_or(fallback_gb)
}

fn canonical_embed_key(model_ref: &str) -> String {
    let lower = model_ref.to_ascii_lowercase();
    if lower.contains("bge") {
        "bge-small-en-v1.5"
    } else {
        "all-minilm-l6-v2"
    }
    .into()
}

fn canonical_llama_key(model_ref: &str) -> String {
    let lower = model_ref.to_ascii_lowercase();
    if models::is_big_llama(model_ref) {
        "qwen2.5-7b-instruct-q4"
    } else if lower.contains("qwen") {
        "qwen2.5-0.5b-instruct-q4"
    } else {
        "llama-3.2-1b-instruct-q4"
    }
    .into()
}

fn canonical_whisper_key(model_ref: &str) -> String {
    if model_ref.to_ascii_lowercase().contains("base") {
        "whisper-base"
    } else {
        "whisper-tiny"
    }
    .into()
}

/// Atomic weighted admission across CPU and RAM. Memory is always acquired
/// first, so every waiter uses the same lock order and cannot deadlock.
#[derive(Debug, Clone)]
pub struct ResourceGate {
    cpu: Arc<Semaphore>,
    memory: Arc<Semaphore>,
}

impl ResourceGate {
    pub fn new(governor: &ResourceGovernor) -> Self {
        Self {
            cpu: Arc::new(Semaphore::new(governor.cpu_units as usize)),
            memory: Arc::new(Semaphore::new(governor.memory_units as usize)),
        }
    }

    pub async fn acquire(
        &self,
        plan: &ResourcePlan,
    ) -> Result<ResourceLease, tokio::sync::AcquireError> {
        let memory = self
            .memory
            .clone()
            .acquire_many_owned(plan.memory_units)
            .await?;
        let cpu = self.cpu.clone().acquire_many_owned(plan.cpu_units).await?;
        Ok(ResourceLease {
            _memory: memory,
            _cpu: cpu,
        })
    }
}

pub struct ResourceLease {
    _memory: OwnedSemaphorePermit,
    _cpu: OwnedSemaphorePermit,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{
        JobConstraints, JobManifest, ModelKind, ModelRef, OutputRef, ServiceTier,
        VerificationPolicy,
    };
    use uuid::Uuid;

    fn manifest(job_type: JobType, model: &str, min_memory_gb: f32) -> JobManifest {
        JobManifest {
            id: Uuid::nil(),
            job_type,
            model: ModelRef {
                kind: ModelKind::Gguf,
                model_ref: model.into(),
            },
            inputs: vec![],
            output: OutputRef { url: String::new() },
            params: serde_json::Value::Null,
            constraints: JobConstraints {
                min_memory_gb,
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

    #[test]
    fn cpu_ceiling_and_queue_depth_scale_with_host() {
        let g = ResourceGovernor::new(16, 75.0, 32.0, true);
        assert_eq!(g.cpu_units(), 12);
        assert_eq!(g.queue_depth(), 24);
    }

    #[test]
    fn warm_llama_stacks_and_reserves_only_working_memory() {
        let g = ResourceGovernor::new(12, 100.0, 16.0, true);
        let m = manifest(
            JobType::BatchInfer {
                max_tokens: 128,
                temperature: 0.0,
            },
            "llama-3.2-1b-instruct-q4",
            0.0,
        );
        let p = g.plan(&m, &["llama-3.2-1b-instruct-q4".into()]);
        assert_eq!(p.mode, ExecutionMode::Stacked);
        assert_eq!(p.cpu_units, 1);
        assert_eq!(p.memory_gb, 1.0);
    }

    #[test]
    fn declared_memory_is_a_hard_floor_and_clamped_to_host_capacity() {
        let g = ResourceGovernor::new(8, 100.0, 8.0, true);
        let m = manifest(
            JobType::Custom {
                image: None,
                command: vec![],
            },
            "",
            20.0,
        );
        let p = g.plan(&m, &[]);
        assert_eq!(p.mode, ExecutionMode::Exclusive);
        assert_eq!(p.memory_gb, 8.0);
        assert_eq!(p.cpu_units, 8);
    }

    #[tokio::test]
    async fn weighted_gate_never_oversubscribes_cpu_or_memory() {
        let g = ResourceGovernor::new(4, 100.0, 2.0, true);
        let gate = ResourceGate::new(&g);
        let plan = ResourcePlan {
            mode: ExecutionMode::Parallel,
            cpu_units: 4,
            memory_units: 8,
            memory_gb: 2.0,
            runtime_key: "x".into(),
            reason: "test",
        };
        let first = gate.acquire(&plan).await.unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(20), gate.acquire(&plan))
                .await
                .is_err()
        );
        drop(first);
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), gate.acquire(&plan))
                .await
                .is_ok()
        );
    }
}
