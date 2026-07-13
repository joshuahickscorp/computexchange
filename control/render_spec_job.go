package main

// render_spec_job.go — the ADDITIVE `render_speculative` job surface: a pure,
// deterministic advisory-quote block for the speculative RENDER lane, plus the
// buyer-facing projection of a landed SpecReceipt onto that surface. This file
// is DESIGN-COMPLETE CODE for the additive layer ONLY — it is NOT wired into
// buildQuote / createJob / the receipt hot path (that is the owner-approved,
// sequenced climb: docs/research/CX_RENDER_SPEC_JOB_SCAFFOLD.md §"sequencing",
// mirroring docs/research/CX_SPEC_LANE_INTEGRATION_DESIGN.md §6). Nothing here
// imports beyond stdlib and it modifies no existing control/*.go file.
//
// The pattern is a 1:1 mirror of the substrate-routing block (entries 93–94 in
// docs/internal/CREED_AND_PATH_TO_TEN.md; control/routing.go + control/receipt.go):
//
//   - pure decision/projection logic — no DB, no clock, no randomness — unit
//     tested in isolation (render_spec_job_test.go);
//   - every quoted speedup carried as [MODELED] from a MEASURED banked receipt,
//     never extrapolated past what was measured (the `gpuBatchCeiling`
//     discipline), and the conservatism points AGAINST our own case;
//   - an honesty boundary at the edge: a shot OUTSIDE the measured envelope gets
//     the tier contract but NO speedup band, exactly as an embed job gets no
//     routing block;
//   - a checked receipt projection that returns nil,nil for absent/non-render
//     input, rejects malformed render rows, and parks naked worker evidence until
//     authoritative server attestation exists (mirror: receiptRouting(inv)).
//
// The load-bearing honesty invariants (docs/research/CONSOLIDATION_PLAN_2026-07-09.md):
//
//  1. NO cross-lane product. A speedup is a single-workload ratio from the
//     ledger; there is deliberately no field, const, or code path that
//     multiplies two lanes' (or two tiles') multipliers. The quote carries a
//     BAND of real same-discipline measurements, never a synthesized figure.
//  2. Strict delivery is MEASURED on two exact recipes, but it is not generalized:
//     the untuned 1080p scene sweep cleared 1/3 scenes. RenderSpecParams binds
//     shape only, not scene content or the repair-policy/build digest. Therefore
//     QuoteRenderSpec never turns those bound receipts into an UNBOUND promise:
//     QuotedTier remains "preview", StrictDeliveryPromised remains false, and an
//     unbound request receives no projected speedup band.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"strings"
)

// --- the MEASURED banked record this surface stands on -----------------------
//
// All figures below are transcribed from the banked ledger, NOT recomputed:
// docs/research/CONSOLIDATION_PLAN_2026-07-09.md (the staged table) and
// docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl.

// Historical standalone-render context only. These are not an integrated
// product band and are never attached to a shape-only quote: standalone renders at the
// delivery gate (g>=0.98, worst-tile>=0.95), draft 8–32 spp vs a 4096-spp
// reference, on an L40S and RE-EXPRESSED device-correct through the render
// adapter (CONSOLIDATION_PLAN table: many_glass 3.87x / sphere_bump 4.59x /
// monkey 6.88x / cube_volume 7.87x). The 14.34x "unusually forgiving" scene is
// a convergence-trivial cherry-pick and is DELIBERATELY EXCLUDED — reporting it
// would be the massaged positive the plan forbids. This is a BAND of real
// same-discipline measurements; it is NEVER interpolated to a per-shot point
// (that would invent a number the ledger does not contain).
const (
	renderSpecStandaloneLowX  = 3.87 // many_glass — hardest representative scene measured
	renderSpecStandaloneHighX = 7.87 // cube_volume — easiest non-forgiving scene measured
)

