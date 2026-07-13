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

use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// Current canonical on-wire schema. Legacy rows without the field can still be
/// inspected, but they cannot assert the v1 artifact-verification bit at the
/// untrusted-ingress boundary; unknown future versions fail validation.
pub const SPEC_RECEIPT_SCHEMA_VERSION: u16 = 1;
/// Hard parse bound for an untrusted worker receipt. Rich per-tile receipts fit
/// comfortably while a forged details bag cannot consume unbounded memory.
pub const MAX_SPEC_RECEIPT_JSON_BYTES: usize = 1 << 20;
/// Maximum object/array nesting at untrusted ingress.
pub const MAX_SPEC_RECEIPT_JSON_DEPTH: usize = 32;
/// The free-form details bag receives only half the total receipt budget, leaving
/// deterministic room for accounting, identity and provenance fields.
pub const MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES: usize = 512 << 10;
/// Cross-lane logical-unit ceiling. Token receipts may legitimately contain up
/// to one million decode rounds; execution engines use tighter per-batch caps.
pub const MAX_SPEC_RECEIPT_UNITS: u32 = 1_000_000;

fn default_schema_version() -> u16 {
    // Direct serde deserialization is useful for legacy inspection but is not a
    // trust boundary. Versionless rows stay explicitly untrusted until the
    // bounded `from_json` migration path handles them.
    0
}

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
    /// Encoded image / raster transformation lane.
    pub fn image() -> Self {
        Modality("image".into())
    }
    /// Video generation/interpolation lane.
    pub fn video() -> Self {
        Modality("video".into())
    }
    /// Video codec/transcode lane.
    pub fn transcode() -> Self {
        Modality("transcode".into())
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
    /// Schema version for explicit cross-language migrations.
    #[serde(default = "default_schema_version")]
    pub schema_version: u16,
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
    /// Policy, assembly, comparison and orchestration wall time that belongs to
    /// none of the three adapter phases. Older emitters omit it and read as zero.
    #[serde(default, alias = "overhead_s")]
    pub overhead_cost_s: f64,
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
    /// The final artifact was checked against the modality's declared contract.
    /// This is deliberately separate from `quality_tier`: render/transcode can
    /// verify a non-bit-exact artifact, while token output can be exact even when
    /// speculation coverage is poor. Legacy aliases are accepted, but a receipt
    /// without `schema_version` is forced to `false` by [`SpecReceipt::from_json`].
    #[serde(default, alias = "delivery_verified", alias = "delivery_eligible")]
    pub artifact_verified: bool,
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

/// A receipt rejected at the trust boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceiptValidationError(pub String);

impl fmt::Display for ReceiptValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ReceiptValidationError {}

/// Parse failures keep malformed JSON distinct from a well-formed but dishonest
/// or internally contradictory receipt.
#[derive(Debug)]
pub enum ReceiptParseError {
    TooLarge { bytes: usize, max: usize },
    Json(serde_json::Error),
    Invalid(ReceiptValidationError),
}

impl fmt::Display for ReceiptParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge { bytes, max } => {
                write!(f, "spec receipt JSON is {bytes} bytes; maximum is {max}")
            }
            Self::Json(err) => write!(f, "invalid spec receipt JSON: {err}"),
            Self::Invalid(err) => write!(f, "invalid spec receipt: {err}"),
        }
    }
}

impl std::error::Error for ReceiptParseError {}

fn finite_nonnegative(name: &str, value: f64) -> Result<(), ReceiptValidationError> {
    if !value.is_finite() || value < 0.0 {
        return Err(ReceiptValidationError(format!(
            "{name} must be finite and >= 0, got {value:?}"
        )));
    }
    Ok(())
}

fn phase_sum_close(a: f64, b: f64) -> bool {
    // Four phase fields and the total are serialized to six decimals. Their
    // worst legitimate independent-rounding drift is only a few microseconds;
    // a relative tolerance would hide seconds on long renders.
    (a - b).abs() <= 5e-6
}

fn rounded_ratio_close(a: f64, b: f64) -> bool {
    // Canonical emitters round the ratio to six decimals after computing it
    // from the rounded wire times.
    (a - b).abs() <= 5e-6
}

pub(crate) fn validate_details(details: &Details) -> Result<(), ReceiptValidationError> {
    let mut stack: Vec<(&serde_json::Value, usize)> =
        details.values().map(|value| (value, 2)).collect();
    while let Some((value, depth)) = stack.pop() {
        if depth > MAX_SPEC_RECEIPT_JSON_DEPTH {
            return Err(ReceiptValidationError(format!(
                "details JSON nesting exceeds {MAX_SPEC_RECEIPT_JSON_DEPTH}"
            )));
        }
        match value {
            serde_json::Value::Array(values) => {
                stack.extend(values.iter().map(|child| (child, depth + 1)));
            }
            serde_json::Value::Object(values) => {
                stack.extend(values.values().map(|child| (child, depth + 1)));
            }
            _ => {}
        }
    }
    let bytes = serde_json::to_vec(details)
        .map_err(|err| ReceiptValidationError(format!("details are not serializable: {err}")))?;
    if bytes.len() > MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES {
        return Err(ReceiptValidationError(format!(
            "details JSON is {} bytes; maximum is {}",
            bytes.len(),
            MAX_SPEC_RECEIPT_DETAILS_JSON_BYTES
        )));
    }
    Ok(())
}

