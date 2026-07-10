//! CX-owned token speculative-decode lane (POC).
//!
//! This is Branch C of `docs/research/CONSOLIDATION_PLAN_2026-07-09.md`. It is a
//! CX-OWNED, framework-independent implementation of the lossless greedy
//! speculative-decode loop:
//!
//! ```text
//! SpecUnit -> DraftProducer -> Verifier -> AcceptancePolicy -> RepairPolicy -> SpecReceipt
//! ```
//!
//! The core here (`run_spec_decode`) is PROVABLY LOSSLESS for greedy decoding:
//! its output is token-for-token identical to the target model's own greedy
//! decode, for ANY draft producer. That property is the differentiator over real
//! vLLM spec-decode, which measured NON-lossless in this repo (0.68x / 0.406x,
//! 29/32 and 41/64 tokens mismatched — see the branch ledger). We beat 1x by
//! being correct first and fast second, not the other way around.
//!
//! What is MEASURED locally (this crate, no fleet):
//!   * losslessness (unit tests, against a deterministic mock target),
//!   * draft ACCEPTANCE rate and TARGET-CALL REDUCTION on real token streams
//!     (the two quantities that govern the speedup ceiling).
//!
//! What is MODELED / needs the fork + fleet (see TOKEN_LANE_FORK_DESIGN.md):
//!   * wall-clock >1x. That requires the K-token verify to cost ~one decode step,
//!     which needs the two small candle additions the design names
//!     (`forward_all_logits` + `KvCacheSlot::truncate`). Until then this crate
//!     reports target-call reduction as the honest ceiling and labels wall-clock
//!     MODELED — never as a speed win.

use serde::{Deserialize, Serialize};

/// One speculation unit of work (mirrors the Branch A / cx_speculative_core
/// `SpecUnit` by contract — matched by shape, not by import, this wave).
#[derive(Clone, Debug)]
pub struct SpecUnit {
    pub unit_id: String,
    pub modality: String,
    /// The prompt token ids that seed generation.
    pub prompt: Vec<u32>,
    pub max_new_tokens: usize,
    pub eos: u32,
}

/// A CX-owned draft producer: given the running context, propose up to `k` next
/// token ids. May return FEWER than `k` (or zero) when it has no confident
/// continuation — the acceptance loop tolerates any length, including empty.
///
/// This is the ONLY place a proposer swaps: n-gram/copy today, a small draft
/// MODEL (SmolLM2-135M -> Llama-1B) or EAGLE-style head later. The verify /
/// accept / repair core below never changes.
pub trait DraftProducer {
    fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32>;
    fn name(&self) -> &str;
}

/// The target model, exposed as the exact primitive lossless spec-decode needs:
/// the greedy (argmax) token that FOLLOWS each prefix of `tokens` from index
/// `from` to `tokens.len()` INCLUSIVE — i.e. one forward pass that yields
/// per-position argmaxes. A real candle backend implements this with an
/// all-positions forward (`forward_all_logits`); the mock returns a fixed
/// stream. Returns `tokens.len() - from + 1` ids.
pub trait TargetModel {
    /// `greedy_after[j]` = argmax of the target logits computed on
    /// `tokens[..from + j]`, for j in `0..=(tokens.len() - from)`.
    fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32>;
    /// Plain greedy next-token for a full prefix (baseline / repair fallback).
    fn greedy_next(&mut self, tokens: &[u32]) -> u32 {
        let v = self.greedy_after_each_prefix(tokens, tokens.len());
        v[0]
    }
    fn name(&self) -> &str;
}

/// The unified speculation receipt. Field-for-field the same shape as
/// `scripts/spec-lab/cx_speculative_core.py::SpecReceipt.to_dict()` and the
/// Branch A substrate contract, so a token receipt and a render receipt compose
/// by the plan's STAGED TABLE (never by a naive product).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SpecReceipt {
    pub branch_id: String,
    pub modality: String,
    /// Total draft tokens PROPOSED across all rounds (the unit base).
    pub units: usize,
    pub attempted_units: usize,
    pub fallback_units: usize,
    /// Draft tokens ACCEPTED (== target's greedy token at that position).
    pub accepted_units: usize,
    /// Rounds that required a correction (a rejected draft token).
    pub repaired_units: usize,
    pub rejected_units: usize,
    pub attempted_fraction: f64,
    pub fallback_fraction: f64,
    /// accepted / attempted — the headline DRAFT ACCEPTANCE RATE.
    pub accepted_fraction: f64,
    pub repaired_fraction: f64,
    pub draft_s: f64,
    pub verify_s: f64,
    pub repair_s: f64,
    pub fallback_s: f64,
    /// Wall-clock of a plain greedy decode of the SAME unit (the real baseline).
    pub baseline_s: f64,
    pub speculative_s: f64,
    /// baseline_s / speculative_s. LABELED in `meta.walltime_label`: on a mock or
    /// stock-candle backend this is MODELED (the verify pass is not yet one decode
    /// step — needs the fork), never presented as a measured speed win.
    pub speedup_x: f64,
    /// True iff output is token-for-token the target's greedy decode. The core
    /// GUARANTEES this; the field is here so a broken backend can prove it failed.
    pub exact: bool,
    pub quality_gate: bool,
    pub meta: ReceiptMeta,
}

