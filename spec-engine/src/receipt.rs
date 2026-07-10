//! The unified [`SpecReceipt`] schema and its honesty-label vocabulary.
//!
//! The consolidation plan mandates the spine
//! `draft_cost_s, verify_cost_s, accepted_fraction, repair_cost_s,
//! total_product_time_s, quality_tier, speedup_vs_baseline` plus a modality tag
//! and a free-form details map. This module adds a small, justified set of
//! honesty/compat fields — each maps to an existing Python ledger column
//! (`scripts/spec-lab/cx_speculative_core.py`'s `SpecReceipt.to_dict`) via a
//! serde alias, or to an explicit plan honesty rule — and labels them inline.
//!
//! The on-wire JSON is a 1:1 match for the Go mirror the control plane can
//! ingest (snake_case keys, a nullable `speedup_vs_baseline`, a `map[string]any`
//! `details`), mirroring the conventions of `control/receipt.go`'s
//! `ClearingReceipt` and `control/quote.go`'s `QuoteRouting`.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Free-form details bag (plan: a free-form details map). A `BTreeMap` gives
/// deterministic key ordering for stable golden JSON, and `serde_json::Value`
/// lets a row carry anything the Python ledger's `meta` did (SSIM triple, scene
/// id, prompt class, `source_ledger`/`source_line` for imported rows, ...).
pub type Details = BTreeMap<String, serde_json::Value>;

/// Modality tag (plan: a modality tag). Kept an open string newtype, not a closed
/// enum, so a future video/audio lane needs no schema churn and it matches the
/// Python ledger's free-string `modality`. `#[serde(transparent)]` serializes it
/// as a bare string (`"render"`, not `{"0":"render"}`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Modality(pub String);

impl Modality {
    /// The render lane's tag.
    pub fn render() -> Self {
        Modality("render".into())
    }
    /// The token lane's tag.
    pub fn token() -> Self {
        Modality("token".into())
    }
    /// A genuinely nested end-to-end workload's tag (plan §2a: the ONLY honest
    /// path to a combined number is one delivered job measured through one
    /// pipeline).
    pub fn combined() -> Self {
        Modality("combined".into())
    }
    /// Borrow the tag as a string slice.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Delivered quality tier (plan: `quality_tier`). One vocabulary both lanes
/// speak, matching the plan's per-branch gate score (0 / 0.5 / 1):
///
/// - **render:** the worst DELIVERED tile's visual tier (a preview-shipped draft
///   is `Preview`; a delivery-grade draft or a repaired-to-reference tile is
///   `Delivery`).
/// - **token:** the SPECULATION COVERAGE tier (whole window accepted losslessly
///   by the proposer is `Delivery`; partial acceptance is `Preview`; poor
///   acceptance is `Fail`). Token output is ALWAYS lossless — that fidelity fact
///   lives in [`SpecReceipt::exact`], orthogonal to this coverage tier.
///
/// Declaration order is the severity order (`Fail` worst), so the derived
/// discriminant drives [`QualityTier::min`]'s worst-wins reduction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum QualityTier {
    /// Speculation/quality gate not cleared.
    Fail,
    /// Preview-grade (partial acceptance / preview-shipped). The neutral default
    /// an imported legacy row (which carried only a boolean `quality_gate`) takes
    /// until it is re-graded.
    #[default]
    Preview,
    /// Full delivery grade.
    Delivery,
}

impl QualityTier {
    /// Cross-modal branch-score weight (deep plan Branch 4): Fail=0, Preview=0.5,
    /// Delivery=1.0.
    pub fn gate_score(self) -> f64 {
        match self {
            Self::Fail => 0.0,
            Self::Preview => 0.5,
            Self::Delivery => 1.0,
        }
    }

    /// Worst-wins reduction across units (render's worst-tile discipline): returns
    /// the lower-severity (worse) of the two tiers.
    pub fn min(self, other: Self) -> Self {
        if (self as u8) <= (other as u8) {
            self
        } else {
            other
        }
    }
}

/// Honesty label on the NUMBER as a whole (plan honesty rule: every number
/// MEASURED / MODELED / SYNTHETIC). `Imported` = adapted from a prior measured
/// row, and is the default an unlabeled legacy ledger row deserializes to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Evidence {
    /// Measured on real hardware for this workload.
    Measured,
    /// Modeled from measured inputs (labeled, not a live measurement).
    Modeled,
    /// Synthetic / illustrative — never a hardware win. The two demo adapters are
    /// hard-wired to this.
    Synthetic,
    /// Adapted from a prior measured ledger row (the default for an unlabeled
    /// legacy import — see the serde aliases on [`SpecReceipt`]).
    #[default]
    Imported,
}

