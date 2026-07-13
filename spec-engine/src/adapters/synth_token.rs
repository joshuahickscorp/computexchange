//! `synth_token` — a deterministic token-like adapter: PARTIAL accept,
//! `truth = Some`, `exact = true`. It stresses the corner the render lane never
//! does — a matched-prefix of length `m < k` (so `accepted_fraction` lands
//! strictly between 0 and 1) and a verifier that carries the authoritative
//! continuation (`truth = Some`) for repair to splice.
//!
//! The proposer emits `k` cheap tokens; the verifier (standing in for the target
//! model's forward pass) compares them to the window's carried `truth` and
//! returns the matched-prefix length `m`. Acceptance is `m` of `k`. Repair
//! splices `draft[..m]` with `truth[m..]`, so the delivered run ALWAYS equals the
//! target continuation — output is lossless (`exact = true`) whether or not the
//! whole window was accepted. The `quality_tier` here is therefore a SPECULATION
//! COVERAGE tier (whole window matched -> `Delivery`; partial -> `Preview`; poor
//! -> `Fail`), not a fidelity tier — fidelity is the separate `exact` flag.

use crate::adapters::fnv_spin;
use crate::receipt::{Modality, QualityTier};
use crate::traits::{AcceptancePolicy, DraftProducer, RepairPolicy, Verifier};
use crate::unit::SpecUnit;
use crate::verify::{Acceptance, Verification};

// Modeled phase costs. The proposer is cheapest; the target-pass verify is the
// expensive phase; repair (a target step over the rejected suffix) is modest.
const DRAFT_ITERS: u32 = 1_500;
const VERIFY_ITERS: u32 = 6_000;
const REPAIR_ITERS: u32 = 3_000;

/// A k-token speculation window. `truth` is the authoritative continuation the
/// target model would produce (length `k`); the proposer never sees it — the
/// verifier compares against it.
pub struct TokenWindow {
    /// Window index (ledger id).
    pub id: u32,
    /// Prompt/context tokens the proposer conditions on.
    pub context: Vec<u32>,
    /// Number of tokens to propose/verify in this window.
    pub k: usize,
    /// The target model's true continuation (length `k`).
    pub truth: Vec<u32>,
}

impl SpecUnit for TokenWindow {
    type Draft = Vec<u32>; // k proposed tokens
    type Output = Vec<u32>; // accepted prefix + corrected suffix (lossless)
    type Score = usize; // matched-prefix length m

    fn unit_id(&self) -> String {
        format!("win_{}", self.id)
    }
    fn modality(&self) -> Modality {
        Modality::token()
    }
}

/// A toy n-gram proposer: predicts `[base+1, base+2, ..., base+k]` where `base`
/// is the last context token. Deterministic and dependency-free — it stands in
/// for a real CX-owned draft producer.
pub struct NgramProposer;

impl DraftProducer<TokenWindow> for NgramProposer {
    fn draft(&self, unit: &TokenWindow) -> Vec<u32> {
        let base = *unit.context.last().unwrap_or(&0);
        let _ = fnv_spin(unit.id as u64 ^ 0x4E47_5241, DRAFT_ITERS); // "NGRA"
        (0..unit.k)
            .map(|i| base.wrapping_add(i as u32 + 1))
            .collect()
    }
}

/// The target-pass verifier: returns the matched-prefix length `m` and carries
/// the authoritative continuation as `truth` (the byproduct a real target
/// forward pass produces) so repair can splice it.
pub struct TargetVerifier;

impl Verifier<TokenWindow> for TargetVerifier {
    fn verify(&self, unit: &TokenWindow, draft: &Vec<u32>) -> Verification<TokenWindow> {
        let m = draft
            .iter()
            .zip(unit.truth.iter())
            .take_while(|(a, b)| a == b)
            .count();
        let _ = fnv_spin(unit.id as u64 ^ 0x5451_5254, VERIFY_ITERS); // "TQRT"
        Verification {
            score: m,
            truth: Some(unit.truth.clone()),
        }
    }
}

/// The acceptance policy: accept the `m`-token matched prefix of `k`. `exact` is
/// always `true` — the delivered output is lossless by construction (see
/// [`SplicingRepair`]).
pub struct PrefixAcceptPolicy;

impl AcceptancePolicy<TokenWindow> for PrefixAcceptPolicy {
    fn decide(&self, unit: &TokenWindow, verification: &Verification<TokenWindow>) -> Acceptance {
        let m = verification.score;
        let k = unit.k;
        // Coverage tier: whole window matched -> Delivery; at least half -> Preview;
        // otherwise -> Fail. (Output fidelity is the separate `exact` flag.)
        let tier = if m == k {
            QualityTier::Delivery
        } else if m * 2 >= k {
            QualityTier::Preview
        } else {
            QualityTier::Fail
        };
        Acceptance {
            drafted: k as f64,
            accepted: m as f64,
            tier,
            exact: true, // lossless: repair splices the target's true tokens
        }
    }
}

/// The repair policy: accept returns the fully-matched draft; repair splices the
/// accepted prefix `draft[..m]` with the target correction `truth[m..]`.
pub struct SplicingRepair;

impl RepairPolicy<TokenWindow> for SplicingRepair {
    fn accept(&self, _unit: &TokenWindow, draft: Vec<u32>) -> Vec<u32> {
        // All k matched: the draft IS the delivered run.
        draft
    }
    fn repair(
        &self,
        _unit: &TokenWindow,
        draft: Vec<u32>,
        verification: Verification<TokenWindow>,
        acc: &Acceptance,
    ) -> Vec<u32> {
        let m = acc.accepted as usize;
        let _ = fnv_spin(m as u64 ^ 0x5350_4C43, REPAIR_ITERS); // "SPLC"
        let mut out = draft; // move, no clone
        out.truncate(m); // keep the accepted prefix draft[..m]
        if let Some(truth) = verification.truth {
            // Splice the target's correction truth[m..]; the result equals `truth`,
            // so the delivered run is lossless.
            out.extend_from_slice(&truth[m..]);
        }
        out
    }
}

