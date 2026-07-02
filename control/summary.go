package main

// summary.go — the birds-eye roll-up behind GET /admin/summary, the control room's
// top band. One authenticated fetch answers "how is the exchange doing": platform
// take-home vs the flow-through owed between users (CX is the conduit — buyers owe
// suppliers; the platform's own money is the take), fleet state, runs by status,
// supplier liabilities by payout status, and open fraud flags. Every figure is an
// aggregate over real rows — nothing here is sampled, cached, or estimated.

import (
	"context"
	"net/http"
)

// AdminMoney is the ledger rolled up by kind. Signs are normalized to "positive =
// the thing named" (buyer_charge rows are stored negative; clawback rows negative).
type AdminMoney struct {
	// ChargedUSD is everything buyers have been charged (per-task, settled at commit).
	ChargedUSD float64 `json:"charged_usd"`
	// SupplierCreditUSD is everything credited to suppliers (their ~90% share).
	SupplierCreditUSD float64 `json:"supplier_credit_usd"`
	// PlatformTakeUSD is the take-home: the exact complement of the supplier share.
	PlatformTakeUSD float64 `json:"platform_take_usd"`
	// RefundedUSD is charges returned to buyers (failed jobs).
	RefundedUSD float64 `json:"refunded_usd"`
	// ClawedBackUSD is supplier credit reversed on confirmed-bad results.
	ClawedBackUSD float64 `json:"clawed_back_usd"`
	// FlowOwedUSD is supplier credit not yet released (held + pending): money in
	// stasis BETWEEN users. Not a platform liability — the users owe each other;
	// the exchange holds and routes it.
	FlowOwedUSD float64 `json:"flow_owed_usd"`
}

// AdminPayoutAgg is one payout_status bucket of supplier credit.
type AdminPayoutAgg struct {
	Count int     `json:"count"`
	USD   float64 `json:"usd"`
}

// AdminWorkerSummary is the fleet at a glance. Live uses the scheduler's own 60s
// liveness window so the console and the claim filter agree on "online".
type AdminWorkerSummary struct {
	Total     int            `json:"total"`
	Live      int            `json:"live"`
	Throttled int            `json:"throttled"` // live workers currently throttled
	ByClass   map[string]int `json:"by_class"`  // live workers per hw_class
}

// AdminSummary is the whole birds-eye view.
type AdminSummary struct {
	Money           AdminMoney                `json:"money"`
	Workers         AdminWorkerSummary        `json:"workers"`
	JobsByStatus    map[string]int            `json:"jobs_by_status"`
	PayoutsByStatus map[string]AdminPayoutAgg `json:"payouts_by_status"`
	FraudFlags      int                       `json:"fraud_flags"`
}

// AdminSummaryData aggregates the summary in a handful of read-only queries.
func (s *Store) AdminSummaryData(ctx context.Context) (AdminSummary, error) {
	var out AdminSummary
	out.Workers.ByClass = map[string]int{}
	out.JobsByStatus = map[string]int{}
	out.PayoutsByStatus = map[string]AdminPayoutAgg{}

	// Money: one pass over the ledger, sign-normalized per kind.
	if err := s.pool.QueryRow(ctx,
		`SELECT
		   COALESCE(SUM(CASE WHEN kind = 'buyer_charge'    THEN -amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'supplier_credit' THEN  amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'platform_take'   THEN  amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'refund'          THEN  amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'clawback'        THEN -amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'supplier_credit'
		                     AND payout_status IN ('held','pending') THEN amount_usd END),0)::float8
		 FROM ledger_entries`,
	).Scan(&out.Money.ChargedUSD, &out.Money.SupplierCreditUSD, &out.Money.PlatformTakeUSD,
		&out.Money.RefundedUSD, &out.Money.ClawedBackUSD, &out.Money.FlowOwedUSD); err != nil {
		return out, err
	}

	// Fleet: totals + the 60s liveness window the scheduler itself filters on.
	if err := s.pool.QueryRow(ctx,
		`SELECT count(*),
		        count(*) FILTER (WHERE last_seen_at > now() - interval '60 seconds'),
		        count(*) FILTER (WHERE throttled AND last_seen_at > now() - interval '60 seconds')
		 FROM workers`,
	).Scan(&out.Workers.Total, &out.Workers.Live, &out.Workers.Throttled); err != nil {
		return out, err
	}
	rows, err := s.pool.Query(ctx,
		`SELECT hw_class, count(*) FROM workers
		 WHERE last_seen_at > now() - interval '60 seconds'
		 GROUP BY hw_class ORDER BY hw_class`)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var class string
		var n int
		if err := rows.Scan(&class, &n); err != nil {
			rows.Close()
			return out, err
		}
		out.Workers.ByClass[class] = n
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return out, err
	}

	// Runs by status.
	rows, err = s.pool.Query(ctx, `SELECT status, count(*) FROM jobs GROUP BY status`)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var st string
		var n int
		if err := rows.Scan(&st, &n); err != nil {
			rows.Close()
			return out, err
		}
		out.JobsByStatus[st] = n
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return out, err
	}

	// Supplier credit by payout status (the liabilities panel, aggregated).
	rows, err = s.pool.Query(ctx,
		`SELECT payout_status, count(*), COALESCE(SUM(amount_usd),0)::float8
		 FROM ledger_entries WHERE kind = 'supplier_credit'
		 GROUP BY payout_status`)
	if err != nil {
		return out, err
	}
	for rows.Next() {
		var st string
		var agg AdminPayoutAgg
		if err := rows.Scan(&st, &agg.Count, &agg.USD); err != nil {
			rows.Close()
			return out, err
		}
		out.PayoutsByStatus[st] = agg
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return out, err
	}

	// Open fraud flags: reuse the exact criteria the fraud view lists.
	flags, err := s.ListFraudFlags(ctx)
	if err != nil {
		return out, err
	}
	out.FraudFlags = len(flags)
	return out, nil
}

// handleAdminSummary serves GET /admin/summary (authAdmin).
func (s *Server) handleAdminSummary(w http.ResponseWriter, r *http.Request) {
	sum, err := s.store.AdminSummaryData(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "summary: "+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, sum)
}