// The integrated 4K preview/no-repair anchor: REAL RUN 3 (A100 SECURE, classroom
// 3840x2160, ref 4096 / draft 512, keyframe_every=1, zero reprojection) —
// 4546.761007 / 817.567636 = 5.561327x @ global 0.9854 / p5 0.9664 /
// worst-tile 0.9095, MEASURED integrated product time.
// It self-pruned at the STRICT delivery gate (worst-tile 0.91 < 0.95) but
// CLEARS the historically-published 0.95/preview tier and reproduces the banked
// 5.84x product number within 5% on different silicon. It sits inside the
// representative band above and is carried as an explicit reference point, never
// as a promise for a shot that is not this exact config.
const renderSpec4KPreviewAnchorX = 5.561327

// The strict-delivery 4K anchor: H100 classroom, the bound aov_edge +
// match-reference repair recipe. 2770.964385 / 1130.593241 = 2.450894x,
// global 0.9902 / p5 0.9738 / worst-tile 0.9501. The untuned 1080p sweep then
// cleared strict on only pavilion (1.658801x), pruning classroom and bmw27;
// these are historical measured facts, not a universal interval.
const renderSpec4KStrictAnchorX = 2.450894

// The MEASURED envelope. Outside it there is no honest band to quote (the
// routing precedent: "the sweep measured generative decode only, so every other
// shape gets NO routing block"). Each bound is a real ledger boundary:
const (
	// The 4K ceiling is RUN 3's resolution; nothing above 4K has been measured
	// integrated, so a higher resolution gets the tier contract but no band.
	renderSpecMaxWidth  = 3840
	renderSpecMaxHeight = 2160
	// The integrated receipts (RUN 1–4) all rendered exactly 4 frames. Per the
	// integration design, "frames >> 4" is out of envelope; we refuse to
	// extrapolate a band onto a sequence length no integrated receipt measured.
	renderSpecMaxFrames = 4
	// The measured reference/draft spp ratio spans 8x (RUN 3: 4096/512) to 512x
	// (representative many_glass: 4096/8). A ratio outside this is an unmeasured
	// draft coarseness — no band.
	renderSpecMinSPPRatio = 8.0
	renderSpecMaxSPPRatio = 512.0
)

// renderSpecQuoteBasis is the quoteRoutingBasis analog: the one string a buyer
// can follow to the raw numbers, with the honesty label welded to every figure.
// It names the banked receipts, states that the band is a projection of prior
// MEASURED runs (not a measurement of THIS shot), and that it excludes
// provisioning — so the conservatism points the right way.
const renderSpecQuoteBasis = "NO PER-JOB SPEED FORECAST — this shape-only request does not bind scene content or the repair-policy/build digest. Historical MEASURED context only, never multiplied or projected: integrated 4K classroom preview/no-repair 5.561327x (4546.761007s / 817.567636s); integrated 4K classroom strict delivery 2.450894x (2770.964385s / 1130.593241s, global 0.9902, worst-tile 0.9501); untuned 1080p strict sweep passed 1/3 scenes (pavilion 1.658801x) and pruned classroom/bmw27. Standalone-render context 3.87x-7.87x is not an integrated-product band. Sources: docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl and scene_sweep_ledger.jsonl. Excludes provisioning/queue time."

// Tier vocabulary echoed from spec_receipt.go (SpecTier*): the render quote may
// only ever offer an unbound "preview". "delivery" is accepted as an ask but
// is never quoted without scene/config/policy binding; exact recipes have cleared it.
const (
	renderSpecTierPreview  = SpecTierPreview
	renderSpecTierDelivery = SpecTierDelivery // exact-recipe target, never an unbound promise
)

// --- (a) the advisory quote --------------------------------------------------

// RenderSpecParams is the shot shape QuoteRenderSpec reads — the validated
// projection of scripts/spec-lab/pod/exp_render_stack.py's config JSON that the
// later `render_speculative` job type will carry (design §3.1). Pure inputs; no
// object keys or credentials live here.
type RenderSpecParams struct {
	Width         int    // pixels; per-frame render width
	Height        int    // pixels; per-frame render height
	Frames        int    // frames in the shot (>= 1)
	RefSPP        int    // reference (full-quality) samples per pixel
	DraftSPP      int    // draft (speculative) samples per pixel; must be < RefSPP
	KeyframeEvery int    // 1 = all-anchor (measured-safe); >1 = the reprojection lever
	RequestedTier string // "" | "preview" | "delivery" (delivery needs evidence binding)
}

