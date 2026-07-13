//! The engine: the accept/repair loop ([`SpecPipeline::run_unit`]), the driver
//! that folds a delivered unit-set into ONE receipt ([`SpecPipeline::run_batch`]),
//! and the honest [`aggregate`] fold.

use std::collections::HashSet;
use std::fmt;
use std::marker::PhantomData;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::time::Instant;

use crate::receipt::{
    validate_details, BaselineSource, Details, Evidence, Modality, QualityTier,
    ReceiptValidationError, SpecReceipt, SPEC_RECEIPT_SCHEMA_VERSION,
};
use crate::traits::{AcceptancePolicy, DraftProducer, RepairPolicy, Verifier};
use crate::unit::SpecUnit;

/// Bound the extra output/trace allocation and callback work a single batch can
/// trigger. Adapters that need more should stream bounded batches and aggregate
/// server-side instead of asking one worker receipt to allocate without limit.
pub const MAX_SPEC_BATCH_UNITS: usize = 100_000;

/// Per-unit timing + outcome. `drafted`/`accepted` are quantities (see
/// [`crate::verify::Acceptance`]), so the fold computes an honest Σ/Σ ratio.
#[derive(Debug, Clone)]
pub struct UnitTrace {
    /// Ledger id of the unit.
    pub unit_id: String,
    /// Draft-phase seconds.
    pub draft_s: f64,
    /// Verify-phase seconds.
    pub verify_s: f64,
    /// Repair-phase seconds (0 on the accept path).
    pub repair_s: f64,
    /// Policy, assembly, id and bookkeeping wall time outside adapter phases.
    pub overhead_s: f64,
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

/// Checked execution failure. Adapter callback panics are contained and mapped
/// to [`EngineError::CallbackPanic`]; value-contract failures return a typed
/// error before an unsafe branch is taken.
#[derive(Debug)]
pub enum EngineError {
    InvalidIdentity(String),
    InvalidUnitIdentity(String),
    DuplicateUnitIdentity(String),
    ModalityMismatch {
        unit: String,
        expected: String,
        actual: String,
    },
    InvalidAcceptance {
        unit: String,
        reason: String,
    },
    InvalidTrace {
        unit: String,
        reason: String,
    },
    InvalidBaseline(String),
    CallbackPanic {
        unit: String,
        phase: &'static str,
    },
    EmptyBatch,
    TooManyUnits {
        actual: usize,
        max: usize,
    },
    InvalidReceipt(ReceiptValidationError),
}

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidIdentity(reason) => write!(f, "invalid pipeline identity: {reason}"),
            Self::InvalidUnitIdentity(reason) => write!(f, "invalid unit identity: {reason}"),
            Self::DuplicateUnitIdentity(unit) => {
                write!(f, "duplicate unit identity {unit:?}")
            }
            Self::ModalityMismatch {
                unit,
                expected,
                actual,
            } => write!(
                f,
                "unit {unit:?} modality {actual:?} does not match pipeline {expected:?}"
            ),
            Self::InvalidAcceptance { unit, reason } => {
                write!(f, "unit {unit:?} returned invalid acceptance: {reason}")
            }
            Self::InvalidTrace { unit, reason } => {
                write!(f, "unit {unit:?} has invalid trace: {reason}")
            }
            Self::InvalidBaseline(reason) => write!(f, "invalid baseline: {reason}"),
            Self::CallbackPanic { unit, phase } => {
                write!(f, "unit {unit:?} panicked during {phase}")
            }
            Self::EmptyBatch => write!(f, "speculation batch must contain at least one unit"),
            Self::TooManyUnits { actual, max } => {
                write!(f, "batch has {actual} units; safety maximum is {max}")
            }
            Self::InvalidReceipt(err) => write!(f, "invalid aggregate receipt: {err}"),
        }
    }
}

