//! Integration test: the `synth_render` adapter implements all four traits and
//! aggregates to a sane render-shaped `SpecReceipt` via the public API.

use cx_spec_engine::adapters::synth_render::{self, RenderTile, DRAFT_SPP, REFERENCE_SPP};
use cx_spec_engine::{BaselineSource, Details, Evidence, Modality, QualityTier};

#[test]
fn render_batch_aggregates_worst_wins_and_reference_repair() {
    let pipe = synth_render::pipeline("cx_render_it");
    // Delivery, Preview, Fail (see the adapter's residual->tier mapping).
    let tiles = vec![
        RenderTile { id: 0, residual: 0.00 },
        RenderTile { id: 1, residual: 0.05 },
        RenderTile { id: 2, residual: 0.40 },
    ];
    let (outputs, receipt) = pipe.run_batch(
        &tiles,
        2.0,
        BaselineSource::Modeled,
        Evidence::Synthetic,
        Details::new(),
    );

    assert_eq!(receipt.modality, Modality::render());
    assert_eq!(receipt.modality.as_str(), "render");
    assert_eq!(receipt.units, 3);
    assert!(!receipt.exact);

    // A delivery/preview mix reduces worst-wins to Preview.
    assert_eq!(receipt.quality_tier, QualityTier::Preview);

    // repair_cost_s > 0 iff a tile was repaired (here: the failed tile).
    assert!(receipt.repair_cost_s > 0.0);
    assert!((receipt.repaired_fraction - 1.0 / 3.0).abs() < 1e-9);

    // The repaired tile is at reference spp; the accepted tiles ship the draft.
    assert_eq!(outputs[0].spp, DRAFT_SPP);
    assert_eq!(outputs[1].spp, DRAFT_SPP);
    assert_eq!(outputs[2].spp, REFERENCE_SPP);

    // accepted_fraction is a real ratio in [0,1].
    assert!((0.0..=1.0).contains(&receipt.accepted_fraction));
    assert!((receipt.accepted_fraction - 2.0 / 3.0).abs() < 1e-9);

    // speedup = baseline / spec.
    let sp = receipt.speedup_vs_baseline.expect("modeled baseline => Some");
    assert!((sp - 2.0 / receipt.total_product_time_s).abs() < 1e-9);
}