/// Token-lane specifics that do not exist in the render adapter live in `meta`,
/// exactly as cx_speculative_core stashes modality extras in its `meta` dict.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Default)]
pub struct ReceiptMeta {
    pub tokens_emitted: usize,
    pub rounds: usize,
    /// Number of target forward passes (== rounds for this lossless loop).
    pub target_calls: usize,
    /// tokens_emitted / target_calls — the MEASURED speedup CEILING (what a
    /// zero-overhead, one-pass verify would deliver). This is the honest local
    /// number; wall-clock >1x is a separate, MODELED, fork-gated claim.
    pub target_call_reduction_x: f64,
    pub mean_accept_len: f64,
    pub draft_producer: String,
    pub target_backend: String,
    /// "MEASURED" or "MODELED" for the wall-clock speedup, spelled out so a
    /// reader never mistakes a mock/stock-candle timing for a fleet speed win.
    pub walltime_label: String,
    pub notes: String,
}

/// Outcome of the full loop: the emitted tokens plus the receipt.
pub struct SpecOutcome {
    pub output: Vec<u32>,
    pub receipt: SpecReceipt,
}

/// One lossless speculation round. Pure accounting + the accept-longest-prefix +
/// bonus-token rule. Returns `(emitted_tokens, n_accepted, proposed_len)`.
///
/// Contract of `target_greedy_after`: element `j` is the target's greedy token
/// AFTER `ctx ++ draft[..j]`, for `j in 0..=draft.len()`. Element 0 is the token
/// the target would emit with NO draft (the plain-greedy token) — that is the
/// bonus when zero draft tokens are accepted, so the loop always advances by at
/// least one target-correct token.
fn accept_round(draft: &[u32], target_greedy_after: &[u32]) -> (Vec<u32>, usize) {
    debug_assert_eq!(target_greedy_after.len(), draft.len() + 1);
    let mut n_accept = 0usize;
    while n_accept < draft.len() && draft[n_accept] == target_greedy_after[n_accept] {
        n_accept += 1;
    }
    // The bonus/repair token is the target's greedy token at the first
    // non-matching position — FREE, it was computed in the same verify pass.
    let bonus = target_greedy_after[n_accept];
    let mut emit = draft[..n_accept].to_vec();
    emit.push(bonus);
    (emit, n_accept)
}

/// Run the full lossless greedy speculative-decode loop for one unit and produce
/// a SpecReceipt. `k` is the draft window (tokens proposed per round).
///
/// LOSSLESSNESS: the returned `output` is exactly `target`'s own greedy decode of
/// `unit.prompt` (same length, same tokens), for ANY `draft` producer. Proven by
/// `tests/lossless.rs` against a deterministic mock and asserted at runtime into
/// `receipt.exact` by re-deriving the greedy reference.
pub fn run_spec_decode(
    unit: &SpecUnit,
    draft: &mut dyn DraftProducer,
    target: &mut dyn TargetModel,
    k: usize,
    branch_id: &str,
) -> SpecOutcome {
    use std::time::Instant;

    let mut ctx = unit.prompt.clone();
    let mut output: Vec<u32> = Vec::new();

    let mut attempted_units = 0usize;
    let mut accepted_units = 0usize;
    let mut repaired_rounds = 0usize;
    let mut rounds = 0usize;
    let mut draft_s = 0f64;
    let mut verify_s = 0f64;

    let spec_start = Instant::now();
    while output.len() < unit.max_new_tokens {
        // --- DraftProducer -------------------------------------------------
        let remaining = unit.max_new_tokens - output.len();
        let want = k.min(remaining.saturating_sub(1).max(0)); // leave room for the bonus
        let t0 = Instant::now();
        let proposed = draft.propose(&ctx, want);
        draft_s += t0.elapsed().as_secs_f64();

        // --- Verifier (one target pass over ctx ++ draft) ------------------
        let mut verify_tokens = ctx.clone();
        verify_tokens.extend_from_slice(&proposed);
        let from = ctx.len();
        let t1 = Instant::now();
        let greedy_after = target.greedy_after_each_prefix(&verify_tokens, from);
        verify_s += t1.elapsed().as_secs_f64();

        // --- AcceptancePolicy + RepairPolicy (lossless) --------------------
        let (emit, n_accept) = accept_round(&proposed, &greedy_after);

        attempted_units += proposed.len();
        accepted_units += n_accept;
        if n_accept < proposed.len() {
            repaired_rounds += 1;
        }
        rounds += 1;

        // Commit, honoring max_new_tokens and EOS.
        for &tok in &emit {
            if output.len() >= unit.max_new_tokens {
                break;
            }
            ctx.push(tok);
            output.push(tok);
            if tok == unit.eos {
                let speculative_s = spec_start.elapsed().as_secs_f64();
                return finalize(
                    unit, branch_id, draft, target, k, output, attempted_units,
                    accepted_units, repaired_rounds, rounds, draft_s, verify_s, speculative_s,
                );
            }
        }
    }
    let speculative_s = spec_start.elapsed().as_secs_f64();
    finalize(
        unit, branch_id, draft, target, k, output, attempted_units, accepted_units,
        repaired_rounds, rounds, draft_s, verify_s, speculative_s,
    )
}

