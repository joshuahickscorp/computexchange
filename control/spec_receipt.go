package main

// spec_receipt.go — the Go mirror of the canonical SpecEngine receipt
// (spec-engine/src/receipt.rs), closing the consolidation wave's one named gap:
// "control/spec_receipt.go does NOT exist (Go ingest is proven structurally,
// not by a real unmarshal)" (docs/research/CONSOLIDATION_PLAN_2026-07-09.md,
// Branch A "Remains").
//
// PURE ADDITIVE TYPE + PARSE + VALIDATE. Nothing here is wired into quote/
// submit/store/receipt — the product-surface wiring is a later, owner-approved
// climb (design: docs/research/CX_SPEC_LANE_INTEGRATION_DESIGN.md). Stdlib
// encoding/json only; no new dependencies.
//
// Wire contract, mirrored 1:1 from receipt.rs:
//   - canonical snake_case keys (the Serialize side of receipt.rs);
//   - the documented serde aliases accepted on ingest, so legacy and canonical lanes'
//     REAL emitters parse (cx_speculative_core.py `*_s`, the render adapter's
//     no-suffix `*_cost`, token-spec-poc's `*_s`) — proven against verbatim
//     emitter output in spec_receipt_test.go;
//   - `#[serde(default)]` fields default the same way (baseline_source=modeled,
//     quality_tier=preview, evidence=imported, details={});
//   - `speedup_vs_baseline` is nullable and never invented;
//   - unknown keys are ignored (serde's default), except that a present legacy
//     quality_gate is type-checked and reconciled with quality_tier; other extra
//     columns (accepted_units, global_ssim, ...) pass through harmlessly —
//     anything worth keeping lives in the emitter's `meta`, which lands in
//     Details via the alias.
//
// Canonical+alias conflicts and duplicate keys at any JSON depth are rejected,
// matching the Rust trust boundary instead of silently choosing one value.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"strings"
	"unicode/utf8"
)

const (
	specReceiptSchemaVersion = 1
	maxSpecReceiptBytes      = 1 << 20
	maxSpecReceiptJSONDepth  = 32
	maxSpecReceiptUnits      = 1_000_000
)

// Quality tier vocabulary (receipt.rs QualityTier, snake_case on the wire).
// fail < preview < delivery; the render lane's worst-wins discipline means a
// shot's tier is its worst delivered unit's tier.
const (
	SpecTierFail     = "fail"
	SpecTierPreview  = "preview" // the default an unlabeled legacy row takes
	SpecTierDelivery = "delivery"
)

// Evidence vocabulary (receipt.rs Evidence): the honesty label on the NUMBER
// as a whole. Every number is MEASURED / MODELED / SYNTHETIC; `imported` marks
// a row adapted from a prior measured ledger (the unlabeled-legacy default).
const (
	SpecEvidenceMeasured  = "measured"
	SpecEvidenceModeled   = "modeled"
	SpecEvidenceSynthetic = "synthetic"
	SpecEvidenceImported  = "imported" // default for an unlabeled legacy row
)

// Baseline-source vocabulary (receipt.rs BaselineSource): the honesty label on
// the speedup DENOMINATOR. `absent` means no baseline was supplied and
// speedup_vs_baseline MUST be null — the engine never invents a baseline.
const (
	SpecBaselineMeasured = "measured"
	SpecBaselineModeled  = "modeled" // conservative default for a legacy row
	SpecBaselineAbsent   = "absent"
)

