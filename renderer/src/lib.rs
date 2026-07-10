//! # cx-renderer — Track 3 Phase 0
//!
//! The Rust-native, reuse-first renderer for ComputeExchange. This crate is the
//! Phase-0 de-risking scaffold from
//! `docs/research/ORIGINAL_ENGINE_THREE_TRACKS_2026-07-07.md` (Track 3):
//!
//! * **M0** ([`furnace`]) — a pure-CPU, zero-dependency unidirectional path
//!   tracer whose correctness is gated by the white furnace test. See
//!   `tests/furnace_test.rs` for the automated gate (`cargo test`).
//! * **M1** (`examples/wgpu_smoke.rs`, behind `--features gpu`) — wgpu + WGSL
//!   compute plumbing proving the device/pipeline/buffer round-trip on the
//!   Metal backend (Apple Silicon) and, unchanged, Vulkan (RunPod NVIDIA). This
//!   is the substrate the real dual-backend path tracer will be built on.
//! * **Phase 1 design** — `DECOUPLED_SHADING_NOTES.md` sketches the
//!   path-structure cache that lets N material variants re-shade against one
//!   traced geometric skeleton.
//!
//! Kept standalone (not merged into `agent/`) because rendering and inference
//! job-execution are separate concerns.

pub mod furnace;
pub mod decoupled;
pub mod rng;
pub mod vec3;

pub use vec3::{vec3, Vec3};
