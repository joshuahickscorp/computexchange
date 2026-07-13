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
use std::collections::HashMap;
use std::fmt;

pub const SPEC_RECEIPT_SCHEMA_VERSION: u16 = 1;
pub const MAX_PROMPT_TOKENS: usize = 1_000_000;
pub const MAX_NEW_TOKENS: usize = 1_000_000;
pub const MAX_DRAFT_WINDOW: usize = 65_536;
/// Public n-gram order ceiling, aligned with the CLI's historical accepted
/// range. The rolling index is O(prompt) space regardless of this value.
pub const MAX_NGRAM_ORDER: usize = 1_024;
pub const MAX_NGRAM_SPAN: usize = MAX_DRAFT_WINDOW;

const NGRAM_HASH_BASE_A: u64 = 1_000_000_007;
const NGRAM_HASH_BASE_B: u64 = 1_000_000_021;
/// Candidate disambiguation is deliberately local: enough preceding context to
/// separate prompt examples, without turning proposal into a context-length scan.
const NGRAM_MAX_RANK_CONTEXT: usize = 64;
/// A packed index entry stores two 32-bit continuation offsets in one u64. The
/// product limit is two million context tokens; absurd direct-call contexts past
/// this sentinel safely stop indexing (and therefore stop drafting).
const NGRAM_NO_ALTERNATE: u32 = u32::MAX;

fn pack_ngram_candidates(primary: usize, alternate: Option<usize>) -> u64 {
    let primary = u32::try_from(primary).expect("n-gram continuation offset was preflighted");
    let alternate = alternate
        .map(|offset| u32::try_from(offset).expect("n-gram alternate offset was preflighted"))
        .unwrap_or(NGRAM_NO_ALTERNATE);
    u64::from(primary) | (u64::from(alternate) << 32)
}

fn unpack_ngram_primary(encoded: u64) -> usize {
    (encoded as u32) as usize
}

fn unpack_ngram_alternate(encoded: u64) -> Option<usize> {
    let alternate = (encoded >> 32) as u32;
    (alternate != NGRAM_NO_ALTERNATE).then_some(alternate as usize)
}

fn default_schema_version() -> u16 {
    SPEC_RECEIPT_SCHEMA_VERSION
}

fn default_baseline_source() -> String {
    "measured".to_string()
}

fn default_quality_tier() -> String {
    "preview".to_string()
}

fn default_evidence() -> String {
    "modeled".to_string()
}

fn default_true() -> bool {
    true
}

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
    /// Start a fresh generation session. Stateful drafters must discard prior
    /// request state and seed themselves from this prompt.
    fn reset(&mut self, _prompt: &[u32]) {}
    fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32>;
    /// Observe only tokens actually committed by the target. This lets an
    /// incremental n-gram/model drafter update in O(new tokens), without copying
    /// the full context every round.
    fn commit(&mut self, _tokens: &[u32]) {}
    fn name(&self) -> &str;
}

/// The target model, exposed as the exact primitive lossless spec-decode needs:
/// the greedy (argmax) token that FOLLOWS each prefix of `tokens` from index
/// `from` to `tokens.len()` INCLUSIVE — i.e. one forward pass that yields
/// per-position argmaxes. A real candle backend implements this with an
/// all-positions forward (`forward_all_logits`); the mock returns a fixed
/// stream. Returns `tokens.len() - from + 1` ids.
pub trait TargetModel {
    /// Reset all request/KV state and seed a fresh session from `prompt`.
    fn reset(&mut self, _prompt: &[u32]) {}
    /// Snapshot/begin one speculative round. A stateful backend can checkpoint KV.
    fn begin_round(&mut self, _ctx: &[u32]) {}
    /// `greedy_after[j]` = argmax of the target logits computed on
    /// `tokens[..from + j]`, for j in `0..=(tokens.len() - from)`.
    fn greedy_after_each_prefix(&mut self, tokens: &[u32], from: usize) -> Vec<u32>;
    /// Optimized split-slice verifier. Backends may consume an existing KV-backed
    /// context plus only the draft tokens. The compatibility default builds the
    /// old contiguous input; MockTarget and future real KV backends override it.
    fn greedy_after_draft(&mut self, ctx: &[u32], draft: &[u32]) -> Vec<u32> {
        let mut tokens = Vec::with_capacity(ctx.len() + draft.len());
        tokens.extend_from_slice(ctx);
        tokens.extend_from_slice(draft);
        self.greedy_after_each_prefix(&tokens, ctx.len())
    }
    /// Roll back speculative KV mutations after verification.
    fn rollback_round(&mut self) {}
    /// Commit target-approved tokens into persistent request/KV state.
    fn commit(&mut self, _tokens: &[u32]) {}
    /// Plain greedy next-token for a full prefix (baseline / repair fallback).
    fn greedy_next(&mut self, tokens: &[u32]) -> u32 {
        let v = self.greedy_after_each_prefix(tokens, tokens.len());
        v[0]
    }
    fn name(&self) -> &str;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpecDecodeError {
    InvalidInput(String),
    VerifierLength { expected: usize, actual: usize },
}

impl fmt::Display for SpecDecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidInput(reason) => write!(f, "invalid speculative decode input: {reason}"),
            Self::VerifierLength { expected, actual } => write!(
                f,
                "target verifier returned {actual} tokens; expected exactly {expected}"
            ),
        }
    }
}

