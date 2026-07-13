//! Dependency-free regression microbenchmark for the incremental n-gram index.
//! It compares the previous copy+reverse-scan proposer with `NgramDraft` over the
//! same lossless loop and target. Run: `cargo run --release --example bench_ngram`.

use std::time::Instant;
use token_spec_poc::{try_run_spec_decode, DraftProducer, MockTarget, NgramDraft, SpecUnit};

struct LegacyNgram {
    order: usize,
    max_span: usize,
    history: Vec<u32>,
}

impl LegacyNgram {
    fn new(order: usize, max_span: usize) -> Self {
        Self {
            order,
            max_span,
            history: Vec::new(),
        }
    }
}

impl DraftProducer for LegacyNgram {
    fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32> {
        self.history = ctx.to_vec();
        let k = k.min(self.max_span);
        if k == 0 || self.history.len() <= self.order {
            return Vec::new();
        }
        let n = self.history.len();
        let key = &self.history[n - self.order..];
        let last_start = n - self.order;
        for start in (0..last_start).rev() {
            if &self.history[start..start + self.order] == key {
                let cont = start + self.order;
                return self.history[cont..(cont + k).min(n)].to_vec();
            }
        }
        Vec::new()
    }

    fn name(&self) -> &str {
        "legacy_copy_reverse_scan"
    }
}

fn run<D: DraftProducer>(mut draft: D, unit: &SpecUnit, truth: &[u32]) -> (u128, f64) {
    let mut target = MockTarget::new(unit.prompt.len(), truth.to_vec());
    let start = Instant::now();
    let outcome = try_run_spec_decode(unit, &mut draft, &mut target, 16, "ngram-bench").unwrap();
    let elapsed = start.elapsed().as_nanos();
    assert!(outcome.receipt.exact);
    (elapsed, outcome.receipt.accepted_fraction)
}

fn median(mut values: Vec<u128>) -> u128 {
    values.sort_unstable();
    values[values.len() / 2]
}

fn main() {
    const TOKENS: usize = 32_768;
    const ORDER: usize = 3;
    const REPS: usize = 7;
    let stream: Vec<u32> = (0..TOKENS).map(|i| (i % 31) as u32).collect();
    let prompt = stream[..ORDER].to_vec();
    let truth = &stream[ORDER..];
    let unit = SpecUnit {
        unit_id: "periodic-32k".into(),
        modality: "token".into(),
        prompt,
        max_new_tokens: truth.len(),
        eos: u32::MAX,
    };

    // Warm both implementations, then alternate order to reduce thermal bias.
    let _ = run(LegacyNgram::new(ORDER, 64), &unit, truth);
    let _ = run(NgramDraft::new(ORDER, 64), &unit, truth);
    let mut legacy = Vec::with_capacity(REPS);
    let mut indexed = Vec::with_capacity(REPS);
    let mut acceptance = 0.0;
    for rep in 0..REPS {
        if rep % 2 == 0 {
            let (a, _) = run(LegacyNgram::new(ORDER, 64), &unit, truth);
            let (b, acc) = run(NgramDraft::new(ORDER, 64), &unit, truth);
            legacy.push(a);
            indexed.push(b);
            acceptance = acc;
        } else {
            let (b, acc) = run(NgramDraft::new(ORDER, 64), &unit, truth);
            let (a, _) = run(LegacyNgram::new(ORDER, 64), &unit, truth);
            indexed.push(b);
            legacy.push(a);
            acceptance = acc;
        }
    }
    let legacy_ns = median(legacy);
    let indexed_ns = median(indexed);
    println!(
        "{}",
        serde_json::json!({
            "benchmark": "token_ngram_incremental_index",
            "tokens": TOKENS,
            "reps": REPS,
            "legacy_median_ms": legacy_ns as f64 / 1e6,
            "indexed_median_ms": indexed_ns as f64 / 1e6,
            "speedup_x": legacy_ns as f64 / indexed_ns as f64,
            "accepted_fraction": acceptance,
            "exact": true
        })
    );
}
