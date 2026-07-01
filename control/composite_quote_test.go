package main

import "testing"

// Item 4: composeQuotes aggregates per-stage quotes honestly: total cost is the SUM, the
// ETA band spans best-case parallel (max p50) to sequential worst case (sum of worst
// cases), confidence is the WORST stage, and risk is the WORST across stages.
func TestComposeQuotes(t *testing.T) {
	stages := []Quote{
		{
			Cost:       QuoteCost{MinUSD: 1, ExpectedUSD: 2, MaxUSD: 3, VerificationOverheadUSD: 0.1, PlatformTakeUSD: 0.05},
			Time:       QuoteTime{P50Secs: 30, P90Secs: 60, WorstCaseSecs: 120},
			Confidence: QuoteConfidence{Score: 0.9, Reasons: []string{"sampled tokens"}},
			Execution:  QuoteExecution{OOMRisk: "low", ColdStartRisk: "medium"},
			Warnings:   []string{"w1"},
		},
		{
			Cost:       QuoteCost{MinUSD: 2, ExpectedUSD: 4, MaxUSD: 6, VerificationOverheadUSD: 0.2, PlatformTakeUSD: 0.1},
			Time:       QuoteTime{P50Secs: 50, P90Secs: 80, WorstCaseSecs: 200},
			Confidence: QuoteConfidence{Score: 0.7, Reasons: []string{"sampled tokens", "cold supply"}},
			Execution:  QuoteExecution{OOMRisk: "high", ColdStartRisk: "low"},
			Warnings:   []string{"w2"},
		},
	}
	c := composeQuotes(stages)
	if c.TotalCost.MinUSD != 3 || c.TotalCost.ExpectedUSD != 6 || c.TotalCost.MaxUSD != 9 {
		t.Fatalf("total cost must be the SUM; got %+v", c.TotalCost)
	}
	if c.TimeBand.P50Secs != 50 {
		t.Fatalf("band p50 must be the slowest stage p50 (50); got %d", c.TimeBand.P50Secs)
	}
	if c.TimeBand.WorstCaseSecs != 320 {
		t.Fatalf("band worst_case must be the SUM (320); got %d", c.TimeBand.WorstCaseSecs)
	}
	if c.Confidence.Score != 0.7 {
		t.Fatalf("confidence must be the worst stage (0.7); got %v", c.Confidence.Score)
	}
	if len(c.Confidence.Reasons) != 2 {
		t.Fatalf("reasons must be the deduped union (2); got %v", c.Confidence.Reasons)
	}
	if c.OOMRisk != "high" || c.ColdStartRisk != "medium" {
		t.Fatalf("risk must be the worst across stages; got oom=%s cold=%s", c.OOMRisk, c.ColdStartRisk)
	}
	if len(c.Warnings) != 2 {
		t.Fatalf("warnings must aggregate; got %v", c.Warnings)
	}
	if composeQuotes(nil).Confidence.Score != 0 {
		t.Fatal("an empty composite has zero confidence")
	}
}