impl std::error::Error for EngineError {}

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
        Self::new(
            branch_id,
            Modality::render(),
            producer,
            verifier,
            acceptance,
            repair,
        )
    }

    /// Convenience constructor tagging the pipeline `token`.
    pub fn token(
        branch_id: impl Into<String>,
        producer: D,
        verifier: V,
        acceptance: A,
        repair: R,
    ) -> Self {
        Self::new(
            branch_id,
            Modality::token(),
            producer,
            verifier,
            acceptance,
            repair,
        )
    }

    /// Drive ONE unit end to end -> (Output, [`UnitTrace`]). THIS is the
    /// accept/repair loop: draft -> verify -> decide -> accept-or-repair. The
    /// draft is moved into exactly one of accept/repair (never cloned); acceptance
    /// is the pure branch condition and is not separately timed.
    pub fn run_unit(&self, unit: &U) -> (U::Output, UnitTrace) {
        self.try_run_unit(unit).expect(
            "SpecPipeline::run_unit contract violation; use try_run_unit at trust boundaries",
        )
    }

    /// Checked form of [`Self::run_unit`]. It validates identity, modality and the
    /// adapter's acceptance quantities before choosing accept versus repair.
    pub fn try_run_unit(&self, unit: &U) -> Result<(U::Output, UnitTrace), EngineError> {
        validate_identity(&self.branch_id, &self.modality)?;
        let unit_id = self.preflight_unit(unit)?;
        self.try_run_unit_preflighted(unit, unit_id)
    }

    /// Resolve the adapter-owned identity without allowing a panic or modality
    /// mismatch to cross the checked engine boundary.
    fn preflight_unit(&self, unit: &U) -> Result<String, EngineError> {
        let unit_id = checked_callback("<unresolved>", "unit_id", || unit.unit_id())?;
        if unit_id.trim().is_empty() || unit_id.len() > 256 {
            return Err(EngineError::InvalidUnitIdentity(
                "unit_id must be non-empty and <= 256 bytes".into(),
            ));
        }
        let actual_modality = checked_callback(&unit_id, "modality", || unit.modality())?;
        if actual_modality != self.modality {
            return Err(EngineError::ModalityMismatch {
                unit: unit_id.clone(),
                expected: self.modality.as_str().into(),
                actual: actual_modality.as_str().into(),
            });
        }
        Ok(unit_id)
    }

    /// Execute a unit whose identity/modality was already preflighted. Batch
    /// callers use this after checking the complete identity ledger for
    /// duplicates, so no product callback runs before that global check passes.
    fn try_run_unit_preflighted(
        &self,
        unit: &U,
        unit_id: String,
    ) -> Result<(U::Output, UnitTrace), EngineError> {
        let wall_start = Instant::now();
        let t0 = Instant::now();
        let draft = checked_callback(&unit_id, "draft", || self.producer.draft(unit))?;
        let draft_s = t0.elapsed().as_secs_f64();

        let t1 = Instant::now();
        let verification =
            checked_callback(&unit_id, "verify", || self.verifier.verify(unit, &draft))?;
        let verify_s = t1.elapsed().as_secs_f64();

        let acc = checked_callback(&unit_id, "acceptance", || {
            self.acceptance.decide(unit, &verification)
        })?;
        acc.validate()
            .map_err(|err| EngineError::InvalidAcceptance {
                unit: unit_id.clone(),
                reason: err.to_string(),
            })?;

        let (output, repair_s, repaired) = if acc.is_full() {
            // Accept path: the draft is the delivery; no repair is timed.
            (
                checked_callback(&unit_id, "accept", || self.repair.accept(unit, draft))?,
                0.0,
                false,
            )
        } else {
            let t2 = Instant::now();
            let out = checked_callback(&unit_id, "repair", || {
                self.repair.repair(unit, draft, verification, &acc)
            })?;
            (out, t2.elapsed().as_secs_f64(), true)
        };

        let accounted = draft_s + verify_s + repair_s;
        // Include unit-id construction and pure policy/output assembly. Saturate
        // tiny timer quantization inversions at zero.
        let overhead_s = (wall_start.elapsed().as_secs_f64() - accounted).max(0.0);
        Ok((
            output,
            UnitTrace {
                unit_id,
                draft_s,
                verify_s,
                repair_s,
                overhead_s,
                drafted: acc.drafted,
                accepted: acc.accepted,
                repaired,
                tier: acc.tier,
                exact: acc.exact,
            },
        ))
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
        self.try_run_batch(
            units,
            baseline_total_time_s,
            baseline_source,
            evidence,
            details,
        )
        .expect("SpecPipeline::run_batch contract violation; use try_run_batch at trust boundaries")
    }

    /// Checked batch driver for production/trust-boundary callers.
    ///
    /// This validates the entire identity ledger before product callbacks, but
    /// it is not an external-side-effect transaction: if a later unit fails,
    /// earlier adapter callbacks have already run. Adapters must keep callbacks
    /// pure or provide their own idempotency/rollback boundary.
    pub fn try_run_batch(
        &self,
        units: &[U],
        baseline_total_time_s: f64,
        baseline_source: BaselineSource,
        evidence: Evidence,
        details: Details,
    ) -> Result<(Vec<U::Output>, SpecReceipt), EngineError> {
        let batch_wall_start = Instant::now();
        validate_identity(&self.branch_id, &self.modality)?;
        validate_baseline(baseline_total_time_s, baseline_source)?;
        validate_details(&details).map_err(EngineError::InvalidReceipt)?;
        if units.is_empty() {
            return Err(EngineError::EmptyBatch);
        }
        if units.len() > MAX_SPEC_BATCH_UNITS {
            return Err(EngineError::TooManyUnits {
                actual: units.len(),
                max: MAX_SPEC_BATCH_UNITS,
            });
        }
        let mut unit_ids = Vec::with_capacity(units.len());
        let mut unique_ids = HashSet::with_capacity(units.len());
        for unit in units {
            let unit_id = self.preflight_unit(unit)?;
            if !unique_ids.insert(unit_id.clone()) {
                return Err(EngineError::DuplicateUnitIdentity(unit_id));
            }
            unit_ids.push(unit_id);
        }
        let mut outs = Vec::with_capacity(units.len());
        let mut traces = Vec::with_capacity(units.len());
        for (u, unit_id) in units.iter().zip(unit_ids) {
            let (o, t) = self.try_run_unit_preflighted(u, unit_id)?;
            outs.push(o);
            traces.push(t);
        }
        // Every trace below was constructed by `try_run_unit_preflighted` only
        // after the complete identity ledger passed validation. Avoid hashing
        // those same ids and rechecking engine-owned scalar invariants a second
        // time. The public `try_aggregate` path still performs the full checks
        // for imported/caller-built traces, and `fold_prevalidated` still runs
        // the receipt's final cross-field/details validation.
        let mut receipt = build_prevalidated_receipt(
            &self.branch_id,
            self.modality.clone(),
            &traces,
            baseline_total_time_s,
            baseline_source,
            evidence,
            details,
        );
        charge_batch_wall(&mut receipt, batch_wall_start.elapsed().as_secs_f64());
        receipt.validate().map_err(EngineError::InvalidReceipt)?;
        Ok((outs, receipt))
    }
}

