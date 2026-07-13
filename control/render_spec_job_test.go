package main

// render_spec_job_test.go — proves the additive render-spec surface is (1) a
// pure deterministic quote that can NEVER emit a strict-delivery promise, (2) a
// receipt projection that round-trips a REAL landed SpecReceipt and returns nil
// at the honesty boundary, and (3) input validation that rejects the impossible.
//
// The receipt tests reuse the VERBATIM emitter/ledger blobs already defined in
// spec_receipt_test.go (same package): specRun3Render (the MEASURED 4K headline,
// self-pruned fail tier), specLane2Render (a delivered-tier render receipt), and
// specLane3Token (a token receipt — the honesty-boundary reject). The specClose
// float helper defined there is reused here.

import (
	"math"
	"reflect"
	"strings"
	"testing"
)

// run3Params is the exact shot shape of the banked 4K RUN 3 receipt — the one
// config guaranteed to sit inside the measured envelope.
func run3Params() RenderSpecParams {
	return RenderSpecParams{
		Width: 3840, Height: 2160, Frames: 4,
		RefSPP: 4096, DraftSPP: 512, KeyframeEvery: 1,
		RequestedTier: renderSpecTierPreview,
	}
}

// TestRenderSpecQuoteDeterministic: identical inputs yield a byte-identical
// quote (pure, no clock/rand/DB), for both an in-envelope and an out-of-envelope
// shot.
func TestRenderSpecQuoteDeterministic(t *testing.T) {
	inputs := map[string]RenderSpecParams{
		"in_envelope_4k":   run3Params(),
		"out_of_envelope":  {Width: 7680, Height: 4320, Frames: 4, RefSPP: 4096, DraftSPP: 512, KeyframeEvery: 1},
		"kf_reprojection":  {Width: 1920, Height: 1080, Frames: 4, RefSPP: 1536, DraftSPP: 192, KeyframeEvery: 4},
		"delivery_request": {Width: 1920, Height: 1080, Frames: 2, RefSPP: 4096, DraftSPP: 32, KeyframeEvery: 1, RequestedTier: renderSpecTierDelivery},
	}
	for name, p := range inputs {
		t.Run(name, func(t *testing.T) {
			a, err := QuoteRenderSpec(p)
			if err != nil {
				t.Fatalf("QuoteRenderSpec: %v", err)
			}
			b, err := QuoteRenderSpec(p)
			if err != nil {
				t.Fatalf("QuoteRenderSpec (2nd): %v", err)
			}
			if !reflect.DeepEqual(a, b) {
				t.Errorf("non-deterministic quote:\n a=%+v\n b=%+v", a, b)
			}
		})
	}
}

// TestRenderSpecQuoteNeverStrict is the load-bearing honesty invariant: over a
// broad matrix of shapes and tier asks, an unbound quote never promises strict
// delivery or projects a speedup from unrelated scene receipts.
func TestRenderSpecQuoteNeverStrict(t *testing.T) {
	widths := []int{640, 1920, 3840, 7680}
	frames := []int{1, 4, 8, 240}
	drafts := []int{8, 64, 512, 2048}
	kfs := []int{1, 2, 4}
	tiers := []string{"", renderSpecTierPreview, renderSpecTierDelivery}

	for _, w := range widths {
		for _, f := range frames {
			for _, d := range drafts {
				for _, kf := range kfs {
					for _, tier := range tiers {
						p := RenderSpecParams{
							Width: w, Height: w * 9 / 16, Frames: f,
							RefSPP: 4096, DraftSPP: d, KeyframeEvery: kf,
							RequestedTier: tier,
						}
						if err := p.Validate(); err != nil {
							continue // skip params the contract legitimately rejects
						}
						q, err := QuoteRenderSpec(p)
						if err != nil {
							t.Fatalf("QuoteRenderSpec(%+v): %v", p, err)
						}
						if q.QuotedTier != renderSpecTierPreview {
							t.Errorf("quoted tier %q for %+v — must always be preview", q.QuotedTier, p)
						}
						if q.StrictDeliveryPromised {
							t.Errorf("StrictDeliveryPromised true for %+v — must never promise strict delivery", p)
						}
						// The reason must never read as a delivery/guarantee promise.
						low := strings.ToLower(q.Reason)
						for _, banned := range []string{"guarantee", "promise the delivery", "will deliver at the delivery tier", "strict delivery tier delivered"} {
							if strings.Contains(low, banned) {
								t.Errorf("reason promises strict delivery (%q) for %+v: %s", banned, p, q.Reason)
							}
						}
						if q.InEnvelope || q.SpeedupBandLowX != nil ||
							q.SpeedupBandHighX != nil || q.Anchor4KSpeedupX != nil {
							t.Errorf("unbound shape-only quote leaked a speed forecast for %+v: %+v", p, q)
						}
					}
				}
			}
		}
	}
}