/// JSON shape walk that rejects duplicate keys at every depth and caps nesting
/// before serde collapses objects into maps. Duplicate details keys are just as
/// ambiguous as duplicate accounting keys and must not become last-wins.
struct StrictJsonSeed {
    depth: usize,
}

impl<'de> DeserializeSeed<'de> for StrictJsonSeed {
    type Value = ();

    fn deserialize<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        if self.depth > MAX_SPEC_RECEIPT_JSON_DEPTH {
            return Err(de::Error::custom(format!(
                "JSON nesting exceeds {MAX_SPEC_RECEIPT_JSON_DEPTH}"
            )));
        }
        deserializer.deserialize_any(StrictJsonVisitor { depth: self.depth })
    }
}

struct StrictJsonVisitor {
    depth: usize,
}

impl<'de> Visitor<'de> for StrictJsonVisitor {
    type Value = ();

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("valid JSON without duplicate keys or excessive nesting")
    }

    fn visit_bool<E>(self, _value: bool) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_i64<E>(self, _value: i64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_u64<E>(self, _value: u64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_f64<E>(self, _value: f64) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_str<E>(self, _value: &str) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_string<E>(self, _value: String) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(())
    }

    fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        while seq
            .next_element_seed(StrictJsonSeed {
                depth: self.depth + 1,
            })?
            .is_some()
        {}
        Ok(())
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut keys = BTreeSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !keys.insert(key.clone()) {
                return Err(de::Error::custom(format!("duplicate JSON key {key:?}")));
            }
            map.next_value_seed(StrictJsonSeed {
                depth: self.depth + 1,
            })?;
        }
        Ok(())
    }
}

fn validate_json_shape(input: &str) -> Result<(), serde_json::Error> {
    let mut deserializer = serde_json::Deserializer::from_str(input);
    StrictJsonSeed { depth: 0 }.deserialize(&mut deserializer)?;
    deserializer.end()
}

impl SpecReceipt {
    /// Strict untrusted-ingress parser: size bound, explicit legacy quality-gate
    /// migration, serde contract checks, then semantic validation.
    pub fn from_json(input: &str) -> Result<Self, ReceiptParseError> {
        if input.len() > MAX_SPEC_RECEIPT_JSON_BYTES {
            return Err(ReceiptParseError::TooLarge {
                bytes: input.len(),
                max: MAX_SPEC_RECEIPT_JSON_BYTES,
            });
        }
        validate_json_shape(input).map_err(ReceiptParseError::Json)?;
        // Deserialize the struct directly so serde rejects duplicate canonical/
        // alias fields instead of first collapsing them into a Value map.
        let mut receipt: Self = serde_json::from_str(input).map_err(ReceiptParseError::Json)?;
        let legacy: serde_json::Value =
            serde_json::from_str(input).map_err(ReceiptParseError::Json)?;
        // Compatibility parsing is not attestation. Old rows predate the v1
        // verification contract, so even a similarly named extra field cannot
        // make them billable merely by surviving deserialization.
        if legacy.get("schema_version").is_none() {
            receipt.schema_version = SPEC_RECEIPT_SCHEMA_VERSION;
            receipt.artifact_verified = false;
        }
        // Old emitters carried a boolean quality_gate. Treat it as a strict
        // compatibility projection of the tier: false=fail, true=non-fail.
        // Wrong types and dual-field contradictions are ambiguous at a trust
        // boundary and must fail instead of being silently ignored.
        if let Some(gate_value) = legacy.get("quality_gate") {
            let gate = gate_value.as_bool().ok_or_else(|| {
                ReceiptParseError::Invalid(ReceiptValidationError(
                    "quality_gate must be a boolean when present".into(),
                ))
            })?;
            if legacy.get("quality_tier").is_some() {
                let tier_passes = receipt.quality_tier != QualityTier::Fail;
                if gate != tier_passes {
                    return Err(ReceiptParseError::Invalid(ReceiptValidationError(format!(
                        "quality_gate={gate} contradicts quality_tier={:?}",
                        receipt.quality_tier
                    ))));
                }
            } else if !gate {
                receipt.quality_tier = QualityTier::Fail;
            }
        }
        receipt.validate().map_err(ReceiptParseError::Invalid)?;
        Ok(receipt)
    }

