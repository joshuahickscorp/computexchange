//! # cx-spec-engine — the ComputeExchange owned speculation substrate
//!
//! One modality-general accept/verify/repair core that both a render lane and a
//! token lane can instantiate, emitting ONE receipt schema. This is the
//! compiling, monomorphized Rust re-expression of the Python spine at
//! `scripts/spec-lab/cx_speculative_core.py`, built to the contract in
//! `docs/research/CONSOLIDATION_PLAN_2026-07-09.md`:
//!
//! ```text
//! SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
//! ```
//!
//! ## Honesty invariants baked into the types (not just documented)
//!
//! 1. **No invented baselines.** [`engine::aggregate`] emits
//!    [`SpecReceipt::speedup_vs_baseline`] as `None` (serialized `null`) unless the
//!    caller supplies a baseline AND labels it [`BaselineSource::Measured`] or
//!    [`BaselineSource::Modeled`]. The engine never fabricates a denominator.
//! 2. **No multiplied multipliers.** A [`SpecReceipt`] only ever holds ONE
//!    workload's `baseline/spec` ratio. There is no field or method that composes
//!    two lanes' speedups — a combined number can exist only by running a genuinely
//!    nested unit-set through one pipeline (a real end-to-end measurement).
//! 3. **Every number carries a label.** [`Evidence`] and [`BaselineSource`] travel
//!    on the receipt; the two in-crate demo adapters are hard-wired
//!    [`Evidence::Synthetic`], so a stub can never masquerade as a measured win.
//!
//! ## Zero abstraction tax (the plan's "Kill" defense)
//!
//! The core is fully generic and monomorphized: [`SpecPipeline`] compiles to
//! straight-line calls with no `dyn`, no boxing. The draft is *moved* (never
//! cloned) into the accept/repair step, and [`Verification::truth`] is an
//! `Option` so the render lane allocates no reference it does not have while the
//! token lane still carries the target continuation its verify pass produces.

pub mod adapters;
pub mod engine;
pub mod receipt;
pub mod traits;
pub mod unit;
pub mod verify;

pub use engine::{
    aggregate, try_aggregate, EngineError, SpecPipeline, UnitTrace, MAX_SPEC_BATCH_UNITS,
};
pub use receipt::{
    BaselineSource, Details, Evidence, Modality, QualityTier, ReceiptParseError,
    ReceiptValidationError, SpecReceipt, MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES,
    MAX_SPEC_RECEIPT_JSON_BYTES, MAX_SPEC_RECEIPT_JSON_DEPTH, MAX_SPEC_RECEIPT_UNITS,
    SPEC_RECEIPT_SCHEMA_VERSION,
};
pub use traits::{AcceptancePolicy, DraftProducer, RepairPolicy, Verifier};
pub use unit::SpecUnit;
pub use verify::{Acceptance, AcceptanceError, Verification};