// SpecReceipt is the one receipt every speculation lane emits (render, token,
// and — only when genuinely nested — combined). Field-for-field mirror of
// spec-engine/src/receipt.rs; json tags are the canonical wire keys. Costs are
// seconds; speedup_vs_baseline is ALWAYS a single-workload ratio baseline/spec
// — there is deliberately no field, method, or code path for multiplying two
// lanes' multipliers (the plan's invariant #1).
type SpecReceipt struct {
	// Explicit cross-language migration version. Legacy rows default to v1.
	SchemaVersion uint16 `json:"schema_version"`
	// Identity.
	BranchID string `json:"branch_id"` // experiment/branch id
	Modality string `json:"modality"`  // open tag: "render" | "token" | "combined" | future lanes

	// Costs (plan-mandated spine). Ingest aliases per receipt.rs:
	//   draft_cost_s:          draft_s (Python ledger), draft_cost (render adapter)
	//   verify_cost_s:         verify_s, verify_cost
	//   repair_cost_s:         repair_s, repair_cost
	//   total_product_time_s:  speculative_s, total_product_time
	DraftCostS  float64 `json:"draft_cost_s"`
	VerifyCostS float64 `json:"verify_cost_s"`
	RepairCostS float64 `json:"repair_cost_s"`
	// Policy/assembly/accounting wall time outside the three adapter phases.
	// Legacy rows omit this and default to zero.
	OverheadCostS     float64 `json:"overhead_cost_s"`
	TotalProductTimeS float64 `json:"total_product_time_s"`

	// The honest denominator: a REAL (or explicitly modeled) single-lane run of
	// the SAME delivered unit. Aliases: baseline_s, baseline_cost.
	BaselineTotalTimeS float64 `json:"baseline_total_time_s"`
	// BaselineSource labels the denominator; "absent" forces a null speedup.
	// Defaults to "modeled" when the row does not carry it (receipt.rs default).
	BaselineSource string `json:"baseline_source"`

	// Outcome.
	Units uint32 `json:"units"` // u32 in receipt.rs: negative counts are a parse error
	// AcceptedFraction is Σaccepted/Σdrafted across units — a true ratio of
	// quantities, always in [0,1].
	AcceptedFraction float64 `json:"accepted_fraction"`
	// RepairedFraction is the fraction of units that took the repair path.
	RepairedFraction float64 `json:"repaired_fraction"`
	// Exact: lossless vs baseline. Token lanes set true (repaired output equals
	// the target continuation); render sets false (never bit-exact vs a full
	// reference). Orthogonal to QualityTier.
	Exact bool `json:"exact"`
	// ArtifactVerified says the final output was checked against the modality's
	// declared contract. It is not a worker-controlled billing decision: product
	// eligibility also requires measured evidence and a non-fail outcome. Rows
	// predating schema_version cannot assert it.
	ArtifactVerified bool `json:"artifact_verified"`
	// QualityTier is the delivered/coverage tier (fail|preview|delivery),
	// worst-wins across units. Defaults to "preview" for an unlabeled legacy row.
	QualityTier string `json:"quality_tier"`

	// SpeedupVsBaseline is nullable: non-nil ONLY when a real baseline exists
	// (BaselineSource != "absent") — a speedup is never fabricated. Ingest
	// alias: speedup_x (legacy rows' bare ratio).
	SpeedupVsBaseline *float64 `json:"speedup_vs_baseline"`

	// Evidence labels the number as a whole; defaults to "imported" for an
	// unlabeled legacy row.
	Evidence string `json:"evidence"`
	// Details is the free-form bag (SSIM triple, scene id, prompt class,
	// source_ledger, selector_recall, ...). Ingest alias: meta. Defaults to
	// an empty (non-nil) map so canonical re-serialization emits {} like the
	// Rust BTreeMap does.
	Details map[string]any `json:"details"`
}

// specReceiptField is one canonical wire field plus its accepted ingest
// aliases, in receipt.rs declaration order (canonical first).
type specReceiptField struct {
	canonical string
	aliases   []string
}

