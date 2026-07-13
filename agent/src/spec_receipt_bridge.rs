//! Default-off bridge from a *completed* Candle speculative decode to the
//! canonical cross-modality [`cx_spec_engine::SpecReceipt`].
//!
//! This module intentionally does not drive a model, mutate KV state, select a
//! runtime path, publish an artifact, or make a billing/admission decision. The
//! production Candle loop already owns the exact target-authoritative
//! accept/correct transaction; wrapping that stateful loop in the generic engine
//! would either execute it twice or split its rollback boundary. Instead, this
//! bridge folds its immutable counters only after successful completion.
//!
//! The current loop does not collect per-phase wall times. A caller therefore
//! supplies one measured end-to-end duration, which is charged exactly once to
//! `overhead_cost_s` and explicitly labelled as unattributed in `details`. This
//! is deliberately conservative: it avoids fabricating draft/verify/repair
//! timings while still making hidden time impossible. Receipts remain
//! `artifact_verified=false`, carry no baseline or speedup, and are telemetry
//! only until an independently checked, job-bound output contract exists.

use std::fmt;
use std::time::Duration;

use cx_spec_engine::{
    try_aggregate, BaselineSource, Details, EngineError, Evidence, Modality, QualityTier,
    SpecReceipt, UnitTrace,
};
use serde_json::json;

/// Largest integer whose conversion to `f64` is exact. The canonical engine's
/// acceptance fold uses floating-point quantities, so reject larger counters
/// rather than silently rounding an observed token count.
const MAX_EXACT_TOKEN_QUANTITY: u64 = 1u64 << 53;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum CandleSpecPath {
    Scalar,
    Batch { rows: usize },
}

impl CandleSpecPath {
    fn as_str(self) -> &'static str {
        match self {
            Self::Scalar => "scalar",
            Self::Batch { .. } => "batch",
        }
    }

    fn rows(self) -> usize {
        match self {
            Self::Scalar => 1,
            Self::Batch { rows } => rows,
        }
    }
}

/// Immutable evidence already produced by a successful target-authoritative
/// Candle speculation call. Fields stay private so construction is forced
/// through the checked constructor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct CompletedCandleSpecObservation {
    branch_id: String,
    unit_id: String,
    path: CandleSpecPath,
    attempted_draft_tokens: usize,
    accepted_draft_tokens: usize,
    synchronized_clamp_tokens: usize,
    output_tokens: usize,
    target_calls: usize,
    rounds: usize,
    e2e_elapsed: Duration,
}

#[derive(Debug)]
pub(crate) enum ReceiptBridgeError {
    InvalidObservation(String),
    CommonEngine(EngineError),
}

impl fmt::Display for ReceiptBridgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidObservation(reason) => {
                write!(
                    formatter,
                    "invalid completed Candle speculation observation: {reason}"
                )
            }
            Self::CommonEngine(error) => {
                write!(formatter, "common spec-engine rejected receipt: {error}")
            }
        }
    }
}

impl std::error::Error for ReceiptBridgeError {}

impl From<EngineError> for ReceiptBridgeError {
    fn from(error: EngineError) -> Self {
        Self::CommonEngine(error)
    }
}