#[allow(clippy::too_many_arguments)]
fn finalize(
    unit: &SpecUnit,
    branch_id: &str,
    draft: &dyn DraftProducer,
    target: &mut dyn TargetModel,
    k: usize,
    output: Vec<u32>,
    attempted_units: usize,
    accepted_units: usize,
    repaired_rounds: usize,
    rounds: usize,
    draft_s: f64,
    verify_s: f64,
    speculative_s: f64,
) -> SpecOutcome {
    use std::time::Instant;

    // Baseline: a plain greedy decode of the SAME unit, timed. This is the real
    // single-lane run the plan requires `speedup_vs_baseline` to be computed
    // against — no synthetic baseline.
    let b0 = Instant::now();
    let baseline = greedy_decode(unit, target);
    let baseline_s = b0.elapsed().as_secs_f64();

    // Losslessness check, at runtime, into the receipt: spec output must equal the
    // plain greedy decode token-for-token.
    let exact = output == baseline;

    let rejected_units = attempted_units - accepted_units;
    let tokens_emitted = output.len();
    let target_calls = rounds; // exactly one verify pass per round
    let target_call_reduction_x = if target_calls > 0 {
        tokens_emitted as f64 / target_calls as f64
    } else {
        0.0
    };
    let mean_accept_len = if rounds > 0 {
        accepted_units as f64 / rounds as f64
    } else {
        0.0
    };
    let accepted_fraction = frac(accepted_units, attempted_units);
    let repaired_fraction = frac(repaired_rounds, rounds);

    let speedup_x = if speculative_s > 0.0 {
        baseline_s / speculative_s
    } else {
        0.0
    };

    let receipt = SpecReceipt {
        branch_id: branch_id.to_string(),
        modality: unit.modality.clone(),
        units: attempted_units,
        attempted_units,
        fallback_units: 0,
        accepted_units,
        repaired_units: repaired_rounds,
        rejected_units,
        attempted_fraction: 1.0,
        fallback_fraction: 0.0,
        accepted_fraction,
        repaired_fraction,
        draft_s: round6(draft_s),
        verify_s: round6(verify_s),
        repair_s: 0.0, // repair is folded into the verify pass (the bonus token is free)
        fallback_s: 0.0,
        baseline_s: round6(baseline_s),
        speculative_s: round6(speculative_s),
        speedup_x: round6(speedup_x),
        exact,
        quality_gate: exact,
        meta: ReceiptMeta {
            tokens_emitted,
            rounds,
            target_calls,
            target_call_reduction_x: round6(target_call_reduction_x),
            mean_accept_len: round6(mean_accept_len),
            draft_producer: draft.name().to_string(),
            target_backend: target.name().to_string(),
            walltime_label: "MODELED".to_string(),
            notes: format!(
                "k={k}; speedup_x is MODELED (verify pass is not yet one decode step — \
                 needs forward_all_logits + KvCacheSlot::truncate per TOKEN_LANE_FORK_DESIGN.md). \
                 accepted_fraction and target_call_reduction_x are MEASURED."
            ),
        },
    };

    SpecOutcome { output, receipt }
}