impl std::error::Error for SpecDecodeError {}

/// The unified speculation receipt. Field-for-field the same shape as
/// `scripts/spec-lab/cx_speculative_core.py::SpecReceipt.to_dict()` and the
/// Branch A substrate contract, so a token receipt and a render receipt compose
/// by the plan's STAGED TABLE (never by a naive product).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SpecReceipt {
    #[serde(default = "default_schema_version")]
    pub schema_version: u16,
    pub branch_id: String,
    pub modality: String,
    /// Decode rounds/windows folded into this receipt.
    pub units: usize,
    pub attempted_units: usize,
    /// Decode rounds where the drafter proposed zero tokens and the target's
    /// bonus token became a plain-greedy fallback step.
    pub fallback_units: usize,
    /// Draft tokens ACCEPTED (== target's greedy token at that position).
    pub accepted_units: usize,
    /// Rounds that required a correction (a rejected draft token).
    pub repaired_units: usize,
    pub rejected_units: usize,
    /// Fraction of decode rounds with a non-empty speculative proposal.
    pub attempted_fraction: f64,
    /// Fraction of decode rounds that fell back because the proposal was empty.
    pub fallback_fraction: f64,
    /// accepted / attempted — the headline DRAFT ACCEPTANCE RATE.
    pub accepted_fraction: f64,
    pub repaired_fraction: f64,
    #[serde(rename = "draft_cost_s", alias = "draft_s")]
    pub draft_s: f64,
    #[serde(rename = "verify_cost_s", alias = "verify_s")]
    pub verify_s: f64,
    #[serde(rename = "repair_cost_s", alias = "repair_s")]
    pub repair_s: f64,
    /// Authoritative greedy-baseline escape cost when candidate exactness fails.
    /// Empty-proposal target calls remain charged once in verify_cost_s. Because
    /// canonical v1 has no fallback phase, a nonzero value is also folded into
    /// overhead_cost_s rather than added to the canonical phase sum twice.
    pub fallback_s: f64,
    /// Loop/KV lifecycle/assembly wall time outside draft and target verification.
    #[serde(default, rename = "overhead_cost_s", alias = "overhead_s")]
    pub overhead_s: f64,
    /// Wall-clock of a plain greedy decode of the SAME unit (the real baseline).
    #[serde(rename = "baseline_total_time_s", alias = "baseline_s")]
    pub baseline_s: f64,
    #[serde(rename = "total_product_time_s", alias = "speculative_s")]
    pub speculative_s: f64,
    /// baseline_s / speculative_s. LABELED in `meta.walltime_label`: on a mock or
    /// stock-candle backend this is MODELED (the verify pass is not yet one decode
    /// step — needs the fork), never presented as a measured speed win.
    #[serde(rename = "speedup_vs_baseline", alias = "speedup_x")]
    pub speedup_x: f64,
    /// True iff output is token-for-token the target's greedy decode. The core
    /// GUARANTEES this; the field is here so a broken backend can prove it failed.
    pub exact: bool,
    /// Product-verification proof. The standalone POC has no authoritative
    /// evidence/job binding, so it always emits false even when exactness passes.
    #[serde(default)]
    pub artifact_verified: bool,
    /// Legacy compatibility projection of `quality_tier`: true for preview or
    /// delivery, false for fail. Losslessness is represented only by `exact`.
    pub quality_gate: bool,
    #[serde(default = "default_baseline_source")]
    pub baseline_source: String,
    #[serde(default = "default_quality_tier")]
    pub quality_tier: String,
    #[serde(default = "default_evidence")]
    pub evidence: String,
    #[serde(rename = "details", alias = "meta")]
    pub meta: ReceiptMeta,
}

