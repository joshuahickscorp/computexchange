//! The engine: the accept/repair loop ([`SpecPipeline::run_unit`]), the driver
//! that folds a delivered unit-set into ONE receipt ([`SpecPipeline::run_batch`]),
//! and the honest [`aggregate`] fold.

use std::marker::PhantomData;
use std::time::Instant;

use crate::receipt::{BaselineSource, Details, Evidence, Modality, QualityTier, SpecReceipt};
use crate::traits::{AcceptancePolicy, DraftProducer, RepairPolicy, Verifier};
use crate::unit::SpecUnit;

/// Per-unit timing + outcome. `drafted`/`accepted` are quantities (see
/// [`crate::verify::Acceptance`]), so the fold computes an honest Σ/Σ ratio.
pub struct UnitTrace {
    /// Ledger id of the unit.
    pub unit_id: String,
    /// Draft-phase seconds.
    pub draft_s: f64,
    /// Verify-phase seconds.
    pub verify_s: f64,
    /// Repair-phase seconds (0 on the accept path).
    pub repair_s: f64,
    /// Drafted quantity (k tokens; 1 tile).
    pub drafted: f64,
    /// Accepted-as-is quantity.
    pub accepted: f64,
    /// Whether the unit took the repair path.
    pub repaired: bool,
    /// Delivered tier for the unit.
    pub tier: QualityTier,
    /// Whether the unit's output is lossless vs baseline.
    pub exact: bool,
}

/// Holds the four roles + identity. Generic over every role type, so it
/// monomorphizes to zero-dispatch calls. `PhantomData<U>` ties the shared unit
/// type together without storing one.
pub struct SpecPipeline<U, D, V, A, R> {
    /// Experiment/branch id stamped onto the receipt.
    pub branch_id: String,
    /// Modality tag stamped onto the receipt.
    pub modality: Modality,
    /// The cheap proposer.
    pub producer: D,
    /// The expensive truth/quality source.
    pub verifier: V,
    /// The accept/repair branch condition.
    pub acceptance: A,
    /// The output materializer.
    pub repair: R,
    _pd: PhantomData<U>,
}

impl<U, D, V, A, R> SpecPipeline<U, D, V, A, R>
where
    U: SpecUnit,
    D: DraftProducer<U>,
    V: Verifier<U>,
    A: AcceptancePolicy<U>,
    R: RepairPolicy<U>,
{
    /// Assemble a pipeline for an explicit modality.
    pub fn new(
        branch_id: impl Into<String>,
        modality: Modality,
        producer: D,
        verifier: V,
        acceptance: A,
        repair: R,
    ) -> Self {
        Self {
            branch_id: branch_id.into(),
            modality,
            producer,
            verifier,
            acceptance,
            repair,
            _pd: PhantomData,
        }
    }

    /// Convenience constructor tagging the pipeline `render`.
    pub fn render(
        branch_id: impl Into<String>,
        producer: D,
        verifier: V,
        acceptance: A,
        repair: R,
    ) -> Self {
        Self::new(branch_id, Modality::render(), producer, verifier, acceptance, repair)
    }

    /// Convenience constructor tagging the pipeline `token`.
    pub fn token(
        branch_id: impl Into<String>,
        producer: D,
        verifier: V,
        acceptance: A,
        repair: R,
    ) -> Self {
        Self::new(branch_id, Modality::token(), producer, verifier, acceptance, repair)
    }

    /// Drive ONE unit end to end -> (Output, [`UnitTrace`]). THIS is the
    /// accept/repair loop: draft -> verify -> decide -> accept-or-repair. The
    /// draft is moved into exactly one of accept/repair (never cloned); acceptance
    /// is the pure branch condition and is not separately timed.
    pub fn run_unit(&self, unit: &U) -> (U::Output, UnitTrace) {
        let t0 = Instant::now();
        let draft = self.producer.draft(unit);
        let draft_s = t0.elapsed().as_secs_f64();

        let t1 = Instant::now();
        let verification = self.verifier.verify(unit, &draft);
        let verify_s = t1.elapsed().as_secs_f64();

        let acc = self.acceptance.decide(unit, &verification);

        let (output, repair_s, repaired) = if acc.is_full() {
            // Accept path: the draft is the delivery; no repair is timed.
            (self.repair.accept(unit, draft), 0.0, false)
        } else {
            let t2 = Instant::now();
            let out = self.repair.repair(unit, draft, verification, &acc);
            (out, t2.elapsed().as_secs_f64(), true)
        };

        (
            output,
            UnitTrace {
                unit_id: unit.unit_id(),
                draft_s,
                verify_s,
                repair_s,
                drafted: acc.drafted,
                accepted: acc.accepted,
                repaired,
                tier: acc.tier,
                exact: acc.exact,
            },
        )
    }

    /// Drive a delivered unit-set and fold it into ONE [`SpecReceipt`] — the
    /// modality-general driver the plan's Branch A gate names.
    ///
    /// `baseline_total_time_s` is a MEASURED (or explicitly MODELED) INPUT: the
    /// engine never fabricates it. Pass [`BaselineSource::Absent`] with any value
    /// to declare "no baseline", and the receipt's `speedup_vs_baseline` comes
    /// back `null`.
    pub fn run_batch(
        &self,
        units: &[U],
        baseline_total_time_s: f64,
        baseline_source: BaselineSource,
        evidence: Evidence,
        details: Details,
    ) -> (Vec<U::Output>, SpecReceipt) {
        let mut outs = Vec::with_capacity(units.len());
        let mut traces = Vec::with_capacity(units.len());
        for u in units {
            let (o, t) = self.run_unit(u);
            outs.push(o);
            traces.push(t);
        }
        let receipt = aggregate(
            &self.branch_id,
            self.modality.clone(),
            &traces,
            baseline_total_time_s,
            baseline_source,
            evidence,
            details,
        );
        (outs, receipt)
    }
}