// Validate enforces the range/enum contract on the shot params. Like
// SpecReceipt.Validate it does NOT judge merit — it rejects only the physically
// impossible or the out-of-vocabulary, so a legitimately hard shot still quotes.
func (p RenderSpecParams) Validate() error {
	if p.Width <= 0 || p.Height <= 0 {
		return fmt.Errorf("render spec quote: width and height must be > 0, got %dx%d", p.Width, p.Height)
	}
	if p.Frames < 1 {
		return fmt.Errorf("render spec quote: frames must be >= 1, got %d", p.Frames)
	}
	if p.RefSPP <= 0 {
		return fmt.Errorf("render spec quote: ref_spp must be > 0, got %d", p.RefSPP)
	}
	if p.DraftSPP <= 0 {
		return fmt.Errorf("render spec quote: draft_spp must be > 0, got %d", p.DraftSPP)
	}
	if p.DraftSPP >= p.RefSPP {
		return fmt.Errorf("render spec quote: draft_spp (%d) must be < ref_spp (%d) — a draft that is not cheaper than the reference is not a draft", p.DraftSPP, p.RefSPP)
	}
	if p.KeyframeEvery < 1 {
		return fmt.Errorf("render spec quote: keyframe_every must be >= 1, got %d", p.KeyframeEvery)
	}
	switch p.RequestedTier {
	case "", renderSpecTierPreview, renderSpecTierDelivery:
	default:
		return fmt.Errorf("render spec quote: requested_tier must be one of \"\"|preview|delivery, got %q", p.RequestedTier)
	}
	return nil
}

// RenderSpecQuote is the advisory block QuoteRenderSpec attaches (the render
// analog of QuoteRouting). Shape alone is not evidence binding, so the retained
// speedup fields stay nil until scene and policy/build digests exist.
type RenderSpecQuote struct {
	Modality string `json:"modality"` // always "render"
	// QuotedTier is the unbound tier this shape-only surface can offer. Strict
	// delivery exists, but only for evidence-bound recipes this type cannot name.
	QuotedTier string `json:"quoted_tier"`
	// RequestedTier echoes the ask so the receipt can later show promised-vs-asked.
	RequestedTier string `json:"requested_tier,omitempty"`
	// StrictDeliveryPromised is always false: this surface cannot bind the scene
	// and verifier/repair policy needed to reproduce a strict receipt.
	StrictDeliveryPromised bool `json:"strict_delivery_promised"`
	// InEnvelope is false until the request is evidence-bound.
	InEnvelope bool `json:"in_envelope"`
	// Deprecated shape-projection fields, retained for additive wire compatibility.
	SpeedupBandLowX  *float64 `json:"speedup_band_low_x,omitempty"`
	SpeedupBandHighX *float64 `json:"speedup_band_high_x,omitempty"`
	Anchor4KSpeedupX *float64 `json:"anchor_4k_speedup_x,omitempty"`
	// Reason is the plain-english why, quote-warnings voice — names the envelope
	// decision and why an exact-recipe result is not an unbound promise.
	Reason string `json:"reason"`
	// Basis welds the honesty label to every number (renderSpecQuoteBasis).
	Basis string `json:"basis"`
}

// QuoteRenderSpec reads a shot's shape and produces the advisory render-spec
// quote. PURE and DETERMINISTIC: identical inputs yield an identical quote. No
// input can emit an unbound strict-delivery promise or a scene-agnostic speedup
// forecast. Returns an error only for params that
// fail Validate; a valid-but-out-of-envelope shot is NOT an error, it is a quote
// with no band.
func QuoteRenderSpec(p RenderSpecParams) (RenderSpecQuote, error) {
	if err := p.Validate(); err != nil {
		return RenderSpecQuote{}, err
	}

	q := RenderSpecQuote{
		Modality:               "render",
		QuotedTier:             renderSpecTierPreview,
		RequestedTier:          p.RequestedTier,
		StrictDeliveryPromised: false, // structural invariant #2
		Basis:                  renderSpecQuoteBasis,
	}

	// A delivery ask is acknowledged against current evidence, not the stale
	// pre-GROW claim that strict had never cleared.
	deliveryClause := ""
	if p.RequestedTier == renderSpecTierDelivery {
		deliveryClause = " delivery was requested: strict delivery has been demonstrated on exact measured recipes, but this shape-only request does not bind scene content or repair policy; the untuned scene sweep cleared 1 of 3 scenes, so this quote makes no unbound strict-delivery promise."
	}

	inEnv, envReason := renderSpecInEnvelope(p)
	q.InEnvelope = inEnv
	q.Reason = strings.TrimSpace(fmt.Sprintf(
		"quoting the preview tier with NO per-job speedup band: %s — historical receipts remain context, never a projection onto an unbound shot.%s",
		envReason, deliveryClause))
	return q, nil
}

