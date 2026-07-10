package main

// spec_receipt_test.go — proves the Go mirror ingests REAL emitter output from
// every speculation lane, not a hand-authored fixture.
//
// The first three blobs are copied VERBATIM from spec-engine/tests/
// ingest_lanes.rs (LANE1_CORE / LANE2_RENDER / LANE3_TOKEN), where each was
// produced by ACTUALLY INVOKING its emitter (Python to_dict() / `cargo run` on
// the POC) and pasted unedited — so passing here is the real cross-language
// composition proof the consolidation plan flagged as missing ("Go ingest is
// proven structurally, not by a real unmarshal").
//
// The last two blobs are the REAL RUN 3 (A100, 4K kf=1, 5.561x headline) and
// REAL RUN 4 (H100, 4K kf=1 + repair, the decisive selector_recall=0.0
// negative) integrated receipts from docs/speed-lane-reports/spec-lab/
// integrated_spec_render_token_ledger.jsonl, mapped through the REAL
// scripts/spec-lab/cx_render_spec_adapter.py (RenderSpecAdapter.
// from_stack_metrics(render_metrics).to_dict(), run 2026-07-10) — RUN 3 takes
// the legacy no-repair path (1 shot-unit, repair_cost = fixed_overhead_s
// stand-in), RUN 4 the repair path (256 grading-tile units, repair_cost = the
// measured repair_total_s). Both carry quality_tier "fail": they self-pruned
// at the strict delivery gate (g>=0.98, wt>=0.95) — honest negatives ingested
// as-is, exactly as the ledger recorded them.

import (
	"encoding/json"
	"math"
	"testing"
)

// --- verbatim emitter output ------------------------------------------------

// Lane 1 — scripts/spec-lab/cx_speculative_core.py SpecReceipt.to_dict()
// (legacy *_s keys, bare speedup_x, bool quality_gate, meta bag).
const specLane1Core = `{"branch_id": "cx_core_demo", "modality": "token", "units": 8, "attempted_units": 8, "fallback_units": 0, "accepted_units": 6, "repaired_units": 2, "rejected_units": 2, "attempted_fraction": 1.0, "fallback_fraction": 0.0, "accepted_fraction": 0.75, "accepted_attempt_fraction": 0.75, "repaired_fraction": 0.25, "draft_s": 7e-06, "verify_s": 9e-06, "repair_s": 2e-06, "fallback_s": 0.0, "baseline_s": 2e-06, "speculative_s": 1.8e-05, "speedup_x": 0.106433, "exact": true, "quality_gate": true, "meta": {"prompt_class": "repeat"}}`

// Lane 2 — scripts/spec-lab/cx_render_spec_adapter.py RenderSpecReceipt.to_dict()
// (no-suffix *_cost keys, enum quality_tier, explicit exact=false).
const specLane2Render = `{"draft_cost": 4.0, "verify_cost": 0.4, "accepted_fraction": 0.75, "repair_cost": 8.0, "total_product_time": 12.4, "quality_tier": "delivery", "speedup_vs_baseline": 2.580645, "exact": false, "modality": "render", "branch_id": "render", "units": 4, "accepted_units": 3, "repaired_units": 1, "repaired_fraction": 0.25, "baseline_cost": 32.0, "quality_gate": true, "delivery_eligible": true, "evidence": "synthetic", "global_ssim": 0.97, "worst_tile_ssim": 0.8, "p5_ssim": null, "claim_scope": "Measured single delivered unit ratio only; per-tile ratios are NOT multiplied. baseline_cost is a real reference-quality render of this unit.", "meta": {"quality_gate_spec": "g>=0.98,wt>=0.95"}}`