/// Plain greedy decode of a unit through the target — the honest baseline and the
/// losslessness oracle.
pub fn greedy_decode(unit: &SpecUnit, target: &mut dyn TargetModel) -> Vec<u32> {
    let mut ctx = unit.prompt.clone();
    let mut out = Vec::new();
    while out.len() < unit.max_new_tokens {
        let next = target.greedy_next(&ctx);
        ctx.push(next);
        out.push(next);
        if next == unit.eos {
            break;
        }
    }
    out
}

fn frac(a: usize, b: usize) -> f64 {
    if b == 0 {
        0.0
    } else {
        round6(a as f64 / b as f64)
    }
}

fn round6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

// ---------------------------------------------------------------------------
// DraftProducers (CX-owned, framework-independent)
// ---------------------------------------------------------------------------

/// Prompt-lookup / copy drafter (a.k.a. n-gram / REST-style). Keeps an index of
/// the running context and, given the last `order` tokens as a key, copies the
/// continuation that followed the most recent identical key. Zero model, zero
/// GPU — this is the cheapest possible DraftProducer and the one that already
/// wins on structured/repetitive output (code, JSON, retrieval, edits).
pub struct NgramDraft {
    order: usize,
    max_span: usize,
    history: Vec<u32>,
}

impl NgramDraft {
    pub fn new(order: usize, max_span: usize) -> Self {
        Self {
            order,
            max_span,
            history: Vec::new(),
        }
    }
}

impl DraftProducer for NgramDraft {
    fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32> {
        // Refresh history to the full context (cheap; POC clarity over micro-opt).
        self.history = ctx.to_vec();
        let k = k.min(self.max_span);
        if k == 0 || self.history.len() <= self.order {
            return Vec::new();
        }
        let n = self.history.len();
        let key = &self.history[n - self.order..];
        // Search backwards for the most recent earlier occurrence of `key`.
        // i indexes the START of a candidate key window ending before position n-order.
        let mut best: Option<usize> = None;
        let last_start = n - self.order; // exclusive of the current key window
        for start in (0..last_start).rev() {
            if &self.history[start..start + self.order] == key {
                best = Some(start + self.order); // continuation begins here
                break;
            }
        }
        match best {
            Some(cont) => {
                let end = (cont + k).min(n);
                self.history[cont..end].to_vec()
            }
            None => Vec::new(),
        }
    }
    fn name(&self) -> &str {
        "ngram_prompt_lookup"
    }
}

// ---------------------------------------------------------------------------
// Mock target (deterministic) — the losslessness oracle for unit tests.
// ---------------------------------------------------------------------------

/// A target whose greedy decode of a given prompt is a FIXED stream. This lets us
/// prove — deterministically, with no model — that `run_spec_decode` reproduces
/// the target's greedy output token-for-token for ANY draft producer.
///
/// Because the acceptance loop only ever evaluates `greedy_after` at positions
/// whose prefix is an exact prefix of `prompt ++ truth`, the mock can answer any
/// verify query by table lookup into `truth`.
pub struct MockTarget {
    prompt_len: usize,
    /// The full greedy continuation (what the model "would" emit), including EOS.
    truth: Vec<u32>,
    name: String,
}

impl MockTarget {
    pub fn new(prompt_len: usize, truth: Vec<u32>) -> Self {
        Self {
            prompt_len,
            truth,
            name: "mock_fixed_stream".to_string(),
        }
    }
    fn greedy_at_offset(&self, off: usize) -> u32 {
        // off = number of generated tokens already committed. If we run past the
        // truth stream, keep returning the last token (a stuck model) — harmless
        // because callers stop at max_new_tokens / EOS.
        if off < self.truth.len() {
            self.truth[off]
        } else {
            *self.truth.last().unwrap_or(&0)
        }
    }
}

