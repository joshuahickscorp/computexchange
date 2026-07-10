//! The [`SpecUnit`] descriptor — the tiny per-modality type that binds one
//! modality's concrete artifact types together. Everything downstream is generic
//! over it, so there is no `dyn`, no boxing, and no shared "any" payload.

use crate::receipt::Modality;

/// One speculatable work item. Deliberately the NATURAL verify-batch of its
/// modality — one render tile, or one k-token window — so token spec-decode's
/// "a single target forward pass verifies k tokens at once" survives intact
/// (the engine never splits a verify call) and render's per-tile gate is
/// preserved.
///
/// The three associated types keep the quality signal (`Score`) distinct from
/// the cheap artifact (`Draft`) and the final artifact (`Output`), so the
/// acceptance policy never recomputes quality and the engine never allocates an
/// output it does not need.
pub trait SpecUnit {
    /// The cheap drafted artifact: a low-spp tile; the k proposed tokens.
    type Draft;
    /// The final delivered artifact, and the type repair produces: the final tile
    /// pixels; the accepted-prefix-plus-correction token run.
    type Output;
    /// The per-unit quality signal the acceptance policy reads — the SSIM triple;
    /// the matched-prefix length. Cheap / `Copy` in practice.
    type Score;

    /// Ledger id for this unit (tile index / window index).
    fn unit_id(&self) -> String;
    /// The modality tag this unit belongs to.
    fn modality(&self) -> Modality;
}
