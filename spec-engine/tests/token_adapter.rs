//! Integration test: the `synth_token` adapter exercises the PARTIAL-acceptance
//! path (m of k) that render never does, proving the partial-accept math and the
//! `Some`/`None` speedup honesty rule via the public API.

use cx_spec_engine::adapters::synth_token::{self, window_with_match};
use cx_spec_engine::{BaselineSource, Details, Evidence, Modality, QualityTier};

#[test]
fn token_batch_partial_acceptance_is_sum_m_over_sum_k() {
    let pipe = synth_token::pipeline("cx_token_it");
    // m/k = 8/8, 5/8, 3/8  => Σm/Σk = 16/24.
    let windows = vec![
        window_with_match(0, 3, 8, 8),
        window_with_match(1, 11, 8, 5),
        window_with_match(2, 21, 8, 3),
    ];

    // Under a Modeled baseline the speedup is present.
    let (outputs, receipt) = pipe.run_batch(
        &windows,
        0.05,
        BaselineSource::Modeled,
        Evidence::Synthetic,
        Details::new(),
    );

    assert_eq!(receipt.modality, Modality::token());
    assert!(receipt.exact, "token output is lossless");
    // Every delivered run equals the target continuation.
    for (out, win) in outputs.iter().zip(windows.iter()) {
        assert_eq!(out, &win.truth);
    }

    // The partial-accept assertion: Σm/Σk exactly.
    assert!((receipt.accepted_fraction - 16.0 / 24.0).abs() < 1e-9);
    assert!(receipt.accepted_fraction > 0.0 && receipt.accepted_fraction < 1.0);

    // worst-wins over [Delivery(8/8), Preview(5/8), Fail(3/8)] => Fail.
    assert_eq!(receipt.quality_tier, QualityTier::Fail);

    let sp = receipt
        .speedup_vs_baseline
        .expect("modeled baseline => Some");
    assert!((sp - 0.05 / receipt.total_product_time_s).abs() < 1e-9);

    // Under Absent baseline the SAME batch reports None (never fabricated).
    let (_outs2, receipt_absent) = pipe.run_batch(
        &windows,
        0.0,
        BaselineSource::Absent,
        Evidence::Synthetic,
        Details::new(),
    );
    assert!(receipt_absent.speedup_vs_baseline.is_none());
}