// Lane 3 — token-spec-poc `cargo run` first line (the "code" stream): same
// *_s shape as Lane 1 with a rich meta object.
const specLane3Token = `{"branch_id":"token-spec-poc","modality":"token","units":694,"attempted_units":694,"fallback_units":0,"accepted_units":133,"repaired_units":46,"rejected_units":561,"attempted_fraction":1.0,"fallback_fraction":0.0,"accepted_fraction":0.191643,"repaired_fraction":0.136499,"draft_s":0.002218,"verify_s":0.000078,"repair_s":0.0,"fallback_s":0.0,"baseline_s":0.000065,"speculative_s":0.002638,"speedup_x":0.024516,"exact":true,"quality_gate":true,"meta":{"tokens_emitted":470,"rounds":337,"target_calls":337,"target_call_reduction_x":1.394659,"mean_accept_len":0.394659,"draft_producer":"ngram_prompt_lookup","target_backend":"mock_fixed_stream","walltime_label":"MODELED","notes":"stream=code; order=3; k=16; speedup_x is MODELED (verify pass is not yet one decode step — needs forward_all_logits + KvCacheSlot::truncate per TOKEN_LANE_FORK_DESIGN.md). accepted_fraction and target_call_reduction_x are MEASURED."}}`

// RUN 3 — the MEASURED 4K headline (A100 SECURE, ledger ts 2026-07-09T21:56:43),
// render_metrics mapped through the adapter's legacy (no-repair) path.
const specRun3Render = `{"draft_cost": 812.8458, "verify_cost": 0.0, "accepted_fraction": 1.0, "repair_cost": 4.5146, "total_product_time": 817.3604, "quality_tier": "fail", "speedup_vs_baseline": 5.562736, "exact": false, "modality": "render", "branch_id": "render", "units": 1, "accepted_units": 1, "repaired_units": 0, "repaired_fraction": 0.0, "baseline_cost": 4546.76, "quality_gate": false, "delivery_eligible": false, "evidence": "measured", "global_ssim": 0.9854, "worst_tile_ssim": 0.9095, "p5_ssim": 0.9664, "claim_scope": "Measured single delivered unit ratio only; per-tile ratios are NOT multiplied. baseline_cost is a real reference-quality render of this unit.", "meta": {"accepted_fraction_tiles": 1.0, "repaired_fraction_tiles": 0.0, "source_ledger": "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl", "source_event_ts": "2026-07-09T21:56:43-0400", "gpu": "NVIDIA A100 80GB PCIe", "net_speedup_reported": 5.5627, "device": "GPU/OPTIX", "scene": "classroom", "resolution": "3840x2160", "frames": 4, "keyframes": 4, "ref_spp": 4096, "draft_spp": 512, "verify_cost_note": "SSIM/mask is measurement-only; not charged to T_stack", "repair_note": "the disocclusion crop re-render is already INSIDE T_stack; repair_cost here is the runner's directly-exposed fixed_overhead_s (a lower bound of the true crop cost) so total_product_time stays == T_stack and is not double-charged. The exact, load-bearing numbers are total_product_time and speedup_vs_baseline.", "quality_gate_spec": "g>=0.98,wt>=0.95"}}`