// TestRenderSpecQuoteEnvelope pins the hardened evidence boundary: even the
// exact RUN-3 shape has no band because scene/policy digests are absent; the
// older shape gates still produce useful diagnostics and never invent a band.
func TestRenderSpecQuoteEnvelope(t *testing.T) {
	// Same shape as the banked 4K config, but not evidence-bound.
	q, err := QuoteRenderSpec(run3Params())
	if err != nil {
		t.Fatalf("QuoteRenderSpec(run3): %v", err)
	}
	if q.InEnvelope {
		t.Fatalf("shape alone must not be treated as an evidence-bound envelope")
	}
	if q.SpeedupBandLowX != nil || q.SpeedupBandHighX != nil || q.Anchor4KSpeedupX != nil {
		t.Fatalf("unbound quote must carry no speed forecast, got %+v", q)
	}
	lowReason := strings.ToLower(q.Reason + " " + q.Basis)
	if !strings.Contains(lowReason, "unbound") || !strings.Contains(lowReason, "2.450894x") {
		t.Fatalf("quote must explain binding and cite the real strict anchor: %s", lowReason)
	}

	// Each lever pushes the SAME base shape out of envelope; the band must drop.
	out := []struct {
		name   string
		mutate func(*RenderSpecParams)
	}{
		{"above 4k", func(p *RenderSpecParams) { p.Width, p.Height = 7680, 4320 }},
		{"frames >> 4", func(p *RenderSpecParams) { p.Frames = 240 }},
		{"keyframe reprojection", func(p *RenderSpecParams) { p.KeyframeEvery = 4 }},
		{"draft too coarse (ratio > 512x)", func(p *RenderSpecParams) { p.DraftSPP = 4 }}, // 4096/4 = 1024x
		{"draft too fine (ratio < 8x)", func(p *RenderSpecParams) { p.DraftSPP = 2048 }},  // 4096/2048 = 2x
	}
	for _, tc := range out {
		t.Run(tc.name, func(t *testing.T) {
			p := run3Params()
			tc.mutate(&p)
			q, err := QuoteRenderSpec(p)
			if err != nil {
				t.Fatalf("QuoteRenderSpec: %v (out-of-envelope must not be an error)", err)
			}
			if q.InEnvelope {
				t.Errorf("%s must remain unbound", tc.name)
			}
			if q.SpeedupBandLowX != nil || q.SpeedupBandHighX != nil || q.Anchor4KSpeedupX != nil {
				t.Errorf("%s: out-of-envelope quote must carry NO band, got %+v", tc.name, q)
			}
			if q.QuotedTier != renderSpecTierPreview {
				t.Errorf("%s: tier contract must still stand at preview, got %q", tc.name, q.QuotedTier)
			}
		})
	}
}

