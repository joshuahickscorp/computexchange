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
//   - a pure receipt projection that returns nil at the honesty boundary, never
//     re-decides, and carries the receipt's OWN measured-vs-modeled labels
//     through to the buyer (mirror: receiptRouting(inv)).
//
// The load-bearing honesty invariants (docs/research/CONSOLIDATION_PLAN_2026-07-09.md):
//
//  1. NO cross-lane product. A speedup is a single-workload ratio from the
//     ledger; there is deliberately no field, const, or code path that
//     multiplies two lanes' (or two tiles') multipliers. The quote carries a
//     BAND of real same-discipline measurements, never a synthesized figure.
//  2. The ONLY sellable render tier today is the 0.95/preview band — the only
//     tier the integrated ledger has delivered at >1x. Strict delivery
//     (worst-tile >= 0.95, = SpecTierDelivery) is STILL OPEN: RUN 3 self-pruned
//     at it (worst-tile 0.9095 < 0.95). So QuoteRenderSpec NEVER emits a
//     delivery/strict promise — QuotedTier is always "preview" and
//     StrictDeliveryPromised is a compile-time-constant false.

import (
	"fmt"
	"math"
	"strings"
)

// --- the MEASURED banked record this surface stands on -----------------------
//
// All figures below are transcribed from the banked ledger, NOT recomputed:
// docs/research/CONSOLIDATION_PLAN_2026-07-09.md (the staged table) and
// docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl.

// The representative-scene MEASURED speedup band: standalone renders at the
// delivery gate (g>=0.98, worst-tile>=0.95), draft 8–32 spp vs a 4096-spp
// reference, on an L40S and RE-EXPRESSED device-correct through the render
// adapter (CONSOLIDATION_PLAN table: many_glass 3.87x / sphere_bump 4.59x /
// monkey 6.88x / cube_volume 7.87x). The 14.34x "unusually forgiving" scene is
// a convergence-trivial cherry-pick and is DELIBERATELY EXCLUDED — reporting it
// would be the massaged positive the plan forbids. This is a BAND of real
// same-discipline measurements; it is NEVER interpolated to a per-shot point
// (that would invent a number the ledger does not contain).
const (
	renderSpecBandLowX  = 3.87 // many_glass — the hardest representative scene measured
	renderSpecBandHighX = 7.87 // cube_volume — the easiest NON-forgiving scene measured
)

// The integrated 4K headline anchor: REAL RUN 3 (A100 SECURE, classroom
// 3840x2160, ref 4096 / draft 512, keyframe_every=1, zero reprojection) —
// 5.561x @ global 0.9854 / p5 0.9664 / worst-tile 0.9095, MEASURED end-to-end.
// It self-pruned at the STRICT delivery gate (worst-tile 0.91 < 0.95) but
// CLEARS the historically-published 0.95/preview tier and reproduces the banked
// 5.84x product number within 5% on different silicon. It sits inside the
// representative band above and is carried as an explicit reference point, never
// as a promise for a shot that is not this exact config.
const renderSpec4KAnchorX = 5.561

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
const renderSpecQuoteBasis = "speedup_band [MODELED] — a projection of MEASURED banked receipts onto this shot's shape, NOT a measurement of this job and NOT a product of any lane/tile multipliers: the representative-scene band 3.87x-7.87x (standalone delivery-tier renders, device-correct) and the integrated 4K anchor 5.561x (REAL RUN 3, A100, classroom 3840x2160, ref 4096/draft 512, keyframe_every=1 — measured 4546.76s reference vs 817.36s spec, self-pruned at the STRICT delivery gate but clearing the 0.95/preview tier). Source: docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl + docs/research/CONSOLIDATION_PLAN_2026-07-09.md. Excludes provisioning/queue time."

