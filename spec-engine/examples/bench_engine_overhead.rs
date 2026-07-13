//! Measure the checked spec-engine orchestration floor with callbacks that do
//! almost no work. This isolates engine bookkeeping from model/render time.
//!
//! Usage:
//!   cargo run --release --example bench_engine_overhead -- [units] [reps]

use std::hint::black_box;
use std::time::Instant;

use cx_spec_engine::{
    Acceptance, AcceptancePolicy, BaselineSource, Details, DraftProducer, Evidence, Modality,
    QualityTier, RepairPolicy, SpecPipeline, SpecUnit, Verification, Verifier,
};

struct Unit(u32);

impl SpecUnit for Unit {
    type Draft = u32;
    type Output = u32;
    type Score = ();

    fn unit_id(&self) -> String {
        self.0.to_string()
    }

    fn modality(&self) -> Modality {
        Modality::token()
    }
}

struct Producer;

impl DraftProducer<Unit> for Producer {
    fn draft(&self, unit: &Unit) -> u32 {
        unit.0
    }
}

struct ExactVerifier;

impl Verifier<Unit> for ExactVerifier {
    fn verify(&self, _unit: &Unit, _draft: &u32) -> Verification<Unit> {
        Verification {
            score: (),
            truth: None,
        }
    }
}

struct AcceptAll;

impl AcceptancePolicy<Unit> for AcceptAll {
    fn decide(&self, _unit: &Unit, _verification: &Verification<Unit>) -> Acceptance {
        Acceptance {
            drafted: 1.0,
            accepted: 1.0,
            tier: QualityTier::Delivery,
            exact: true,
        }
    }
}

struct Passthrough;

impl RepairPolicy<Unit> for Passthrough {
    fn accept(&self, _unit: &Unit, draft: u32) -> u32 {
        draft
    }

    fn repair(
        &self,
        _unit: &Unit,
        draft: u32,
        _verification: Verification<Unit>,
        _acceptance: &Acceptance,
    ) -> u32 {
        draft
    }
}

fn median(samples: &mut [f64]) -> f64 {
    samples.sort_by(f64::total_cmp);
    samples[samples.len() / 2]
}

fn main() {
    let mut args = std::env::args().skip(1);
    let units = args
        .next()
        .map(|value| value.parse::<usize>().expect("units must be an integer"))
        .unwrap_or(100_000);
    let reps = args
        .next()
        .map(|value| value.parse::<usize>().expect("reps must be an integer"))
        .unwrap_or(9);
    assert!((1..=cx_spec_engine::MAX_SPEC_BATCH_UNITS).contains(&units));
    assert!(reps > 0);

    let input: Vec<Unit> = (0..units as u32).map(Unit).collect();
    let pipeline = SpecPipeline::token(
        "native-overhead-bench",
        Producer,
        ExactVerifier,
        AcceptAll,
        Passthrough,
    );

    // Allocator/code warm-up is deliberately outside the samples.
    let warm = pipeline
        .try_run_batch(
            &input,
            0.0,
            BaselineSource::Absent,
            Evidence::Synthetic,
            Details::new(),
        )
        .expect("warm checked batch");
    black_box(warm);

    let mut samples_ms = Vec::with_capacity(reps);
    for _ in 0..reps {
        let started = Instant::now();
        let result = pipeline
            .try_run_batch(
                &input,
                0.0,
                BaselineSource::Absent,
                Evidence::Synthetic,
                Details::new(),
            )
            .expect("checked batch");
        samples_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
        black_box(result);
    }
    let median_ms = median(&mut samples_ms);
    println!(
        "{{\"kind\":\"spec_engine_overhead\",\"units\":{units},\"reps\":{reps},\"median_ms\":{median_ms:.6},\"units_per_second\":{:.3},\"samples_ms\":{:?}}}",
        units as f64 * 1_000.0 / median_ms,
        samples_ms,
    );
}