// renderSpecInEnvelope decides whether a request carries enough evidence to
// project a speedup. Shape checks remain for precise diagnostics, but the
// answer stays false until scene and policy/build digests are part of params.
// The gates, each a real ledger boundary (see the const block):
//   - keyframe_every must be 1. kf>1 is the reprojection lever whose ONLY
//     integrated measurement (RUN 1, kf=4) quality-FAILED (worst-tile 0.164) —
//     quoting a speedup for a config we measured to fail would be dishonest.
//   - resolution must be within the 4K ceiling.
//   - frames must be within the measured count (not >> 4).
//   - the ref/draft spp ratio must sit inside the measured [8x, 512x] span.
func renderSpecInEnvelope(p RenderSpecParams) (bool, string) {
	if p.KeyframeEvery != 1 {
		return false, fmt.Sprintf(
			"keyframe_every=%d uses the reprojection path whose only integrated measurement (run 1, kf=4) failed the quality gate at worst-tile 0.164; the measured-safe path is keyframe_every=1 (all-anchor, zero reprojection)",
			p.KeyframeEvery)
	}
	if p.Width > renderSpecMaxWidth || p.Height > renderSpecMaxHeight {
		return false, fmt.Sprintf(
			"%dx%d exceeds the measured 4k ceiling (%dx%d, run 3); no integrated receipt exists above 4k",
			p.Width, p.Height, renderSpecMaxWidth, renderSpecMaxHeight)
	}
	if p.Frames > renderSpecMaxFrames {
		return false, fmt.Sprintf(
			"%d frames exceeds the measured integrated count (%d, runs 1-4); a longer sequence is unmeasured and we do not extrapolate a band onto it",
			p.Frames, renderSpecMaxFrames)
	}
	ratio := float64(p.RefSPP) / float64(p.DraftSPP)
	if ratio < renderSpecMinSPPRatio || ratio > renderSpecMaxSPPRatio {
		return false, fmt.Sprintf(
			"ref/draft spp ratio %.1fx (ref %d / draft %d) is outside the measured [%.0fx, %.0fx] span; that draft coarseness is unmeasured",
			ratio, p.RefSPP, p.DraftSPP, renderSpecMinSPPRatio, renderSpecMaxSPPRatio)
	}
	return false, fmt.Sprintf(
		"shape is inside prior measurements (%dx%d, %d frame(s), ref %d / draft %d spp, %.1fx, keyframe_every=1), but scene content and repair-policy/build digests are unbound",
		p.Width, p.Height, p.Frames, p.RefSPP, p.DraftSPP, ratio)
}

// --- (b) the buyer-facing receipt projection ---------------------------------

