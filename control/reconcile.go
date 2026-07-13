package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"sort"
	"time"

	"github.com/google/uuid"
)

// reconcile.go — the ledger↔Stripe reconciliation loop (ACCRETION Wave 4:
// "Ledger↔Stripe reconciliation job + payout retry/alerting"). It is a read-only
// audit: it compares every durable cash_moved payout operation to what Stripe says
// it actually TRANSFERRED to that supplier's connected account, including cash
// whose ledger row later became reversal_required. It also flags every unresolved
// provider outcome and released row without cash proof. It NEVER moves money,
// marks a row, or "fixes" a discrepancy — surfacing drift honestly is the whole
// job. Wired into the same Workers.Run ticker loop as the payout/stale/webhook
// sweeps.

// reconcileInterval is how often the ledger↔Stripe audit runs. Deliberately slow —
// this is an audit, not a hot path, and each cycle makes one Stripe list call per
// paid supplier, so a tight cadence would burn rate limit for no benefit.
const reconcileInterval = 15 * time.Minute

// reconcileEpsilonUSD is the tolerance below which a ledger-vs-Stripe delta is
// treated as rounding noise, not drift. Transfers settle in integer cents, so
// sub-cent differences are expected float artefacts; anything at or above a cent is
// a real discrepancy worth flagging.
const reconcileEpsilonUSD = 0.01

// reconcileLedger audits durable supplier payout operations against actual Stripe
// transfers and logs any drift. It is honest about its own limits: with no Stripe key it
// cannot see real transfers, so it logs that it is skipping rather than inventing a
// "reconciled" result (the stub/manual-export rails never produce Stripe transfers,
// so there is nothing to reconcile against). A per-supplier lookup error is logged
// and skipped so one bad supplier never aborts the whole audit.
func (wk *Workers) reconcileLedger(ctx context.Context) error {
	if stripeKey() == "" {
		// No Stripe rail → no real transfers exist to reconcile against. Say so
		// plainly instead of reporting a clean reconciliation that proves nothing.
		log.Print("workers: reconcile skipped — Stripe not configured (no transfers to reconcile against)")
		return nil
	}

	// Per-supplier cash and anomaly totals come from the same operation-backed
	// rollup as /admin/payouts. Current ledger status never erases cash_moved.
	rollups, err := wk.store.ListPayoutsAdmin(ctx)
	if err != nil {
		return err
	}

	type supplierCashExpectation struct {
		cashSentUSD         float64
		liabilityUSD        float64
		carriedUSD          float64
		releasedWithoutCash int
		outcomeUnknown      int
	}
	expected := make(map[uuid.UUID]*supplierCashExpectation)
	for _, r := range rollups {
		if r.SupplierID == (uuid.UUID{}) {
			continue
		}
		e := expected[r.SupplierID]
		if e == nil {
			e = &supplierCashExpectation{}
			expected[r.SupplierID] = e
		}
		e.cashSentUSD += r.CashSentUSD
		e.liabilityUSD += r.AmountUSD
		e.carriedUSD += r.CarriedRemainderUSD
		e.releasedWithoutCash += r.ReleasedWithoutCashCount
		e.outcomeUnknown += r.OutcomeUnknownCount
	}
	suppliers := make([]uuid.UUID, 0, len(expected))
	for supplierID, e := range expected {
		if e.cashSentUSD > 0 || e.releasedWithoutCash > 0 || e.outcomeUnknown > 0 {
			suppliers = append(suppliers, supplierID)
		}
	}
	sort.Slice(suppliers, func(i, j int) bool { return suppliers[i].String() < suppliers[j].String() })

	var checked, drifted int
	for _, supplierID := range suppliers {
		e := expected[supplierID]
		acct, aerr := wk.store.SupplierStripeAcct(ctx, supplierID)
		if aerr != nil {
			log.Printf("workers: reconcile: supplier %s stripe account lookup: %v", supplierID, aerr)
			continue
		}
		if acct == "" {
			// Ledger says we released money to a supplier that has no connected
			// account: a real anomaly (a transfer could not have succeeded), so flag
			// it rather than silently skipping.
			log.Printf("workers: reconcile DRIFT: supplier %s shows $%.2f cash sent ($%.6f liability, $%.6f carried) but has no connected Stripe account",
				supplierID, e.cashSentUSD, e.liabilityUSD, e.carriedUSD)
			drifted++
			metrics.reconcileDrift.Add(1)
			continue
		}
		if e.releasedWithoutCash > 0 {
			log.Printf("workers: reconcile DRIFT: supplier %s (%s) has %d released liability row(s) without a cash-moved payout operation (rollup liability $%.6f)",
				supplierID, acct, e.releasedWithoutCash, e.liabilityUSD)
			drifted++
			metrics.reconcileDrift.Add(1)
		}
		if e.outcomeUnknown > 0 {
			log.Printf("workers: reconcile DRIFT: supplier %s (%s) has %d unresolved provider outcome(s); possible cash must be resolved by exact payout key",
				supplierID, acct, e.outcomeUnknown)
			drifted++
			metrics.reconcileDrift.Add(1)
		}
		if e.cashSentUSD <= 0 {
			continue
		}
		transferred, terr := stripeTransferredUSD(ctx, acct)
		if terr != nil {
			log.Printf("workers: reconcile: supplier %s (%s) stripe transfers: %v", supplierID, acct, terr)
			continue
		}
		checked++
		if delta := e.cashSentUSD - transferred; math.Abs(delta) >= reconcileEpsilonUSD {
			// Honest flag only. A positive delta = ledger says we paid more than
			// Stripe shows (under-transfer / missing transfer); negative = Stripe
			// transferred more than the ledger released (an out-of-band or duplicate
			// transfer). Either way the operator investigates — we never move money.
			log.Printf("workers: reconcile DRIFT: supplier %s (%s): ledger cash sent $%.2f vs stripe transferred $%.2f (delta $%.2f; liability $%.6f, carried $%.6f)",
				supplierID, acct, e.cashSentUSD, transferred, delta, e.liabilityUSD, e.carriedUSD)
			drifted++
			metrics.reconcileDrift.Add(1)
		}
	}
	log.Printf("workers: reconcile complete — %d supplier(s) checked, %d with drift", checked, drifted)
	return nil
}