/// Honesty label on the speedup DENOMINATOR specifically.
/// [`SpecReceipt::speedup_vs_baseline`] is `None` unless this is `Measured` or
/// `Modeled` — the engine never invents a baseline (plan: the baseline must be a
/// REAL single-lane run of the same delivered unit).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum BaselineSource {
    /// A real single-lane run of the same unit was timed.
    Measured,
    /// The denominator is modeled from measured inputs (the conservative default
    /// an unlabeled legacy row — which still carried a real `baseline_s` — takes).
    #[default]
    Modeled,
    /// No baseline was supplied; `speedup_vs_baseline` MUST be null.
    Absent,
}

/// The one receipt both lanes emit. Field names follow the plan spine; each
/// added field is a compat mirror of a Python ledger column (via `#[serde(alias
/// = ...)]`) or an explicit honesty label. Serializes to exactly the snake_case
/// keys the Go mirror (`control/spec_receipt.go`, sketched in the design doc)
/// binds, with `speedup_vs_baseline` nullable.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpecReceipt {
    // --- identity (matches the Python row's branch_id + modality) ---
    /// Experiment/branch id (Python `branch_id`).
    pub branch_id: String,
    /// Modality tag (plan-mandated).
    pub modality: Modality,

    // --- costs (plan-mandated names). Two legacy families read via aliases:
    //     the Python ledger's `*_s` (cx_speculative_core / token-spec-poc) AND the
    //     render adapter's no-suffix `*_cost` / `total_product_time` / `baseline_cost`
    //     (scripts/spec-lab/cx_render_spec_adapter.py) — additive, canonical names
    //     unchanged, so all three consolidation lanes land on this one spine. ---
    /// Σ draft time across units. Aliases: `draft_s` (Python ledger),
    /// `draft_cost` (render adapter).
    #[serde(alias = "draft_s", alias = "draft_cost")]
    pub draft_cost_s: f64,
    /// Σ verify time across units. Aliases: `verify_s`, `verify_cost`.
    #[serde(alias = "verify_s", alias = "verify_cost")]
    pub verify_cost_s: f64,
    /// Σ repair time across units (0 when nothing was repaired). Aliases:
    /// `repair_s`, `repair_cost`.
    #[serde(alias = "repair_s", alias = "repair_cost")]
    pub repair_cost_s: f64,
    /// Spec wall-clock = draft + verify + repair. Aliases: `speculative_s`
    /// (Python), `total_product_time` (render adapter).
    #[serde(alias = "speculative_s", alias = "total_product_time")]
    pub total_product_time_s: f64,

    // --- the honest denominator (compat + plan number-model; NOT invented) ---
    /// The MEASURED (or explicitly MODELED) baseline input. Aliases: `baseline_s`
    /// (Python), `baseline_cost` (render adapter).
    #[serde(alias = "baseline_s", alias = "baseline_cost")]
    pub baseline_total_time_s: f64,
    /// Honesty label on the denominator above. Absent => speedup is null.
    #[serde(default)]
    pub baseline_source: BaselineSource,

    // --- outcome ---
    /// Number of units folded into this receipt.
    pub units: u32,
    /// Plan-mandated. Σaccepted / Σdrafted across units — a true ratio of
    /// quantities (token-granular), never a mis-weighted mean-of-ratios. Always in
    /// `[0, 1]`.
    pub accepted_fraction: f64,
    /// Fraction of units that took the repair path (Python `repaired_fraction`).
    pub repaired_fraction: f64,
    /// Lossless vs baseline. Token sets this `true` (the repaired output equals the
    /// target continuation); render sets it `false` (never bit-exact vs a full
    /// reference). Orthogonal to `quality_tier`.
    pub exact: bool,
    /// Plan-mandated delivered/coverage tier; worst-wins across units. Defaults to
    /// `Preview` for an unlabeled legacy import.
    #[serde(default)]
    pub quality_tier: QualityTier,

    /// Plan-mandated. `Some` ONLY when `baseline_source != Absent` and the spec
    /// time is positive — else `null` on the wire (a speedup is never fabricated).
    /// ALWAYS a single-workload ratio `baseline/spec`; the engine has no notion of
    /// multiplying two lanes' multipliers. Legacy rows' bare `speedup_x` reads in
    /// via the alias.
    #[serde(alias = "speedup_x")]
    pub speedup_vs_baseline: Option<f64>,

    // --- provenance / free-form (plan-mandated details) ---
    /// Honesty label on the number as a whole. Defaults to `Imported` for an
    /// unlabeled legacy row.
    #[serde(default)]
    pub evidence: Evidence,
    /// Free-form details (plan-mandated). Reads a legacy row's `meta` via the
    /// alias; defaults to empty when absent.
    #[serde(default, alias = "meta")]
    pub details: Details,
}
