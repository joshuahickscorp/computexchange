package main

import (
	"strings"
	"testing"
)

// pricing_test.go — pure unit tests for the Buyer Advantage & Pricing Edge 4.5->5
// repricing formula (no DB). The DB-writing side (ApplyRepricing) is covered by
// the integration suite (TestApplyRepricingUsesRealSupplierEconomics).

// TestRepriceFromSupplierEconomicsMathIsCorrect pins the exact arithmetic against
// a hand-computed example, so a future refactor cannot silently change the formula
// while every test still passes.
func TestRepriceFromSupplierEconomicsMathIsCorrect(t *testing.T) {
	b := measuredThroughput{
		ModelID: "test-model", JobType: "embed", UnitsPerSec: 1000.0, HWClass: "apple_silicon_pro",
		SourceCitation: "test",
	}
	// apple_silicon_pro = 30W, electricity $0.10/kWh -> electricity_usd_hr = 0.003
	// units_per_hr = 3,600,000; supplier_share = 0.97 (3% take)
	// price_per_1k = (2.0 + 0.003) / (3600000/1000 * 0.97) = 2.003 / 3492 = 0.00057360...
	got := repriceFromSupplierEconomics(b, 0.97, 0.10)
	wantPrice := (targetSupplierUSDHr + 0.003) / (3600000.0 / 1000.0 * 0.97)
	if diff := got.PricePer1K - wantPrice; diff > 1e-9 || diff < -1e-9 {
		t.Fatalf("price_per_1k = %.10f, want %.10f", got.PricePer1K, wantPrice)
	}
	if got.ModelID != "test-model" || got.JobType != "embed" {
		t.Fatalf("result header wrong: %+v", got)
	}
	// The formula string is the proof artifact: it must cite the real inputs, not
	// just the output number, so a price is traceable.
	for _, want := range []string{"test", "apple_silicon_pro", "30W", "target_supplier_usd_hr=2.00"} {
		if !strings.Contains(got.Formula, want) {
			t.Fatalf("formula missing %q: %s", want, got.Formula)
		}
	}
}

// TestRepriceFromSupplierEconomicsHigherThroughputMeansLowerPrice proves the
// formula's direction is sane: a model that produces more units/sec needs a LOWER
// per-1k price to deliver the SAME target supplier $/hr — this is the whole point
// of pricing off real throughput instead of a flat guess.
func TestRepriceFromSupplierEconomicsHigherThroughputMeansLowerPrice(t *testing.T) {
	slow := measuredThroughput{ModelID: "slow", JobType: "batch_infer", UnitsPerSec: 100, HWClass: "apple_silicon_pro"}
	fast := measuredThroughput{ModelID: "fast", JobType: "batch_infer", UnitsPerSec: 1000, HWClass: "apple_silicon_pro"}
	slowPrice := repriceFromSupplierEconomics(slow, 0.97, 0.15).PricePer1K
	fastPrice := repriceFromSupplierEconomics(fast, 0.97, 0.15).PricePer1K
	if fastPrice >= slowPrice {
		t.Fatalf("10x throughput should reprice to a materially lower price_per_1k: slow=%.8f fast=%.8f", slowPrice, fastPrice)
	}
}

// TestRepriceFromSupplierEconomicsUnknownHWClassFallsBackConservatively proves an
// hw_class this file has no wattage figure for still produces a real, positive
// price using the conservative apple_silicon_pro-equivalent default (30W) rather
// than a zero-electricity-cost (unrealistically cheap) or a divide-by-zero.
func TestRepriceFromSupplierEconomicsUnknownHWClassFallsBackConservatively(t *testing.T) {
	b := measuredThroughput{ModelID: "m", JobType: "embed", UnitsPerSec: 500, HWClass: "some_future_chip"}
	got := repriceFromSupplierEconomics(b, 0.97, 0.15)
	if got.PricePer1K <= 0 {
		t.Fatalf("unknown hw_class should still yield a positive price, got %v", got.PricePer1K)
	}
	if !strings.Contains(got.Formula, "30W") {
		t.Fatalf("unknown hw_class should fall back to the 30W conservative default, formula: %s", got.Formula)
	}
}

// TestRepriceCatalogueFromSupplierEconomicsOmitsUnmeasuredModels proves the
// honesty rule directly: qwen2.5-7b-instruct-q4 (no real measured throughput,
// docs/GPU_CAPABILITY.md's own note that its GGUF ref 404s) must NEVER appear in
// the repricing output — inventing a number for it would violate the Creed's
// "never fake supply/measurement" rule this whole file exists to satisfy.
func TestRepriceCatalogueFromSupplierEconomicsOmitsUnmeasuredModels(t *testing.T) {
	results := RepriceCatalogueFromSupplierEconomics(0.97)
	if len(results) == 0 {
		t.Fatal("expected at least the two really-measured models")
	}
	seen := map[string]bool{}
	for _, r := range results {
		seen[r.ModelID] = true
		if r.PricePer1K <= 0 {
			t.Fatalf("repriced model %s has non-positive price %v", r.ModelID, r.PricePer1K)
		}
	}
	if !seen["all-minilm-l6-v2"] || !seen["llama-3.2-1b-instruct-q4"] {
		t.Fatalf("expected the two really-measured models in the result, got %v", seen)
	}
	if seen["qwen2.5-7b-instruct-q4"] {
		t.Fatal("qwen2.5-7b-instruct-q4 has no real measured throughput (GPU_CAPABILITY.md: GGUF ref 404s) and must never be repriced")
	}
	if seen["whisper-tiny"] || seen["whisper-base"] {
		t.Fatal("whisper models have no real measured throughput in this file's benchmark table and must never be repriced")
	}
}

// --- Project Detection & Quotation 6.5->7: cost-drift auto-tune clamping -------

// TestClampAutoTuneAdjustmentBoundsBothDirections proves the ±15% damping band:
// a wild drift ratio (either direction, or from thin/noisy data) never moves a
// price by more than the clamp in one pass.
func TestClampAutoTuneAdjustmentBoundsBothDirections(t *testing.T) {
	cases := []struct {
		name string
		in   float64
		want float64
	}{
		{"no drift", 1.0, 1.0},
		{"small underquote", 1.05, 1.05},
		{"small overquote", 0.92, 0.92},
		{"huge underquote clamps up", 5.0, 1.15},
		{"huge overquote clamps down", 0.1, 0.85},
		{"exactly at the upper clamp", 1.15, 1.15},
		{"exactly at the lower clamp", 0.85, 0.85},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := clampAutoTuneAdjustment(c.in)
			if diff := got - c.want; diff > 1e-9 || diff < -1e-9 {
				t.Fatalf("clampAutoTuneAdjustment(%v) = %v, want %v", c.in, got, c.want)
			}
		})
	}
}