// TestRenderSpecQuoteValidate: the range/enum contract rejects the impossible.
func TestRenderSpecQuoteValidate(t *testing.T) {
	base := run3Params()
	if err := base.Validate(); err != nil {
		t.Fatalf("base params must validate, got: %v", err)
	}
	// A valid params always produces a quote without error.
	if _, err := QuoteRenderSpec(base); err != nil {
		t.Fatalf("QuoteRenderSpec on valid params must not error: %v", err)
	}

	cases := []struct {
		name   string
		mutate func(*RenderSpecParams)
	}{
		{"zero width", func(p *RenderSpecParams) { p.Width = 0 }},
		{"negative height", func(p *RenderSpecParams) { p.Height = -1 }},
		{"zero frames", func(p *RenderSpecParams) { p.Frames = 0 }},
		{"zero ref_spp", func(p *RenderSpecParams) { p.RefSPP = 0 }},
		{"negative draft_spp", func(p *RenderSpecParams) { p.DraftSPP = -8 }},
		{"draft >= ref", func(p *RenderSpecParams) { p.DraftSPP = p.RefSPP }},
		{"draft above ref", func(p *RenderSpecParams) { p.DraftSPP = p.RefSPP + 1 }},
		{"zero keyframe_every", func(p *RenderSpecParams) { p.KeyframeEvery = 0 }},
		{"unknown tier", func(p *RenderSpecParams) { p.RequestedTier = "gold" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := run3Params()
			tc.mutate(&p)
			if err := p.Validate(); err == nil {
				t.Errorf("Validate must reject: %s", tc.name)
			}
			// The quote function surfaces the same rejection as an error.
			if _, err := QuoteRenderSpec(p); err == nil {
				t.Errorf("QuoteRenderSpec must reject: %s", tc.name)
			}
		})
	}
}

// TestReceiptRenderSpecRoundTrip projects a REAL landed SpecReceipt (the RUN 3
// 4K headline, self-pruned) and a delivered-tier render receipt, and checks the
// buyer-facing block restates the receipt's own measured facts and labels
// verbatim — nothing re-judged, nothing invented.
func TestReceiptRenderSpecRoundTrip(t *testing.T) {
	t.Run("run3_self_pruned_measured", func(t *testing.T) {
		r, err := ParseSpecReceipt([]byte(specRun3Render))
		if err != nil {
			t.Fatalf("ParseSpecReceipt(run3): %v", err)
		}
		got, err := receiptRenderSpec(&r)
		if err != nil {
			t.Fatalf("receiptRenderSpec(run3): %v", err)
		}
		if got == nil {
			t.Fatalf("render receipt must project, got nil")
		}
		if got.Modality != "render" {
			t.Errorf("modality: got %q, want render", got.Modality)
		}
		if got.QualityTier != SpecTierFail {
			t.Errorf("quality_tier: got %q, want fail", got.QualityTier)
		}
		if !got.SelfPruned {
			t.Errorf("run3 fail tier must project SelfPruned=true")
		}
		if got.Delivered {
			t.Errorf("a fail-tier receipt must not read as Delivered")
		}
		if got.Evidence != SpecEvidenceMeasured {
			t.Errorf("evidence: got %q, want measured", got.Evidence)
		}
		if got.BaselineSource != SpecBaselineModeled {
			t.Errorf("baseline_source: got %q, want modeled (RUN 3 blob carries no key)", got.BaselineSource)
		}
		// The speedup passes through verbatim — NOT recomputed, NOT rounded away.
		if got.SpeedupVsBaseline == nil {
			t.Fatalf("run3 speedup must pass through, got nil")
		}
		specClose(t, "speedup passthrough", *got.SpeedupVsBaseline, 5.562736)
		specClose(t, "total_product_time_s", got.TotalProductTimeS, 817.3604)
		specClose(t, "baseline_total_time_s", got.BaselineTotalTimeS, 4546.76)
		if got.Units != 1 {
			t.Errorf("units: got %d, want 1", got.Units)
		}
		// Details pass through (the SSIM/scene/device provenance).
		if got.Details["scene"] != "classroom" {
			t.Errorf("details.scene: got %v, want classroom", got.Details["scene"])
		}
		// The basis names the self-prune AND the measured speedup honestly.
		low := strings.ToLower(got.Basis)
		if !strings.Contains(low, "self-pruned") {
			t.Errorf("basis must state the self-prune: %s", got.Basis)
		}
		if !strings.Contains(low, "measured") {
			t.Errorf("basis must carry the MEASURED label: %s", got.Basis)
		}
		// The projection must not alias the receipt's details map.
		got.Details["scene"] = "mutated"
		if r.Details["scene"] != "classroom" {
			t.Errorf("projection aliased the receipt details map (mutation leaked back)")
		}
	})

	t.Run("lane2_delivered", func(t *testing.T) {
		r, err := ParseSpecReceipt([]byte(specLane2Render))
		if err != nil {
			t.Fatalf("ParseSpecReceipt(lane2): %v", err)
		}
		got, err := receiptRenderSpec(&r)
		if err != nil {
			t.Fatalf("receiptRenderSpec(lane2): %v", err)
		}
		if got == nil {
			t.Fatalf("render receipt must project, got nil")
		}
		if got.QualityTier != SpecTierDelivery {
			t.Errorf("quality_tier: got %q, want delivery", got.QualityTier)
		}
		if got.Delivered || got.DeliveryEligible || !got.Parked {
			t.Errorf("a synthetic delivery-tier receipt must park, got %+v", got)
		}
		if got.SelfPruned {
			t.Errorf("a delivery-tier receipt must not read as SelfPruned")
		}
		if got.Evidence != SpecEvidenceSynthetic {
			t.Errorf("evidence: got %q, want synthetic", got.Evidence)
		}
		if got.SpeedupVsBaseline == nil {
			t.Fatalf("lane2 speedup must pass through, got nil")
		}
		specClose(t, "lane2 speedup", *got.SpeedupVsBaseline, 2.580645)
	})
}

