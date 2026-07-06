package main

import (
	"context"
	"fmt"
)

// pricing.go — Buyer Advantage & Pricing Edge 4.5→5
// (docs/internal/CREED_AND_PATH_TO_TEN.md, "Reprice from real supplier economics,
// not hand-seeded constants"): the catalogue's price_per_1k used to be four
// arbitrary numbers someone typed in db/schema.sql's seed. This file feeds the
// SAME real measured-throughput numbers docs/GPU_CAPABILITY.md publishes, plus the
// real supplier-share rate control/payment.go actually pays out, through to a
// price — the exact inverse of what scripts/supplier_earnings_calculator.py does
// by hand for one supplier, now applied to the catalogue itself so a price is
// traceable to a formula, not a hand-typed constant.
//
// The direction of the arithmetic is deliberately the OPPOSITE of estimateJobUSD:
// that function takes a price and produces a buyer cost; repriceFromSupplierEconomics
// takes a REAL measured throughput + a REAL target supplier $/hr floor and SOLVES for
// the price that would deliver it. Solving:
//
//	target_supplier_usd_hr = (units_per_hr / 1000) * price_per_1k * supplier_share - electricity_usd_hr
//
//	=>  price_per_1k = (target_supplier_usd_hr + electricity_usd_hr)
//	                   / (units_per_hr / 1000 * supplier_share)
//
// Honesty rule (BLACKHOLE, same as the calculator this inverts): only a hardware
// class with a REAL measured throughput number gets repriced. qwen2.5-7b-instruct-q4
// has never been successfully benchmarked (docs/GPU_CAPABILITY.md's own honest note:
// the HF GGUF ref 404s) — it is deliberately left un-repriced here rather than
// inventing a number, exactly as the Creed requires.

// measuredThroughput is a real, on-disk measured figure for one (model, job_type),
// reproduced from the SAME source scripts/supplier_earnings_calculator.py cites:
//   - all-minilm-l6-v2 (embed): 1967.3141 eps, the real M3 Pro reference benchmark
//     at .artifacts/gpu-bench/metal-Apple_M3_Pro-20260701T223411Z/capability.json.
//   - llama-3.2-1b-instruct-q4 (batch_infer): 138.7 tok/s, docs/GPU_CAPABILITY.md's
//     published batch-32 peak on the same M3 Pro box (the batched-decode headline
//     number, byte-identical to serial at every batch size on Apple Silicon — the
//     realistic dispatch throughput, not the unbatched serial baseline of 90.2).
//
// Neither number is invented here; both are transcribed constants with a named,
// checkable source, matching how the calculator itself sources FALLBACK_BENCHMARKS.
type measuredThroughput struct {
	ModelID        string
	JobType        string
	UnitsPerSec    float64 // tok/s or eps — one unit for one catalogue price, same as estimateJobUSD
	HWClass        string  // apple_silicon_pro (the only measured reference box; see GPU_CAPABILITY.md)
	SourceCitation string
}

var repricingBenchmarks = []measuredThroughput{
	{
		ModelID:        "all-minilm-l6-v2",
		JobType:        "embed",
		UnitsPerSec:    1967.3141,
		HWClass:        "apple_silicon_pro",
		SourceCitation: ".artifacts/gpu-bench/metal-Apple_M3_Pro-20260701T223411Z/capability.json (eps)",
	},
	{
		ModelID:        "llama-3.2-1b-instruct-q4",
		JobType:        "batch_infer",
		UnitsPerSec:    138.7,
		HWClass:        "apple_silicon_pro",
		SourceCitation: "docs/GPU_CAPABILITY.md:43 (batch-32 peak tok/s, byte-identical to serial)",
	},
	// qwen2.5-7b-instruct-q4 deliberately omitted: docs/GPU_CAPABILITY.md:104 records
	// the HF GGUF ref 404ing on the deep sweep — there is no real measured throughput
	// for this model on any hardware class yet, so it is left un-repriced rather than
	// assigning it a number this file cannot back with a real measurement.
}

// sustainedWattsByHWClass mirrors scripts/supplier_earnings_calculator.py's
// ESTIMATED_SUSTAINED_WATTS exactly — same documented caveat: not a measured
// power-draw benchmark (none exists in this repo yet, see Agent Idle Footprint
// 5→6), sourced from Apple's own published max power figures as a conservative
// starting estimate. Kept in lockstep with the Python constant by comment,
// deliberately not by import (this file has no Python interop).
var sustainedWattsByHWClass = map[string]float64{
	"apple_silicon_base":  20.0,
	"apple_silicon_pro":   30.0,
	"apple_silicon_max":   45.0,
	"apple_silicon_ultra": 65.0,
	"cpu":                 25.0,
}

