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

// --- Quote-to-settlement visibility (not execution-cost telemetry) -------------
//
// jobs.actual_usd sounds like observed execution cost, but its source is the
// buyer-charge ledger. Each completed task is charged jobs.estimated_usd/task_count
// (scheduleTaskPayout in api.go), and SetJobActualUSD then sums those quote-derived
// buyer_charge entries. It is therefore useful settlement/charge-realization data,
// but it is circular evidence for catalogue pricing: changing the catalogue changes
// the estimate, which changes actual_usd, which must not be presented as independent
// proof that the catalogue price was economically correct.

// actualUSDBasisQuoteDerivedSettlement is emitted verbatim on every admin row so
// consumers cannot mistake AvgActualUSD for measured execution cost.
const actualUSDBasisQuoteDerivedSettlement = "quote_derived_per_task_buyer_charge_settlement"

// PriceTuningBlockReason is a stable, machine-readable reason an admin surface can
// use without string-matching an error message.
type PriceTuningBlockReason string

const priceTuningBlockedNoIndependentTelemetry PriceTuningBlockReason = "independent_execution_cost_telemetry_unavailable"

const requiredPriceTuningTelemetry = "independent per-task execution cost from measured runtime, energy, hardware amortization, supplier compensation, and platform/rail costs"

// PriceTuningUnavailableError is returned instead of silently treating settlement
// arithmetic as observed economics. It is intentionally structured so the HTTP
// layer can later map it to a stable non-500 response without parsing Error().
type PriceTuningUnavailableError struct {
	Reason            PriceTuningBlockReason `json:"reason"`
	ActualUSDBasis    string                 `json:"actual_usd_basis"`
	RequiredTelemetry string                 `json:"required_telemetry"`
}

func (e *PriceTuningUnavailableError) Error() string {
	return fmt.Sprintf(
		"price auto-tuning refused (%s): jobs.actual_usd basis=%q is settlement/charge data, not independent execution cost; required telemetry: %s",
		e.Reason, e.ActualUSDBasis, e.RequiredTelemetry,
	)
}

func newPriceTuningUnavailableError() *PriceTuningUnavailableError {
	return &PriceTuningUnavailableError{
		Reason:            priceTuningBlockedNoIndependentTelemetry,
		ActualUSDBasis:    actualUSDBasisQuoteDerivedSettlement,
		RequiredTelemetry: requiredPriceTuningTelemetry,
	}
}

// CostDriftRow retains its historical name for API compatibility, but represents
// quote-to-settlement charge realization, not execution-cost drift. Only
// quote-bound terminal jobs are counted because actual_usd is not settled earlier.
type CostDriftRow struct {
	JobType           string                 `json:"job_type"`
	ModelRef          string                 `json:"model_ref"`
	Samples           int                    `json:"samples"`             // quote-bound, terminal jobs behind this rollup
	AvgQuotedUSD      float64                `json:"avg_quoted_usd"`      // mean quotes.cost_expected_usd
	AvgActualUSD      float64                `json:"avg_actual_usd"`      // mean quote-derived jobs.actual_usd settlement
	DriftRatio        float64                `json:"drift_ratio"`         // settled charge / quoted charge; not a cost-overrun ratio
	DriftPct          float64                `json:"drift_pct"`           // (drift_ratio - 1) * 100, signed charge-realization difference
	ActualUSDBasis    string                 `json:"actual_usd_basis"`    // explicitly names the source semantics of AvgActualUSD
	UsingForTuning    bool                   `json:"using_for_tuning"`    // always false until independent economic telemetry exists
	TuningBlockReason PriceTuningBlockReason `json:"tuning_block_reason"` // machine-readable fail-closed reason
}

// finalizeCostDriftRow computes the display ratio and stamps the semantics and
// fail-closed tuning decision. Kept pure so the economic boundary is unit-testable
// without Postgres.
func finalizeCostDriftRow(d CostDriftRow) CostDriftRow {
	if d.AvgQuotedUSD > 0 {
		d.DriftRatio = d.AvgActualUSD / d.AvgQuotedUSD
		d.DriftPct = (d.DriftRatio - 1) * 100
	}
	d.ActualUSDBasis = actualUSDBasisQuoteDerivedSettlement
	d.UsingForTuning = false
	d.TuningBlockReason = priceTuningBlockedNoIndependentTelemetry
	return d
}

// CostDriftRollup returns quote-to-settlement charge realization per (job_type,
// model_ref) for GET /admin/quotes. It joins the quote promise to the settled buyer
// charge, while every returned row explicitly says that the latter is quote-derived
// and ineligible for economic auto-tuning.
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
		out = append(out, finalizeCostDriftRow(d))
	}
	return out, rows.Err()
}

// AutoTunePrices refuses before reading or writing the database. The only current
// monetary rollup is circular settlement data, so no sample count or damping clamp
// can make it valid execution-cost evidence. When independent telemetry is landed,
// this function must be deliberately reimplemented against that source; until then
// the typed error is the honest contract and models remain untouched.
func (s *Store) AutoTunePrices(ctx context.Context) ([]PriceTuneResult, error) {
	return nil, newPriceTuningUnavailableError()
}

// PriceTuneResult is retained for API compatibility and for the future independent
// telemetry implementation. The current AutoTunePrices returns no such rows.
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