// RenderSpecReceipt is the buyer-facing projection of a landed SpecReceipt onto
// the render surface (the render analog of the ClearingReceipt.Routing facet).
// It carries the receipt's OWN honesty labels — nothing here re-judges or
// re-computes the numbers; it re-states them plainly for a buyer.
type RenderSpecReceipt struct {
	Modality string `json:"modality"`
	// QualityTier is what was DELIVERED, worst-wins across units (fail|preview|
	// delivery). A "fail" tier on a finished job means the lane self-pruned.
	QualityTier string `json:"quality_tier"`
	// Delivered is true when the lane cleared a sellable tier (preview or better).
	Delivered bool `json:"delivered"`
	// DeliveryEligible is derived from measured evidence + a cleared tier. A
	// synthetic/modeled/imported row remains visible but parks and cannot bill.
	DeliveryEligible bool `json:"delivery_eligible"`
	Parked           bool `json:"parked"`
	// SelfPruned is true when QualityTier == fail: the lane rejected its own
	// output and the buyer got the reference-path render (billed as a plain
	// render, not spec-lane seconds — design §3.5). The honest attempt still shows.
	SelfPruned bool `json:"self_pruned"`
	// SpeedupVsBaseline is the ONE ratio baseline/spec for THIS job; nil when the
	// receipt carried no baseline (baseline_source=absent) — a speedup is never
	// invented. Passed through verbatim from the SpecReceipt.
	SpeedupVsBaseline *float64 `json:"speedup_vs_baseline"`
	// The raw cost/outcome facts, restated. Times are seconds.
	TotalProductTimeS  float64 `json:"total_product_time_s"`
	BaselineTotalTimeS float64 `json:"baseline_total_time_s"`
	Units              uint32  `json:"units"`
	AcceptedFraction   float64 `json:"accepted_fraction"`
	RepairedFraction   float64 `json:"repaired_fraction"`
	Exact              bool    `json:"exact"`
	// The three honesty labels, carried through unchanged from the receipt.
	Evidence       string `json:"evidence"`        // measured|modeled|synthetic|imported
	BaselineSource string `json:"baseline_source"` // measured|modeled|absent
	// Basis is the composed honesty sentence for THIS receipt (unlike the quote's
	// const basis, a receipt's honesty is per-row: it depends on evidence +
	// baseline_source + tier). Built by renderSpecReceiptBasis.
	Basis string `json:"basis"`
	// Details carries the SSIM triple / scene / device / selector_recall through
	// a bounded deep clone (omitempty so an empty bag stays off the wire).
	Details map[string]any `json:"details,omitempty"`
}

// boundedRenderDetailsBuffer prevents a direct in-memory receipt from making
// projection allocate an encoded details blob larger than the receipt ingress
// limit. Encoder.Encode adds one trailing newline, which is intentionally
// charged to this conservative cap.
type boundedRenderDetailsBuffer struct {
	bytes.Buffer
}

func (w *boundedRenderDetailsBuffer) Write(p []byte) (int, error) {
	if len(p) > maxSpecReceiptBytes-w.Len() {
		return 0, fmt.Errorf("details JSON exceeds %d-byte receipt limit", maxSpecReceiptBytes)
	}
	return w.Buffer.Write(p)
}

// cloneRenderSpecDetails accepts only the same JSON-compatible tree the receipt
// ingress accepts, enforces the same byte and nesting bounds, and returns a
// structurally independent map. JSON encoding fails cleanly on NaN/Inf,
// unsupported values and reference cycles.
func cloneRenderSpecDetails(details map[string]any) (map[string]any, error) {
	if len(details) == 0 {
		return nil, nil
	}
	var encoded boundedRenderDetailsBuffer
	if err := json.NewEncoder(&encoded).Encode(details); err != nil {
		return nil, fmt.Errorf("encode details: %w", err)
	}
	payload := bytes.TrimSpace(encoded.Bytes())
	if err := rejectDuplicateSpecJSONKeys(payload); err != nil {
		return nil, fmt.Errorf("validate details: %w", err)
	}
	var cloned map[string]any
	if err := json.Unmarshal(payload, &cloned); err != nil {
		return nil, fmt.Errorf("decode details clone: %w", err)
	}
	return cloned, nil
}