/// Honest fold of per-unit traces into a receipt.
///
/// - `accepted_fraction` is `Σaccepted / Σdrafted` (token-granular, correct) —
///   never a mean of per-unit fractions.
/// - `quality_tier` is worst-wins across units.
/// - `speedup_vs_baseline` is `Some(baseline/spec)` ONLY when a baseline was
///   supplied (`baseline_source != Absent`) and spec time is positive; otherwise
///   `None`. There is NO code path here that multiplies two lanes' speedups.
pub fn aggregate(
    branch_id: &str,
    modality: Modality,
    traces: &[UnitTrace],
    baseline_total_time_s: f64,
    baseline_source: BaselineSource,
    evidence: Evidence,
    details: Details,
) -> SpecReceipt {
    let draft: f64 = traces.iter().map(|t| t.draft_s).sum();
    let verify: f64 = traces.iter().map(|t| t.verify_s).sum();
    let repair: f64 = traces.iter().map(|t| t.repair_s).sum();
    let total = draft + verify + repair;

    let drafted: f64 = traces.iter().map(|t| t.drafted).sum();
    let accepted: f64 = traces.iter().map(|t| t.accepted).sum();
    let repaired = traces.iter().filter(|t| t.repaired).count() as f64;

    let tier = traces
        .iter()
        .map(|t| t.tier)
        .reduce(|a, b| a.min(b))
        .unwrap_or(QualityTier::Fail);
    let exact = !traces.is_empty() && traces.iter().all(|t| t.exact);

    let speedup = match baseline_source {
        BaselineSource::Absent => None,
        _ if total > 0.0 => Some(baseline_total_time_s / total),
        _ => None,
    };

    SpecReceipt {
        branch_id: branch_id.into(),
        modality,
        draft_cost_s: draft,
        verify_cost_s: verify,
        repair_cost_s: repair,
        total_product_time_s: total,
        baseline_total_time_s,
        baseline_source,
        units: traces.len() as u32,
        accepted_fraction: if drafted > 0.0 { accepted / drafted } else { 0.0 },
        repaired_fraction: if traces.is_empty() {
            0.0
        } else {
            repaired / traces.len() as f64
        },
        exact,
        quality_tier: tier,
        speedup_vs_baseline: speedup,
        evidence,
        details,
    }
}