// UnmarshalJSON ingests a receipt from ANY of the three lanes' real emitters:
// canonical keys first, then the receipt.rs serde aliases. Required fields
// (everything receipt.rs declares without #[serde(default)] — the cost spine,
// identity, units, fractions, exact) error when absent under every accepted
// name; defaulted fields take the receipt.rs defaults; unknown keys are
// ignored. speedup_vs_baseline is optional (missing or null => nil), matching
// serde's Option<f64> handling.
func (r *SpecReceipt) UnmarshalJSON(data []byte) error {
	if len(data) > maxSpecReceiptBytes {
		return fmt.Errorf("spec receipt: JSON is %d bytes; maximum is %d", len(data), maxSpecReceiptBytes)
	}
	if !utf8.Valid(data) {
		return fmt.Errorf("spec receipt: JSON must be valid UTF-8")
	}
	if err := rejectDuplicateSpecJSONKeys(data); err != nil {
		return err
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("spec receipt: %w", err)
	}

	pick := func(f specReceiptField) (json.RawMessage, bool, error) {
		var found json.RawMessage
		var names []string
		for _, name := range append([]string{f.canonical}, f.aliases...) {
			if v, ok := raw[name]; ok {
				found = v
				names = append(names, name)
			}
		}
		if len(names) > 1 {
			return nil, false, fmt.Errorf(
				"spec receipt: conflicting canonical/alias fields for %q: %v",
				f.canonical, names,
			)
		}
		return found, len(names) == 1, nil
	}
	// required: the field must be present under some accepted name and decode.
	required := func(dst any, f specReceiptField) error {
		v, ok, err := pick(f)
		if err != nil {
			return err
		}
		if !ok {
			if len(f.aliases) == 0 {
				return fmt.Errorf("spec receipt: missing required field %q", f.canonical)
			}
			return fmt.Errorf("spec receipt: missing required field %q (accepted aliases %v)", f.canonical, f.aliases)
		}
		if bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			return fmt.Errorf("spec receipt: field %q cannot be null", f.canonical)
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", f.canonical, err)
		}
		return nil
	}
	// optional: absent leaves the pre-set default in place. Explicit null is not
	// absence for scalar/map fields and is rejected to match Rust serde.
	optional := func(dst any, f specReceiptField) error {
		v, ok, err := pick(f)
		if err != nil {
			return err
		}
		if !ok {
			return nil
		}
		if bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			return fmt.Errorf("spec receipt: field %q cannot be null", f.canonical)
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", f.canonical, err)
		}
		return nil
	}
	optionalNullable := func(dst any, f specReceiptField) error {
		v, ok, err := pick(f)
		if err != nil || !ok {
			return err
		}
		if bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			return nil
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", f.canonical, err)
		}
		return nil
	}

	// receipt.rs #[serde(default)] values, applied before decode.
	out := SpecReceipt{
		SchemaVersion:  specReceiptSchemaVersion,
		BaselineSource: SpecBaselineModeled,
		QualityTier:    SpecTierPreview,
		Evidence:       SpecEvidenceImported,
		Details:        map[string]any{},
	}

	// The required spine (receipt.rs fields without #[serde(default)]).
	steps := []error{
		optional(&out.SchemaVersion, specReceiptField{canonical: "schema_version"}),
		required(&out.BranchID, specReceiptField{canonical: "branch_id"}),
		required(&out.Modality, specReceiptField{canonical: "modality"}),
		required(&out.DraftCostS, specReceiptField{"draft_cost_s", []string{"draft_s", "draft_cost"}}),
		required(&out.VerifyCostS, specReceiptField{"verify_cost_s", []string{"verify_s", "verify_cost"}}),
		required(&out.RepairCostS, specReceiptField{"repair_cost_s", []string{"repair_s", "repair_cost"}}),
		optional(&out.OverheadCostS, specReceiptField{"overhead_cost_s", []string{"overhead_s"}}),
		required(&out.TotalProductTimeS, specReceiptField{"total_product_time_s", []string{"speculative_s", "total_product_time"}}),
		required(&out.BaselineTotalTimeS, specReceiptField{"baseline_total_time_s", []string{"baseline_s", "baseline_cost"}}),
		required(&out.Units, specReceiptField{canonical: "units"}),
		required(&out.AcceptedFraction, specReceiptField{canonical: "accepted_fraction"}),
		required(&out.RepairedFraction, specReceiptField{canonical: "repaired_fraction"}),
		required(&out.Exact, specReceiptField{canonical: "exact"}),
		// Defaulted / optional fields.
		optional(&out.BaselineSource, specReceiptField{canonical: "baseline_source"}),
		optional(&out.ArtifactVerified, specReceiptField{"artifact_verified", []string{"delivery_verified", "delivery_eligible"}}),
		optional(&out.QualityTier, specReceiptField{canonical: "quality_tier"}),
		optionalNullable(&out.SpeedupVsBaseline, specReceiptField{"speedup_vs_baseline", []string{"speedup_x"}}),
		optional(&out.Evidence, specReceiptField{canonical: "evidence"}),
		optional(&out.Details, specReceiptField{"details", []string{"meta"}}),
	}
	for _, err := range steps {
		if err != nil {
			return err
		}
	}
	// Compatibility parsing is not attestation. Legacy rows predate the v1
	// artifact-verification contract, so similarly named extras remain audit-only.
	if _, hasSchema := raw["schema_version"]; !hasSchema {
		out.ArtifactVerified = false
	}
	// Explicit legacy migration. The boolean is a compatibility projection of
	// quality_tier: false=fail and true=non-fail. Never accept a wrong type or
	// contradictory dual representation at the trust boundary.
	if gateRaw, ok := raw["quality_gate"]; ok {
		if bytes.Equal(bytes.TrimSpace(gateRaw), []byte("null")) {
			return fmt.Errorf("spec receipt: field %q cannot be null", "quality_gate")
		}
		var gate bool
		if err := json.Unmarshal(gateRaw, &gate); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", "quality_gate", err)
		}
		if _, hasTier := raw["quality_tier"]; hasTier {
			tierPasses := out.QualityTier != SpecTierFail
			if gate != tierPasses {
				return fmt.Errorf(
					"spec receipt: quality_gate=%t contradicts quality_tier=%q",
					gate, out.QualityTier,
				)
			}
		} else if !gate {
			out.QualityTier = SpecTierFail
		}
	}
	*r = out
	return nil
}