// RUN 4 — the MEASURED decisive repair negative (H100 SECURE, ledger ts
// 2026-07-10T00:21:34): repair path, 256 tile-units, 12 repaired, worst-tile
// UNCHANGED at 0.9095, selector_recall = 0.0 in details.
const specRun4Render = `{"draft_cost": 621.5214, "verify_cost": 0.0, "accepted_fraction": 0.953125, "repair_cost": 426.0835, "total_product_time": 1047.6049, "quality_tier": "fail", "speedup_vs_baseline": 2.703753, "exact": false, "modality": "render", "branch_id": "render", "units": 256, "accepted_units": 244, "repaired_units": 12, "repaired_fraction": 0.046875, "baseline_cost": 2832.4645, "quality_gate": false, "delivery_eligible": false, "evidence": "measured", "global_ssim": 0.9855, "worst_tile_ssim": 0.9095, "p5_ssim": 0.9664, "claim_scope": "Measured single delivered unit ratio only; per-tile ratios are NOT multiplied. baseline_cost is a real reference-quality render of this unit.", "meta": {"accepted_fraction_tiles": 1.0, "repaired_fraction_tiles": 0.0, "source_ledger": "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl", "source_event_ts": "2026-07-10T00:21:34-0400", "gpu": "NVIDIA H100 80GB HBM3", "net_speedup_reported": 2.7038, "device": "GPU/OPTIX", "scene": "classroom", "resolution": "3840x2160", "frames": 4, "keyframes": 4, "ref_spp": 4096, "draft_spp": 512, "verify_cost_note": "SSIM/mask is measurement-only; not charged to T_stack", "repair_note": "repair_cost is the runner's REAL measured repair_total_s (selection drafts + divergence scoring + bordered tile re-renders + feathered compositing), already INSIDE T_stack — total_product_time stays == T_stack and is not double-charged. Units are grading tiles (frames x 64); accepted/repaired fractions are real tile counts. The tile selector is reference-free (two-independent-draft divergence); SSIM-vs-reference remains measurement-only.", "selection_cost_s": 172.5641, "repair_cost_s": 253.5193, "repaired_tile_indices": [[[0, 5], [1, 5]], [[0, 5], [0, 6], [1, 5]], [[0, 5], [0, 6], [1, 5]], [[0, 5], [0, 6], [1, 5], [1, 6]]], "selector_recall": 0.0, "quality_gate_spec": "g>=0.98,wt>=0.95"}}`

// specClose mirrors ingest_lanes.rs's close(): the blobs are frozen snapshots,
// so equality is exact in practice; the epsilon documents floats, not drift.
func specClose(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) >= 1e-9 {
		t.Errorf("%s: got %v, want %v", name, got, want)
	}
}

func specSpeedup(t *testing.T, name string, got *float64, want float64) {
	t.Helper()
	if got == nil {
		t.Fatalf("%s: speedup_vs_baseline is nil, want %v", name, want)
	}
	specClose(t, name+" speedup_vs_baseline", *got, want)
}

