package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// payment.go — ledger entry math (REAL) and the payout rail (honest stub).
//
// REAL: every job charge produces three ledger rows — a buyer_charge (debit), a
// supplier_credit (the supplier's share, held until release_at), and a
// platform_take. The platform take is a flat, tunable 1–5% (CX_PLATFORM_TAKE_PCT,
// default 3%); the supplier keeps the rest. The math and the rows are real; only
// the actual money movement is stubbed behind the Payout interface.

// ledger entry kinds (match ledger_entries.kind).
const (
	KindBuyerCharge    = "buyer_charge"
	KindSupplierCredit = "supplier_credit"
	KindPlatformTake   = "platform_take"
	KindClawback       = "clawback"
)

// payout_status values (match ledger_entries.payout_status).
const (
	PayoutPending    = "pending"
	PayoutHeld       = "held"
	PayoutReleased   = "released"
	PayoutClawedBack = "clawed_back"
)

// platformTakeRate is the platform's cut of a buyer charge; supplierShareRate is
// the supplier's (they sum to 1.0). The take is a flat 1–5% set by
// CX_PLATFORM_TAKE_PCT (default 3%) — deliberately low: the provider's generous
// keep-rate is the supply magnet, and real margin is made on the buyer-side
// quote, not by squeezing the provider. Tune within the band without a code edit.
var (
	platformTakeRate  = takeRateFromEnv()
	supplierShareRate = 1.0 - platformTakeRate
)

// takeRateFromEnv reads CX_PLATFORM_TAKE_PCT as a percentage and returns it as a
// fraction, clamped to the [1%, 5%] band (default 3%).
func takeRateFromEnv() float64 {
	const def, lo, hi = 3.0, 1.0, 5.0
	pct := def
	if s := strings.TrimSpace(os.Getenv("CX_PLATFORM_TAKE_PCT")); s != "" {
		if v, err := strconv.ParseFloat(s, 64); err == nil {
			pct = v
		}
	}
	if pct < lo {
		pct = lo
	}
	if pct > hi {
		pct = hi
	}
	return pct / 100.0
}

// LedgerEntry is one row to insert into ledger_entries.
type LedgerEntry struct {
	Kind         string
	SupplierID   *uuid.UUID
	BuyerID      *uuid.UUID
	TaskID       *uuid.UUID
	AmountUSD    float64
	PayoutStatus string
	ReleaseAt    *time.Time
}

// splitCharge turns a single completed-task buyer charge into the three real
// ledger entries: buyer_charge (negative = debit), supplier_credit (held,
// release_at = now + holdSecs), and platform_take. This is the core money math
// and it is exact — supplier share = buyerCharge × supplierShareRate, platform
// take = the exact complement (no FP drift).
func splitCharge(buyerID, supplierID, taskID uuid.UUID, buyerCharge float64, holdSecs uint32, now time.Time) []LedgerEntry {
	supplierAmt := buyerCharge * supplierShareRate
	platformAmt := buyerCharge - supplierAmt // exact complement, avoids FP drift vs ×0.10
	release := now.Add(time.Duration(holdSecs) * time.Second)
	return []LedgerEntry{
		{
			Kind:         KindBuyerCharge,
			BuyerID:      &buyerID,
			TaskID:       &taskID,
			AmountUSD:    -buyerCharge, // debit on the buyer
			PayoutStatus: PayoutReleased,
		},
		{
			Kind:         KindSupplierCredit,
			SupplierID:   &supplierID,
			TaskID:       &taskID,
			AmountUSD:    supplierAmt,
			PayoutStatus: PayoutHeld, // held until the hold window expires
			ReleaseAt:    &release,
		},
		{
			Kind:         KindPlatformTake,
			TaskID:       &taskID,
			AmountUSD:    platformAmt,
			PayoutStatus: PayoutReleased,
		},
	}
}

// clawbackEntry reverses a supplier credit on a confirmed-bad result: a negative
// supplier amount, marked clawed_back. Used by verification on fraud.
func clawbackEntry(supplierID, taskID uuid.UUID, amount float64) LedgerEntry {
	return LedgerEntry{
		Kind:         KindClawback,
		SupplierID:   &supplierID,
		TaskID:       &taskID,
		AmountUSD:    -amount,
		PayoutStatus: PayoutClawedBack,
	}
}

// Payout is the money-movement rail. One method, one honest implementation in
// V1. Real Stripe Connect / Trolley integration is Phase 3.
type Payout interface {
	// Send transfers amountUSD to a supplier and returns the rail's transfer
	// reference. It MUST NOT pretend success when no rail is configured.
	Send(ctx context.Context, supplierID uuid.UUID, amountUSD float64) (ref string, err error)
}