    /// Enforce the cross-language accounting and honesty contract. Imported
    /// legacy rows receive range/provenance checks but skip phase-sum coherence:
    /// old token emitters omitted loop overhead, which is why schema v1 now has
    /// `overhead_cost_s` explicitly.
    pub fn validate(&self) -> Result<(), ReceiptValidationError> {
        if self.schema_version != SPEC_RECEIPT_SCHEMA_VERSION {
            return Err(ReceiptValidationError(format!(
                "unsupported schema_version {}; expected {}",
                self.schema_version, SPEC_RECEIPT_SCHEMA_VERSION
            )));
        }
        validate_details(&self.details)?;
        let branch = self.branch_id.trim();
        if branch.is_empty() || branch.len() > 256 {
            return Err(ReceiptValidationError(
                "branch_id must be non-empty and <= 256 bytes".into(),
            ));
        }
        let modality = self.modality.as_str();
        if modality.is_empty()
            || modality.len() > 64
            || !modality
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        {
            return Err(ReceiptValidationError(
                "modality must be 1..64 ASCII alphanumeric/._- bytes".into(),
            ));
        }
        for (name, value) in [
            ("draft_cost_s", self.draft_cost_s),
            ("verify_cost_s", self.verify_cost_s),
            ("repair_cost_s", self.repair_cost_s),
            ("overhead_cost_s", self.overhead_cost_s),
            ("total_product_time_s", self.total_product_time_s),
            ("baseline_total_time_s", self.baseline_total_time_s),
        ] {
            finite_nonnegative(name, value)?;
        }
        if self.units > MAX_SPEC_RECEIPT_UNITS {
            return Err(ReceiptValidationError(format!(
                "units {} exceeds safety maximum {}",
                self.units, MAX_SPEC_RECEIPT_UNITS
            )));
        }
        for (name, value) in [
            ("accepted_fraction", self.accepted_fraction),
            ("repaired_fraction", self.repaired_fraction),
        ] {
            if !value.is_finite() || !(0.0..=1.0).contains(&value) {
                return Err(ReceiptValidationError(format!(
                    "{name} must be finite in [0,1], got {value:?}"
                )));
            }
        }
        if self.units == 0
            && (self.accepted_fraction != 0.0
                || self.repaired_fraction != 0.0
                || self.exact
                || self.artifact_verified
                || self.quality_tier != QualityTier::Fail
                || self.draft_cost_s != 0.0
                || self.verify_cost_s != 0.0
                || self.repair_cost_s != 0.0
                || self.overhead_cost_s != 0.0
                || self.total_product_time_s != 0.0
                || self.baseline_total_time_s != 0.0
                || self.baseline_source != BaselineSource::Absent
                || self.speedup_vs_baseline.is_some())
        {
            return Err(ReceiptValidationError(
                "an empty receipt cannot claim work, a baseline, speedup, correctness, verification or a non-fail tier"
                    .into(),
            ));
        }
        if self.artifact_verified
            && (self.evidence != Evidence::Measured || self.quality_tier == QualityTier::Fail)
        {
            return Err(ReceiptValidationError(
                "artifact_verified=true requires measured evidence and a non-fail quality tier"
                    .into(),
            ));
        }
        if self.units > 0 && self.total_product_time_s <= 0.0 {
            return Err(ReceiptValidationError(
                "a non-empty receipt must charge positive total_product_time_s".into(),
            ));
        }
        if self.evidence != Evidence::Imported {
            let phase_total =
                self.draft_cost_s + self.verify_cost_s + self.repair_cost_s + self.overhead_cost_s;
            if !phase_sum_close(self.total_product_time_s, phase_total) {
                return Err(ReceiptValidationError(format!(
                    "total_product_time_s {} contradicts charged phase sum {}",
                    self.total_product_time_s, phase_total
                )));
            }
        }
        match self.baseline_source {
            BaselineSource::Absent => {
                if self.baseline_total_time_s != 0.0 || self.speedup_vs_baseline.is_some() {
                    return Err(ReceiptValidationError(
                        "baseline_source=absent requires zero baseline time and null speedup"
                            .into(),
                    ));
                }
            }
            BaselineSource::Measured | BaselineSource::Modeled => {
                if self.baseline_total_time_s <= 0.0 {
                    return Err(ReceiptValidationError(format!(
                        "baseline_source={:?} requires positive baseline_total_time_s",
                        self.baseline_source
                    )));
                }
                if let Some(speedup) = self.speedup_vs_baseline {
                    if !speedup.is_finite() || speedup <= 0.0 {
                        return Err(ReceiptValidationError(format!(
                            "speedup_vs_baseline must be finite and > 0, got {speedup:?}"
                        )));
                    }
                    if self.baseline_total_time_s <= 0.0 || self.total_product_time_s <= 0.0 {
                        return Err(ReceiptValidationError(
                            "a reported speedup requires positive baseline and product times"
                                .into(),
                        ));
                    }
                    if self.evidence != Evidence::Imported {
                        let expected = self.baseline_total_time_s / self.total_product_time_s;
                        if !rounded_ratio_close(speedup, expected) {
                            return Err(ReceiptValidationError(format!(
                                "speedup_vs_baseline {speedup} contradicts baseline/total {expected}"
                            )));
                        }
                    }
                }
            }
        }
        Ok(())
    }

    /// Product eligibility is derived from verification, evidence and outcome.
    /// `artifact_verified` is a modality-contract fact, not a billability bit;
    /// synthetic/modeled/imported evidence still parks even when tests verified
    /// the fixture output.
    pub fn delivery_eligible(&self) -> bool {
        self.validate().is_ok()
            && self.artifact_verified
            && self.evidence == Evidence::Measured
            && self.quality_tier != QualityTier::Fail
    }
}