func TestSpecReceiptIngestAllLanes(t *testing.T) {
	cases := []struct {
		name       string
		blob       string
		branchID   string
		modality   string
		units      uint32
		draft      float64
		verify     float64
		repair     float64
		total      float64
		baseline   float64
		accepted   float64
		repaired   float64
		exact      bool
		tier       string
		evidence   string
		baseSource string
		speedup    float64
		// details assertions: key -> expected value (decoded via encoding/json,
		// so numbers are float64 and strings are string).
		details map[string]any
	}{
		{
			name:     "lane1_cx_speculative_core",
			blob:     specLane1Core,
			branchID: "cx_core_demo", modality: "token", units: 8,
			draft: 7e-6, verify: 9e-6, repair: 2e-6, total: 1.8e-5, baseline: 2e-6,
			accepted: 0.75, repaired: 0.25, exact: true,
			// A legacy row carries only a bool quality_gate (ignored) and no
			// labels — the receipt.rs defaults apply.
			tier: SpecTierPreview, evidence: SpecEvidenceImported, baseSource: SpecBaselineModeled,
			speedup: 0.106433,
			details: map[string]any{"prompt_class": "repeat"},
		},
		{
			name:     "lane2_render_adapter",
			blob:     specLane2Render,
			branchID: "render", modality: "render", units: 4,
			draft: 4.0, verify: 0.4, repair: 8.0, total: 12.4, baseline: 32.0,
			accepted: 0.75, repaired: 0.25, exact: false,
			tier: SpecTierDelivery, evidence: SpecEvidenceSynthetic, baseSource: SpecBaselineModeled,
			speedup: 2.580645,
			// The SSIM gate-spec string survives in details — it is NOT a tier.
			details: map[string]any{"quality_gate_spec": "g>=0.98,wt>=0.95"},
		},
		{
			name:     "lane3_token_spec_poc",
			blob:     specLane3Token,
			branchID: "token-spec-poc", modality: "token", units: 694,
			draft: 0.002218, verify: 0.000078, repair: 0.0, total: 0.002638, baseline: 0.000065,
			accepted: 0.191643, repaired: 0.136499, exact: true,
			tier: SpecTierPreview, evidence: SpecEvidenceImported, baseSource: SpecBaselineModeled,
			speedup: 0.024516,
			details: map[string]any{
				"walltime_label":          "MODELED",
				"target_call_reduction_x": 1.394659,
			},
		},
		{
			name:     "run3_integrated_4k_headline",
			blob:     specRun3Render,
			branchID: "render", modality: "render", units: 1,
			draft: 812.8458, verify: 0.0, repair: 4.5146, total: 817.3604, baseline: 4546.76,
			accepted: 1.0, repaired: 0.0, exact: false,
			// Self-pruned at the strict delivery gate (wt 0.9095 < 0.95): an
			// honest fail-tier receipt with a MEASURED 5.56x — both facts kept.
			tier: SpecTierFail, evidence: SpecEvidenceMeasured, baseSource: SpecBaselineModeled,
			speedup: 5.562736,
			details: map[string]any{
				"gpu":           "NVIDIA A100 80GB PCIe",
				"scene":         "classroom",
				"resolution":    "3840x2160",
				"source_ledger": "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl",
			},
		},
		{
			name:     "run4_integrated_repair_negative",
			blob:     specRun4Render,
			branchID: "render", modality: "render", units: 256,
			draft: 621.5214, verify: 0.0, repair: 426.0835, total: 1047.6049, baseline: 2832.4645,
			accepted: 0.953125, repaired: 0.046875, exact: false,
			tier: SpecTierFail, evidence: SpecEvidenceMeasured, baseSource: SpecBaselineModeled,
			speedup: 2.703753,
			details: map[string]any{
				"gpu":              "NVIDIA H100 80GB HBM3",
				"selector_recall":  0.0, // the decisive negative, preserved verbatim
				"selection_cost_s": 172.5641,
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, err := ParseSpecReceipt([]byte(tc.blob))
			if err != nil {
				t.Fatalf("ParseSpecReceipt: %v", err)
			}
			if r.BranchID != tc.branchID {
				t.Errorf("branch_id: got %q, want %q", r.BranchID, tc.branchID)
			}
			if r.Modality != tc.modality {
				t.Errorf("modality: got %q, want %q", r.Modality, tc.modality)
			}
			if r.Units != tc.units {
				t.Errorf("units: got %d, want %d", r.Units, tc.units)
			}
			specClose(t, "draft_cost_s", r.DraftCostS, tc.draft)
			specClose(t, "verify_cost_s", r.VerifyCostS, tc.verify)
			specClose(t, "repair_cost_s", r.RepairCostS, tc.repair)
			specClose(t, "total_product_time_s", r.TotalProductTimeS, tc.total)
			specClose(t, "baseline_total_time_s", r.BaselineTotalTimeS, tc.baseline)
			specClose(t, "accepted_fraction", r.AcceptedFraction, tc.accepted)
			specClose(t, "repaired_fraction", r.RepairedFraction, tc.repaired)
			if r.Exact != tc.exact {
				t.Errorf("exact: got %v, want %v", r.Exact, tc.exact)
			}
			if r.QualityTier != tc.tier {
				t.Errorf("quality_tier: got %q, want %q", r.QualityTier, tc.tier)
			}
			if r.Evidence != tc.evidence {
				t.Errorf("evidence: got %q, want %q", r.Evidence, tc.evidence)
			}
			if r.BaselineSource != tc.baseSource {
				t.Errorf("baseline_source: got %q, want %q", r.BaselineSource, tc.baseSource)
			}
			specSpeedup(t, tc.name, r.SpeedupVsBaseline, tc.speedup)
			for k, want := range tc.details {
				got, ok := r.Details[k]
				if !ok {
					t.Errorf("details[%q]: missing (meta must land in details)", k)
					continue
				}
				switch w := want.(type) {
				case float64:
					g, isNum := got.(float64)
					if !isNum {
						t.Errorf("details[%q]: got %T, want float64", k, got)
					} else {
						specClose(t, "details."+k, g, w)
					}
				default:
					if got != want {
						t.Errorf("details[%q]: got %v, want %v", k, got, want)
					}
				}
			}
		})
	}
}