func TestReceiptRenderSpecDetailsAreBoundedDeepClone(t *testing.T) {
	r, err := ParseSpecReceipt([]byte(specLane2Render))
	if err != nil {
		t.Fatalf("ParseSpecReceipt(lane2): %v", err)
	}
	r.Details = map[string]any{
		"nested": map[string]any{
			"items": []any{
				map[string]any{"value": "authoritative"},
				[]any{"kept", float64(7)},
			},
		},
	}
	got, err := receiptRenderSpec(&r)
	if err != nil {
		t.Fatalf("receiptRenderSpec(nested details): %v", err)
	}
	buyerNested := got.Details["nested"].(map[string]any)
	buyerItems := buyerNested["items"].([]any)
	buyerItems[0].(map[string]any)["value"] = "buyer-mutated"
	buyerItems[1].([]any)[0] = "buyer-mutated"
	buyerNested["added"] = true

	authoritativeNested := r.Details["nested"].(map[string]any)
	authoritativeItems := authoritativeNested["items"].([]any)
	if authoritativeItems[0].(map[string]any)["value"] != "authoritative" {
		t.Fatal("nested buyer map mutation leaked into authoritative receipt")
	}
	if authoritativeItems[1].([]any)[0] != "kept" {
		t.Fatal("nested buyer slice mutation leaked into authoritative receipt")
	}
	if _, exists := authoritativeNested["added"]; exists {
		t.Fatal("buyer map insertion leaked into authoritative receipt")
	}
	if r.SpeedupVsBaseline == nil || got.SpeedupVsBaseline == nil {
		t.Fatal("precondition: both source and projection need a speedup")
	}
	originalSpeedup := *r.SpeedupVsBaseline
	*got.SpeedupVsBaseline = 99
	if *r.SpeedupVsBaseline != originalSpeedup {
		t.Fatal("buyer speedup pointer mutation leaked into authoritative receipt")
	}
	*r.SpeedupVsBaseline = 77
	if *got.SpeedupVsBaseline != 99 {
		t.Fatal("authoritative speedup pointer mutation leaked into buyer projection")
	}

	tooDeep := any("leaf")
	for range maxSpecReceiptJSONDepth + 1 {
		tooDeep = map[string]any{"child": tooDeep}
	}
	for name, details := range map[string]map[string]any{
		"non-finite":  {"value": math.NaN()},
		"unsupported": {"value": make(chan int)},
		"oversized":   {"value": strings.Repeat("x", maxSpecReceiptBytes)},
		"too deep":    {"value": tooDeep},
	} {
		t.Run(name, func(t *testing.T) {
			bad := r
			bad.Details = details
			projected, err := receiptRenderSpec(&bad)
			if err == nil || projected != nil {
				t.Fatalf("unsafe details must fail closed, got=%+v err=%v", projected, err)
			}
		})
	}
}