impl CompletedCandleSpecObservation {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn try_new(
        branch_id: &str,
        unit_id: &str,
        path: CandleSpecPath,
        attempted_draft_tokens: usize,
        accepted_draft_tokens: usize,
        synchronized_clamp_tokens: usize,
        output_tokens: usize,
        target_calls: usize,
        rounds: usize,
        e2e_elapsed: Duration,
    ) -> Result<Self, ReceiptBridgeError> {
        if branch_id.trim().is_empty() || branch_id.len() > 256 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "branch_id must be non-empty and <= 256 bytes".into(),
            ));
        }
        if unit_id.trim().is_empty() || unit_id.len() > 256 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "unit_id must be non-empty and <= 256 bytes".into(),
            ));
        }
        if attempted_draft_tokens == 0 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "no draft tokens were attempted; there is no speculation receipt to emit".into(),
            ));
        }
        for (name, value) in [
            ("attempted_draft_tokens", attempted_draft_tokens),
            ("accepted_draft_tokens", accepted_draft_tokens),
            ("synchronized_clamp_tokens", synchronized_clamp_tokens),
            ("output_tokens", output_tokens),
            ("target_calls", target_calls),
            ("rounds", rounds),
            ("rows", path.rows()),
        ] {
            let value_u64 = u64::try_from(value).map_err(|_| {
                ReceiptBridgeError::InvalidObservation(format!(
                    "{name}={value} cannot be represented by the canonical receipt counter"
                ))
            })?;
            if value_u64 > MAX_EXACT_TOKEN_QUANTITY {
                return Err(ReceiptBridgeError::InvalidObservation(format!(
                    "{name}={value} exceeds the exact receipt quantity ceiling {MAX_EXACT_TOKEN_QUANTITY}"
                )));
            }
        }
        if accepted_draft_tokens > attempted_draft_tokens {
            return Err(ReceiptBridgeError::InvalidObservation(format!(
                "accepted_draft_tokens={accepted_draft_tokens} exceeds attempted_draft_tokens={attempted_draft_tokens}"
            )));
        }
        let unaccepted_draft_tokens = attempted_draft_tokens - accepted_draft_tokens;
        if synchronized_clamp_tokens > unaccepted_draft_tokens {
            return Err(ReceiptBridgeError::InvalidObservation(format!(
                "synchronized_clamp_tokens={synchronized_clamp_tokens} exceeds unaccepted_draft_tokens={unaccepted_draft_tokens}"
            )));
        }
        if path.rows() == 0 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "a completed batch must contain at least one row".into(),
            ));
        }
        // Production counters count only visible accepted tokens (EOS is not
        // returned), so an accepted count beyond delivered output is malformed.
        if accepted_draft_tokens > output_tokens {
            return Err(ReceiptBridgeError::InvalidObservation(format!(
                "accepted_draft_tokens={accepted_draft_tokens} exceeds output_tokens={output_tokens}"
            )));
        }
        if output_tokens == 0 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "a completed attempted decode must have at least one output token".into(),
            ));
        }
        if target_calls == 0 || rounds == 0 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "a completed attempted decode must have positive target_calls and rounds".into(),
            ));
        }
        if e2e_elapsed.is_zero() {
            return Err(ReceiptBridgeError::InvalidObservation(
                "measured end-to-end duration must be positive".into(),
            ));
        }

        Ok(Self {
            branch_id: branch_id.to_owned(),
            unit_id: unit_id.to_owned(),
            path,
            attempted_draft_tokens,
            accepted_draft_tokens,
            synchronized_clamp_tokens,
            output_tokens,
            target_calls,
            rounds,
            e2e_elapsed,
        })
    }

    /// Fold one completed production call into the common receipt type.
    ///
    /// `exact=true` records the live loop's target-authoritative accept-or-correct
    /// contract, not an independent comparison. Consequently the receipt always
    /// remains unverified and ineligible for delivery/settlement admission.
    pub(crate) fn try_into_receipt(self) -> Result<SpecReceipt, ReceiptBridgeError> {
        let tier = if self.accepted_draft_tokens == self.attempted_draft_tokens {
            QualityTier::Delivery
        } else if self.accepted_draft_tokens > 0 {
            QualityTier::Preview
        } else {
            QualityTier::Fail
        };
        let unaccepted_draft_tokens = self
            .attempted_draft_tokens
            .checked_sub(self.accepted_draft_tokens)
            .expect("accepted count was checked against attempted count");
        let elapsed_s = self.e2e_elapsed.as_secs_f64();
        if !elapsed_s.is_finite() || elapsed_s <= 0.0 {
            return Err(ReceiptBridgeError::InvalidObservation(
                "measured end-to-end duration is not a finite positive number of seconds".into(),
            ));
        }

        let mut details = Details::new();
        details.insert("bridge".into(), json!("candle_common_spec_receipt_v1"));
        details.insert("inference_path".into(), json!(self.path.as_str()));
        details.insert("rows".into(), json!(self.path.rows()));
        details.insert(
            "timing_scope".into(),
            json!("end_to_end_unattributed_charged_once_to_overhead"),
        );
        details.insert(
            "exactness_basis".into(),
            json!("target_authoritative_accept_or_correction_by_construction"),
        );
        details.insert("independent_output_verification".into(), json!(false));
        details.insert("artifact_binding".into(), json!("absent"));
        details.insert("admission_scope".into(), json!("telemetry_only"));
        details.insert(
            "attempted_draft_tokens".into(),
            json!(self.attempted_draft_tokens),
        );
        details.insert(
            "accepted_draft_tokens".into(),
            json!(self.accepted_draft_tokens),
        );
        // Dense cohorts can deliberately decline a target-correct token beyond
        // their common committed prefix. `unaccepted` is therefore the honest
        // complement; calling every such token a verifier rejection would lie.
        details.insert(
            "unaccepted_draft_tokens".into(),
            json!(unaccepted_draft_tokens),
        );
        details.insert(
            "synchronized_clamp_tokens".into(),
            json!(self.synchronized_clamp_tokens),
        );
        details.insert("output_tokens".into(), json!(self.output_tokens));
        details.insert("target_calls".into(), json!(self.target_calls));
        details.insert("rounds".into(), json!(self.rounds));

        let trace = UnitTrace {
            unit_id: self.unit_id,
            draft_s: 0.0,
            verify_s: 0.0,
            repair_s: 0.0,
            // Phase timers do not exist in the production loop. Charging its
            // entire measured wall once here is explicit and conservative.
            overhead_s: elapsed_s,
            drafted: self.attempted_draft_tokens as f64,
            accepted: self.accepted_draft_tokens as f64,
            repaired: unaccepted_draft_tokens > 0,
            tier,
            exact: true,
        };
        let receipt = try_aggregate(
            &self.branch_id,
            Modality::token(),
            &[trace],
            0.0,
            BaselineSource::Absent,
            Evidence::Measured,
            details,
        )?;

        // Defense in depth over common-engine invariants. Keep this a runtime
        // check (not a debug assertion) so a future schema change cannot turn a
        // telemetry bridge into product authorization in a release build.
        if receipt.artifact_verified
            || receipt.baseline_source != BaselineSource::Absent
            || receipt.baseline_total_time_s != 0.0
            || receipt.speedup_vs_baseline.is_some()
            || receipt.delivery_eligible()
        {
            return Err(ReceiptBridgeError::InvalidObservation(
                "common receipt unexpectedly crossed the unverified/no-baseline admission boundary"
                    .into(),
            ));
        }
        Ok(receipt)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observation(accepted: usize) -> CompletedCandleSpecObservation {
        CompletedCandleSpecObservation::try_new(
            "candle-production-observation-v1",
            "request-7",
            CandleSpecPath::Scalar,
            10,
            accepted,
            0,
            12,
            4,
            4,
            Duration::from_millis(25),
        )
        .unwrap()
    }

    #[test]
    fn partial_acceptance_folds_token_quantities_and_charges_wall_once() {
        let receipt = observation(6).try_into_receipt().unwrap();
        assert_eq!(receipt.modality, Modality::token());
        assert_eq!(receipt.units, 1);
        assert!((receipt.accepted_fraction - 0.6).abs() < 1e-12);
        assert_eq!(receipt.repaired_fraction, 1.0);
        assert_eq!(receipt.quality_tier, QualityTier::Preview);
        assert_eq!(receipt.draft_cost_s, 0.0);
        assert_eq!(receipt.verify_cost_s, 0.0);
        assert_eq!(receipt.repair_cost_s, 0.0);
        assert_eq!(receipt.overhead_cost_s, 0.025);
        assert_eq!(receipt.total_product_time_s, 0.025);
        assert!(receipt.exact);
        assert!(!receipt.artifact_verified);
        assert!(!receipt.delivery_eligible());
        assert_eq!(receipt.baseline_source, BaselineSource::Absent);
        assert_eq!(receipt.baseline_total_time_s, 0.0);
        assert_eq!(receipt.speedup_vs_baseline, None);
        assert_eq!(receipt.details["unaccepted_draft_tokens"], json!(4));
        assert_eq!(
            receipt.details["independent_output_verification"],
            json!(false)
        );
    }

    #[test]
    fn coverage_tiers_do_not_grant_artifact_verification() {
        let delivery = observation(10).try_into_receipt().unwrap();
        assert_eq!(delivery.quality_tier, QualityTier::Delivery);
        assert_eq!(delivery.repaired_fraction, 0.0);
        assert!(!delivery.artifact_verified);
        assert!(!delivery.delivery_eligible());

        let fail = observation(0).try_into_receipt().unwrap();
        assert_eq!(fail.quality_tier, QualityTier::Fail);
        assert_eq!(fail.repaired_fraction, 1.0);
        assert!(
            fail.exact,
            "target correction still preserves greedy output"
        );
        assert!(!fail.artifact_verified);
    }

    #[test]
    fn canonical_json_round_trip_stays_parked() {
        let receipt = observation(6).try_into_receipt().unwrap();
        let wire = serde_json::to_string(&receipt).unwrap();
        let parsed = SpecReceipt::from_json(&wire).unwrap();
        assert_eq!(parsed.branch_id, receipt.branch_id);
        assert_eq!(parsed.accepted_fraction, receipt.accepted_fraction);
        assert!(!parsed.artifact_verified);
        assert!(!parsed.delivery_eligible());
    }

    #[test]
    fn malformed_or_non_speculative_observations_fail_closed() {
        let make = |attempted, accepted, output, calls, rounds, elapsed| {
            CompletedCandleSpecObservation::try_new(
                "branch",
                "unit",
                CandleSpecPath::Batch { rows: 2 },
                attempted,
                accepted,
                0,
                output,
                calls,
                rounds,
                elapsed,
            )
        };
        assert!(make(0, 0, 1, 1, 1, Duration::from_nanos(1)).is_err());
        assert!(make(2, 3, 3, 1, 1, Duration::from_nanos(1)).is_err());
        assert!(make(3, 2, 1, 1, 1, Duration::from_nanos(1)).is_err());
        assert!(make(2, 1, 2, 0, 1, Duration::from_nanos(1)).is_err());
        assert!(make(2, 1, 2, 1, 0, Duration::from_nanos(1)).is_err());
        assert!(make(2, 1, 2, 1, 1, Duration::ZERO).is_err());
        assert!(CompletedCandleSpecObservation::try_new(
            "branch",
            "unit",
            CandleSpecPath::Batch { rows: 0 },
            2,
            1,
            0,
            2,
            1,
            1,
            Duration::from_nanos(1),
        )
        .is_err());
        assert!(CompletedCandleSpecObservation::try_new(
            "branch",
            "unit",
            CandleSpecPath::Batch { rows: 2 },
            2,
            1,
            2,
            2,
            1,
            1,
            Duration::from_nanos(1),
        )
        .is_err());
    }

    #[test]
    fn local_preflight_rejects_invalid_identity() {
        let observation = CompletedCandleSpecObservation::try_new(
            " ",
            "unit",
            CandleSpecPath::Scalar,
            1,
            1,
            0,
            1,
            1,
            1,
            Duration::from_nanos(1),
        );
        assert!(matches!(
            observation,
            Err(ReceiptBridgeError::InvalidObservation(_))
        ));
    }
}
