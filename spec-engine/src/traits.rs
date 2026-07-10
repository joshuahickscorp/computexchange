//! The four role traits an adapter implements. Kept SEPARATE (not one bundled
//! adapter) precisely because the plan's experiment loop swaps them
//! independently — e.g. a cheaper [`DraftProducer`] against the same
//! [`Verifier`]. All are generic over [`SpecUnit`], so the engine monomorphizes
//! to straight-line, zero-dispatch calls.

use crate::unit::SpecUnit;
use crate::verify::{Acceptance, Verification};

/// The cheap proposer — the fast lane. (render: low-spp + OIDN tile; token:
/// n-gram / draft-model / structural predictor.)
pub trait DraftProducer<U: SpecUnit> {
    /// Propose a cheap draft for this unit.
    fn draft(&self, unit: &U) -> U::Draft;
}

/// The single expensive truth/quality source — the ONLY phase timed as
/// `verify_cost`. (render: the cheap SSIM-proxy gate; token: one target-model
/// forward pass over the k+1 positions.)
pub trait Verifier<U: SpecUnit> {
    /// Score the draft, optionally returning the authoritative truth (see
    /// [`Verification`]).
    fn verify(&self, unit: &U, draft: &U::Draft) -> Verification<U>;
}

/// The pure, cheap branch condition between verify and repair — NOT separately
/// timed. (render: SSIM thresholds -> tier, accept∈{0,1}; token: accept = matched
/// prefix m of k.)
pub trait AcceptancePolicy<U: SpecUnit> {
    /// Decide how much of the draft is acceptable and at what delivered tier.
    fn decide(&self, unit: &U, verification: &Verification<U>) -> Acceptance;
}

/// Materializes the final [`SpecUnit::Output`]. Two faces of one modality-owned
/// concern:
///
/// - [`RepairPolicy::accept`]: the full-accept assembly, cheap, NOT counted as
///   `repair_cost` (render: the draft tile IS the delivery; token: the k matched
///   tokens).
/// - [`RepairPolicy::repair`]: the not-fully-accepted path, timed as
///   `repair_cost` (render: re-render the tile at reference spp; token: splice the
///   accepted prefix with the target correction carried in `verification.truth`).
///
/// Both take `draft` by value, so the engine MOVES and never clones.
pub trait RepairPolicy<U: SpecUnit> {
    /// Assemble the delivered output when the whole draft was accepted.
    fn accept(&self, unit: &U, draft: U::Draft) -> U::Output;
    /// Assemble the delivered output on the repair path, consuming the draft and
    /// the verification (whose `truth` the token lane splices in).
    fn repair(
        &self,
        unit: &U,
        draft: U::Draft,
        verification: Verification<U>,
        acceptance: &Acceptance,
    ) -> U::Output;
}
