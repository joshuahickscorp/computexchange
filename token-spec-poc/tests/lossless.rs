//! End-to-end losslessness + accounting invariants for the CX-owned token lane.
//!
//! The claim these tests defend: for ANY draft producer and ANY target greedy
//! stream, `run_spec_decode`'s output equals the target's plain greedy decode
//! token-for-token. That is the property real vLLM spec-decode FAILED on in this
//! repo (29/32, 41/64 mismatched). Here it is a machine-checked invariant.

use token_spec_poc::{
    greedy_decode, run_spec_decode, try_greedy_decode, try_run_spec_decode, DraftProducer,
    MockTarget, NgramDraft, SpecDecodeError, SpecUnit, TargetModel, MAX_NEW_TOKENS,
    MAX_NGRAM_ORDER, MAX_NGRAM_SPAN,
};

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
                    assert!(
                        out.receipt.accepted_fraction >= 0.0
                            && out.receipt.accepted_fraction <= 1.0
                    );
                    assert_eq!(out.receipt.units, out.receipt.meta.rounds);
                    assert!(out.receipt.fallback_units <= out.receipt.units);
                    assert!(
                        (out.receipt.attempted_fraction + out.receipt.fallback_fraction - 1.0)
                            .abs()
                            <= 1e-6
                    );
                    let charged = out.receipt.draft_s
                        + out.receipt.verify_s
                        + out.receipt.repair_s
                        + out.receipt.overhead_s;
                    assert!((out.receipt.speculative_s - charged).abs() <= 1e-9);
                    if out.receipt.quality_tier == "delivery" {
                        assert_eq!(out.receipt.fallback_units, 0);
                    }
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
    assert!(
        a_periodic > 0.8,
        "period-3 acceptance {a_periodic} unexpectedly low"
    );
}

#[test]
fn ngram_overlap_copy_fills_the_requested_window() {
    let history = vec![1, 2, 3, 1, 2, 3];
    let mut draft = NgramDraft::new(2, 16);
    draft.reset(&history);

    assert_eq!(
        draft.propose(&history, 8),
        vec![1, 2, 3, 1, 2, 3, 1, 2],
        "the matched suffix should remain a bounded overlapping copy source"
    );
}

#[test]
fn ngram_overlap_copy_obeys_call_and_configuration_bounds() {
    let history = vec![7, 7];
    let mut draft = NgramDraft::new(1, 4);
    draft.reset(&history);

    assert_eq!(draft.propose(&history, 0), Vec::<u32>::new());
    assert_eq!(draft.propose(&history, 2), vec![7, 7]);
    assert_eq!(draft.propose(&history, 64), vec![7; 4]);
}

#[test]
fn ngram_direct_propose_and_context_replacement_rebuild_state() {
    let first = vec![1, 2, 3, 1, 2, 3];
    let mut draft = NgramDraft::new(2, 8);
    assert_eq!(
        draft.propose(&first, 6),
        vec![1, 2, 3, 1, 2, 3],
        "a direct caller should not need an implicit prior reset"
    );

    let replaced = vec![9, 8, 9, 8, 9, 8];
    assert_eq!(
        draft.propose(&replaced, 5),
        vec![9, 8, 9, 8, 9],
        "a replaced/rolled-back context must not draft from stale indexed history"
    );
}

#[test]
fn ngram_overlap_copy_collapses_target_calls_on_short_periods() {
    let stream: Vec<u32> = (0..99).map(|i| (i % 3) as u32).collect();
    let (unit, mut target) = build(stream[..3].to_vec(), stream[3..].to_vec(), u32::MAX);
    let mut draft = NgramDraft::new(3, 16);
    let outcome = run_spec_decode(&unit, &mut draft, &mut target, 16, "overlap-call-gate");

    assert_eq!(outcome.output, stream[3..]);
    assert!(outcome.receipt.exact);
    assert!(
        outcome.receipt.meta.target_calls <= 9,
        "96 periodic output tokens should need at most nine target verifies, got {}",
        outcome.receipt.meta.target_calls
    );
    assert!(
        outcome.receipt.meta.target_call_reduction_x >= 10.0,
        "overlap drafting should expose a double-digit target-call ceiling, got {}x",
        outcome.receipt.meta.target_call_reduction_x
    );
}

