//! The two verify-phase data carriers: [`Verification`] (what the verifier
//! returns) and [`Acceptance`] (what the acceptance policy decides).

use crate::receipt::QualityTier;
use crate::unit::SpecUnit;
use std::fmt;

/// What a [`crate::traits::Verifier`] returns: a comparable score, and
/// OPTIONALLY the authoritative truth.
///
/// The token lane's target pass yields the true continuation as a byproduct, so
/// it returns `truth: Some(..)` for repair to splice. The render lane's cheap
/// gate does NOT render a reference at delivery time, so it returns
/// `truth: None` and allocates nothing it does not have. `truth` is moved into
/// repair by value — no clone on the hot path. Neither lane pays for the other's
/// shape.
pub struct Verification<U: SpecUnit> {
    /// The comparable quality signal (SSIM triple; matched-prefix length).
    pub score: U::Score,
    /// The authoritative output, when the verify phase produced one (token: the
    /// target continuation; render: `None`).
    pub truth: Option<U::Output>,
}

/// How much of the draft is acceptable and at what tier, decided by an
/// [`crate::traits::AcceptancePolicy`].
///
/// It carries the accepted *quantity* (not just a fraction) so the engine folds
/// an honest `Σaccepted / Σdrafted` ratio, not a mis-weighted mean-of-ratios.
/// `tier` is the DELIVERED tier of this unit (render's repaired-to-reference tile
/// is `Delivery`; a preview-shipped draft is `Preview`); `exact` is whether the
/// unit's final output will be lossless vs its baseline.
pub struct Acceptance {
    /// Total drafted work in this unit (k tokens; 1 tile).
    pub drafted: f64,
    /// Accepted-as-is (draft-reused) work (m matched tokens; 1.0 or 0.0 tile).
    pub accepted: f64,
    /// Delivered quality tier for this unit.
    pub tier: QualityTier,
    /// Whether the final unit output is lossless vs baseline.
    pub exact: bool,
}

impl Acceptance {
    /// Reject malformed adapter output before it can steer the accept/repair
    /// branch. Written so NaN and infinities fail closed.
    pub fn validate(&self) -> Result<(), AcceptanceError> {
        if !self.drafted.is_finite() || self.drafted <= 0.0 {
            return Err(AcceptanceError(format!(
                "drafted must be finite and > 0, got {:?}",
                self.drafted
            )));
        }
        if !self.accepted.is_finite() || self.accepted < 0.0 || self.accepted > self.drafted {
            return Err(AcceptanceError(format!(
                "accepted must be finite in [0,drafted], got {:?} for drafted {:?}",
                self.accepted, self.drafted
            )));
        }
        Ok(())
    }

    /// True when the whole draft is accepted as-is — the engine takes the cheap
    /// accept path (no repair cost) exactly when this holds.
    pub fn is_full(&self) -> bool {
        self.accepted >= self.drafted
    }

    /// This unit's own accepted fraction (`accepted / drafted`), 0 when nothing
    /// was drafted. The engine aggregates the Σ/Σ form, not a mean of these.
    pub fn fraction(&self) -> f64 {
        if self.drafted > 0.0 {
            self.accepted / self.drafted
        } else {
            0.0
        }
    }
}

/// Invalid quantity output from a modality adapter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AcceptanceError(pub String);

impl fmt::Display for AcceptanceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for AcceptanceError {}
