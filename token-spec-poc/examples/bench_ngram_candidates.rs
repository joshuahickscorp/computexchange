//! Regression benchmark for bounded contextual candidate ranking.
//!
//! Compares the exact previous indexed policy (`new_recent_only`) with the
//! default ranked policy on (1) an unambiguous periodic fast path and (2) many
//! short, branching prompt-lookup requests. Run with:
//! `cargo run --release --example bench_ngram_candidates`.

use std::time::Instant;
use token_spec_poc::{try_run_spec_decode, MockTarget, NgramDraft, SpecUnit};

#[derive(Clone, Copy)]
struct Sample {
    ns: u128,
    calls: usize,
    accepted_fraction: f64,
}

fn run_one(mut draft: NgramDraft, unit: &SpecUnit, truth: &[u32], k: usize) -> Sample {
    let mut target = MockTarget::new(unit.prompt.len(), truth.to_vec());
    let start = Instant::now();
    let outcome = try_run_spec_decode(unit, &mut draft, &mut target, k, "candidate-bench")
        .expect("benchmark input is valid");
    let ns = start.elapsed().as_nanos();
    assert!(outcome.receipt.exact);
    assert_eq!(outcome.output, truth);
    Sample {
        ns,
        calls: outcome.receipt.meta.target_calls,
        accepted_fraction: outcome.receipt.accepted_fraction,
    }
}

fn run_short_batch(ranked: bool, unit: &SpecUnit, truth: &[u32], requests: usize) -> Sample {
    let start = Instant::now();
    let mut calls = 0;
    let mut accepted_fraction = 0.0;
    for _ in 0..requests {
        let draft = if ranked {
            NgramDraft::new(2, 16)
        } else {
            NgramDraft::new_recent_only(2, 16)
        };
        let sample = run_one(draft, unit, truth, 16);
        calls += sample.calls;
        accepted_fraction = sample.accepted_fraction;
    }
    Sample {
        ns: start.elapsed().as_nanos(),
        calls,
        accepted_fraction,
    }
}

fn median(mut values: Vec<u128>) -> u128 {
    values.sort_unstable();
    values[values.len() / 2]
}

fn main() {
    const TOKENS: usize = 32_768;
    const REPS: usize = 9;
    const SHORT_REQUESTS: usize = 4_096;

    let periodic: Vec<u32> = (0..TOKENS).map(|i| (i % 31) as u32).collect();
    let periodic_unit = SpecUnit {
        unit_id: "periodic-fast-path".into(),
        modality: "token".into(),
        prompt: periodic[..3].to_vec(),
        max_new_tokens: periodic.len() - 3,
        eos: u32::MAX,
    };
    let periodic_truth = &periodic[3..];

    let prompt = vec![10, 1, 2, 100, 101, 20, 1, 2, 200, 201, 10, 1, 2];
    let cycle = [100, 101, 20, 1, 2, 200, 201, 10, 1, 2];
    let branch_truth: Vec<u32> = cycle.into_iter().cycle().take(16).collect();
    let branch_unit = SpecUnit {
        unit_id: "branching-short-request".into(),
        modality: "token".into(),
        prompt,
        max_new_tokens: branch_truth.len(),
        eos: u32::MAX,
    };

    // Warm both code paths before alternating order to reduce thermal bias.
    let _ = run_one(
        NgramDraft::new_recent_only(3, 64),
        &periodic_unit,
        periodic_truth,
        16,
    );
    let _ = run_one(NgramDraft::new(3, 64), &periodic_unit, periodic_truth, 16);
    let _ = run_short_batch(false, &branch_unit, &branch_truth, 128);
    let _ = run_short_batch(true, &branch_unit, &branch_truth, 128);

    let mut periodic_recent_ns = Vec::with_capacity(REPS);
    let mut periodic_ranked_ns = Vec::with_capacity(REPS);
    let mut branch_recent_ns = Vec::with_capacity(REPS);
    let mut branch_ranked_ns = Vec::with_capacity(REPS);
    let mut periodic_recent = Sample {
        ns: 0,
        calls: 0,
        accepted_fraction: 0.0,
    };
    let mut periodic_ranked = periodic_recent;
    let mut branch_recent = periodic_recent;
    let mut branch_ranked = periodic_recent;

    for rep in 0..REPS {
        if rep % 2 == 0 {
            periodic_recent = run_one(
                NgramDraft::new_recent_only(3, 64),
                &periodic_unit,
                periodic_truth,
                16,
            );
            periodic_ranked = run_one(NgramDraft::new(3, 64), &periodic_unit, periodic_truth, 16);
            branch_recent = run_short_batch(false, &branch_unit, &branch_truth, SHORT_REQUESTS);
            branch_ranked = run_short_batch(true, &branch_unit, &branch_truth, SHORT_REQUESTS);
        } else {
            periodic_ranked = run_one(NgramDraft::new(3, 64), &periodic_unit, periodic_truth, 16);
            periodic_recent = run_one(
                NgramDraft::new_recent_only(3, 64),
                &periodic_unit,
                periodic_truth,
                16,
            );
            branch_ranked = run_short_batch(true, &branch_unit, &branch_truth, SHORT_REQUESTS);
            branch_recent = run_short_batch(false, &branch_unit, &branch_truth, SHORT_REQUESTS);
        }
        periodic_recent_ns.push(periodic_recent.ns);
        periodic_ranked_ns.push(periodic_ranked.ns);
        branch_recent_ns.push(branch_recent.ns);
        branch_ranked_ns.push(branch_ranked.ns);
    }

    let periodic_recent_median = median(periodic_recent_ns);
    let periodic_ranked_median = median(periodic_ranked_ns);
    let branch_recent_median = median(branch_recent_ns);
    let branch_ranked_median = median(branch_ranked_ns);
    println!(
        "{}",
        serde_json::json!({
            "benchmark": "token_ngram_contextual_candidates",
            "reps": REPS,
            "exact": true,
            "periodic_fast_path": {
                "tokens": periodic_truth.len(),
                "recent_only_median_ms": periodic_recent_median as f64 / 1e6,
                "ranked_median_ms": periodic_ranked_median as f64 / 1e6,
                "recent_over_ranked_x": periodic_recent_median as f64 / periodic_ranked_median as f64,
                "recent_target_calls": periodic_recent.calls,
                "ranked_target_calls": periodic_ranked.calls,
                "ranked_accepted_fraction": periodic_ranked.accepted_fraction,
            },
            "branching_short_requests": {
                "requests": SHORT_REQUESTS,
                "tokens_per_request": branch_truth.len(),
                "recent_only_median_ms": branch_recent_median as f64 / 1e6,
                "ranked_median_ms": branch_ranked_median as f64 / 1e6,
                "speedup_x": branch_recent_median as f64 / branch_ranked_median as f64,
                "recent_target_calls": branch_recent.calls,
                "ranked_target_calls": branch_ranked.calls,
                "target_call_reduction_x": branch_recent.calls as f64 / branch_ranked.calls as f64,
                "recent_accepted_fraction": branch_recent.accepted_fraction,
                "ranked_accepted_fraction": branch_ranked.accepted_fraction,
            }
        })
    );
}
