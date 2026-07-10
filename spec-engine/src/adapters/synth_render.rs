//! `synth_render` — a deterministic render-like adapter: whole-unit accept,
//! `truth = None`, `exact = false`. It stresses the corner the token lane never
//! does — a verifier that allocates NO reference (its `truth` is `None`) and an
//! acceptance policy that either ships the cheap draft or re-renders at
//! reference — exactly the shape of the real render rows in the spec-lab ledger
//! (`exact: false`, an SSIM quality gate).
//!
//! ## The three-way per-tile decision (honest, and it yields a real tier mix)
//!
//! A tile's `residual` measures how far the cheap low-spp draft sits from the
//! converged reference. The SSIM gate judges the DRAFT:
//!
//! - clears the DELIVERY gate  -> ship the draft as-is           => tier `Delivery`, draft REUSED
//! - clears only the PREVIEW gate -> ship the draft as a preview => tier `Preview`,  draft REUSED
//! - fails even preview        -> re-render at reference spp      => tier `Delivery`, draft DISCARDED
//!
//! So `quality_tier` (worst-wins over DELIVERED tiers) drops to `Preview` exactly
//! when some tile was preview-shipped, while `accepted_fraction` (draft reuse)
//! and `repaired_fraction` (the re-rendered fails) are separate, orthogonal
//! signals. A repaired tile is reference quality, so it honestly records
//! `Delivery` — the receipt never understates a re-rendered tile's fidelity.

use crate::adapters::fnv_spin;
use crate::receipt::{Modality, QualityTier};
use crate::traits::{AcceptancePolicy, DraftProducer, RepairPolicy, Verifier};
use crate::unit::SpecUnit;
use crate::verify::{Acceptance, Verification};

/// Low sample count of the cheap draft render.
pub const DRAFT_SPP: u32 = 4;
/// Reference sample count a repaired tile is re-rendered at.
pub const REFERENCE_SPP: u32 = 512;

// Modeled phase costs (iteration counts for `fnv_spin`): draft is cheap, verify
// is the SSIM proxy, repair (a reference re-render) is the most expensive.
const DRAFT_ITERS: u32 = 2_000;
const VERIFY_ITERS: u32 = 4_000;
const REPAIR_ITERS: u32 = 24_000;

/// A render tile. `residual` in `[0, 1]` is the distance of the cheap draft from
/// the converged reference (0 = already converged, 1 = garbage).
pub struct RenderTile {
    /// Tile index (ledger id).
    pub id: u32,
    /// Draft-vs-converged distance in `[0, 1]`.
    pub residual: f64,
}

/// The cheap drafted tile.
pub struct TileDraft {
    /// Deterministic hash standing in for the tile's pixels.
    pub pixels_hash: u64,
    /// Sample count the draft was rendered at.
    pub spp: u32,
}

/// The three-facet SSIM signal (global + worst-tile + p5), derived from the
/// residual. `Copy` — the acceptance policy reads it without recomputation.
#[derive(Clone, Copy, Debug)]
pub struct Ssim {
    /// Global SSIM.
    pub global: f64,
    /// Worst-region SSIM (render's worst-tile discipline).
    pub worst: f64,
    /// 5th-percentile SSIM.
    pub p5: f64,
}

/// The delivered tile.
pub struct TilePixels {
    /// Deterministic hash standing in for the delivered pixels.
    pub pixels_hash: u64,
    /// Sample count the delivered tile was rendered at.
    pub spp: u32,
}

impl SpecUnit for RenderTile {
    type Draft = TileDraft;
    type Output = TilePixels;
    type Score = Ssim;

    fn unit_id(&self) -> String {
        format!("tile_{}", self.id)
    }
    fn modality(&self) -> Modality {
        Modality::render()
    }
}

#[inline]
fn clamp01(x: f64) -> f64 {
    x.clamp(0.0, 1.0)
}

/// The cheap low-spp draft producer.
pub struct LowSppDrafter;