// receiptRenderSpec projects a landed SpecReceipt onto the buyer-facing render
// block — the pure twin of receiptRouting(inv). It returns nil,nil at the
// honesty boundary (nil or non-render), but a malformed render row is an
// explicit error and can never masquerade as an absent block.
func receiptRenderSpec(r *SpecReceipt) (*RenderSpecReceipt, error) {
	if r == nil || r.Modality != "render" {
		return nil, nil
	}
	if err := r.Validate(); err != nil {
		return nil, fmt.Errorf("render spec receipt: %w", err)
	}
	var speedup *float64
	if r.SpeedupVsBaseline != nil {
		value := *r.SpeedupVsBaseline
		speedup = &value
	}
	out := &RenderSpecReceipt{
		Modality:    r.Modality,
		QualityTier: r.QualityTier,
		// A naked worker receipt is evidence, not server attestation. Keep every
		// non-fail row parked until job/input/artifact/policy binding is verified.
		Delivered:          false,
		DeliveryEligible:   false,
		Parked:             r.QualityTier != SpecTierFail,
		SelfPruned:         r.QualityTier == SpecTierFail,
		SpeedupVsBaseline:  speedup, // value copied; nil stays nil
		TotalProductTimeS:  r.TotalProductTimeS,
		BaselineTotalTimeS: r.BaselineTotalTimeS,
		Units:              r.Units,
		AcceptedFraction:   r.AcceptedFraction,
		RepairedFraction:   r.RepairedFraction,
		Exact:              r.Exact,
		Evidence:           r.Evidence,
		BaselineSource:     r.BaselineSource,
	}
	if len(r.Details) > 0 {
		details, err := cloneRenderSpecDetails(r.Details)
		if err != nil {
			return nil, fmt.Errorf("render spec receipt details: %w", err)
		}
		out.Details = details
	}
	out.Basis = renderSpecReceiptBasis(r)
	return out, nil
}

// renderSpecReceiptBasis composes the per-receipt honesty sentence a buyer
// reads. It states, plainly and only from the receipt's own labels: what tier
// was delivered (and, on a self-prune, that they got the reference render);
// whether the speedup is measured/modeled and against what kind of baseline;
// and, when there is no baseline, that no speedup is claimed.
func renderSpecReceiptBasis(r *SpecReceipt) string {
	var b strings.Builder
	parkReason := func() string {
		if r.DeliveryEligible() {
			return "its emitter-local proof is not bound to authoritative server attestation"
		}
		if !r.ArtifactVerified {
			return "the final artifact lacks authoritative verification proof"
		}
		if r.Evidence != SpecEvidenceMeasured {
			return fmt.Sprintf("its evidence is %s, not measured", r.Evidence)
		}
		return "it did not clear the validated delivery contract"
	}

	switch r.QualityTier {
	case SpecTierFail:
		b.WriteString("self-pruned at the quality gate: the speculative artifact was rejected. This naked receipt does not prove which fallback artifact was ultimately delivered or how it was billed; authoritative delivery requires bound server attestation.")
	case SpecTierPreview:
		b.WriteString(fmt.Sprintf("preview quality was recorded, but the receipt is PARKED because %s; it is not delivery-eligible or billable.", parkReason()))
	case SpecTierDelivery:
		b.WriteString(fmt.Sprintf("delivery-tier quality was recorded, but the receipt is PARKED because %s; it is not delivery-eligible or billable.", parkReason()))
	default:
		b.WriteString(fmt.Sprintf("delivered at tier %q.", r.QualityTier))
	}

	if r.SpeedupVsBaseline == nil || r.BaselineSource == SpecBaselineAbsent {
		b.WriteString(" no baseline was rendered for this job, so no speedup is claimed (a speedup is never invented).")
		return b.String()
	}

	label := strings.ToUpper(r.Evidence)
	switch r.BaselineSource {
	case SpecBaselineMeasured:
		b.WriteString(fmt.Sprintf(" speedup_vs_baseline %.4fx is %s against a REAL reference render of this same unit (baseline_source=measured).", *r.SpeedupVsBaseline, label))
	case SpecBaselineModeled:
		b.WriteString(fmt.Sprintf(" speedup_vs_baseline %.4fx is %s against a MODELED baseline (baseline_source=modeled — e.g. a cached same-scene reference), and the label rides the receipt.", *r.SpeedupVsBaseline, label))
	default:
		b.WriteString(fmt.Sprintf(" speedup_vs_baseline %.4fx (%s).", *r.SpeedupVsBaseline, label))
	}
	return b.String()
}

// renderSpecRoundSpeedup rounds a speedup figure to 4 decimals for display — a
// modeled/measured ratio printed past that is false precision. Exposed for the
// tests and any future wiring; unused by the projection itself (which passes the
// raw ledger value through untouched).
func renderSpecRoundSpeedup(x float64) float64 { return math.Round(x*10000) / 10000 }