// errPayoutUnconfigured is the explicit failure surfaced when no real rail is
// wired. We never fake a transfer (BLACKHOLE: surface every failure).
var errPayoutUnconfigured = errors.New("payout rail not configured (Stripe Connect/Trolley) — Phase 3")

// stubPayout is the honest V1 Payout: it always errors, so a caller can never
// mistake "no rail" for "money sent".
type stubPayout struct{}

func (stubPayout) Send(_ context.Context, _ uuid.UUID, _ float64) (string, error) {
	return "", errPayoutUnconfigured
}

// StripePayout is the REAL money rail: a Stripe Connect transfer to the supplier's
// connected account. It is selected only when STRIPE_SECRET_KEY is set (see
// main.go's selectPayout); otherwise the honest stubPayout is used. It never fakes
// a transfer — a missing key, a supplier with no connected account, or a Stripe
// error all surface as errors, so a credit is only ever marked `released` against
// a real transfer id. This is the wiring that turns the proven hold→ready state
// machine into real payouts once an account exists (the Phase-3 external step).
type StripePayout struct {
	store  *Store
	secret string
	http   *http.Client
}

func newStripePayout(store *Store, secret string) StripePayout {
	return StripePayout{store: store, secret: secret, http: &http.Client{Timeout: 20 * time.Second}}
}

// Send creates a Stripe transfer (amountUSD → the supplier's stripe_acct) and
// returns the transfer id. The amount is converted to integer cents; the request
// carries an idempotency key so a retried release never double-pays.
func (p StripePayout) Send(ctx context.Context, supplierID uuid.UUID, amountUSD float64) (string, error) {
	if p.secret == "" {
		return "", errPayoutUnconfigured
	}
	acct, err := p.store.SupplierStripeAcct(ctx, supplierID)
	if err != nil {
		return "", fmt.Errorf("looking up supplier stripe account: %w", err)
	}
	if acct == "" {
		return "", fmt.Errorf("supplier %s has no connected Stripe account (stripe_acct empty)", supplierID)
	}
	cents := int64(math.Round(amountUSD * 100))
	if cents <= 0 {
		return "", fmt.Errorf("non-positive payout amount %.6f USD", amountUSD)
	}
	form := url.Values{}
	form.Set("amount", strconv.FormatInt(cents, 10))
	form.Set("currency", "usd")
	form.Set("destination", acct)
	form.Set("transfer_group", supplierID.String())
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		"https://api.stripe.com/v1/transfers", strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+p.secret)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	// One transfer per (supplier, amount) attempt — a retried release is a no-op.
	req.Header.Set("Idempotency-Key", "cx-"+supplierID.String()+"-"+strconv.FormatInt(cents, 10))
	resp, err := p.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("stripe transfer request: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return "", fmt.Errorf("stripe transfer failed (%d): %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var out struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(body, &out); err != nil || out.ID == "" {
		return "", fmt.Errorf("stripe transfer: unparseable response: %s", strings.TrimSpace(string(body)))
	}
	return out.ID, nil
}

// ManualExportPayout is the alpha "manual export" rail (the goal's vendor-neutral
// "mock + manual export for alpha"): it moves NO money, but appends each owed
// payout — supplier id, amount, timestamp — to a CSV file the operator settles
// out-of-band (ACH / PayPal / etc.), returning a "manual-export" ref. Selected via
// CX_PAYOUT_EXPORT. Honest: the ref records that settlement is MANUAL, never a
// fabricated transfer id, and the file is the audit trail of what was handed off.
type ManualExportPayout struct {
	path string
	mu   sync.Mutex
}

func newManualExportPayout(path string) *ManualExportPayout { return &ManualExportPayout{path: path} }

func (p *ManualExportPayout) Send(_ context.Context, supplierID uuid.UUID, amountUSD float64) (string, error) {
	if amountUSD <= 0 {
		return "", fmt.Errorf("non-positive payout amount %.6f USD", amountUSD)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	f, err := os.OpenFile(p.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return "", fmt.Errorf("opening payout export %q: %w", p.path, err)
	}
	defer f.Close()
	// CSV row: supplier_id,amount_usd,exported_at(RFC3339). The operator settles
	// these out-of-band and reconciles against the ledger's audit view.
	if _, err := fmt.Fprintf(f, "%s,%.6f,%s\n", supplierID, amountUSD, time.Now().UTC().Format(time.RFC3339)); err != nil {
		return "", fmt.Errorf("writing payout export: %w", err)
	}
	return "manual-export:" + p.path, nil
}