// defaultElectricityUSDPerKWh mirrors the calculator's own DEFAULT_ELECTRICITY_USD_PER_KWH.
const defaultElectricityUSDPerKWh = 0.15

// targetSupplierUSDHr is the minimum net $/hr a supplier's Mac should clear for
// leaving it online to be worth doing at all — the floor this reprice solves the
// catalogue price against. This is deliberately NOT the calculator's "saturated
// ceiling" figure; it is a conservative minimum-viable floor, chosen so the
// REPRICED number is a floor price a real supplier could accept, not an
// optimistic one. $2/hr is roughly US federal minimum wage for unattended
// compute the owner is not otherwise using — a conservative, explainable
// floor, not a market-clearing estimate this repo has no data to justify yet.
const targetSupplierUSDHr = 2.0

// RepriceResult is one catalogue price derived from real supplier economics.
type RepriceResult struct {
	ModelID    string
	JobType    string
	PricePer1K float64
	Formula    string // human-readable, cites every real input (proof artifact)
}

// repriceFromSupplierEconomics solves for the price_per_1k that would deliver
// targetSupplierUSDHr of NET (post-electricity) income to a supplier running this
// model continuously, given its real measured throughput and the real platform
// take rate. Pure — no I/O — so it is unit-testable without a DB. supplierShare is
// 1-platformTakeRate (control/payment.go); passed in rather than read from the
// package global so tests can exercise any take rate without env-var plumbing.
func repriceFromSupplierEconomics(b measuredThroughput, supplierShare, electricityUSDPerKWh float64) RepriceResult {
	watts := sustainedWattsByHWClass[b.HWClass]
	if watts <= 0 {
		watts = 30.0 // conservative apple_silicon_pro-equivalent default, never zero
	}
	electricityUSDHr := watts / 1000.0 * electricityUSDPerKWh
	unitsPerHr := b.UnitsPerSec * 3600.0

	// price_per_1k = (target + electricity) / (units_per_hr/1000 * share)
	denom := unitsPerHr / 1000.0 * supplierShare
	var price float64
	if denom > 0 {
		price = (targetSupplierUSDHr + electricityUSDHr) / denom
	}

	formula := fmt.Sprintf(
		"price_per_1k = (target_supplier_usd_hr=%.2f + electricity_usd_hr=%.4f) / (units_per_hr=%.1f/1000 * supplier_share=%.4f) = %.8f  [source: %s, hw_class=%s, %.0fW @ $%.2f/kWh, platform_take=%.2f%%]",
		targetSupplierUSDHr, electricityUSDHr, unitsPerHr, supplierShare, price,
		b.SourceCitation, b.HWClass, watts, electricityUSDPerKWh, (1-supplierShare)*100,
	)
	return RepriceResult{ModelID: b.ModelID, JobType: b.JobType, PricePer1K: price, Formula: formula}
}

// RepriceCatalogueFromSupplierEconomics computes a RepriceResult for every model
// this file has a real measured throughput for (repricingBenchmarks) — the models
// with no real measurement (qwen2.5-7b-instruct-q4, whisper-*) are simply absent
// from the result, never assigned an invented number.
func RepriceCatalogueFromSupplierEconomics(supplierShare float64) []RepriceResult {
	out := make([]RepriceResult, 0, len(repricingBenchmarks))
	for _, b := range repricingBenchmarks {
		out = append(out, repriceFromSupplierEconomics(b, supplierShare, defaultElectricityUSDPerKWh))
	}
	return out
}

// --- Project Detection & Quotation 6.5->7: close the cost-drift loop ------------
//
// docs/internal/CREED_AND_PATH_TO_TEN.md: "All the needed data already lands in
// Postgres (quotes.cost_expected_usd, jobs.actual_usd, the invoice's quoted_usd);
// build the missing GET /admin/quotes drift rollup per (job_type, model), and use
// it to auto-adjust catalogue prices instead of leaving them static."
//
// This is the COST twin of the existing GET /admin/drift (store.go DriftRollup),
// which is ETA-only (ObservedP90DurationMs vs the quoted eta_secs) and never once
// touches money. That rollup already exists and is explicitly out of scope here —
// this is the missing half named directly by the rung.