impl DraftProducer<RenderTile> for LowSppDrafter {
    fn draft(&self, unit: &RenderTile) -> TileDraft {
        TileDraft {
            pixels_hash: fnv_spin(unit.id as u64 ^ 0x5350_5F44, DRAFT_ITERS), // "SP_D"
            spp: DRAFT_SPP,
        }
    }
}

/// The SSIM-proxy verifier. Derives the SSIM triple from the tile's residual and
/// returns `truth: None` — no reference is rendered at verify time.
pub struct SsimGate;

impl Verifier<RenderTile> for SsimGate {
    fn verify(&self, unit: &RenderTile, _draft: &TileDraft) -> Verification<RenderTile> {
        let r = unit.residual;
        let ssim = Ssim {
            global: clamp01(1.0 - r),
            worst: clamp01(1.0 - 1.6 * r),
            p5: clamp01(1.0 - 1.3 * r),
        };
        // A little real work so verify_cost registers, and so the return is a
        // function of the draft too (kept, not asserted on).
        let _ = fnv_spin(unit.id as u64 ^ 0x5353_494D, VERIFY_ITERS); // "SSIM"
        Verification { score: ssim, truth: None }
    }
}

/// SSIM thresholds for the three-way decision.
const DELIVERY_GLOBAL: f64 = 0.98;
const DELIVERY_WORST: f64 = 0.95;
const PREVIEW_GLOBAL: f64 = 0.90;
const PREVIEW_WORST: f64 = 0.85;

/// The per-tile acceptance policy (whole-tile: `accepted ∈ {0, 1}`, `drafted = 1`).
pub struct TileGatePolicy;

impl AcceptancePolicy<RenderTile> for TileGatePolicy {
    fn decide(&self, _unit: &RenderTile, verification: &Verification<RenderTile>) -> Acceptance {
        let s = verification.score; // Ssim: Copy
        let (tier, accepted) = if s.global >= DELIVERY_GLOBAL && s.worst >= DELIVERY_WORST {
            // Draft is delivery-grade: ship it as-is (the win).
            (QualityTier::Delivery, 1.0)
        } else if s.global >= PREVIEW_GLOBAL && s.worst >= PREVIEW_WORST {
            // Draft is preview-grade: ship it as a preview (reused, lower tier).
            (QualityTier::Preview, 1.0)
        } else {
            // Draft failed: it will be repaired (re-rendered at reference), which
            // DELIVERS reference/Delivery quality — record the delivered tier,
            // with accepted = 0 (the draft is discarded).
            (QualityTier::Delivery, 0.0)
        };
        Acceptance {
            drafted: 1.0,
            accepted,
            tier,
            exact: false, // render is never bit-exact vs a full reference render
        }
    }
}

/// The repair policy: accept ships the draft; repair re-renders at reference spp.
pub struct ReRenderRepair;

impl RepairPolicy<RenderTile> for ReRenderRepair {
    fn accept(&self, _unit: &RenderTile, draft: TileDraft) -> TilePixels {
        // The cheap draft IS the delivered tile.
        TilePixels {
            pixels_hash: draft.pixels_hash,
            spp: draft.spp,
        }
    }
    fn repair(
        &self,
        unit: &RenderTile,
        _draft: TileDraft,
        _verification: Verification<RenderTile>,
        _acc: &Acceptance,
    ) -> TilePixels {
        // Re-render at reference spp (deterministic, modeled at the highest cost).
        TilePixels {
            pixels_hash: fnv_spin(unit.id as u64 ^ 0x5245_4632, REPAIR_ITERS), // "REF2"
            spp: REFERENCE_SPP,
        }
    }
}