/// Token-lane specifics that do not exist in the render adapter live in `meta`,
/// exactly as cx_speculative_core stashes modality extras in its `meta` dict.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Default)]
pub struct ReceiptMeta {
    pub tokens_emitted: usize,
    pub rounds: usize,
    /// Whether the speculative candidate itself matched authoritative greedy
    /// decoding. `SpecReceipt::exact` describes the escaped output returned to
    /// the caller, which remains true after authoritative fallback.
    #[serde(default = "default_true")]
    pub candidate_exact: bool,
    /// True when a wrong candidate was replaced with the measured greedy
    /// baseline before returning from the checked API.
    #[serde(default)]
    pub authoritative_fallback: bool,
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
#[cfg(test)]
fn accept_round(draft: &[u32], target_greedy_after: &[u32]) -> (Vec<u32>, usize) {
    let mut emit = Vec::with_capacity(draft.len() + 1);
    let n_accept = accept_round_into(draft, target_greedy_after, &mut emit);
    (emit, n_accept)
}

fn accept_round_into(draft: &[u32], target_greedy_after: &[u32], emit: &mut Vec<u32>) -> usize {
    debug_assert_eq!(target_greedy_after.len(), draft.len() + 1);
    let mut n_accept = 0usize;
    while n_accept < draft.len() && draft[n_accept] == target_greedy_after[n_accept] {
        n_accept += 1;
    }
    // The bonus/repair token is the target's greedy token at the first
    // non-matching position — FREE, it was computed in the same verify pass.
    let bonus = target_greedy_after[n_accept];
    emit.clear();
    emit.extend_from_slice(&draft[..n_accept]);
    emit.push(bonus);
    n_accept
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
    try_run_spec_decode(unit, draft, target, k, branch_id)
        .expect("run_spec_decode contract violation; use try_run_spec_decode at trust boundaries")
}

/// Checked lossless decode entrypoint. Malformed target output and hostile
/// overlong draft proposals are handled before any unchecked indexing/work
/// amplification can occur.
pub fn try_run_spec_decode<D, T>(
    unit: &SpecUnit,
    draft: &mut D,
    target: &mut T,
    k: usize,
    branch_id: &str,
) -> Result<SpecOutcome, SpecDecodeError>
where
    D: DraftProducer + ?Sized,
    T: TargetModel + ?Sized,
{
    use std::time::Instant;

    if branch_id.trim().is_empty() || branch_id.len() > 256 {
        return Err(SpecDecodeError::InvalidInput(
            "branch_id must be non-empty and <= 256 bytes".into(),
        ));
    }
    validate_unit(unit)?;
    if k == 0 || k > MAX_DRAFT_WINDOW {
        return Err(SpecDecodeError::InvalidInput(format!(
            "k must be in [1,{MAX_DRAFT_WINDOW}], got {k}"
        )));
    }

    let mut ctx = Vec::with_capacity(unit.prompt.len().saturating_add(unit.max_new_tokens));
    ctx.extend_from_slice(&unit.prompt);
    let mut output: Vec<u32> = Vec::with_capacity(unit.max_new_tokens);
    let mut emit = Vec::with_capacity(k.saturating_add(1));
    // Start at the same lifecycle boundary as the baseline in `finalize`.
    // Draft/target session reset and prompt-prefill are product work, not free.
    let spec_start = Instant::now();
    draft.reset(&unit.prompt);
    target.reset(&unit.prompt);

    let mut attempted_units = 0usize;
    let mut accepted_units = 0usize;
    let mut repaired_rounds = 0usize;
    let mut fallback_rounds = 0usize;
    let mut rounds = 0usize;
    let mut draft_s = 0f64;
    let mut verify_s = 0f64;

    while output.len() < unit.max_new_tokens {
        // --- DraftProducer -------------------------------------------------
        let remaining = unit.max_new_tokens - output.len();
        let want = k.min(remaining.saturating_sub(1)); // leave room for the bonus
        let t0 = Instant::now();
        let mut proposed = draft.propose(&ctx, want);
        // A hostile/buggy proposer cannot amplify target work beyond the caller's
        // verified budget. This also fixes the old 1000-token flood path.
        proposed.truncate(want);
        draft_s += t0.elapsed().as_secs_f64();

        // --- Verifier (one target pass over ctx ++ draft) ------------------
        target.begin_round(&ctx);
        let t1 = Instant::now();
        let greedy_after = target.greedy_after_draft(&ctx, &proposed);
        verify_s += t1.elapsed().as_secs_f64();
        let expected = proposed.len() + 1;
        if greedy_after.len() != expected {
            target.rollback_round();
            return Err(SpecDecodeError::VerifierLength {
                expected,
                actual: greedy_after.len(),
            });
        }

        // --- AcceptancePolicy + RepairPolicy (lossless) --------------------
        let n_accept = accept_round_into(&proposed, &greedy_after, &mut emit);
        // Never commit work past EOS or the caller's output bound. In particular,
        // accepted tokens after EOS no longer inflate acceptance accounting.
        emit.truncate(remaining);
        if let Some(eos_pos) = emit.iter().position(|&tok| tok == unit.eos) {
            emit.truncate(eos_pos + 1);
        }
        let committed_accept = n_accept.min(emit.len());
        let bonus_committed = emit.len() > committed_accept;

        attempted_units += proposed.len();
        accepted_units += committed_accept;
        if proposed.is_empty() {
            fallback_rounds += 1;
        }
        if bonus_committed && n_accept < proposed.len() {
            repaired_rounds += 1;
        }
        rounds += 1;

        // Commit, honoring max_new_tokens and EOS.
        target.rollback_round();
        target.commit(&emit);
        draft.commit(&emit);
        for &tok in &emit {
            if output.len() >= unit.max_new_tokens {
                break;
            }
            ctx.push(tok);
            output.push(tok);
            if tok == unit.eos {
                let speculative_s = spec_start.elapsed().as_secs_f64();
                return finalize(
                    unit,
                    branch_id,
                    draft,
                    target,
                    k,
                    output,
                    attempted_units,
                    accepted_units,
                    repaired_rounds,
                    fallback_rounds,
                    rounds,
                    draft_s,
                    verify_s,
                    speculative_s,
                );
            }
        }
    }
    let speculative_s = spec_start.elapsed().as_secs_f64();
    finalize(
        unit,
        branch_id,
        draft,
        target,
        k,
        output,
        attempted_units,
        accepted_units,
        repaired_rounds,
        fallback_rounds,
        rounds,
        draft_s,
        verify_s,
        speculative_s,
    )
}

fn validate_unit(unit: &SpecUnit) -> Result<(), SpecDecodeError> {
    if unit.unit_id.trim().is_empty() || unit.unit_id.len() > 256 {
        return Err(SpecDecodeError::InvalidInput(
            "unit_id must be non-empty and <= 256 bytes".into(),
        ));
    }
    if unit.modality != "token" {
        return Err(SpecDecodeError::InvalidInput(format!(
            "token lane requires modality=token, got {:?}",
            unit.modality
        )));
    }
    if unit.prompt.len() > MAX_PROMPT_TOKENS {
        return Err(SpecDecodeError::InvalidInput(format!(
            "prompt has {} tokens; maximum is {MAX_PROMPT_TOKENS}",
            unit.prompt.len()
        )));
    }
    if unit.max_new_tokens == 0 || unit.max_new_tokens > MAX_NEW_TOKENS {
        return Err(SpecDecodeError::InvalidInput(format!(
            "max_new_tokens must be in [1,{MAX_NEW_TOKENS}], got {}",
            unit.max_new_tokens
        )));
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn finalize<D, T>(
    unit: &SpecUnit,
    branch_id: &str,
    draft: &D,
    target: &mut T,
    k: usize,
    mut output: Vec<u32>,
    attempted_units: usize,
    accepted_units: usize,
    repaired_rounds: usize,
    fallback_rounds: usize,
    rounds: usize,
    draft_s: f64,
    verify_s: f64,
    spec_loop_s: f64,
) -> Result<SpecOutcome, SpecDecodeError>
where
    D: DraftProducer + ?Sized,
    T: TargetModel + ?Sized,
{
    use std::time::Instant;

    // Baseline: a plain greedy decode of the SAME unit, timed. This is the real
    // single-lane run the plan requires `speedup_vs_baseline` to be computed
    // against — no synthetic baseline.
    let b0 = Instant::now();
    let baseline = try_greedy_decode(unit, target)?;
    let baseline_s = b0.elapsed().as_secs_f64();

    // Everything below is delivered-product accounting/verification work. Time
    // it separately so the O(n) exact comparison, aggregation and owned receipt
    // strings are charged, while the counterfactual baseline above is excluded.
    let finalize_start = Instant::now();

    // Losslessness check at runtime. A wrong candidate never escapes as Ok: the
    // checked API substitutes the authoritative greedy result. `exact` therefore
    // describes the output returned to the caller; candidate_exact preserves the
    // speculative attempt's truth without conflating it with the safe fallback.
    let candidate_exact = output == baseline;
    if candidate_exact {
        drop(baseline);
    } else {
        output = baseline;
    }
    let exact = true;

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
    let attempted_fraction = frac(rounds - fallback_rounds, rounds);
    let fallback_fraction = frac(fallback_rounds, rounds);
    let quality_tier = if !candidate_exact {
        "fail"
    } else if fallback_rounds == 0 && attempted_units > 0 && accepted_units == attempted_units {
        "delivery"
    } else if accepted_fraction >= 0.5 {
        "preview"
    } else {
        "fail"
    };

    let baseline_total_time_s = round_positive_seconds(baseline_s);
    let authoritative_fallback_s = if candidate_exact {
        0.0
    } else {
        baseline_total_time_s
    };
    let draft_cost_s = round6(draft_s);
    let verify_cost_s = round6(verify_s);
    let mut receipt = SpecReceipt {
        schema_version: SPEC_RECEIPT_SCHEMA_VERSION,
        branch_id: branch_id.to_string(),
        modality: unit.modality.clone(),
        units: rounds,
        attempted_units,
        fallback_units: fallback_rounds,
        accepted_units,
        repaired_units: repaired_rounds,
        rejected_units,
        attempted_fraction,
        fallback_fraction,
        accepted_fraction,
        repaired_fraction,
        draft_s: draft_cost_s,
        verify_s: verify_cost_s,
        repair_s: 0.0, // repair is folded into the verify pass (the bonus token is free)
        fallback_s: authoritative_fallback_s,
        overhead_s: 0.0,
        baseline_s: baseline_total_time_s,
        speculative_s: 0.0,
        speedup_x: 0.0,
        exact,
        artifact_verified: false,
        // Legacy compatibility projection of quality_tier. Fidelity/losslessness
        // remains the separate `exact` field.
        quality_gate: quality_tier != "fail",
        baseline_source: "measured".to_string(),
        quality_tier: quality_tier.to_string(),
        evidence: "modeled".to_string(),
        meta: ReceiptMeta {
            tokens_emitted,
            rounds,
            candidate_exact,
            authoritative_fallback: !candidate_exact,
            target_calls,
            target_call_reduction_x: round6(target_call_reduction_x),
            mean_accept_len: round6(mean_accept_len),
            draft_producer: draft.name().to_string(),
            target_backend: target.name().to_string(),
            walltime_label: "MODELED".to_string(),
            notes: format!(
                "k={k}; fallback_rounds={fallback_rounds}; candidate_exact={candidate_exact}; \
                 authoritative_fallback={}; speedup_x is MODELED \
                 (verify pass is not yet one decode step — \
                 needs forward_all_logits + KvCacheSlot::truncate per TOKEN_LANE_FORK_DESIGN.md). \
                 accepted_fraction and target_call_reduction_x are MEASURED.",
                !candidate_exact
            ),
        },
    };

    // The loop wall time contains draft + verify plus lifecycle/accept/commit
    // overhead. Add the separately measured finalize work, then derive total
    // from the rounded canonical phase fields so wire arithmetic is coherent.
    let loop_overhead_s = (spec_loop_s - draft_s - verify_s).max(0.0);
    let finalize_cost_s = round_positive_seconds(finalize_start.elapsed().as_secs_f64());
    receipt.overhead_s = round6(loop_overhead_s + finalize_cost_s + authoritative_fallback_s);
    receipt.speculative_s = round_positive_seconds(
        receipt.draft_s + receipt.verify_s + receipt.repair_s + receipt.overhead_s,
    );
    receipt.speedup_x = round6(receipt.baseline_s / receipt.speculative_s);

    Ok(SpecOutcome { output, receipt })
}

/// Plain greedy decode of a unit through the target — the honest baseline and the
/// losslessness oracle.
pub fn greedy_decode(unit: &SpecUnit, target: &mut dyn TargetModel) -> Vec<u32> {
    try_greedy_decode(unit, target)
        .expect("greedy_decode target contract violation; use try_greedy_decode")
}

pub fn try_greedy_decode<T>(unit: &SpecUnit, target: &mut T) -> Result<Vec<u32>, SpecDecodeError>
where
    T: TargetModel + ?Sized,
{
    // This public checked entrypoint owns the complete baseline lifecycle: reject
    // hostile sizes before allocation, then discard any prior request/KV state.
    validate_unit(unit)?;
    target.reset(&unit.prompt);
    let mut ctx = Vec::with_capacity(unit.prompt.len().saturating_add(unit.max_new_tokens));
    ctx.extend_from_slice(&unit.prompt);
    let mut out = Vec::with_capacity(unit.max_new_tokens);
    while out.len() < unit.max_new_tokens {
        target.begin_round(&ctx);
        let greedy = target.greedy_after_draft(&ctx, &[]);
        if greedy.len() != 1 {
            target.rollback_round();
            return Err(SpecDecodeError::VerifierLength {
                expected: 1,
                actual: greedy.len(),
            });
        }
        let next = greedy[0];
        target.rollback_round();
        target.commit(&[next]);
        ctx.push(next);
        out.push(next);
        if next == unit.eos {
            break;
        }
    }
    Ok(out)
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

fn round_positive_seconds(x: f64) -> f64 {
    if x > 0.0 {
        round6(x).max(0.000001)
    } else {
        0.0
    }
}

// ---------------------------------------------------------------------------
// DraftProducers (CX-owned, framework-independent)
// ---------------------------------------------------------------------------

/// Prompt-lookup / copy drafter (a.k.a. n-gram / REST-style). Keeps an index of
/// the running context and, given the last `order` tokens as a key, copies the
/// continuation that followed the most recent identical key. When that source
/// continuation reaches the current history boundary, copying continues with
/// LZ-style overlap semantics. This lets a known repeated suffix fill the whole
/// requested window instead of stopping after one period. If the same key has
/// branched inside the prompt to different immediate continuations, the most
/// recent alternate is retained and the two candidates are ranked by up to 64
/// tokens of preceding context. A prompt with no such branch locks the session
/// onto the original single-candidate update path, so ordinary generation does
/// not pay continuous ambiguity-discovery overhead.
/// Keys are a pair of fixed-size rolling fingerprints followed by an exact key
/// check: index construction is O(prompt) time/space, not O(prompt * order). A
/// false periodic extension or candidate choice can only alter a proposal; every
/// token is checked by the authoritative target before commit.
pub struct NgramDraft {
    order: usize,
    max_span: usize,
    history: Vec<u32>,
    /// Packed most-recent and (when branching) alternate continuation offsets.
    /// This remains one machine word per key on supported 64-bit targets.
    index: HashMap<(u64, u64), u64>,
    rank_ambiguous: bool,
    session_has_ambiguity: bool,
    base_power_a: u64,
    base_power_b: u64,
    rolling_a: u64,
    rolling_b: u64,
}

impl NgramDraft {
    /// Compatibility constructor for trusted/static configuration. At trust
    /// boundaries use [`Self::try_new`] to surface invalid bounds without panic.
    pub fn new(order: usize, max_span: usize) -> Self {
        Self::try_new(order, max_span)
            .expect("invalid n-gram configuration; use NgramDraft::try_new at trust boundaries")
    }

    pub fn try_new(order: usize, max_span: usize) -> Result<Self, SpecDecodeError> {
        Self::try_new_with_ranking(order, max_span, true)
    }

    /// Exact ablation of the previous most-recent-only policy. Kept public but
    /// hidden from normal docs so regression benchmarks can compare target-call
    /// yield and hot-path cost without maintaining a second index implementation.
    #[doc(hidden)]
    pub fn new_recent_only(order: usize, max_span: usize) -> Self {
        Self::try_new_recent_only(order, max_span).expect(
            "invalid n-gram configuration; use NgramDraft::try_new_recent_only at trust boundaries",
        )
    }

    #[doc(hidden)]
    pub fn try_new_recent_only(order: usize, max_span: usize) -> Result<Self, SpecDecodeError> {
        Self::try_new_with_ranking(order, max_span, false)
    }

    fn try_new_with_ranking(
        order: usize,
        max_span: usize,
        rank_ambiguous: bool,
    ) -> Result<Self, SpecDecodeError> {
        if order == 0 || order > MAX_NGRAM_ORDER {
            return Err(SpecDecodeError::InvalidInput(format!(
                "n-gram order must be in [1,{MAX_NGRAM_ORDER}], got {order}"
            )));
        }
        if max_span == 0 || max_span > MAX_NGRAM_SPAN {
            return Err(SpecDecodeError::InvalidInput(format!(
                "n-gram max_span must be in [1,{MAX_NGRAM_SPAN}], got {max_span}"
            )));
        }
        Ok(Self {
            order,
            max_span,
            history: Vec::new(),
            index: HashMap::new(),
            rank_ambiguous,
            session_has_ambiguity: false,
            base_power_a: NGRAM_HASH_BASE_A.wrapping_pow((order - 1) as u32),
            base_power_b: NGRAM_HASH_BASE_B.wrapping_pow((order - 1) as u32),
            rolling_a: 0,
            rolling_b: 0,
        })
    }

    fn insert_recent(&mut self, n: usize) -> Option<((u64, u64), Option<u64>)> {
        if n >= self.order {
            if n < NGRAM_NO_ALTERNATE as usize {
                let key = (self.rolling_a, self.rolling_b);
                let previous = self.index.insert(key, pack_ngram_candidates(n, None));
                return Some((key, previous));
            } else {
                // Continuing to emit empty proposals is exact and avoids offset
                // truncation for unsupported >4B-token direct-call histories.
                self.index.clear();
            }
        }
        None
    }

    fn advance_history(&mut self, token: u32, n: usize) {
        let value = u64::from(token) + 1;
        if n >= self.order {
            let outgoing = u64::from(self.history[n - self.order]) + 1;
            self.rolling_a = self
                .rolling_a
                .wrapping_sub(outgoing.wrapping_mul(self.base_power_a))
                .wrapping_mul(NGRAM_HASH_BASE_A)
                .wrapping_add(value);
            self.rolling_b = self
                .rolling_b
                .wrapping_sub(outgoing.wrapping_mul(self.base_power_b))
                .wrapping_mul(NGRAM_HASH_BASE_B)
                .wrapping_add(value);
        } else {
            self.rolling_a = self
                .rolling_a
                .wrapping_mul(NGRAM_HASH_BASE_A)
                .wrapping_add(value);
            self.rolling_b = self
                .rolling_b
                .wrapping_mul(NGRAM_HASH_BASE_B)
                .wrapping_add(value);
        }
        self.history.push(token);
    }

    fn observe_recent(&mut self, token: u32) {
        let n = self.history.len();
        self.insert_recent(n);
        self.advance_history(token, n);
    }

    fn observe_ranked(&mut self, token: u32) {
        let n = self.history.len();
        if let Some((key, Some(encoded))) = self.insert_recent(n) {
            let previous_cont = unpack_ngram_primary(encoded);
            let previous_alternate = unpack_ngram_alternate(encoded);
            let alternate = if self.history[previous_cont] != token {
                // The immediately previous observation is the latest occurrence
                // of a different next-token branch.
                Some(previous_cont)
            } else {
                previous_alternate
            };
            if alternate.is_some() {
                self.index.insert(key, pack_ngram_candidates(n, alternate));
                self.session_has_ambiguity = true;
            }
        }
        self.advance_history(token, n);
    }

    fn exact_candidate(&self, cont: usize, n: usize) -> bool {
        cont < n
            && cont >= self.order
            && self.history[cont - self.order..cont] == self.history[n - self.order..n]
    }

    fn preceding_context_score(&self, cont: usize, n: usize) -> usize {
        let mut candidate = cont - self.order;
        let mut current = n - self.order;
        let limit = NGRAM_MAX_RANK_CONTEXT.min(candidate).min(current);
        let mut score = 0;
        while score < limit {
            candidate -= 1;
            current -= 1;
            if self.history[candidate] != self.history[current] {
                break;
            }
            score += 1;
        }
        score
    }

    fn copy_from(&self, cont: usize, n: usize, k: usize) -> Vec<u32> {
        // Treat history[cont..n] as an overlapping copy source: once the source
        // cursor reaches n it reads the proposal prefix just copied. This is the
        // same bounded self-reference used by LZ decoders.
        let mut proposal = Vec::with_capacity(k);
        let seed_len = (n - cont).min(k);
        proposal.extend_from_slice(&self.history[cont..cont + seed_len]);
        while proposal.len() < k {
            let take = (k - proposal.len()).min(proposal.len());
            proposal.extend_from_within(..take);
        }
        proposal
    }
}

impl DraftProducer for NgramDraft {
    fn reset(&mut self, prompt: &[u32]) {
        self.history.clear();
        self.index.clear();
        self.session_has_ambiguity = false;
        self.rolling_a = 0;
        self.rolling_b = 0;
        if self.rank_ambiguous {
            for &token in prompt {
                self.observe_ranked(token);
            }
        } else {
            for &token in prompt {
                self.observe_recent(token);
            }
        }
    }

    fn propose(&mut self, ctx: &[u32], k: usize) -> Vec<u32> {
        // `reset` + `commit` is the hot-path contract, but `ctx` remains part of
        // the public proposer surface and older/direct callers may supply it
        // without first seeding this incremental index. Rebuild on an observable
        // mismatch instead of panicking in debug or drafting from stale history in
        // release. Comparing only the bounded key tail keeps steady-state proposal
        // work independent of total context length; every proposal is still target
        // verified if an unsupported same-length/interior replacement preserves
        // that entire tail.
        let tail = self.order.min(ctx.len());
        let state_matches = ctx.len() == self.history.len()
            && ctx[ctx.len() - tail..] == self.history[self.history.len() - tail..];
        if !state_matches {
            self.reset(ctx);
        }
        let k = k.min(self.max_span);
        if k == 0 || self.history.len() <= self.order {
            return Vec::new();
        }
        let n = self.history.len();
        let key = (self.rolling_a, self.rolling_b);
        let Some(encoded) = self.index.get(&key).copied() else {
            return Vec::new();
        };
        let primary = unpack_ngram_primary(encoded);
        // Exact checks here keep double-hash collisions from becoming proposals
        // without adding O(order) comparisons to incremental index construction.
        if !self.exact_candidate(primary, n) {
            return Vec::new();
        }

        let mut selected = primary;
        if let Some(alternate) = unpack_ngram_alternate(encoded) {
            if self.exact_candidate(alternate, n)
                && self.history[alternate] != self.history[primary]
                && self.preceding_context_score(alternate, n)
                    > self.preceding_context_score(primary, n)
            {
                selected = alternate;
            }
        }
        self.copy_from(selected, n, k)
    }
    fn commit(&mut self, tokens: &[u32]) {
        if self.rank_ambiguous && self.session_has_ambiguity {
            for &token in tokens {
                self.observe_ranked(token);
            }
        } else {
            for &token in tokens {
                self.observe_recent(token);
            }
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
    fn greedy_after_draft(&mut self, ctx: &[u32], draft: &[u32]) -> Vec<u32> {
        let base = ctx.len() - self.prompt_len;
        (0..=draft.len())
            .map(|j| self.greedy_at_offset(base + j))
            .collect()
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
            "schema_version",
            "branch_id",
            "modality",
            "units",
            "attempted_units",
            "fallback_units",
            "accepted_units",
            "repaired_units",
            "rejected_units",
            "accepted_fraction",
            "attempted_fraction",
            "fallback_fraction",
            "draft_cost_s",
            "verify_cost_s",
            "repair_cost_s",
            "overhead_cost_s",
            "baseline_total_time_s",
            "baseline_source",
            "total_product_time_s",
            "speedup_vs_baseline",
            "exact",
            "artifact_verified",
            "quality_tier",
            "evidence",
            "quality_gate",
            "details",
        ] {
            assert!(j.get(key).is_some(), "receipt missing contract key {key}");
        }
        assert_eq!(j["baseline_source"], "measured");
        assert_eq!(j["evidence"], "modeled");
        assert!(!j["artifact_verified"].as_bool().unwrap());
        assert_eq!(
            j["quality_gate"].as_bool().unwrap(),
            j["quality_tier"] != "fail"
        );
        // details must carry the honest modality labels.
        assert_eq!(j["details"]["walltime_label"], "MODELED");
        assert!(j["details"]["candidate_exact"].as_bool().unwrap());
        assert!(!j["details"]["authoritative_fallback"].as_bool().unwrap());
        assert!(j["details"]["target_call_reduction_x"].as_f64().unwrap() >= 1.0);
    }

    #[test]
    fn accept_round_bonus_is_always_present() {
        // Even with an empty draft, a round emits exactly one (bonus) token.
        let (emit, n) = accept_round(&[], &[42]);
        assert_eq!(emit, vec![42]);
        assert_eq!(n, 0);
    }
}