impl TargetModel for MockTarget {
    fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32> {
        // For each prefix length L in from..=tokens.len(), the greedy next token
        // is truth[L - prompt_len]. The verify loop only consumes indices up to
        // the first draft/truth divergence + the bonus, all of which are exact
        // truth prefixes, so this table lookup is exactly what a real argmax pass
        // would return on those in-distribution prefixes.
        let mut out = Vec::with_capacity(tokens.len() - from + 1);
        for l in from..=tokens.len() {
            let off = l - self.prompt_len;
            out.push(self.greedy_at_offset(off));
        }
        out
    }
    fn name(&self) -> &str {
        &self.name
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(prompt: Vec<u32>, truth: &[u32], eos: u32) -> (SpecUnit, MockTarget) {
        let plen = prompt.len();
        let u = SpecUnit {
            unit_id: "t".into(),
            modality: "token".into(),
            prompt,
            max_new_tokens: truth.len(),
            eos,
        };
        let t = MockTarget::new(plen, truth.to_vec());
        (u, t)
    }

    #[test]
    fn lossless_with_ngram_on_repetitive_stream() {
        // A repetitive truth stream where the n-gram drafter will land long
        // accepted runs.
        let prompt = vec![1u32, 2, 3];
        let truth: Vec<u32> = (0..40).map(|i| (i % 4) as u32).collect();
        let (u, mut target) = unit(prompt, &truth, 999);
        let mut draft = NgramDraft::new(2, 16);
        let out = run_spec_decode(&u, &mut draft, &mut target, 8, "test");
        assert!(out.receipt.exact, "spec output must equal greedy");
        assert_eq!(out.output, truth);
        // On a period-4 stream an order-2 lookup should accept a lot.
        assert!(
            out.receipt.accepted_fraction > 0.5,
            "acceptance {} too low",
            out.receipt.accepted_fraction
        );
        // Ceiling reduction must exceed 1 when acceptance is real.
        assert!(out.receipt.meta.target_call_reduction_x > 1.0);
    }

    #[test]
    fn lossless_with_zero_acceptance_drafter() {
        // A drafter that always proposes wrong tokens must STILL be lossless — it
        // just degrades to plain greedy (one target token per round).
        struct AlwaysWrong;
        impl DraftProducer for AlwaysWrong {
            fn propose(&mut self, _ctx: &[u32], k: usize) -> Vec<u32> {
                vec![7u32; k] // 7 is never in truth below
            }
            fn name(&self) -> &str {
                "always_wrong"
            }
        }
        let prompt = vec![5u32];
        let truth = vec![1u32, 2, 3, 4, 5, 6, 8, 9];
        let (u, mut target) = unit(prompt, &truth, 999);
        let mut draft = AlwaysWrong;
        let out = run_spec_decode(&u, &mut draft, &mut target, 4, "test");
        assert!(out.receipt.exact);
        assert_eq!(out.output, truth);
        assert_eq!(out.receipt.accepted_units, 0);
        // One emitted token per round (bonus only) => reduction ~1.0.
        assert!((out.receipt.meta.target_call_reduction_x - 1.0).abs() < 1e-9);
    }

    #[test]
    fn lossless_stops_at_eos() {
        let prompt = vec![1u32];
        // EOS (=0) in the middle: output must stop AT eos, not run to max.
        let truth = vec![2u32, 3, 0, 4, 5];
        let plen = prompt.len();
        let u = SpecUnit {
            unit_id: "t".into(),
            modality: "token".into(),
            prompt,
            max_new_tokens: 32,
            eos: 0,
        };
        let mut target = MockTarget::new(plen, truth.clone());
        let mut draft = NgramDraft::new(1, 8);
        let out = run_spec_decode(&u, &mut draft, &mut target, 4, "test");
        assert!(out.receipt.exact);
        assert_eq!(out.output, vec![2, 3, 0]);
    }

    #[test]
    fn receipt_json_has_contract_keys() {
        let prompt = vec![1u32, 2];
        let truth: Vec<u32> = (0..12).map(|i| (i % 3) as u32).collect();
        let (u, mut target) = unit(prompt, &truth, 999);
        let mut draft = NgramDraft::new(2, 8);
        let out = run_spec_decode(&u, &mut draft, &mut target, 6, "token-spec-poc");
        let j = serde_json::to_value(&out.receipt).unwrap();
        for key in [
            "branch_id",
            "modality",
            "units",
            "attempted_units",
            "accepted_units",
            "repaired_units",
            "rejected_units",
            "accepted_fraction",
            "draft_s",
            "verify_s",
            "repair_s",
            "baseline_s",
            "speculative_s",
            "speedup_x",
            "exact",
            "quality_gate",
            "meta",
        ] {
            assert!(j.get(key).is_some(), "receipt missing contract key {key}");
        }
        // meta must carry the honest labels.
        assert_eq!(j["meta"]["walltime_label"], "MODELED");
        assert!(j["meta"]["target_call_reduction_x"].as_f64().unwrap() >= 1.0);
    }

    #[test]
    fn accept_round_bonus_is_always_present() {
        // Even with an empty draft, a round emits exactly one (bonus) token.
        let (emit, n) = accept_round(&[], &[42]);
        assert_eq!(emit, vec![42]);
        assert_eq!(n, 0);
    }
}