/// Build a ready-to-run render pipeline.
pub fn pipeline(
    branch_id: &str,
) -> crate::engine::SpecPipeline<RenderTile, LowSppDrafter, SsimGate, TileGatePolicy, ReRenderRepair>
{
    crate::engine::SpecPipeline::render(branch_id, LowSppDrafter, SsimGate, TileGatePolicy, ReRenderRepair)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::receipt::{BaselineSource, Details, Evidence};

    // Residuals chosen to land each tile in a known tier (see the SSIM formulas):
    //  0.00 -> global 1.00, worst 1.00            -> Delivery (accept)
    //  0.05 -> global 0.95, worst 0.92            -> Preview  (accept as preview)
    //  0.40 -> global 0.60                        -> Fail     (repair to reference)
    fn mixed_tiles() -> Vec<RenderTile> {
        vec![
            RenderTile { id: 0, residual: 0.00 },
            RenderTile { id: 1, residual: 0.05 },
            RenderTile { id: 2, residual: 0.40 },
        ]
    }

    #[test]
    fn implements_all_four_traits_and_emits_a_sane_receipt() {
        let pipe = pipeline("cx_synth_render_demo");
        let tiles = mixed_tiles();
        let (outputs, receipt) = pipe.run_batch(
            &tiles,
            /* baseline_total_time_s */ 1.0,
            BaselineSource::Modeled,
            Evidence::Synthetic,
            Details::new(),
        );

        // One delivered output per tile.
        assert_eq!(outputs.len(), 3);
        // The repaired (failed) tile is re-rendered at reference spp.
        assert_eq!(outputs[2].spp, REFERENCE_SPP);
        // Accepted tiles ship the cheap draft.
        assert_eq!(outputs[0].spp, DRAFT_SPP);
        assert_eq!(outputs[1].spp, DRAFT_SPP);

        assert_eq!(receipt.modality, Modality::render());
        assert_eq!(receipt.units, 3);
        assert!(!receipt.exact, "render is never bit-exact vs a full reference");

        // accepted_fraction is a sane ratio in [0,1]: tiles 0 and 1 reuse the draft
        // (accepted 1 each), tile 2 is repaired (accepted 0) => 2/3.
        assert!((0.0..=1.0).contains(&receipt.accepted_fraction));
        assert!((receipt.accepted_fraction - 2.0 / 3.0).abs() < 1e-9);

        // worst-wins over [Delivery, Preview, Delivery] => Preview.
        assert_eq!(receipt.quality_tier, QualityTier::Preview);

        // Exactly the failed tile was repaired.
        assert!((receipt.repaired_fraction - 1.0 / 3.0).abs() < 1e-9);
        assert!(receipt.repair_cost_s > 0.0, "a repaired tile must cost repair time");

        // total = draft + verify + repair, and it is positive.
        assert!(receipt.total_product_time_s > 0.0);
        let sum = receipt.draft_cost_s + receipt.verify_cost_s + receipt.repair_cost_s;
        assert!((receipt.total_product_time_s - sum).abs() < 1e-12);

        // speedup = baseline / spec, present because the baseline was Modeled.
        let sp = receipt.speedup_vs_baseline.expect("modeled baseline => Some");
        assert!((sp - 1.0 / receipt.total_product_time_s).abs() < 1e-9);

        // JSON round-trips.
        let json = serde_json::to_string(&receipt).unwrap();
        let back: crate::receipt::SpecReceipt = serde_json::from_str(&json).unwrap();
        assert_eq!(
            serde_json::to_value(&back).unwrap(),
            serde_json::to_value(&receipt).unwrap()
        );
    }

    #[test]
    fn no_repair_means_zero_repair_cost() {
        // Only delivery/preview tiles: nothing is repaired.
        let pipe = pipeline("cx_synth_render_norepair");
        let tiles = vec![
            RenderTile { id: 10, residual: 0.00 },
            RenderTile { id: 11, residual: 0.05 },
        ];
        let (_outs, receipt) = pipe.run_batch(
            &tiles,
            0.5,
            BaselineSource::Modeled,
            Evidence::Synthetic,
            Details::new(),
        );
        assert_eq!(receipt.repair_cost_s, 0.0, "no repaired tile => exactly zero repair cost");
        assert_eq!(receipt.repaired_fraction, 0.0);
        // Both drafts reused.
        assert!((receipt.accepted_fraction - 1.0).abs() < 1e-9);
    }
}
