package main

// summary.go — the birds-eye roll-up behind GET /admin/summary, the control room's
// top band. One authenticated fetch answers "how is the exchange doing": platform
// take-home vs the flow-through owed between users (CX is the conduit — buyers owe
// suppliers; the platform's own money is the take), fleet state, runs by status,
// supplier liabilities by payout status, and open fraud flags. Every figure is an
// aggregate over real rows — nothing here is sampled, cached, or estimated.

import (
	"context"
	"log"
	"net/http"
	"sync"
	"time"
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
	// CollectedUSD is what was actually PULLED from buyers' cards (jobs with
	// charge_status='charged') — real external money-in, not ledger bookkeeping.
	CollectedUSD float64 `json:"collected_usd"`
	// UncollectedUSD is settled terminal work not yet collected: terminal jobs
	// with a real actual_usd whose charge_status is anything but 'charged'
	// (deferred, failed, no card, never attempted). Honest debt, never hidden.
	UncollectedUSD float64 `json:"uncollected_usd"`
	// StripeFeesUSD is the sum of REAL Stripe processing fees (stripe_fee rows —
	// fetched from Stripe per PaymentIntent, never estimated).
	StripeFeesUSD float64 `json:"stripe_fees_usd"`
	// TakeNetUSD is the platform take after real Stripe fees.
	TakeNetUSD float64 `json:"take_net_usd"`
	// TransferredUSD is supplier credit actually SENT onward (payout_status
	// 'released' — each such row carries a real rail reference by constraint).
	TransferredUSD float64 `json:"transferred_usd"`
}

// AdminAccessible answers "of the money physically in the operator's Stripe
// balance, what is actually mine to touch?" — every figure is scoped to jobs
// whose charge was CONFIRMED collected (charge_status='charged'), so it never
// counts take on money that was only ever ledger bookkeeping.
type AdminAccessible struct {
	// TakeCollectedUSD is platform take on collected jobs only.
	TakeCollectedUSD float64 `json:"take_collected_usd"`
	// FeesUSD mirrors money.stripe_fees_usd (real fees, never estimated).
	FeesUSD float64 `json:"fees_usd"`
	// YoursNetUSD is TakeCollectedUSD minus FeesUSD: the operator's own money.
	YoursNetUSD float64 `json:"yours_net_usd"`
	// TheirsPendingTransferUSD is supplier credit (held|pending) on collected
	// jobs: money physically in the operator's balance but owed onward.
	TheirsPendingTransferUSD float64 `json:"theirs_pending_transfer_usd"`
	// SpendableEstimateUSD is stripe.available_usd minus money.flow_owed_usd,
	// floored at 0 — nil when the live Stripe balance is unavailable (never a
	// fabricated number).
	SpendableEstimateUSD *float64 `json:"spendable_estimate_usd"`
}