// TestSpecReceiptCanonicalRoundTrip is the composition claim from
// ingest_lanes.rs's all_three_lanes_share_one_schema, extended to the two real
// ledger receipts: every lane lands in the ONE Go type, re-serializes carrying
// the canonical plan-spine keys, and re-ingests value-identically.
func TestSpecReceiptCanonicalRoundTrip(t *testing.T) {
	blobs := map[string]string{
		"lane1": specLane1Core,
		"lane2": specLane2Render,
		"lane3": specLane3Token,
		"run3":  specRun3Render,
		"run4":  specRun4Render,
	}
	spine := []string{
		"branch_id", "modality",
		"draft_cost_s", "verify_cost_s", "repair_cost_s", "total_product_time_s",
		"baseline_total_time_s", "baseline_source",
		"units", "accepted_fraction", "repaired_fraction", "exact",
		"quality_tier", "speedup_vs_baseline", "evidence", "details",
	}
	for name, blob := range blobs {
		t.Run(name, func(t *testing.T) {
			r, err := ParseSpecReceipt([]byte(blob))
			if err != nil {
				t.Fatalf("ParseSpecReceipt: %v", err)
			}
			out, err := json.Marshal(&r)
			if err != nil {
				t.Fatalf("Marshal: %v", err)
			}
			var keys map[string]json.RawMessage
			if err := json.Unmarshal(out, &keys); err != nil {
				t.Fatalf("re-parse of canonical form: %v", err)
			}
			for _, k := range spine {
				if _, ok := keys[k]; !ok {
					t.Errorf("canonical serialization missing spine key %q", k)
				}
			}
			var again SpecReceipt
			if err := json.Unmarshal(out, &again); err != nil {
				t.Fatalf("canonical re-ingest: %v", err)
			}
			if again.BranchID != r.BranchID || again.Units != r.Units ||
				again.QualityTier != r.QualityTier || again.Evidence != r.Evidence ||
				again.Exact != r.Exact || again.BaselineSource != r.BaselineSource {
				t.Errorf("canonical round-trip changed identity/label fields: %+v vs %+v", again, r)
			}
			specClose(t, "round-trip total_product_time_s", again.TotalProductTimeS, r.TotalProductTimeS)
			specClose(t, "round-trip baseline_total_time_s", again.BaselineTotalTimeS, r.BaselineTotalTimeS)
			if (again.SpeedupVsBaseline == nil) != (r.SpeedupVsBaseline == nil) {
				t.Errorf("round-trip changed speedup nullability")
			}
		})
	}
}

// TestSpecReceiptAliasPrecedence pins the documented divergence from serde:
// when a canonical key and an alias are both present, the canonical key wins
// (serde would error on the duplicate; no real emitter sends both).
func TestSpecReceiptAliasPrecedence(t *testing.T) {
	blob := `{"branch_id":"p","modality":"token","units":1,
		"draft_cost_s": 1.0, "draft_s": 99.0,
		"verify_cost_s": 0.0, "repair_cost_s": 0.0,
		"total_product_time_s": 1.0, "baseline_total_time_s": 2.0,
		"accepted_fraction": 1.0, "repaired_fraction": 0.0, "exact": true}`
	r, err := ParseSpecReceipt([]byte(blob))
	if err != nil {
		t.Fatalf("ParseSpecReceipt: %v", err)
	}
	specClose(t, "canonical-over-alias draft_cost_s", r.DraftCostS, 1.0)
}

