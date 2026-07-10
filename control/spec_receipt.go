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
//   - the documented serde aliases accepted on ingest, so all three lanes'
//     REAL emitters parse (cx_speculative_core.py `*_s`, the render adapter's
//     no-suffix `*_cost`, token-spec-poc's `*_s`) — proven against verbatim
//     emitter output in spec_receipt_test.go;
//   - `#[serde(default)]` fields default the same way (baseline_source=modeled,
//     quality_tier=preview, evidence=imported, details={});
//   - `speedup_vs_baseline` is nullable and never invented;
//   - unknown keys are ignored (serde's default), so a legacy row's extra
//     columns (quality_gate, accepted_units, global_ssim, ...) pass through
//     harmlessly — anything worth keeping lives in the emitter's `meta`, which
//     lands in Details via the alias.
//
// One documented divergence from serde: when a canonical key AND one of its
// aliases are both present, serde errors ("duplicate field") while this mirror
// takes the CANONICAL key and ignores the alias — a deterministic, documented
// precedence (canonical first, then aliases in receipt.rs declaration order).
// No real emitter sends both.

import (
	"encoding/json"
	"fmt"
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
	// Identity.
	BranchID string `json:"branch_id"` // experiment/branch id
	Modality string `json:"modality"`  // open tag: "render" | "token" | "combined" | future lanes

	// Costs (plan-mandated spine). Ingest aliases per receipt.rs:
	//   draft_cost_s:          draft_s (Python ledger), draft_cost (render adapter)
	//   verify_cost_s:         verify_s, verify_cost
	//   repair_cost_s:         repair_s, repair_cost
	//   total_product_time_s:  speculative_s, total_product_time
	DraftCostS        float64 `json:"draft_cost_s"`
	VerifyCostS       float64 `json:"verify_cost_s"`
	RepairCostS       float64 `json:"repair_cost_s"`
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
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("spec receipt: %w", err)
	}

	pick := func(f specReceiptField) (json.RawMessage, bool) {
		if v, ok := raw[f.canonical]; ok {
			return v, true
		}
		for _, a := range f.aliases {
			if v, ok := raw[a]; ok {
				return v, true
			}
		}
		return nil, false
	}
	// required: the field must be present under some accepted name and decode.
	required := func(dst any, f specReceiptField) error {
		v, ok := pick(f)
		if !ok {
			if len(f.aliases) == 0 {
				return fmt.Errorf("spec receipt: missing required field %q", f.canonical)
			}
			return fmt.Errorf("spec receipt: missing required field %q (accepted aliases %v)", f.canonical, f.aliases)
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", f.canonical, err)
		}
		return nil
	}
	// optional: absent leaves the pre-set default in place. An explicit JSON
	// null also leaves the default (stdlib Unmarshal treats null as a no-op for
	// non-pointer targets), which is the lenient reading of a legacy row.
	optional := func(dst any, f specReceiptField) error {
		v, ok := pick(f)
		if !ok {
			return nil
		}
		if err := json.Unmarshal(v, dst); err != nil {
			return fmt.Errorf("spec receipt: field %q: %w", f.canonical, err)
		}
		return nil
	}

	// receipt.rs #[serde(default)] values, applied before decode.
	out := SpecReceipt{
		BaselineSource: SpecBaselineModeled,
		QualityTier:    SpecTierPreview,
		Evidence:       SpecEvidenceImported,
		Details:        map[string]any{},
	}

	// The required spine (receipt.rs fields without #[serde(default)]).
	steps := []error{
		required(&out.BranchID, specReceiptField{canonical: "branch_id"}),
		required(&out.Modality, specReceiptField{canonical: "modality"}),
		required(&out.DraftCostS, specReceiptField{"draft_cost_s", []string{"draft_s", "draft_cost"}}),
		required(&out.VerifyCostS, specReceiptField{"verify_cost_s", []string{"verify_s", "verify_cost"}}),
		required(&out.RepairCostS, specReceiptField{"repair_cost_s", []string{"repair_s", "repair_cost"}}),
		required(&out.TotalProductTimeS, specReceiptField{"total_product_time_s", []string{"speculative_s", "total_product_time"}}),
		required(&out.BaselineTotalTimeS, specReceiptField{"baseline_total_time_s", []string{"baseline_s", "baseline_cost"}}),
		required(&out.Units, specReceiptField{canonical: "units"}),
		required(&out.AcceptedFraction, specReceiptField{canonical: "accepted_fraction"}),
		required(&out.RepairedFraction, specReceiptField{canonical: "repaired_fraction"}),
		required(&out.Exact, specReceiptField{canonical: "exact"}),
		// Defaulted / optional fields.
		optional(&out.BaselineSource, specReceiptField{canonical: "baseline_source"}),
		optional(&out.QualityTier, specReceiptField{canonical: "quality_tier"}),
		optional(&out.SpeedupVsBaseline, specReceiptField{"speedup_vs_baseline", []string{"speedup_x"}}),
		optional(&out.Evidence, specReceiptField{canonical: "evidence"}),
		optional(&out.Details, specReceiptField{"details", []string{"meta"}}),
	}
	for _, err := range steps {
		if err != nil {
			return err
		}
	}
	if out.Details == nil { // an explicit `"details": null` — keep the non-nil invariant
		out.Details = map[string]any{}
	}
	*r = out
	return nil
}

// specFractionValid reports 0 <= v <= 1, written so NaN fails too.
func specFractionValid(v float64) bool { return v >= 0 && v <= 1 }

// specTimeValid reports v >= 0, written so NaN fails too.
func specTimeValid(v float64) bool { return v >= 0 }

// Validate enforces the receipt.rs range/enum contract on an already-parsed
// receipt: fractions in [0,1], every cost/time >= 0, speedup null-or-positive,
// the three closed vocabularies, and the baseline honesty rule (an "absent"
// baseline forbids a speedup — receipt.rs: "No baseline was supplied;
// speedup_vs_baseline MUST be null"). It deliberately does NOT judge the
// numbers' merit — a 0.02x lossless loss and a self-pruned fail-tier receipt
// are both VALID (a real negative beats a massaged positive); honesty lives in
// the labels, not in gating the values.
func (r SpecReceipt) Validate() error {
	times := []struct {
		name string
		v    float64
	}{
		{"draft_cost_s", r.DraftCostS},
		{"verify_cost_s", r.VerifyCostS},
		{"repair_cost_s", r.RepairCostS},
		{"total_product_time_s", r.TotalProductTimeS},
		{"baseline_total_time_s", r.BaselineTotalTimeS},
	}
	for _, t := range times {
		if !specTimeValid(t.v) {
			return fmt.Errorf("spec receipt: %s must be >= 0, got %v", t.name, t.v)
		}
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
	if r.BaselineSource == SpecBaselineAbsent && r.SpeedupVsBaseline != nil {
		return fmt.Errorf("spec receipt: baseline_source=absent requires a null speedup_vs_baseline (a speedup is never invented), got %v", *r.SpeedupVsBaseline)
	}
	return nil
}

// ParseSpecReceipt is the one-call ingest path: unmarshal (canonical keys +
// receipt.rs aliases) then Validate. Pure; no I/O; callers decide what to do
// with the receipt — nothing in the control plane consumes it yet (the wiring
// is the sequenced, owner-approved climb the integration design describes).
func ParseSpecReceipt(data []byte) (SpecReceipt, error) {
	var r SpecReceipt
	if err := json.Unmarshal(data, &r); err != nil {
		return SpecReceipt{}, err
	}
	if err := r.Validate(); err != nil {
		return SpecReceipt{}, err
	}
	return r, nil
}