/// Adapter code is outside the trusted generic core. A checked engine API must
/// not let one buggy modality callback unwind through the worker and potentially
/// strand unrelated in-flight units. The engine does not format the panic
/// payload into its error, but the process-global panic hook may still emit it;
/// panic=abort builds and poisoned adapter state are not containable here.
fn checked_callback<T>(
    unit: &str,
    phase: &'static str,
    callback: impl FnOnce() -> T,
) -> Result<T, EngineError> {
    catch_unwind(AssertUnwindSafe(callback)).map_err(|_| EngineError::CallbackPanic {
        unit: unit.into(),
        phase,
    })
}

fn validate_identity(branch_id: &str, modality: &Modality) -> Result<(), EngineError> {
    if branch_id.trim().is_empty() || branch_id.len() > 256 {
        return Err(EngineError::InvalidIdentity(
            "branch_id must be non-empty and <= 256 bytes".into(),
        ));
    }
    let m = modality.as_str();
    if m.is_empty()
        || m.len() > 64
        || !m
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
    {
        return Err(EngineError::InvalidIdentity(
            "modality must be 1..64 ASCII alphanumeric/._- bytes".into(),
        ));
    }
    Ok(())
}

fn validate_baseline(value: f64, source: BaselineSource) -> Result<(), EngineError> {
    if !value.is_finite() || value < 0.0 {
        return Err(EngineError::InvalidBaseline(format!(
            "time must be finite and >= 0, got {value:?}"
        )));
    }
    match source {
        BaselineSource::Absent if value != 0.0 => Err(EngineError::InvalidBaseline(
            "source=absent requires zero baseline time".into(),
        )),
        BaselineSource::Measured | BaselineSource::Modeled if value <= 0.0 => Err(
            EngineError::InvalidBaseline("measured/modeled baseline time must be > 0".into()),
        ),
        _ => Ok(()),
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
    try_aggregate(
        branch_id,
        modality,
        traces,
        baseline_total_time_s,
        baseline_source,
        evidence,
        details,
    )
    .expect("aggregate contract violation; use try_aggregate at trust boundaries")
}

/// Checked receipt fold. Public so import/collector code can validate recorded
/// traces without executing a pipeline.
pub fn try_aggregate(
    branch_id: &str,
    modality: Modality,
    traces: &[UnitTrace],
    baseline_total_time_s: f64,
    baseline_source: BaselineSource,
    evidence: Evidence,
    details: Details,
) -> Result<SpecReceipt, EngineError> {
    validate_identity(branch_id, &modality)?;
    validate_baseline(baseline_total_time_s, baseline_source)?;
    if traces.is_empty() {
        return Err(EngineError::EmptyBatch);
    }
    if traces.len() > MAX_SPEC_BATCH_UNITS {
        return Err(EngineError::TooManyUnits {
            actual: traces.len(),
            max: MAX_SPEC_BATCH_UNITS,
        });
    }
    let mut unique_ids = HashSet::with_capacity(traces.len());
    for trace in traces {
        if trace.unit_id.trim().is_empty() || trace.unit_id.len() > 256 {
            return Err(EngineError::InvalidTrace {
                unit: "<invalid>".into(),
                reason: "unit_id must be non-empty and <= 256 bytes".into(),
            });
        }
        if !unique_ids.insert(trace.unit_id.as_str()) {
            return Err(EngineError::DuplicateUnitIdentity(trace.unit_id.clone()));
        }
        for (name, value) in [
            ("draft_s", trace.draft_s),
            ("verify_s", trace.verify_s),
            ("repair_s", trace.repair_s),
            ("overhead_s", trace.overhead_s),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(EngineError::InvalidTrace {
                    unit: trace.unit_id.clone(),
                    reason: format!("{name} must be finite and >= 0, got {value:?}"),
                });
            }
        }
        if !trace.drafted.is_finite()
            || trace.drafted <= 0.0
            || !trace.accepted.is_finite()
            || trace.accepted < 0.0
            || trace.accepted > trace.drafted
        {
            return Err(EngineError::InvalidTrace {
                unit: trace.unit_id.clone(),
                reason: "drafted must be >0 and accepted finite in [0,drafted]".into(),
            });
        }
        let fully_accepted = trace.accepted >= trace.drafted;
        if trace.repaired == fully_accepted {
            return Err(EngineError::InvalidTrace {
                unit: trace.unit_id.clone(),
                reason: if trace.repaired {
                    "repaired cannot be true for a fully accepted unit"
                } else {
                    "repaired must be true for a partially accepted unit"
                }
                .into(),
            });
        }
        if !trace.repaired && trace.repair_s != 0.0 {
            return Err(EngineError::InvalidTrace {
                unit: trace.unit_id.clone(),
                reason: "repair_s must be zero when repaired is false".into(),
            });
        }
    }
    fold_prevalidated(
        branch_id,
        modality,
        traces,
        baseline_total_time_s,
        baseline_source,
        evidence,
        details,
    )
}