// TestSpecReceiptRequiredFields: the receipt.rs required spine (no
// #[serde(default)]) must be present under SOME accepted name.
func TestSpecReceiptRequiredFields(t *testing.T) {
	// Missing `exact` (and everything else fine).
	blob := `{"branch_id":"x","modality":"token","units":1,
		"draft_s":0.0,"verify_s":0.0,"repair_s":0.0,"speculative_s":0.0,"baseline_s":0.0,
		"accepted_fraction":0.5,"repaired_fraction":0.0}`
	if _, err := ParseSpecReceipt([]byte(blob)); err == nil {
		t.Fatalf("missing required field `exact` must fail to parse")
	}
	// Negative units must fail (u32 in receipt.rs).
	blob2 := `{"branch_id":"x","modality":"token","units":-1,
		"draft_s":0.0,"verify_s":0.0,"repair_s":0.0,"speculative_s":0.0,"baseline_s":0.0,
		"accepted_fraction":0.5,"repaired_fraction":0.0,"exact":true}`
	if _, err := ParseSpecReceipt([]byte(blob2)); err == nil {
		t.Fatalf("negative units must fail to parse")
	}
}

func TestSpecReceiptValidateRanges(t *testing.T) {
	base := func() SpecReceipt {
		s := 1.5
		return SpecReceipt{
			BranchID: "v", Modality: "render",
			DraftCostS: 1, VerifyCostS: 0.1, RepairCostS: 0, TotalProductTimeS: 1.1,
			BaselineTotalTimeS: 2, BaselineSource: SpecBaselineMeasured,
			Units: 4, AcceptedFraction: 0.75, RepairedFraction: 0.25,
			Exact: false, QualityTier: SpecTierDelivery,
			SpeedupVsBaseline: &s, Evidence: SpecEvidenceMeasured,
			Details: map[string]any{},
		}
	}
	if err := base().Validate(); err != nil {
		t.Fatalf("base receipt must validate, got: %v", err)
	}

	cases := []struct {
		name   string
		mutate func(*SpecReceipt)
	}{
		{"accepted_fraction above 1", func(r *SpecReceipt) { r.AcceptedFraction = 1.5 }},
		{"accepted_fraction NaN", func(r *SpecReceipt) { r.AcceptedFraction = math.NaN() }},
		{"repaired_fraction negative", func(r *SpecReceipt) { r.RepairedFraction = -0.1 }},
		{"draft time negative", func(r *SpecReceipt) { r.DraftCostS = -1 }},
		{"baseline time NaN", func(r *SpecReceipt) { r.BaselineTotalTimeS = math.NaN() }},
		{"speedup zero", func(r *SpecReceipt) { z := 0.0; r.SpeedupVsBaseline = &z }},
		{"speedup negative", func(r *SpecReceipt) { n := -2.0; r.SpeedupVsBaseline = &n }},
		{"unknown quality tier", func(r *SpecReceipt) { r.QualityTier = "gold" }},
		{"unknown evidence", func(r *SpecReceipt) { r.Evidence = "vibes" }},
		{"unknown baseline source", func(r *SpecReceipt) { r.BaselineSource = "guessed" }},
		{"absent baseline with a speedup", func(r *SpecReceipt) { r.BaselineSource = SpecBaselineAbsent }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := base()
			tc.mutate(&r)
			if err := r.Validate(); err == nil {
				t.Errorf("Validate must reject: %s", tc.name)
			}
		})
	}

	// The honesty rule cuts both ways: absent baseline + null speedup is VALID
	// (a receipt without a baseline simply claims no speedup)...
	ok := base()
	ok.BaselineSource = SpecBaselineAbsent
	ok.SpeedupVsBaseline = nil
	if err := ok.Validate(); err != nil {
		t.Errorf("absent baseline with null speedup must validate, got: %v", err)
	}
	// ...and a fail-tier receipt with a big measured speedup is VALID too — a
	// real negative beats a massaged positive, and RUN 3/RUN 4 above ARE that.
	pruned := base()
	pruned.QualityTier = SpecTierFail
	if err := pruned.Validate(); err != nil {
		t.Errorf("a self-pruned fail-tier receipt must validate, got: %v", err)
	}
}