// specFractionValid reports 0 <= v <= 1, written so NaN fails too.
func specFractionValid(v float64) bool { return v >= 0 && v <= 1 }

// specTimeValid reports v >= 0, written so NaN fails too.
func specTimeValid(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) && v >= 0 }

func specPhaseSumClose(a, b float64) bool {
	// Four phase fields and total are six-decimal wire values. Independent
	// rounding can drift only a few microseconds; relative tolerance would hide
	// seconds of uncharged work on a long render.
	return math.Abs(a-b) <= 5e-6
}

func specRoundedRatioClose(a, b float64) bool {
	return math.Abs(a-b) <= 5e-6
}

// Validate enforces the receipt.rs range/enum contract on an already-parsed
// receipt: fractions in [0,1], every cost/time >= 0, speedup null-or-positive,
// the three closed vocabularies, and the baseline honesty rule (an "absent"
// baseline forbids a speedup — receipt.rs: "No baseline was supplied;
// speedup_vs_baseline MUST be null"). It deliberately does NOT judge the
// numbers' merit — a 0.02x lossless loss and a self-pruned fail-tier receipt
// are both VALID (a real negative beats a massaged positive); honesty lives in
// the labels, not in gating the values.
func (r SpecReceipt) Validate() error {
	if r.SchemaVersion != specReceiptSchemaVersion {
		return fmt.Errorf("spec receipt: unsupported schema_version %d (expected %d)", r.SchemaVersion, specReceiptSchemaVersion)
	}
	if strings.TrimSpace(r.BranchID) == "" || len(r.BranchID) > 256 {
		return fmt.Errorf("spec receipt: branch_id must be non-empty and <= 256 bytes")
	}
	if r.Modality == "" || len(r.Modality) > 64 {
		return fmt.Errorf("spec receipt: modality must be 1..64 bytes")
	}
	for _, ch := range []byte(r.Modality) {
		if !((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
			(ch >= '0' && ch <= '9') || ch == '_' || ch == '-' || ch == '.') {
			return fmt.Errorf("spec receipt: modality must be ASCII alphanumeric/._-, got %q", r.Modality)
		}
	}
	times := []struct {
		name string
		v    float64
	}{
		{"draft_cost_s", r.DraftCostS},
		{"verify_cost_s", r.VerifyCostS},
		{"repair_cost_s", r.RepairCostS},
		{"overhead_cost_s", r.OverheadCostS},
		{"total_product_time_s", r.TotalProductTimeS},
		{"baseline_total_time_s", r.BaselineTotalTimeS},
	}
	for _, t := range times {
		if !specTimeValid(t.v) {
			return fmt.Errorf("spec receipt: %s must be >= 0, got %v", t.name, t.v)
		}
	}
	if r.Units > maxSpecReceiptUnits {
		return fmt.Errorf("spec receipt: units %d exceeds safety maximum %d", r.Units, maxSpecReceiptUnits)
	}
	if !specFractionValid(r.AcceptedFraction) {
		return fmt.Errorf("spec receipt: accepted_fraction must be in [0,1], got %v", r.AcceptedFraction)
	}
	if !specFractionValid(r.RepairedFraction) {
		return fmt.Errorf("spec receipt: repaired_fraction must be in [0,1], got %v", r.RepairedFraction)
	}
	if r.SpeedupVsBaseline != nil && !(*r.SpeedupVsBaseline > 0) {
		return fmt.Errorf("spec receipt: speedup_vs_baseline must be null or > 0, got %v", *r.SpeedupVsBaseline)
	}
	if r.SpeedupVsBaseline != nil && (math.IsNaN(*r.SpeedupVsBaseline) || math.IsInf(*r.SpeedupVsBaseline, 0)) {
		return fmt.Errorf("spec receipt: speedup_vs_baseline must be finite, got %v", *r.SpeedupVsBaseline)
	}
	switch r.QualityTier {
	case SpecTierFail, SpecTierPreview, SpecTierDelivery:
	default:
		return fmt.Errorf("spec receipt: quality_tier must be one of fail|preview|delivery, got %q", r.QualityTier)
	}
	switch r.Evidence {
	case SpecEvidenceMeasured, SpecEvidenceModeled, SpecEvidenceSynthetic, SpecEvidenceImported:
	default:
		return fmt.Errorf("spec receipt: evidence must be one of measured|modeled|synthetic|imported, got %q", r.Evidence)
	}
	switch r.BaselineSource {
	case SpecBaselineMeasured, SpecBaselineModeled, SpecBaselineAbsent:
	default:
		return fmt.Errorf("spec receipt: baseline_source must be one of measured|modeled|absent, got %q", r.BaselineSource)
	}
	if (r.BaselineSource == SpecBaselineMeasured || r.BaselineSource == SpecBaselineModeled) && r.BaselineTotalTimeS <= 0 {
		return fmt.Errorf("spec receipt: baseline_source=%s requires baseline_total_time_s > 0", r.BaselineSource)
	}
	if r.BaselineSource == SpecBaselineAbsent && r.SpeedupVsBaseline != nil {
		return fmt.Errorf("spec receipt: baseline_source=absent requires a null speedup_vs_baseline (a speedup is never invented), got %v", *r.SpeedupVsBaseline)
	}
	if r.BaselineSource == SpecBaselineAbsent && r.BaselineTotalTimeS != 0 {
		return fmt.Errorf("spec receipt: baseline_source=absent requires baseline_total_time_s=0")
	}
	if r.Units == 0 {
		if r.AcceptedFraction != 0 || r.RepairedFraction != 0 || r.Exact ||
			r.ArtifactVerified || r.QualityTier != SpecTierFail ||
			r.DraftCostS != 0 || r.VerifyCostS != 0 || r.RepairCostS != 0 ||
			r.OverheadCostS != 0 || r.TotalProductTimeS != 0 ||
			r.BaselineTotalTimeS != 0 || r.BaselineSource != SpecBaselineAbsent ||
			r.SpeedupVsBaseline != nil {
			return fmt.Errorf("spec receipt: an empty receipt cannot claim work, a baseline, speedup, correctness, verification, or a non-fail tier")
		}
	} else if r.TotalProductTimeS <= 0 {
		return fmt.Errorf("spec receipt: a non-empty receipt must charge positive total_product_time_s")
	}
	if r.ArtifactVerified && (r.Evidence != SpecEvidenceMeasured || r.QualityTier == SpecTierFail) {
		return fmt.Errorf("spec receipt: artifact_verified=true requires measured evidence and a non-fail quality tier")
	}
	if r.Evidence != SpecEvidenceImported {
		parts := r.DraftCostS + r.VerifyCostS + r.RepairCostS + r.OverheadCostS
		if !specPhaseSumClose(r.TotalProductTimeS, parts) {
			return fmt.Errorf("spec receipt: total_product_time_s %v contradicts charged phase sum %v", r.TotalProductTimeS, parts)
		}
	}
	if r.SpeedupVsBaseline != nil && r.Evidence != SpecEvidenceImported {
		if r.BaselineTotalTimeS <= 0 || r.TotalProductTimeS <= 0 {
			return fmt.Errorf("spec receipt: a speedup requires positive baseline and product times")
		}
		expected := r.BaselineTotalTimeS / r.TotalProductTimeS
		if !specRoundedRatioClose(*r.SpeedupVsBaseline, expected) {
			return fmt.Errorf("spec receipt: speedup_vs_baseline %v contradicts baseline/total %v", *r.SpeedupVsBaseline, expected)
		}
	}
	return nil
}

// DeliveryEligible is the emitter-local eligibility claim after structural
// validation. It is NEVER a billing decision: production must additionally bind
// job/input/artifact/policy and authoritative server attestation.
func (r SpecReceipt) DeliveryEligible() bool {
	return r.Validate() == nil && r.ArtifactVerified && r.Evidence == SpecEvidenceMeasured &&
		(r.QualityTier == SpecTierPreview || r.QualityTier == SpecTierDelivery)
}

// ParseSpecReceipt is the one-call ingest path: unmarshal (canonical keys +
// receipt.rs aliases) then Validate. Pure; no I/O; callers decide what to do
// with the receipt — nothing in the control plane consumes it yet (the wiring
// is the sequenced, owner-approved climb the integration design describes).
func ParseSpecReceipt(data []byte) (SpecReceipt, error) {
	if len(data) > maxSpecReceiptBytes {
		return SpecReceipt{}, fmt.Errorf("spec receipt: JSON is %d bytes; maximum is %d", len(data), maxSpecReceiptBytes)
	}
	var r SpecReceipt
	if err := json.Unmarshal(data, &r); err != nil {
		return SpecReceipt{}, err
	}
	if err := r.Validate(); err != nil {
		return SpecReceipt{}, err
	}
	return r, nil
}

// rejectDuplicateSpecJSONKeys validates the complete JSON token tree before it
// is collapsed into maps. It catches duplicate keys (including inside details),
// caps nesting, and rejects trailing values.
func rejectDuplicateSpecJSONKeys(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var walk func(int) error
	walk = func(depth int) error {
		if depth > maxSpecReceiptJSONDepth {
			return fmt.Errorf("spec receipt: JSON nesting exceeds %d", maxSpecReceiptJSONDepth)
		}
		tok, err := dec.Token()
		if err != nil {
			return fmt.Errorf("spec receipt: %w", err)
		}
		delim, isDelim := tok.(json.Delim)
		if !isDelim {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]struct{}{}
			for dec.More() {
				keyTok, err := dec.Token()
				if err != nil {
					return fmt.Errorf("spec receipt: %w", err)
				}
				key, ok := keyTok.(string)
				if !ok {
					return fmt.Errorf("spec receipt: object key is not a string")
				}
				if _, exists := seen[key]; exists {
					return fmt.Errorf("spec receipt: duplicate JSON key %q", key)
				}
				seen[key] = struct{}{}
				if err := walk(depth + 1); err != nil {
					return err
				}
			}
			if end, err := dec.Token(); err != nil || end != json.Delim('}') {
				return fmt.Errorf("spec receipt: malformed object")
			}
		case '[':
			for dec.More() {
				if err := walk(depth + 1); err != nil {
					return err
				}
			}
			if end, err := dec.Token(); err != nil || end != json.Delim(']') {
				return fmt.Errorf("spec receipt: malformed array")
			}
		default:
			return fmt.Errorf("spec receipt: unexpected delimiter %q", delim)
		}
		return nil
	}
	if err := walk(0); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		if err == nil {
			return fmt.Errorf("spec receipt: trailing JSON value")
		}
		return fmt.Errorf("spec receipt: %w", err)
	}
	return nil
}