/// Fold traces whose identity and scalar contracts were already checked by the
/// engine-owned batch path. This is deliberately private: callers importing
/// traces must use [`try_aggregate`] and receive its complete validation pass.
fn fold_prevalidated(
    branch_id: &str,
    modality: Modality,
    traces: &[UnitTrace],
    baseline_total_time_s: f64,
    baseline_source: BaselineSource,
    evidence: Evidence,
    details: Details,
) -> Result<SpecReceipt, EngineError> {
    let receipt = build_prevalidated_receipt(
        branch_id,
        modality,
        traces,
        baseline_total_time_s,
        baseline_source,
        evidence,
        details,
    );
    receipt.validate().map_err(EngineError::InvalidReceipt)?;
    Ok(receipt)
}

fn build_prevalidated_receipt(
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
    let overhead: f64 = traces.iter().map(|t| t.overhead_s).sum();
    let total = draft + verify + repair + overhead;

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
        _ if total > 0.0 && baseline_total_time_s > 0.0 => Some(baseline_total_time_s / total),
        _ => None,
    };

    SpecReceipt {
        schema_version: SPEC_RECEIPT_SCHEMA_VERSION,
        branch_id: branch_id.into(),
        modality,
        draft_cost_s: draft,
        verify_cost_s: verify,
        repair_cost_s: repair,
        overhead_cost_s: overhead,
        total_product_time_s: total,
        baseline_total_time_s,
        baseline_source,
        units: traces.len() as u32,
        accepted_fraction: if drafted > 0.0 {
            accepted / drafted
        } else {
            0.0
        },
        repaired_fraction: if traces.is_empty() {
            0.0
        } else {
            repaired / traces.len() as f64
        },
        exact,
        // Internal acceptance traces prove engine control-flow correctness, not
        // authoritative job/artifact attestation. A production adapter may set
        // this only after binding and externally verifying the final artifact.
        artifact_verified: false,
        quality_tier: tier,
        speedup_vs_baseline: speedup,
        evidence,
        details,
    }
}

/// Replace the inner-trace overhead with the successful batch driver's wall
/// remainder. This charges identity/modality preflight, duplicate detection,
/// allocations, trace folding, and receipt construction once while retaining
/// the adapter phase timers as the explicit draft/verify/repair components.
fn charge_batch_wall(receipt: &mut SpecReceipt, elapsed_s: f64) {
    let phase_s = receipt.draft_cost_s + receipt.verify_cost_s + receipt.repair_cost_s;
    let total_s = elapsed_s.max(phase_s);
    receipt.overhead_cost_s = (total_s - phase_s).max(0.0);
    receipt.total_product_time_s = phase_s + receipt.overhead_cost_s;
    receipt.speedup_vs_baseline = match receipt.baseline_source {
        BaselineSource::Absent => None,
        _ if receipt.total_product_time_s > 0.0 && receipt.baseline_total_time_s > 0.0 => {
            Some(receipt.baseline_total_time_s / receipt.total_product_time_s)
        }
        _ => None,
    };
}