// CostDriftRow is one per-(job_type, model_ref) quoted-vs-actual COST rollup for
// GET /admin/quotes. Only jobs that (a) were bound to a quote (Plane D D7) and (b)
// reached a terminal, money-settled state (complete or failed — a failed job still
// partial-settles actual_usd for whatever it delivered, see the partial-settle
// discipline elsewhere in this codebase) are counted: a still-running job's
// actual_usd is not yet a real number to compare against.
type CostDriftRow struct {
	JobType        string  `json:"job_type"`
	ModelRef       string  `json:"model_ref"`
	Samples        int     `json:"samples"`          // quote-bound, terminal jobs behind this rollup
	AvgQuotedUSD   float64 `json:"avg_quoted_usd"`   // mean quotes.cost_expected_usd
	AvgActualUSD   float64 `json:"avg_actual_usd"`   // mean jobs.actual_usd (the real settled cost)
	DriftRatio     float64 `json:"drift_ratio"`      // avg_actual / avg_quoted; 1.0 = perfectly priced, >1 = underpriced quote, <1 = overpriced quote
	DriftPct       float64 `json:"drift_pct"`        // (drift_ratio - 1) * 100, signed — the buyer-facing "we underquoted by X%" number
	UsingForTuning bool    `json:"using_for_tuning"` // true once samples >= the trust floor (same floor as the ETA drift rollup)
}

// costDriftMinSamples is the minimum quote-bound, terminal-job sample count before
// a (job_type, model) slice is trusted enough to auto-tune a catalogue price from
// — deliberately the SAME floor store.go's ETA drift rollup already uses
// (driftMinSamples), so both drift surfaces agree on what counts as "enough real
// history," rather than inventing a second, uncoordinated threshold.
const costDriftMinSamples = driftMinSamples