/// Build a ready-to-run token pipeline.
pub fn pipeline(
    branch_id: &str,
) -> crate::engine::SpecPipeline<
    TokenWindow,
    NgramProposer,
    TargetVerifier,
    PrefixAcceptPolicy,
    SplicingRepair,
> {
    crate::engine::SpecPipeline::token(
        branch_id,
        NgramProposer,
        TargetVerifier,
        PrefixAcceptPolicy,
        SplicingRepair,
    )
}

/// Build a window whose proposer draft matches the target continuation on exactly
/// its first `m` of `k` tokens (test/demo helper). `base` is the last context
/// token, so the proposer emits `[base+1, ..., base+k]`; `truth` reuses that
/// prefix and then diverges into a disjoint high range.
pub fn window_with_match(id: u32, base: u32, k: usize, m: usize) -> TokenWindow {
    assert!(m <= k);
    let draft: Vec<u32> = (0..k).map(|i| base.wrapping_add(i as u32 + 1)).collect();
    let mut truth = draft[..m].to_vec();
    // Diverge with a range guaranteed disjoint from the small proposer values.
    for j in 0..(k - m) {
        truth.push(900_000 + id * 1_000 + j as u32);
    }
    TokenWindow {
        id,
        context: vec![base.saturating_sub(1), base],
        k,
        truth,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::receipt::{BaselineSource, Details, Evidence};

    // A window set that exercises Delivery (m==k), Preview (m>=k/2), Fail (m<k/2):
    //   W0: k=8, m=8 -> Delivery, accept path
    //   W1: k=8, m=6 -> Preview,  repair
    //   W2: k=8, m=2 -> Fail,     repair
    fn mixed_windows() -> Vec<TokenWindow> {
        vec![
            window_with_match(0, 3, 8, 8),
            window_with_match(1, 10, 8, 6),
            window_with_match(2, 20, 8, 2),
        ]
    }

    #[test]
    fn implements_all_four_traits_and_emits_a_sane_receipt() {
        let pipe = pipeline("cx_synth_token_demo");
        let windows = mixed_windows();
        let (outputs, receipt) = pipe.run_batch(
            &windows,
            /* baseline_total_time_s */ 0.02,
            BaselineSource::Modeled,
            Evidence::Synthetic,
            Details::new(),
        );

        // Lossless: every delivered run equals the window's target continuation.
        for (out, win) in outputs.iter().zip(windows.iter()) {
            assert_eq!(out, &win.truth, "token output must be lossless vs target");
        }

        assert_eq!(receipt.modality, Modality::token());
        assert_eq!(receipt.units, 3);
        assert!(
            receipt.exact,
            "token delivered output is lossless by construction"
        );

        // accepted_fraction is EXACTLY Σm/Σk = (8+6+2)/(8+8+8) = 16/24.
        assert!((0.0..=1.0).contains(&receipt.accepted_fraction));
        assert!((receipt.accepted_fraction - 16.0 / 24.0).abs() < 1e-9);
        // Strictly partial — the corner render never reaches.
        assert!(receipt.accepted_fraction > 0.0 && receipt.accepted_fraction < 1.0);

        // worst-wins over [Delivery, Preview, Fail] => Fail.
        assert_eq!(receipt.quality_tier, QualityTier::Fail);

        // W1 and W2 repaired.
        assert!((receipt.repaired_fraction - 2.0 / 3.0).abs() < 1e-9);
        assert!(receipt.repair_cost_s > 0.0);

        // speedup = baseline / spec, present under a Modeled baseline.
        let sp = receipt
            .speedup_vs_baseline
            .expect("modeled baseline => Some");
        assert!((sp - 0.02 / receipt.total_product_time_s).abs() < 1e-9);

        // JSON round-trips.
        let json = serde_json::to_string(&receipt).unwrap();
        let back: crate::receipt::SpecReceipt = serde_json::from_str(&json).unwrap();
        assert_eq!(
            serde_json::to_value(&back).unwrap(),
            serde_json::to_value(&receipt).unwrap()
        );
    }

    #[test]
    fn absent_baseline_yields_null_speedup() {
        let pipe = pipeline("cx_synth_token_absent");
        let windows = mixed_windows();
        let (_outs, receipt) = pipe.run_batch(
            &windows,
            0.0,
            BaselineSource::Absent,
            Evidence::Synthetic,
            Details::new(),
        );
        assert!(
            receipt.speedup_vs_baseline.is_none(),
            "no baseline => speedup is never fabricated"
        );
        // And it serializes to JSON null.
        let v = serde_json::to_value(&receipt).unwrap();
        assert!(v.get("speedup_vs_baseline").unwrap().is_null());
    }

    #[test]
    fn full_acceptance_skips_repair() {
        // Every window fully matches -> accept path only, zero repair cost.
        let pipe = pipeline("cx_synth_token_full");
        let windows = vec![
            window_with_match(0, 3, 8, 8),
            window_with_match(1, 40, 5, 5),
        ];
        let (_outs, receipt) = pipe.run_batch(
            &windows,
            0.01,
            BaselineSource::Modeled,
            Evidence::Synthetic,
            Details::new(),
        );
        assert_eq!(receipt.repair_cost_s, 0.0);
        assert!((receipt.accepted_fraction - 1.0).abs() < 1e-9);
        assert_eq!(receipt.quality_tier, QualityTier::Delivery);
    }
}