// Tier vocabulary echoed from spec_receipt.go (SpecTier*): the render quote may
// only ever offer "preview". "delivery" (= the strict worst-tile>=0.95 gate) is
// accepted as an ASK but is never QUOTED — no integrated receipt has cleared it.
const (
	renderSpecTierPreview  = SpecTierPreview  // the only sellable tier today
	renderSpecTierDelivery = SpecTierDelivery // acceptable as a target, never a promise
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
	RequestedTier string // "" | "preview" | "delivery"  (delivery is an ask, never a quote)
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
// analog of QuoteRouting). Every speedup field is [MODELED] via Basis. The band
// pointers are nil when the shot is outside the measured envelope — the tier
// contract still stands, but there is no honest speedup to quote.
type RenderSpecQuote struct {
	Modality string `json:"modality"` // always "render"
	// QuotedTier is the tier the product will SELL at — always "preview", the
	// only tier the integrated ledger delivers at >1x today.
	QuotedTier string `json:"quoted_tier"`
	// RequestedTier echoes the ask so the receipt can later show promised-vs-asked.
	RequestedTier string `json:"requested_tier,omitempty"`
	// StrictDeliveryPromised is ALWAYS false — the structural guarantee that this
	// surface never sells the still-open strict-delivery tier (invariant #2).
	StrictDeliveryPromised bool `json:"strict_delivery_promised"`
	// InEnvelope reports whether the shot sits inside the MEASURED envelope; when
	// false the band pointers are nil.
	InEnvelope bool `json:"in_envelope"`
	// The [MODELED] representative band and the 4K reference anchor. Nil out of
	// envelope. omitempty keeps an out-of-envelope quote clean (tier + reason only).
	SpeedupBandLowX  *float64 `json:"speedup_band_low_x,omitempty"`
	SpeedupBandHighX *float64 `json:"speedup_band_high_x,omitempty"`
	Anchor4KSpeedupX *float64 `json:"anchor_4k_speedup_x,omitempty"`
	// Reason is the plain-english why, quote-warnings voice — names the envelope
	// decision and, always, that delivery is a target not a promise.
	Reason string `json:"reason"`
	// Basis welds the honesty label to every number (renderSpecQuoteBasis).
	Basis string `json:"basis"`
}

// QuoteRenderSpec reads a shot's shape and produces the advisory render-spec
// quote. PURE and DETERMINISTIC: identical inputs yield an identical quote, and
// there is no path — for any input, including RequestedTier="delivery" — by
// which it emits a strict-delivery promise (QuotedTier stays "preview",
// StrictDeliveryPromised stays false). Returns an error only for params that
// fail Validate; a valid-but-out-of-envelope shot is NOT an error, it is a quote
// with no band.
func QuoteRenderSpec(p RenderSpecParams) (RenderSpecQuote, error) {
	if err := p.Validate(); err != nil {
		return RenderSpecQuote{}, err
	}

	q := RenderSpecQuote{
		Modality:               "render",
		QuotedTier:             renderSpecTierPreview, // never "delivery"
		RequestedTier:          p.RequestedTier,
		StrictDeliveryPromised: false, // structural invariant #2
		Basis:                  renderSpecQuoteBasis,
	}

	// The delivery-is-a-target clause rides EVERY quote, in or out of envelope:
	// even when the buyer asked for delivery we quote preview and say why.
	deliveryClause := ""
	if p.RequestedTier == renderSpecTierDelivery {
		deliveryClause = " you asked for the delivery tier: it is a target, NOT a promise — no integrated receipt has cleared the strict gate (RUN 3 self-pruned at worst-tile 0.91 < 0.95), so this quote is for the preview tier only."
	}

	inEnv, envReason := renderSpecInEnvelope(p)
	q.InEnvelope = inEnv
	if inEnv {
		lo, hi, anchor := renderSpecBandLowX, renderSpecBandHighX, renderSpec4KAnchorX
		q.SpeedupBandLowX, q.SpeedupBandHighX, q.Anchor4KSpeedupX = &lo, &hi, &anchor
		q.Reason = strings.TrimSpace(fmt.Sprintf(
			"quoting the preview tier at a [modeled] %.2fx-%.2fx speedup band (4k anchor %.3fx): %s.%s",
			renderSpecBandLowX, renderSpecBandHighX, renderSpec4KAnchorX, envReason, deliveryClause))
	} else {
		q.Reason = strings.TrimSpace(fmt.Sprintf(
			"quoting the preview tier with NO speedup band: %s — outside the measured envelope we state the tier contract but never invent a speedup.%s",
			envReason, deliveryClause))
	}
	return q, nil
}

// renderSpecInEnvelope decides whether a shot is close enough to the MEASURED
// runs to carry the banked band, and returns the plain-english why either way.
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
	return true, fmt.Sprintf(
		"%dx%d, %d frame(s), ref %d / draft %d spp (%.1fx), keyframe_every=1 sits inside the measured envelope",
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
	// Details passes the SSIM triple / scene / device / selector_recall through
	// untouched (omitempty so an empty bag stays off the wire).
	Details map[string]any `json:"details,omitempty"`
}

// receiptRenderSpec projects a landed SpecReceipt onto the buyer-facing render
// block — the pure twin of receiptRouting(inv). It returns nil at the honesty
// boundary: a nil receipt, or one whose modality is not the render lane (a
// token-only receipt has no render surface to project onto). It NEVER
// re-decides or re-scores — it restates the receipt's own measured facts and
// labels. The caller is expected to have obtained r via ParseSpecReceipt (which
// validates at the door), exactly as receiptRouting trusts the persisted
// columns; a defensive Validate() here would mask a corrupt row the caller
// should have failed loudly on.
func receiptRenderSpec(r *SpecReceipt) *RenderSpecReceipt {
	if r == nil || r.Modality != "render" {
		return nil
	}
	out := &RenderSpecReceipt{
		Modality:           r.Modality,
		QualityTier:        r.QualityTier,
		Delivered:          r.QualityTier == SpecTierPreview || r.QualityTier == SpecTierDelivery,
		SelfPruned:         r.QualityTier == SpecTierFail,
		SpeedupVsBaseline:  r.SpeedupVsBaseline, // verbatim; nil stays nil
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
		// Shallow copy so the projection never aliases (or mutates) the receipt's
		// own details map.
		d := make(map[string]any, len(r.Details))
		for k, v := range r.Details {
			d[k] = v
		}
		out.Details = d
	}
	out.Basis = renderSpecReceiptBasis(r)
	return out
}

// renderSpecReceiptBasis composes the per-receipt honesty sentence a buyer
// reads. It states, plainly and only from the receipt's own labels: what tier
// was delivered (and, on a self-prune, that they got the reference render);
// whether the speedup is measured/modeled and against what kind of baseline;
// and, when there is no baseline, that no speedup is claimed.
func renderSpecReceiptBasis(r *SpecReceipt) string {
	var b strings.Builder

	switch r.QualityTier {
	case SpecTierFail:
		b.WriteString("self-pruned at the quality gate: the spec lane rejected its own output and you received the reference-path render (billed as a plain render, not spec-lane seconds); the attempt is shown honestly.")
	case SpecTierPreview:
		b.WriteString("delivered at the preview tier (the sellable 0.95 band).")
	case SpecTierDelivery:
		b.WriteString("delivered at the strict delivery tier.")
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