// TestReceiptRenderSpecHonestyBoundary: nil at the boundary — a nil receipt and
// a NON-render (token) receipt both project to nil, exactly as receiptRouting
// returns nil for a job that carried no routing block.
func TestReceiptRenderSpecHonestyBoundary(t *testing.T) {
	if got, err := receiptRenderSpec(nil); err != nil || got != nil {
		t.Errorf("nil receipt must project to nil, got %+v", got)
	}
	tok, err := ParseSpecReceipt([]byte(specLane3Token))
	if err != nil {
		t.Fatalf("ParseSpecReceipt(token): %v", err)
	}
	if tok.Modality != "token" {
		t.Fatalf("precondition: lane3 must be a token receipt, got %q", tok.Modality)
	}
	if got, err := receiptRenderSpec(&tok); err != nil || got != nil {
		t.Errorf("a token receipt must not project onto the render block, got %+v", got)
	}
	bad := SpecReceipt{Modality: "render"}
	if got, err := receiptRenderSpec(&bad); err == nil || got != nil {
		t.Errorf("a malformed render row must fail loudly, got=%+v err=%v", got, err)
	}
}

// TestReceiptRenderSpecAbsentBaseline: a render receipt with no baseline
// (baseline_source=absent, null speedup) projects a nil speedup and a basis that
// says no speedup is claimed — a speedup is never invented.
func TestReceiptRenderSpecAbsentBaseline(t *testing.T) {
	r := SpecReceipt{
		SchemaVersion: specReceiptSchemaVersion,
		BranchID:      "render", Modality: "render",
		DraftCostS: 100, VerifyCostS: 1, RepairCostS: 0, TotalProductTimeS: 101,
		BaselineTotalTimeS: 0, BaselineSource: SpecBaselineAbsent,
		Units: 1, AcceptedFraction: 1, RepairedFraction: 0,
		Exact: false, QualityTier: SpecTierPreview,
		ArtifactVerified:  true,
		SpeedupVsBaseline: nil, Evidence: SpecEvidenceMeasured,
		Details: map[string]any{},
	}
	if err := r.Validate(); err != nil {
		t.Fatalf("precondition: absent-baseline receipt must validate, got %v", err)
	}
	got, err := receiptRenderSpec(&r)
	if err != nil {
		t.Fatalf("receiptRenderSpec(absent baseline): %v", err)
	}
	if got == nil {
		t.Fatalf("render receipt must project")
	}
	if got.SpeedupVsBaseline != nil {
		t.Errorf("absent baseline must project a nil speedup, got %v", *got.SpeedupVsBaseline)
	}
	if !strings.Contains(strings.ToLower(got.Basis), "no speedup is claimed") {
		t.Errorf("basis must state no speedup is claimed: %s", got.Basis)
	}
	if got.Delivered || !got.Parked {
		t.Errorf("a naked emitter-verified preview receipt must park pending attestation")
	}
	if !strings.Contains(strings.ToLower(got.Basis), "server attestation") {
		t.Errorf("basis must name the missing authoritative binding: %s", got.Basis)
	}
}
