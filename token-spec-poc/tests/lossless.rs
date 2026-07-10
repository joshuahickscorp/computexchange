//! End-to-end losslessness + accounting invariants for the CX-owned token lane.
//!
//! The claim these tests defend: for ANY draft producer and ANY target greedy
//! stream, `run_spec_decode`'s output equals the target's plain greedy decode
//! token-for-token. That is the property real vLLM spec-decode FAILED on in this
//! repo (29/32, 41/64 mismatched). Here it is a machine-checked invariant.

use token_spec_poc::{greedy_decode, run_spec_decode, DraftProducer, MockTarget, NgramDraft, SpecUnit};

fn build(prompt: Vec<u32>, truth: Vec<u32>, eos: u32) -> (SpecUnit, MockTarget) {
    let plen = prompt.len();
    let u = SpecUnit {
        unit_id: "e2e".into(),
        modality: "token".into(),
        prompt,
        max_new_tokens: truth.len(),
        eos,
    };
    (u, MockTarget::new(plen, truth))
}

/// Deterministic pseudo-random stream so the property is exercised on many shapes.
fn xorshift_stream(seed: u64, n: usize, modulo: u32) -> Vec<u32> {
    let mut x = seed | 1;
    (0..n)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            (x % modulo as u64) as u32
        })
        .collect()
}

#[test]
fn lossless_property_over_many_streams_and_k() {
    // Sweep seeds (stream shapes), draft windows k, and n-gram orders. Every
    // combination must reproduce the target greedy stream exactly.
    for seed in [1u64, 7, 42, 1234, 99991] {
        for &modulo in &[2u32, 5, 17, 64] {
            let truth = xorshift_stream(seed, 60, modulo);
            let prompt = vec![truth[0], truth.get(1).copied().unwrap_or(0)];
            for &k in &[1usize, 4, 8, 16] {
                for &order in &[1usize, 2, 3] {
                    let (u, mut target) = build(prompt.clone(), truth.clone(), u32::MAX);
                    let mut draft = NgramDraft::new(order, 64);
                    let out = run_spec_decode(&u, &mut draft, &mut target, k, "test");
                    // Oracle: plain greedy of the same target.
                    let (u2, mut target2) = build(prompt.clone(), truth.clone(), u32::MAX);
                    let oracle = greedy_decode(&u2, &mut target2);
                    assert_eq!(
                        out.output, oracle,
                        "lossless FAILED seed={seed} mod={modulo} k={k} order={order}"
                    );
                    assert!(out.receipt.exact);
                    // Accounting invariants.
                    assert_eq!(
                        out.receipt.accepted_units + out.receipt.rejected_units,
                        out.receipt.attempted_units
                    );
                    assert!(out.receipt.accepted_fraction >= 0.0 && out.receipt.accepted_fraction <= 1.0);
                    // Ceiling reduction is always >= 1 (each round emits >= 1 token).
                    assert!(out.receipt.meta.target_call_reduction_x >= 1.0 - 1e-9);
                }
            }
        }
    }
}

#[test]
fn acceptance_rises_with_redundancy() {
    // A period-p stream should give higher acceptance than a high-entropy one.
    let periodic: Vec<u32> = (0..120).map(|i| (i % 3) as u32).collect();
    let random = xorshift_stream(31, 120, 251);

    let acc = |stream: Vec<u32>| -> f64 {
        let prompt = stream[..4].to_vec();
        let truth = stream[4..].to_vec();
        let u = SpecUnit {
            unit_id: "a".into(),
            modality: "token".into(),
            prompt,
            max_new_tokens: truth.len(),
            eos: u32::MAX,
        };
        let mut target = MockTarget::new(4, truth);
        let mut draft = NgramDraft::new(3, 64);
        run_spec_decode(&u, &mut draft, &mut target, 16, "test")
            .receipt
            .accepted_fraction
    };

    let a_periodic = acc(periodic);
    let a_random = acc(random);
    assert!(
        a_periodic > a_random,
        "periodic acceptance {a_periodic} should exceed random {a_random}"
    );
    assert!(a_periodic > 0.8, "period-3 acceptance {a_periodic} unexpectedly low");
}

#[test]
fn empty_and_degenerate_inputs_are_safe() {
    // max_new_tokens smaller than a draft window; single-token truth; etc.
    let (u, mut target) = build(vec![9u32], vec![1u32], u32::MAX);
    let mut draft = NgramDraft::new(2, 64);
    let out = run_spec_decode(&u, &mut draft, &mut target, 16, "test");
    assert_eq!(out.output, vec![1u32]);
    assert!(out.receipt.exact);
}

/// A pathological drafter that returns overlong garbage must not corrupt output
/// or the accounting.
#[test]
fn overlong_wrong_draft_is_safe_and_lossless() {
    struct Flood;
    impl DraftProducer for Flood {
        fn propose(&mut self, _ctx: &[u32], _k: usize) -> Vec<u32> {
            vec![u32::MAX - 1; 1000]
        }
        fn name(&self) -> &str {
            "flood"
        }
    }
    let (u, mut target) = build(vec![0u32], (1u32..=20).collect(), u32::MAX);
    let mut draft = Flood;
    let out = run_spec_decode(&u, &mut draft, &mut target, 32, "test");
    assert_eq!(out.output, (1u32..=20).collect::<Vec<_>>());
    assert!(out.receipt.exact);
    assert_eq!(out.receipt.accepted_units, 0);
}