// AdminStripe is the live Stripe balance (USD buckets of available[] and
// pending[]), cached for 60s so the console's 15s poll never hammers Stripe.
type AdminStripe struct {
	AvailableUSD float64 `json:"available_usd"`
	PendingUSD   float64 `json:"pending_usd"`
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

// AdminSummary is the whole birds-eye view. Accessible is omitted only when its
// query fails (logged); Stripe is nil whenever no live balance is available —
// no key, a fetch error — because a fabricated balance is worse than none.
type AdminSummary struct {
	Money           AdminMoney                `json:"money"`
	Accessible      *AdminAccessible          `json:"accessible,omitempty"`
	Stripe          *AdminStripe              `json:"stripe,omitempty"`
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
		                     AND payout_status IN ('held','pending') THEN amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'stripe_fee'      THEN -amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN kind = 'supplier_credit'
		                     AND payout_status = 'released' THEN amount_usd END),0)::float8
		 FROM ledger_entries`,
	).Scan(&out.Money.ChargedUSD, &out.Money.SupplierCreditUSD, &out.Money.PlatformTakeUSD,
		&out.Money.RefundedUSD, &out.Money.ClawedBackUSD, &out.Money.FlowOwedUSD,
		&out.Money.StripeFeesUSD, &out.Money.TransferredUSD); err != nil {
		return out, err
	}
	out.Money.TakeNetUSD = out.Money.PlatformTakeUSD - out.Money.StripeFeesUSD

	// Collection truth: what was actually pulled from buyers' cards vs the
	// settled terminal work still uncollected (both over jobs.actual_usd, the
	// real settled cost, not the estimate).
	if err := s.pool.QueryRow(ctx,
		`SELECT
		   COALESCE(SUM(CASE WHEN charge_status = 'charged' THEN actual_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN status IN ('complete','failed','cancelled')
		                     AND COALESCE(actual_usd,0) > 0
		                     AND charge_status <> 'charged' THEN actual_usd END),0)::float8
		 FROM jobs`,
	).Scan(&out.Money.CollectedUSD, &out.Money.UncollectedUSD); err != nil {
		return out, err
	}

	// Accessible: the operator's-money view, scoped to jobs whose charge was
	// CONFIRMED collected (task_id → tasks.job_id join). A query failure here
	// omits the section (logged) rather than failing the whole summary or
	// shipping a partial fabrication.
	var acc AdminAccessible
	if err := s.pool.QueryRow(ctx,
		`SELECT
		   COALESCE(SUM(CASE WHEN le.kind = 'platform_take' THEN le.amount_usd END),0)::float8,
		   COALESCE(SUM(CASE WHEN le.kind = 'supplier_credit'
		                     AND le.payout_status IN ('held','pending') THEN le.amount_usd END),0)::float8
		 FROM ledger_entries le
		 JOIN tasks t ON t.id = le.task_id
		 JOIN jobs  j ON j.id = t.job_id
		 WHERE j.charge_status = 'charged'`,
	).Scan(&acc.TakeCollectedUSD, &acc.TheirsPendingTransferUSD); err != nil {
		log.Printf("admin summary: accessible-money query failed (section omitted, never fabricated): %v", err)
	} else {
		acc.FeesUSD = out.Money.StripeFeesUSD
		acc.YoursNetUSD = acc.TakeCollectedUSD - acc.FeesUSD
		out.Accessible = &acc // SpendableEstimateUSD is filled by the handler once a live balance exists
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

// --- live Stripe balance (cached) ---

// The console polls /admin/summary every 15s; the Stripe balance changes far
// slower than that, so one fetch is cached for 60s and shared. A fetch failure
// leaves the stripe section nil for that cycle (logged) — a stale-but-real
// number inside the TTL is fine, a fabricated one never is.
const stripeBalanceTTL = 60 * time.Second

var (
	stripeBalanceMu       sync.Mutex
	stripeBalance         *AdminStripe // nil is a REAL cached answer (last fetch failed)
	stripeBalanceAt       time.Time    // last fetch ATTEMPT (success or failure)
	stripeBalanceFetching bool         // single-flight: one fetch at a time, others use the cache
)

// cachedStripeBalance returns the live Stripe balance (USD available/pending)
// via a 60s in-memory cache. nil when no key is set, the fetch failed, or a
// fetch is still in flight with no prior success — never a fabricated balance.
//
// The mutex guards only the cache fields, NEVER the network call: holding it
// across a slow Stripe fetch would serialize every /admin/summary request behind
// a 20s timeout for the whole outage. Failures are cached too (negative cache):
// one attempt per TTL, everyone else gets the honest nil instantly. The fetch
// uses its own bounded context, not the request's — a poller giving up must not
// cancel the fetch every other console poll inherits.
func cachedStripeBalance(ctx context.Context) *AdminStripe {
	if stripeKey() == "" {
		return nil
	}
	stripeBalanceMu.Lock()
	if time.Since(stripeBalanceAt) < stripeBalanceTTL || stripeBalanceFetching {
		var v *AdminStripe
		if stripeBalance != nil {
			c := *stripeBalance
			v = &c
		}
		stripeBalanceMu.Unlock()
		return v
	}
	stripeBalanceFetching = true
	stripeBalanceMu.Unlock()

	fctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 8*time.Second)
	defer cancel()
	out, err := stripeGet(fctx, "balance")

	stripeBalanceMu.Lock()
	defer stripeBalanceMu.Unlock()
	stripeBalanceFetching = false
	stripeBalanceAt = time.Now() // attempt time: success AND failure both start a TTL
	if err != nil {
		stripeBalance = nil
		log.Printf("admin summary: stripe balance fetch failed (stripe section omitted until the next attempt in %s, never fabricated): %v", stripeBalanceTTL, err)
		return nil
	}
	bal := AdminStripe{
		AvailableUSD: sumStripeUSDBuckets(out["available"]),
		PendingUSD:   sumStripeUSDBuckets(out["pending"]),
	}
	stripeBalance = &bal
	v := bal
	return &v
}

// sumStripeUSDBuckets sums the usd-currency buckets of a Stripe balance array
// (available[]/pending[]; amounts are integer cents). Non-usd buckets are
// deliberately excluded — this console reports USD.
func sumStripeUSDBuckets(v any) float64 {
	arr, _ := v.([]any)
	var cents float64
	for _, e := range arr {
		m, _ := e.(map[string]any)
		if cur, _ := m["currency"].(string); cur != "usd" {
			continue
		}
		amt, _ := m["amount"].(float64)
		cents += amt
	}
	return cents / 100
}

// handleAdminSummary serves GET /admin/summary (authAdmin).
func (s *Server) handleAdminSummary(w http.ResponseWriter, r *http.Request) {
	sum, err := s.store.AdminSummaryData(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "summary: "+err.Error())
		return
	}
	// Live Stripe balance (cached 60s) + the spendable estimate it unlocks:
	// available minus the flow owed between users, floored at 0. Both stay
	// absent when no live balance exists — absence over fabrication.
	if st := cachedStripeBalance(r.Context()); st != nil {
		sum.Stripe = st
		if sum.Accessible != nil {
			spendable := st.AvailableUSD - sum.Money.FlowOwedUSD
			if spendable < 0 {
				spendable = 0
			}
			sum.Accessible.SpendableEstimateUSD = &spendable
		}
	}
	writeJSON(w, http.StatusOK, sum)
}