#[test]
fn rolling_ngram_recent_only_matches_simple_backward_reference() {
    fn reference(ctx: &[u32], order: usize, k: usize) -> Vec<u32> {
        if k == 0 || ctx.len() <= order {
            return Vec::new();
        }
        let n = ctx.len();
        let key = &ctx[n - order..];
        let last_start = n - order;
        let Some(start) = (0..last_start)
            .rev()
            .find(|&start| &ctx[start..start + order] == key)
        else {
            return Vec::new();
        };
        let continuation = start + order;
        let period = n - continuation;
        (0..k)
            .map(|offset| ctx[continuation + (offset % period)])
            .collect()
    }

    for seed in [1u64, 7, 42, 9_991] {
        for modulo in [2u32, 3, 7, 31] {
            let stream = xorshift_stream(seed, 96, modulo);
            for len in 1..=stream.len() {
                let ctx = &stream[..len];
                for order in 1..=6 {
                    for k in [0usize, 1, 3, 8, 17, 32] {
                        let mut indexed = NgramDraft::new_recent_only(order, 32);
                        assert_eq!(
                            indexed.propose(ctx, k),
                            reference(ctx, order, k),
                            "indexed/reference mismatch seed={seed} modulo={modulo} len={len} order={order} k={k}"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn contextual_candidate_ranking_beats_recency_on_a_branching_prompt() {
    // Two demonstrations share the exact order-2 key [1, 2] but have different
    // answers. The final query repeats the older A context, while recency alone
    // chooses the newer B answer.
    let prompt = vec![10, 1, 2, 100, 101, 20, 1, 2, 200, 201, 10, 1, 2];
    let cycle = [100, 101, 20, 1, 2, 200, 201, 10, 1, 2];
    let truth: Vec<u32> = cycle.into_iter().cycle().take(16).collect();

    let mut ranked = NgramDraft::new(2, 16);
    ranked.reset(&prompt);
    assert_eq!(ranked.propose(&prompt, 8), truth[..8]);

    let mut recent = NgramDraft::new_recent_only(2, 16);
    recent.reset(&prompt);
    assert_eq!(recent.propose(&prompt, 2), vec![200, 201]);

    let unit = SpecUnit {
        unit_id: "contextual-branch".into(),
        modality: "token".into(),
        prompt: prompt.clone(),
        max_new_tokens: truth.len(),
        eos: u32::MAX,
    };
    let mut ranked_target = MockTarget::new(prompt.len(), truth.clone());
    let ranked_out = run_spec_decode(&unit, &mut ranked, &mut ranked_target, 16, "ranked");
    let mut recent_target = MockTarget::new(prompt.len(), truth.clone());
    let recent_out = run_spec_decode(&unit, &mut recent, &mut recent_target, 16, "recent");

    assert_eq!(ranked_out.output, truth);
    assert_eq!(recent_out.output, ranked_out.output);
    assert_eq!(ranked_out.receipt.meta.target_calls, 1);
    assert_eq!(recent_out.receipt.meta.target_calls, 2);
    assert!(ranked_out.receipt.accepted_fraction > recent_out.receipt.accepted_fraction);
}

#[test]
fn contextual_candidate_ranking_matches_bounded_simple_reference() {
    fn preceding_score(ctx: &[u32], cont: usize, n: usize, order: usize) -> usize {
        let mut candidate = cont - order;
        let mut current = n - order;
        let limit = 64.min(candidate).min(current);
        let mut score = 0;
        while score < limit {
            candidate -= 1;
            current -= 1;
            if ctx[candidate] != ctx[current] {
                break;
            }
            score += 1;
        }
        score
    }

    fn reference(ctx: &[u32], order: usize, k: usize) -> Vec<u32> {
        if k == 0 || ctx.len() <= order {
            return Vec::new();
        }
        let n = ctx.len();
        let key = &ctx[n - order..];
        let mut matches: Vec<usize> = (0..n - order)
            .filter(|&start| &ctx[start..start + order] == key)
            .map(|start| start + order)
            .collect();
        let Some(primary) = matches.pop() else {
            return Vec::new();
        };
        let alternate = matches
            .into_iter()
            .rev()
            .find(|&cont| ctx[cont] != ctx[primary]);
        let selected = alternate
            .filter(|&cont| {
                preceding_score(ctx, cont, n, order) > preceding_score(ctx, primary, n, order)
            })
            .unwrap_or(primary);
        let period = n - selected;
        (0..k)
            .map(|offset| ctx[selected + (offset % period)])
            .collect()
    }

    // Low moduli create dense ambiguity; larger ones exercise the sparse/common
    // path. The indexed implementation must match the deterministic O(n) oracle.
    for seed in [1u64, 7, 42, 9_991, u32::MAX as u64] {
        for modulo in [2u32, 3, 5, 17, 251] {
            let stream = xorshift_stream(seed, 128, modulo);
            for len in 1..=stream.len() {
                let ctx = &stream[..len];
                for order in 1..=6 {
                    for k in [0usize, 1, 3, 8, 17, 32] {
                        let mut indexed = NgramDraft::new(order, 32);
                        assert_eq!(
                            indexed.propose(ctx, k),
                            reference(ctx, order, k),
                            "ranked/reference mismatch seed={seed} modulo={modulo} len={len} order={order} k={k}"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn empty_and_degenerate_inputs_are_safe() {
    // max_new_tokens smaller than a draft window; single-token truth; etc.
    let (u, mut target) = build(vec![9u32], vec![1u32], u32::MAX);
    let mut draft = NgramDraft::new(2, 64);
    let out = run_spec_decode(&u, &mut draft, &mut target, 16, "test");
    assert_eq!(out.output, vec![1u32]);
    assert!(out.receipt.exact);

    let (empty, mut empty_target) = build(vec![9u32], vec![], u32::MAX);
    let mut empty_draft = NgramDraft::new(2, 64);
    assert!(matches!(
        try_run_spec_decode(&empty, &mut empty_draft, &mut empty_target, 16, "test"),
        Err(SpecDecodeError::InvalidInput(_))
    ));
}

#[test]
fn always_empty_drafter_is_accounted_as_full_fallback() {
    struct Empty;
    impl DraftProducer for Empty {
        fn propose(&mut self, _ctx: &[u32], _k: usize) -> Vec<u32> {
            Vec::new()
        }
        fn name(&self) -> &str {
            "always-empty"
        }
    }

    let truth = vec![1, 2, 3, 4, 5, 6];
    let (unit, mut target) = build(vec![9], truth.clone(), u32::MAX);
    let mut draft = Empty;
    let out = try_run_spec_decode(&unit, &mut draft, &mut target, 8, "fallback-test").unwrap();
    assert_eq!(out.output, truth);
    assert!(out.receipt.exact);
    assert_eq!(out.receipt.units, truth.len());
    assert_eq!(out.receipt.fallback_units, out.receipt.units);
    assert_eq!(out.receipt.attempted_units, 0);
    assert_eq!(out.receipt.accepted_units, 0);
    assert_eq!(out.receipt.rejected_units, 0);
    assert_eq!(out.receipt.attempted_fraction, 0.0);
    assert_eq!(out.receipt.fallback_fraction, 1.0);
    assert_eq!(out.receipt.fallback_s, 0.0);
    assert_eq!(out.receipt.quality_tier, "fail");
    assert!(!out.receipt.quality_gate);
}

#[test]
fn any_fallback_round_prevents_delivery_coverage() {
    struct ExactUntilFinal {
        prompt_len: usize,
        truth: Vec<u32>,
    }
    impl DraftProducer for ExactUntilFinal {
        fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32> {
            let offset = ctx.len() - self.prompt_len;
            self.truth[offset..].iter().copied().take(k).collect()
        }
        fn name(&self) -> &str {
            "exact-until-final"
        }
    }

    let truth = vec![1, 2, 3, 4];
    let (unit, mut target) = build(vec![9], truth.clone(), u32::MAX);
    let mut draft = ExactUntilFinal {
        prompt_len: unit.prompt.len(),
        truth,
    };
    let out = try_run_spec_decode(&unit, &mut draft, &mut target, 2, "coverage-test").unwrap();
    assert_eq!(out.receipt.accepted_units, out.receipt.attempted_units);
    assert_eq!(out.receipt.fallback_units, 1);
    assert_eq!(out.receipt.units, 2);
    assert_eq!(out.receipt.attempted_fraction, 0.5);
    assert_eq!(out.receipt.fallback_fraction, 0.5);
    assert_eq!(out.receipt.quality_tier, "preview");
    assert!(out.receipt.quality_gate);
}

#[test]
fn finalize_work_is_charged_but_counterfactual_baseline_is_excluded() {
    use std::thread;
    use std::time::Duration;

    struct DelayedNameDraft;
    impl DraftProducer for DelayedNameDraft {
        fn propose(&mut self, _ctx: &[u32], k: usize) -> Vec<u32> {
            vec![u32::MAX - 1; k]
        }
        fn name(&self) -> &str {
            // `name().to_string()` is product-side receipt assembly in finalize.
            thread::sleep(Duration::from_millis(10));
            "delayed-name"
        }
    }

    struct DelayedBaselineTarget {
        prompt_len: usize,
        truth: Vec<u32>,
        resets: usize,
    }
    impl TargetModel for DelayedBaselineTarget {
        fn reset(&mut self, _prompt: &[u32]) {
            self.resets += 1;
            if self.resets == 2 {
                // The second reset begins the counterfactual greedy baseline.
                thread::sleep(Duration::from_millis(100));
            }
        }
        fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32> {
            (from..=tokens.len())
                .map(|len| self.truth.get(len - self.prompt_len).copied().unwrap_or(0))
                .collect()
        }
        fn name(&self) -> &str {
            "delayed-baseline"
        }
    }

    let truth = vec![1, 2, 3, 4, 5, 6, 7, 8];
    let unit = SpecUnit {
        unit_id: "timing-coherence".into(),
        modality: "token".into(),
        prompt: vec![9],
        max_new_tokens: truth.len(),
        eos: u32::MAX,
    };
    let mut target = DelayedBaselineTarget {
        prompt_len: unit.prompt.len(),
        truth,
        resets: 0,
    };
    let mut draft = DelayedNameDraft;
    let out = try_run_spec_decode(&unit, &mut draft, &mut target, 4, "timing-test").unwrap();
    let receipt = out.receipt;

    assert!(
        receipt.baseline_s >= 0.09,
        "baseline delay was not measured"
    );
    assert!(
        receipt.overhead_s >= 0.009,
        "finalize name/assembly work was not charged: {}",
        receipt.overhead_s
    );
    assert!(
        receipt.speculative_s < receipt.baseline_s,
        "counterfactual baseline leaked into product time: spec={} baseline={}",
        receipt.speculative_s,
        receipt.baseline_s
    );
    let charged = receipt.draft_s + receipt.verify_s + receipt.repair_s + receipt.overhead_s;
    assert!((receipt.speculative_s - charged).abs() <= 1e-9);
    assert!((receipt.speedup_x - receipt.baseline_s / receipt.speculative_s).abs() <= 1e-6);
    assert!((receipt.attempted_fraction + receipt.fallback_fraction - 1.0).abs() <= 1e-6);
}

#[test]
fn wrong_same_length_candidate_falls_back_to_authoritative_baseline() {
    struct Empty;
    impl DraftProducer for Empty {
        fn propose(&mut self, _ctx: &[u32], _k: usize) -> Vec<u32> {
            Vec::new()
        }
        fn name(&self) -> &str {
            "empty-mismatch"
        }
    }

    struct DivergentStatefulTarget {
        prompt_len: usize,
        candidate: Vec<u32>,
        baseline: Vec<u32>,
        resets: usize,
    }
    impl TargetModel for DivergentStatefulTarget {
        fn reset(&mut self, _prompt: &[u32]) {
            self.resets += 1;
        }
        fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32> {
            let stream = if self.resets <= 1 {
                &self.candidate
            } else {
                &self.baseline
            };
            (from..=tokens.len())
                .map(|len| stream.get(len - self.prompt_len).copied().unwrap_or(0))
                .collect()
        }
        fn name(&self) -> &str {
            "divergent-stateful"
        }
    }

    let unit = SpecUnit {
        unit_id: "same-length-mismatch".into(),
        modality: "token".into(),
        prompt: vec![42],
        max_new_tokens: 3,
        eos: u32::MAX,
    };
    let authoritative = vec![9, 8, 7];
    let mut target = DivergentStatefulTarget {
        prompt_len: unit.prompt.len(),
        candidate: vec![1, 2, 3],
        baseline: authoritative.clone(),
        resets: 0,
    };
    let mut draft = Empty;
    let out = try_run_spec_decode(&unit, &mut draft, &mut target, 2, "mismatch-test").unwrap();

    assert_eq!(out.output, authoritative);
    assert!(
        out.receipt.exact,
        "escaped baseline output is authoritative"
    );
    assert!(!out.receipt.meta.candidate_exact);
    assert!(out.receipt.meta.authoritative_fallback);
    assert_eq!(out.receipt.quality_tier, "fail");
    assert!(!out.receipt.quality_gate);
    assert_eq!(out.receipt.fallback_s, out.receipt.baseline_s);
    assert!(out.receipt.overhead_s >= out.receipt.fallback_s);
    let charged =
        out.receipt.draft_s + out.receipt.verify_s + out.receipt.repair_s + out.receipt.overhead_s;
    assert!((out.receipt.speculative_s - charged).abs() <= 1e-9);
}

#[test]
fn greedy_decode_rejects_huge_bound_before_reset_or_allocation() {
    struct MustNotRun {
        resets: usize,
    }
    impl TargetModel for MustNotRun {
        fn reset(&mut self, _prompt: &[u32]) {
            self.resets += 1;
        }
        fn greedy_after_each_prefix(&mut self, _tokens: &[u32], _from: usize) -> Vec<u32> {
            panic!("invalid input must not reach the target")
        }
        fn name(&self) -> &str {
            "must-not-run"
        }
    }

    let unit = SpecUnit {
        unit_id: "huge".into(),
        modality: "token".into(),
        prompt: vec![1],
        max_new_tokens: MAX_NEW_TOKENS + 1,
        eos: u32::MAX,
    };
    let mut target = MustNotRun { resets: 0 };
    assert!(matches!(
        try_greedy_decode(&unit, &mut target),
        Err(SpecDecodeError::InvalidInput(_))
    ));
    assert_eq!(target.resets, 0, "validation must precede target mutation");
}

#[test]
fn ngram_checked_constructor_rejects_resource_amplifying_bounds() {
    for (order, span) in [
        (0, 1),
        (MAX_NGRAM_ORDER + 1, 1),
        (1, 0),
        (1, MAX_NGRAM_SPAN + 1),
    ] {
        assert!(matches!(
            NgramDraft::try_new(order, span),
            Err(SpecDecodeError::InvalidInput(_))
        ));
    }
    assert!(NgramDraft::try_new(MAX_NGRAM_ORDER, MAX_NGRAM_SPAN).is_ok());
}

#[test]
fn greedy_decode_resets_dirty_target_session() {
    struct DirtyTarget {
        truth: Vec<u32>,
        cursor: usize,
        resets: usize,
    }
    impl TargetModel for DirtyTarget {
        fn reset(&mut self, _prompt: &[u32]) {
            self.cursor = 0;
            self.resets += 1;
        }
        fn greedy_after_each_prefix(&mut self, _tokens: &[u32], _from: usize) -> Vec<u32> {
            vec![self.truth[self.cursor]]
        }
        fn commit(&mut self, tokens: &[u32]) {
            self.cursor += tokens.len();
        }
        fn name(&self) -> &str {
            "dirty-stateful"
        }
    }

    let unit = SpecUnit {
        unit_id: "fresh-baseline".into(),
        modality: "token".into(),
        prompt: vec![9],
        max_new_tokens: 3,
        eos: u32::MAX,
    };
    let mut target = DirtyTarget {
        truth: vec![10, 20, 30],
        cursor: 2,
        resets: 0,
    };
    let output = try_greedy_decode(&unit, &mut target).unwrap();
    assert_eq!(output, vec![10, 20, 30]);
    assert_eq!(
        target.resets, 1,
        "greedy entrypoint must start a fresh session"
    );
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
    assert!(
        out.receipt.attempted_units <= u.max_new_tokens * 32,
        "overlong proposer escaped the k-bound: {} attempted",
        out.receipt.attempted_units
    );
}

#[test]
fn malformed_verifier_length_returns_error_instead_of_indexing() {
    struct Short;
    impl TargetModel for Short {
        fn greedy_after_each_prefix(&mut self, _tokens: &[u32], _from: usize) -> Vec<u32> {
            Vec::new()
        }
        fn name(&self) -> &str {
            "short"
        }
    }
    let u = SpecUnit {
        unit_id: "short".into(),
        modality: "token".into(),
        prompt: vec![1],
        max_new_tokens: 4,
        eos: u32::MAX,
    };
    let mut target = Short;
    let mut draft = NgramDraft::new(1, 4);
    assert!(matches!(
        try_run_spec_decode(&u, &mut draft, &mut target, 4, "test"),
        Err(SpecDecodeError::VerifierLength { .. })
    ));
}

#[test]
fn target_session_is_reset_between_spec_and_baseline() {
    struct Stateful {
        prompt_len: usize,
        truth: Vec<u32>,
        resets: usize,
        begins: usize,
        rollbacks: usize,
        commits: usize,
    }
    impl TargetModel for Stateful {
        fn reset(&mut self, prompt: &[u32]) {
            assert_eq!(prompt.len(), self.prompt_len);
            self.resets += 1;
        }
        fn begin_round(&mut self, _ctx: &[u32]) {
            self.begins += 1;
        }
        fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32> {
            (from..=tokens.len())
                .map(|len| self.truth.get(len - self.prompt_len).copied().unwrap_or(0))
                .collect()
        }
        fn rollback_round(&mut self) {
            self.rollbacks += 1;
        }
        fn commit(&mut self, _tokens: &[u32]) {
            self.commits += 1;
        }
        fn name(&self) -> &str {
            "stateful"
        }
    }
    let u = SpecUnit {
        unit_id: "stateful".into(),
        modality: "token".into(),
        prompt: vec![9],
        max_new_tokens: 4,
        eos: u32::MAX,
    };
    let mut target = Stateful {
        prompt_len: 1,
        truth: vec![1, 2, 3, 4],
        resets: 0,
        begins: 0,
        rollbacks: 0,
        commits: 0,
    };
    let mut draft = NgramDraft::new(1, 4);
    let out = try_run_spec_decode(&u, &mut draft, &mut target, 4, "test").unwrap();
    assert_eq!(out.output, vec![1, 2, 3, 4]);
    assert!(out.receipt.exact);
    assert_eq!(
        target.resets, 2,
        "spec and baseline each reset exactly once"
    );
    assert_eq!(target.begins, target.rollbacks);
    assert_eq!(target.begins, target.commits);
}

#[test]
fn eos_inside_accepted_draft_does_not_commit_or_count_tokens_after_eos() {
    struct ExactDraft(Vec<u32>);
    impl DraftProducer for ExactDraft {
        fn propose(&mut self, _ctx: &[u32], k: usize) -> Vec<u32> {
            self.0.iter().copied().take(k).collect()
        }
        fn name(&self) -> &str {
            "exact"
        }
    }
    let truth = vec![1, 0, 99, 100];
    let u = SpecUnit {
        unit_id: "eos".into(),
        modality: "token".into(),
        prompt: vec![7],
        max_new_tokens: 8,
        eos: 0,
    };
    let mut target = MockTarget::new(1, truth.clone());
    let mut draft = ExactDraft(truth);
    let out = try_run_spec_decode(&u, &mut draft, &mut target, 4, "test").unwrap();
    assert_eq!(out.output, vec![1, 0]);
    assert!(out.receipt.accepted_units <= out.output.len());
}