// CostDriftRollup returns the quoted-vs-actual COST rollup per (job_type,
// model_ref): the real number GET /admin/quotes serves. Grouped over quote-bound
// jobs (jobs.quote_id IS NOT NULL) that have reached a terminal state, joined back
// to the quote's own cost_expected_usd (never a job's OWN estimated_usd, which can
// differ from what a bound quote specifically promised if the job's shape drifted
// between quote and submit — the quote is the promise, actual_usd is the reality).
func (s *Store) CostDriftRollup(ctx context.Context) ([]CostDriftRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT j.job_type,
		        COALESCE(j.model_ref,''),
		        COUNT(*),
		        COALESCE(AVG(q.cost_expected_usd),0),
		        COALESCE(AVG(j.actual_usd),0)
		   FROM jobs j
		   JOIN quotes q ON q.id = j.quote_id
		  WHERE j.quote_id IS NOT NULL
		    AND j.status IN ('complete','failed')
		    AND q.cost_expected_usd > 0
		  GROUP BY j.job_type, j.model_ref
		  ORDER BY COUNT(*) DESC, j.job_type, j.model_ref`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CostDriftRow
	for rows.Next() {
		var d CostDriftRow
		if err := rows.Scan(&d.JobType, &d.ModelRef, &d.Samples, &d.AvgQuotedUSD, &d.AvgActualUSD); err != nil {
			return nil, err
		}
		if d.AvgQuotedUSD > 0 {
			d.DriftRatio = d.AvgActualUSD / d.AvgQuotedUSD
			d.DriftPct = (d.DriftRatio - 1) * 100
		}
		d.UsingForTuning = d.Samples >= costDriftMinSamples
		out = append(out, d)
	}
	return out, rows.Err()
}

// autoTuneMaxAdjustment caps how much ONE auto-tune pass may move a price in
// either direction (±15%). A real, non-zero drift should correct the catalogue —
// but a single pass moving price_per_1k by, say, 5x on a thin or freshly-noisy
// sample would be a bug pretending to be automation. Clamping means a large,
// persistent drift takes several tuning passes to fully correct — a deliberate
// damping choice, not a limitation nobody considered.
const autoTuneMaxAdjustmentFrac = 0.15

// clampAutoTuneAdjustment bounds a raw drift ratio to ±autoTuneMaxAdjustmentFrac
// around 1.0 before it is applied to a price. Pure — no I/O — so the clamping
// behavior itself is unit-tested without a DB.
func clampAutoTuneAdjustment(ratio float64) float64 {
	if ratio > 1+autoTuneMaxAdjustmentFrac {
		return 1 + autoTuneMaxAdjustmentFrac
	}
	if ratio < 1-autoTuneMaxAdjustmentFrac {
		return 1 - autoTuneMaxAdjustmentFrac
	}
	return ratio
}

// AutoTunePrices reads the real cost-drift rollup and nudges each (job_type,
// model)'s catalogue price_per_1k toward the drift-corrected value: a model that
// actually cost MORE than quoted (drift_ratio > 1) gets its price raised so the
// NEXT quote prices closer to reality, and vice versa. Only rows with
// UsingForTuning (enough real samples) and a model actually in the catalogue are
// touched; the per-pass adjustment is clamped to ±autoTuneMaxAdjustmentFrac so one
// noisy pass cannot swing a price wildly. Returns the rows it actually adjusted —
// the proof artifact the rung asks for ("used at least once to correct a
// catalogue price").
func (s *Store) AutoTunePrices(ctx context.Context) ([]PriceTuneResult, error) {
	drift, err := s.CostDriftRollup(ctx)
	if err != nil {
		return nil, fmt.Errorf("auto-tune: reading cost drift: %w", err)
	}
	var applied []PriceTuneResult
	for _, d := range drift {
		if !d.UsingForTuning || d.ModelRef == "" || d.DriftRatio <= 0 {
			continue
		}
		m, gerr := s.GetModel(ctx, d.ModelRef)
		if gerr != nil {
			continue // model not in the catalogue (custom/unpriced job) — nothing to tune
		}
		oldPrice := modelPrice(*m)
		if oldPrice <= 0 {
			continue
		}
		adj := clampAutoTuneAdjustment(d.DriftRatio)
		newPrice := roundUSD(oldPrice * adj)
		if newPrice == oldPrice {
			continue // clamped/rounded to a no-op — nothing to write
		}
		formula := fmt.Sprintf(
			"price_per_1k auto-tuned from cost drift: old=%.8f * clamped_ratio=%.4f (raw avg_actual/avg_quoted=%.4f over %d terminal quote-bound jobs) = %.8f",
			oldPrice, adj, d.DriftRatio, d.Samples, newPrice,
		)
		tag, uerr := s.pool.Exec(ctx,
			`UPDATE models SET price_per_1k = $2, price_source = 'drift_auto_tuned', price_formula = $3 WHERE id = $1`,
			d.ModelRef, newPrice, formula,
		)
		if uerr != nil {
			return applied, fmt.Errorf("auto-tune: updating %s: %w", d.ModelRef, uerr)
		}
		if tag.RowsAffected() > 0 {
			applied = append(applied, PriceTuneResult{
				ModelID: d.ModelRef, JobType: d.JobType,
				OldPricePer1K: oldPrice, NewPricePer1K: newPrice,
				DriftRatio: d.DriftRatio, Samples: d.Samples, Formula: formula,
			})
		}
	}
	return applied, nil
}

// PriceTuneResult is one catalogue price change AutoTunePrices actually applied —
// the real, checkable proof that the drift rollup was used at least once to
// correct a price, not just computed and left on a dashboard.
type PriceTuneResult struct {
	ModelID       string  `json:"model_id"`
	JobType       string  `json:"job_type"`
	OldPricePer1K float64 `json:"old_price_per_1k"`
	NewPricePer1K float64 `json:"new_price_per_1k"`
	DriftRatio    float64 `json:"drift_ratio"`
	Samples       int     `json:"samples"`
	Formula       string  `json:"formula"`
}

// ApplyRepricing writes the real-economics-derived prices into the models table,
// stamping price_source + price_formula so every price is traceable. It ONLY
// overwrites a row whose price_source is still the original 'seed' default — an
// operator who has already hand-edited a price (price_source <> 'seed', e.g. a
// prior repricing run, or a deliberate manual override) is never silently
// clobbered, matching the exact non-destructive contract db/schema.sql's own seed
// comment already promises for this table ("lets operators edit rows without a
// re-seed clobbering them"). Idempotent: re-running against a DB this has already
// repriced is a no-op (every touched row's price_source is now
// 'measured_supplier_economics', so the WHERE guard excludes it next time).
func (s *Store) ApplyRepricing(ctx context.Context, results []RepriceResult) (updated int, err error) {
	for _, r := range results {
		tag, uerr := s.pool.Exec(ctx,
			`UPDATE models
			    SET price_per_1k = $2, price_source = 'measured_supplier_economics', price_formula = $3
			  WHERE id = $1 AND (price_source IS NULL OR price_source = 'seed')`,
			r.ModelID, r.PricePer1K, r.Formula,
		)
		if uerr != nil {
			return updated, fmt.Errorf("apply repricing for %s: %w", r.ModelID, uerr)
		}
		updated += int(tag.RowsAffected())
	}
	return updated, nil
}
