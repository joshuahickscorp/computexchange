//go:build integration

package main

// honeypot_injection_guard_test.go — INJECTION-TIME PARAM/MODEL GUARD (byte-exact
// honeypot safety, docs/DETERMINISM_CLASS.md; the guard seed.go's
// demoHoneypotHawkKnownAnswer doc names as REQUIRED before production-scale
// byte-exact seeding). AvailableSeedHoneypots must draw a byte-exact seed honeypot
// ONLY for a job it is byte-valid for — the EXACT model + at least the max_tokens
// the known answer was captured under. Drawing the hawking probe for a batch_infer
// job on a DIFFERENT model, or with max_tokens below the seed's floor, would make
// an HONEST same-class worker produce different bytes and get wrongly quarantined
// (the exact hazard this guard closes). A tolerant seed (NULL bounds) keeps the old
// job_type-only behavior.
//
// Runs against the REAL Postgres the shared TestMain (integration_test.go) stands
// up — TestMain runs seedDemo, so the hawking probe already carries
// answer_model=demoHoneypotHawkModel / answer_min_max_tokens=demoHoneypotHawkMinMaxTokens
// and the embed probe carries NULL bounds.

import (
	"context"
	"testing"
)

// hasRef reports whether any returned seed honeypot has the given input_ref.
func hasRef(hps []SeedHoneypot, ref string) bool {
	for _, hp := range hps {
		if hp.InputRef == ref {
			return true
		}
	}
	return false
}

// TestAvailableSeedHoneypotsParamModelGuard pins the byte-exact injection guard:
// the seeded hawking probe (answer_model=llama-3.2-1b-instruct-q4,
// answer_min_max_tokens=24) is returned for an EXACT-match job, NOT for a
// mismatched model, and NOT for a too-small max_tokens — while the tolerant embed
// probe (NULL bounds) is unaffected by model/max_tokens.
func TestAvailableSeedHoneypotsParamModelGuard(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// (1) EXACT MATCH — same model, max_tokens at the seed's floor: drawn.
	hps, err := itStore.AvailableSeedHoneypots(ctx, "batch_infer", demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (exact match): %v", err)
	}
	if !hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("the byte-exact hawking probe MUST be drawn for its exact model + max_tokens; got %+v", hps)
	}

	// EXACT MATCH, ABOVE the floor: also drawn (>= floor, not just ==).
	hps, err = itStore.AvailableSeedHoneypots(ctx, "batch_infer", demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens+100, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (above floor): %v", err)
	}
	if !hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("the byte-exact hawking probe MUST be drawn for max_tokens above the floor; got %+v", hps)
	}

	// (2) MISMATCHED MODEL — same job_type, WRONG model: NOT drawn (an honest
	// worker on this model would legitimately produce different bytes).
	hps, err = itStore.AvailableSeedHoneypots(ctx, "batch_infer", "qwen2.5-7b-instruct-q4", demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (mismatched model): %v", err)
	}
	if hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("the byte-exact hawking probe MUST NOT be drawn for a DIFFERENT model; got %+v", hps)
	}

	// (3) TOO-SMALL max_tokens — exact model but below the seed's floor: NOT drawn
	// (a truncated row's bytes would depend on the job's max_tokens).
	hps, err = itStore.AvailableSeedHoneypots(ctx, "batch_infer", demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens-1, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (too-small max_tokens): %v", err)
	}
	if hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("the byte-exact hawking probe MUST NOT be drawn below its max_tokens floor; got %+v", hps)
	}

	// (4) TOLERANT SEED — the embed probe has NULL bounds, so it keeps the old
	// job_type-only behavior: drawn regardless of the passed model + max_tokens.
	hps, err = itStore.AvailableSeedHoneypots(ctx, "embed", "some-other-embed-model", 1, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (tolerant embed): %v", err)
	}
	if !hasRef(hps, demoHoneypotEmbedRef) {
		t.Fatalf("a tolerant (NULL-bounds) seed honeypot MUST keep job_type-only behavior; got %+v", hps)
	}
}

// TestSeedBackfillsHoneypotBoundsFailOpen proves the fail-OPEN repair (2026-07-09
// audit). A batch_infer honeypot row seeded BEFORE the answer_model /
// answer_min_max_tokens columns existed keeps them NULL, which AvailableSeedHoneypots
// reads as TOLERANT — so the byte-exact hawking probe would be drawn for ANY model /
// smaller max_tokens and wrongly quarantine an honest same-class worker (a silent
// fail-open of the very guard the WHERE-NOT-EXISTS INSERT was added to close, which
// on its own never heals a pre-existing row). seedDemo's idempotent backfill UPDATE
// must restore the canonical bounds AND keep exact-match coverage.
func TestSeedBackfillsHoneypotBoundsFailOpen(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// However this exits, leave the demo row with its canonical bounds so sibling
	// tests (which assume the seeded, guarded state) are unaffected.
	t.Cleanup(func() {
		_, _ = itPool.Exec(ctx,
			`UPDATE honeypots SET answer_model=$2, answer_min_max_tokens=$3
			 WHERE job_type='batch_infer' AND input_ref=$1`,
			demoHoneypotInferRef, demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens)
	})

	// Simulate a pre-migration row: NULL the byte-exact validity bounds.
	if _, err := itPool.Exec(ctx,
		`UPDATE honeypots SET answer_model=NULL, answer_min_max_tokens=NULL
		 WHERE job_type='batch_infer' AND input_ref=$1`, demoHoneypotInferRef); err != nil {
		t.Fatalf("null the bounds: %v", err)
	}

	// (1) FAIL-OPEN CONFIRMED: with NULL bounds the guard draws the byte-exact probe
	// for a WRONG model — the exact wrongful-quarantine hazard.
	hps, err := itStore.AvailableSeedHoneypots(ctx, "batch_infer", "qwen2.5-7b-instruct-q4", demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (pre-backfill): %v", err)
	}
	if !hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("precondition: a NULL-bounds row must be tolerant (fail-open) and drawn for any model; got %+v", hps)
	}

	// (2) HEAL: seedDemo's idempotent backfill stamps the canonical bounds.
	if err := seedDemo(ctx, itPool, itStorage); err != nil {
		t.Fatalf("seedDemo backfill: %v", err)
	}

	// (3) CLOSED: no longer drawn for the wrong model...
	hps, err = itStore.AvailableSeedHoneypots(ctx, "batch_infer", "qwen2.5-7b-instruct-q4", demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (post-backfill, wrong model): %v", err)
	}
	if hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("after backfill the byte-exact probe MUST NOT be drawn for a DIFFERENT model; fail-open not closed; got %+v", hps)
	}

	// ...and STILL drawn for the exact model — coverage preserved, not just disabled.
	hps, err = itStore.AvailableSeedHoneypots(ctx, "batch_infer", demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots (post-backfill, exact model): %v", err)
	}
	if !hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("after backfill the probe MUST still be drawn for its EXACT model; got %+v", hps)
	}

	// (4) IDEMPOTENT: a second backfill is a no-op and stays healed.
	if err := seedDemo(ctx, itPool, itStorage); err != nil {
		t.Fatalf("seedDemo backfill (2nd, idempotency): %v", err)
	}
	hps, _ = itStore.AvailableSeedHoneypots(ctx, "batch_infer", "qwen2.5-7b-instruct-q4", demoHoneypotHawkMinMaxTokens, 10)
	if hasRef(hps, demoHoneypotInferRef) {
		t.Fatalf("idempotency: probe drawn for wrong model after a 2nd backfill; got %+v", hps)
	}
}