// stripeTransferredUSD sums every Stripe transfer to a connected account, in USD.
// It pages the Transfers list (destination filter, 100/page) via the existing
// stripeGet helper until has_more is false, so a supplier with many payouts is
// fully accounted for rather than truncated at one page. Amounts are integer cents
// on the wire; we convert to dollars. Any malformed page is a real error (never a
// silently-dropped page that would fake a low total).
func stripeTransferredUSD(ctx context.Context, acct string) (float64, error) {
	var totalCents int64
	startingAfter := ""
	for page := 0; page < reconcileMaxPages; page++ {
		path := fmt.Sprintf("transfers?destination=%s&limit=100", acct)
		if startingAfter != "" {
			path += "&starting_after=" + startingAfter
		}
		out, err := stripeGet(ctx, path)
		if err != nil {
			return 0, err
		}
		data, ok := out["data"].([]any)
		if !ok {
			return 0, fmt.Errorf("stripe transfers: missing data array")
		}
		var lastID string
		for _, item := range data {
			t, ok := item.(map[string]any)
			if !ok {
				return 0, fmt.Errorf("stripe transfers: malformed entry")
			}
			amt, ok := t["amount"].(float64) // encoding/json numbers decode as float64
			if !ok {
				return 0, fmt.Errorf("stripe transfers: entry missing numeric amount")
			}
			totalCents += int64(math.Round(amt))
			if id, ok := t["id"].(string); ok {
				lastID = id
			}
		}
		hasMore, _ := out["has_more"].(bool)
		if !hasMore || lastID == "" {
			break
		}
		startingAfter = lastID // cursor onto the next page
	}
	return float64(totalCents) / 100.0, nil
}

// reconcileMaxPages bounds the Transfers pagination so a runaway has_more (or an
// enormous payout history) cannot make one audit cycle loop unboundedly. 100 pages
// × 100 transfers = 10k transfers per supplier per cycle, far past any real volume;
// hitting it would itself be a signal worth the operator's attention.
const reconcileMaxPages = 100
