//! Two in-crate demo adapters that prove the SAME four traits host genuinely
//! different modalities without special-casing:
//!
//! - [`synth_render`] — whole-unit accept, `truth = None`, `exact = false`
//!   (render's per-tile SSIM gate).
//! - [`synth_token`] — PARTIAL accept, `truth = Some`, `exact = true` (token
//!   spec-decode's matched-prefix-plus-repair).
//!
//! Both are deterministic and dependency-free, and both are hard-wired
//! [`crate::receipt::Evidence::Synthetic`] with a MODELED baseline, so a demo
//! receipt can never be mistaken for a measured lane win.

pub mod synth_render;
pub mod synth_token;

/// Deterministic FNV-1a-style mix used by both demo adapters to (a) derive a
/// stable pixel/token hash and (b) do a small, bounded amount of REAL work so the
/// phase timers register a positive, ordered cost (`draft < verify < repair`)
/// without any external dependency or nondeterminism. `iters` scales the modeled
/// cost of a phase.
pub(crate) fn fnv_spin(seed: u64, iters: u32) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325 ^ seed;
    for i in 0..iters {
        h ^= i as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
        h ^= h >> 29;
    }
    h
}
