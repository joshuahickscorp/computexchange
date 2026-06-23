package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"time"

	"github.com/google/uuid"
)

// reconcile.go — the ledger↔Stripe reconciliation loop (ACCRETION Wave 4:
// "Ledger↔Stripe reconciliation job + payout retry/alerting"). It is a read-only
// audit: it compares what our ledger says we have RELEASED to each supplier
// (supplier_credit rows the payout loop marked 'released' against a real transfer)
// to what Stripe says we have actually TRANSFERRED to that supplier's connected
// account, and LOGS any drift. It NEVER moves money, marks a row, or "fixes" a
// discrepancy — surfacing drift honestly is the whole job; the operator resolves it
// (BLACKHOLE: surface every failure, never auto-act on money). Wired into the same
// Workers.Run ticker loop as the payout/stale/webhook sweeps.

// reconcileInterval is how often the ledger↔Stripe audit runs. Deliberately slow —
// this is an audit, not a hot path, and each cycle makes one Stripe list call per
// paid supplier, so a tight cadence would burn rate limit for no benefit.
const reconcileInterval = 15 * time.Minute

// reconcileEpsilonUSD is the tolerance below which a ledger-vs-Stripe delta is
// treated as rounding noise, not drift. Transfers settle in integer cents, so
// sub-cent differences are expected float artefacts; anything at or above a cent is
// a real discrepancy worth flagging.
const reconcileEpsilonUSD = 0.01

// reconcileLedger audits released supplier credits against actual Stripe transfers
// and logs any drift. It is honest about its own limits: with no Stripe key it
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

	// Per-supplier released totals straight from the ledger audit rollup (the same
	// read /admin/payouts uses). Only 'released' supplier_credit rows count: those
	// are the credits the payout loop sent against a real transfer ref. 'held',
	// 'ready', 'pending', and 'clawed_back' are deliberately excluded — they were
	// never transferred, so Stripe should show nothing for them.
	rollups, err := wk.store.ListPayoutsAdmin(ctx)
	if err != nil {
		return err
	}

	var checked, drifted int
	for _, r := range rollups {
		if r.PayoutStatus != PayoutReleased || r.AmountUSD <= 0 {
			continue
		}
		if r.SupplierID == (uuid.UUID{}) {
			// A platform_take or orphaned row with no supplier cannot map to a
			// Connect destination; nothing to reconcile.
			continue
		}
		acct, aerr := wk.store.SupplierStripeAcct(ctx, r.SupplierID)
		if aerr != nil {
			log.Printf("workers: reconcile: supplier %s stripe account lookup: %v", r.SupplierID, aerr)
			continue
		}
		if acct == "" {
			// Ledger says we released money to a supplier that has no connected
			// account: a real anomaly (a transfer could not have succeeded), so flag
			// it rather than silently skipping.
			log.Printf("workers: reconcile DRIFT: supplier %s shows $%.2f released but has no connected Stripe account", r.SupplierID, r.AmountUSD)
			drifted++
			continue
		}
		transferred, terr := stripeTransferredUSD(ctx, acct)
		if terr != nil {
			log.Printf("workers: reconcile: supplier %s (%s) stripe transfers: %v", r.SupplierID, acct, terr)
			continue
		}
		checked++
		if delta := r.AmountUSD - transferred; math.Abs(delta) >= reconcileEpsilonUSD {
			// Honest flag only. A positive delta = ledger says we paid more than
			// Stripe shows (under-transfer / missing transfer); negative = Stripe
			// transferred more than the ledger released (an out-of-band or duplicate
			// transfer). Either way the operator investigates — we never move money.
			log.Printf("workers: reconcile DRIFT: supplier %s (%s): ledger released $%.2f vs stripe transferred $%.2f (delta $%.2f)",
				r.SupplierID, acct, r.AmountUSD, transferred, delta)
			drifted++
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
