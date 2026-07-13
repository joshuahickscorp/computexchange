package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// payment.go — ledger entry math and payout-provider boundary.
//
// Production settlement uses the job's immutable economic plan: buyer charge and
// supplier payout are frozen independently, and platform_take is their gross
// complement before actual processor/control costs. CX_PLATFORM_TAKE_PCT sets the
// supplier share of BASE compute used when building that plan; it is not a flat cut
// of the guarded buyer charge. Provider request code exists, but only external
// charge/transfer/reversal/refund/reconciliation evidence proves money movement.

// ledger entry kinds (match ledger_entries.kind).
const (
	KindBuyerCharge    = "buyer_charge"
	KindSupplierCredit = "supplier_credit"
	KindPlatformTake   = "platform_take"
	KindClawback       = "clawback"
	// KindStripeFee is the REAL Stripe processing fee of one successful
	// PaymentIntent (latest_charge.balance_transaction.fee — fetched from Stripe,
	// never estimated), stored negative with payout_ref = the PI id. One row per
	// PI (ledger_stripe_fee_ref_uniq), so a retried fee fetch never double-counts.
	KindStripeFee = "stripe_fee"
)

// payout_status values (match ledger_entries.payout_status).
const (
	PayoutPending = "pending"
	PayoutHeld    = "held"
	PayoutReady   = "ready"
	// PayoutAwaitingFunding is an owed liability whose hold elapsed but which
	// has no causally linked buyer cash or explicit subsidy reservation. Keeping
	// it out of the due queue prevents old unfunded debt from starving payable
	// rows; an exact cash fact or subsidy authorization re-arms it.
	PayoutAwaitingFunding = "awaiting_funding"
	// PayoutCarried means no provider call was possible because the exact
	// six-decimal liability was below one USD cent. The complete value remains
	// durably recorded in supplier_minor_unit_settlements; it is not released,
	// rounded away, or retried on every sweep.
	PayoutCarried = "carried"
	PayoutSending = "sending"
	// PayoutOutcomeUnknown means a provider request may have crossed the cash
	// boundary but no authoritative response was received. It is owed/exposed,
	// never treated as ready or safely clawed back, and is retried only with the
	// same idempotency key.
	PayoutOutcomeUnknown   = "outcome_unknown"
	PayoutReleased         = "released"
	PayoutExported         = "exported"
	PayoutClawedBack       = "clawed_back"
	PayoutReversalRequired = "reversal_required"
)

const (
	supplierSettlementPolicyFloorCentCarryV1       = "floor_cent_carry_v1"
	microUSDPerCent                          int64 = 10_000
)

// splitSupplierLiabilityMicros is the one deterministic provider-minor-unit
// policy. Internal liabilities are exact millionths of a dollar; a cash rail can
// send only whole cents. We floor cash (never overpay one liability) and preserve
// the complete non-negative remainder durably. Admission continues to reserve the
// full six-decimal liability, so carried value remains owed rather than margin.
func splitSupplierLiabilityMicros(liabilityMicros int64) (cashCents, remainderMicros int64, err error) {
	if liabilityMicros < 0 {
		return 0, 0, fmt.Errorf("supplier liability must be non-negative, got %d microusd", liabilityMicros)
	}
	cashCents = liabilityMicros / microUSDPerCent
	remainderMicros = liabilityMicros % microUSDPerCent
	return cashCents, remainderMicros, nil
}

// A buyer-controlled zero-second hold made a verified task payable on the next
// sweep, before delayed fraud/dispute evidence could arrive. Production now owns
// a non-bypassable floor. Tests that need an already-due row seed release_at
// directly rather than weakening this contract.
const minimumPayoutHold = 24 * time.Hour

func payoutReleaseAt(now time.Time, requestedSecs uint32) time.Time {
	hold := time.Duration(requestedSecs) * time.Second
	if hold < minimumPayoutHold {
		hold = minimumPayoutHold
	}
	return now.Add(hold)
}

// platformTakeRate is the historical name for the complement used to derive the
// supplier's share of BASE compute. The independent margin guard may raise the
// buyer charge without raising supplier payout, so this is no longer the realized
// platform-take percentage of buyer revenue.
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

// splitCharge preserves the pre-plan percentage split for isolated legacy unit
// coverage. Production commit settlement calls splitFrozenCharge below with the
// independently frozen buyer and supplier amounts; do not use this helper for new
// money paths.
func splitCharge(buyerID, supplierID, taskID uuid.UUID, buyerCharge float64, holdSecs uint32, now time.Time) []LedgerEntry {
	supplierAmt := buyerCharge * supplierShareRate
	platformAmt := buyerCharge - supplierAmt // exact complement, avoids FP drift vs ×0.10
	release := payoutReleaseAt(now, holdSecs)
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

// splitFrozenCharge is the margin-guard settlement primitive. Buyer charge and
// supplier payout were independently frozen at admission; the safety fee and
// processor/control reserve therefore remain platform money instead of leaking
// through supplierShareRate. The platform row is the exact complement.
func splitFrozenCharge(buyerID, supplierID, taskID uuid.UUID, buyerCharge, supplierPayout float64, holdSecs uint32, now time.Time) []LedgerEntry {
	platformAmt := buyerCharge - supplierPayout
	release := payoutReleaseAt(now, holdSecs)
	return []LedgerEntry{
		{Kind: KindBuyerCharge, BuyerID: &buyerID, TaskID: &taskID, AmountUSD: -buyerCharge, PayoutStatus: PayoutReleased},
		{Kind: KindSupplierCredit, SupplierID: &supplierID, TaskID: &taskID, AmountUSD: supplierPayout, PayoutStatus: PayoutHeld, ReleaseAt: &release},
		{Kind: KindPlatformTake, TaskID: &taskID, AmountUSD: platformAmt, PayoutStatus: PayoutReleased},
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

// PayoutResult reports the exact outcome returned by a money-movement rail.
type PayoutResult struct {
	Ref       string
	SentCents int64
	Currency  string
	CashMoved bool
}

// Payout is the money-movement rail. One method, one honest implementation in
// V1. Real Stripe Connect / Trolley integration is Phase 3.
type Payout interface {
	// Send transfers the exact integer minor units to a supplier and returns the
	// rail's transfer reference. It MUST NOT round a float or pretend success when
	// no rail is configured.
	//
	// payoutKey is a stable, unique identifier of the SPECIFIC payout being made
	// (the released ledger-entry id) — it is the idempotency discriminator on a
	// money rail that supports one (the Stripe transfer below). It MUST be unique
	// across distinct payouts so two separate credits of identical cents in
	// different release cycles do not collide, and it MUST be stable across a
	// genuine retry of the SAME payout so a retry is a true no-op. The released
	// ledger-entry id satisfies both: a distinct entry has a distinct id, and a
	// retried release of the same row reuses its id.
	Send(ctx context.Context, supplierID uuid.UUID, cents int64, currency, payoutKey string) (PayoutResult, error)
}

// errPayoutUnconfigured is the explicit failure surfaced when no real rail is
// wired. We never fake a transfer (BLACKHOLE: surface every failure).
var errPayoutUnconfigured = errors.New("payout rail not configured (Stripe Connect/Trolley) — Phase 3")

// errPayoutOutcomeUnknown marks a provider error for which the request may have
// crossed the external cash boundary. Callers must preserve conservative state
// and resolve it with the same idempotency key; they must never turn it into the
// definitely-not-sent ready state.
var errPayoutOutcomeUnknown = errors.New("payout provider outcome is unknown")

// errPayoutDefinitelyNotSent is the only error class that may transition an
// in-flight operation to ready. Untyped adapter errors default to unknown so a
// future rail cannot accidentally hide a crossed cash boundary.
var errPayoutDefinitelyNotSent = errors.New("payout provider definitely did not send")

func payoutOutcomeUnknown(cause error) error {
	if cause == nil {
		return errPayoutOutcomeUnknown
	}
	return fmt.Errorf("%w: %v", errPayoutOutcomeUnknown, cause)
}

func payoutDefinitelyNotSent(cause error) error {
	if cause == nil {
		return errPayoutDefinitelyNotSent
	}
	return fmt.Errorf("%w: %w", errPayoutDefinitelyNotSent, cause)
}

// stubPayout is the honest V1 Payout: it always errors, so a caller can never
// mistake "no rail" for "money sent".
type stubPayout struct{}

func (stubPayout) Send(_ context.Context, _ uuid.UUID, _ int64, _, _ string) (PayoutResult, error) {
	return PayoutResult{}, payoutDefinitelyNotSent(errPayoutUnconfigured)
}

// StripePayout is the REAL money rail: a Stripe Connect transfer to the supplier's
// connected account. It is selected only when STRIPE_SECRET_KEY is set (see
// main.go's selectPayout); otherwise the honest stubPayout is used. It never fakes
// a transfer — a missing key, a supplier with no connected account, or a Stripe
// error all surface explicitly, so a credit is only ever marked `released` against
// an exact transfer result. Errors before a possible cash boundary are retry-ready;
// transport/server/malformed-success ambiguity is typed outcome_unknown and cannot
// be mistaken for non-payment.
type StripePayout struct {
	store  *Store
	secret string
	http   *http.Client
}

func newStripePayout(store *Store, secret string) StripePayout {
	return StripePayout{store: store, secret: secret, http: &http.Client{Timeout: 20 * time.Second}}
}

func readStripePayoutResponseBody(r io.Reader) ([]byte, error) {
	body, err := readBoundedRemoteBody(r, stripeAPIResponseMaxBytes)
	if err != nil {
		// The transfer POST already reached Stripe. An oversized, truncated, or
		// unreadable response cannot prove that cash did not move.
		return nil, payoutOutcomeUnknown(fmt.Errorf("stripe transfer response read: %v", err))
	}
	return body, nil
}

// Send creates a Stripe transfer (exact cents → the supplier's stripe_acct) and
// returns the transfer id. The request
// carries an idempotency key keyed on payoutKey (the released ledger-entry id) so
// a retried release of the SAME credit is a no-op while two DISTINCT credits never
// collide — even with identical cents in different cycles.
func (p StripePayout) Send(ctx context.Context, supplierID uuid.UUID, cents int64, currency, payoutKey string) (PayoutResult, error) {
	if p.secret == "" {
		return PayoutResult{}, payoutDefinitelyNotSent(errPayoutUnconfigured)
	}
	acct, err := p.store.SupplierStripeAcct(ctx, supplierID)
	if err != nil {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("looking up supplier stripe account: %w", err))
	}
	if acct == "" {
		return PayoutResult{}, payoutDefinitelyNotSent(
			fmt.Errorf("supplier %s has no connected Stripe account (stripe_acct empty)", supplierID))
	}
	if cents <= 0 {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("non-positive payout amount %d cents", cents))
	}
	if currency != "usd" {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("unsupported payout currency %q", currency))
	}
	form := url.Values{}
	form.Set("amount", strconv.FormatInt(cents, 10))
	form.Set("currency", currency)
	form.Set("destination", acct)
	// A transfer group unique to this payout lets reconciliation resolve an
	// ambiguous response without relying on a supplier-wide aggregate. The same
	// stable payout key is reused on every idempotent retry.
	form.Set("transfer_group", "cxpo_"+payoutKey)
	form.Set("metadata[cx_payout_key]", payoutKey)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		"https://api.stripe.com/v1/transfers", strings.NewReader(form.Encode()))
	if err != nil {
		return PayoutResult{}, payoutDefinitelyNotSent(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.secret)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Idempotency-Key", stripeIdempotencyKey(supplierID, cents, payoutKey))
	resp, err := p.http.Do(req)
	if err != nil {
		return PayoutResult{}, payoutOutcomeUnknown(fmt.Errorf("stripe transfer request: %w", err))
	}
	defer resp.Body.Close()
	body, readErr := readStripePayoutResponseBody(resp.Body)
	if readErr != nil {
		return PayoutResult{}, readErr
	}
	if resp.StatusCode/100 != 2 {
		err := fmt.Errorf("stripe transfer failed (%d): %s", resp.StatusCode, strings.TrimSpace(string(body)))
		if resp.StatusCode >= http.StatusInternalServerError ||
			resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusConflict {
			return PayoutResult{}, payoutOutcomeUnknown(err)
		}
		return PayoutResult{}, payoutDefinitelyNotSent(err)
	}
	var out struct {
		ID       string `json:"id"`
		Amount   int64  `json:"amount"`
		Currency string `json:"currency"`
	}
	if err := json.Unmarshal(body, &out); err != nil || out.ID == "" {
		return PayoutResult{}, payoutOutcomeUnknown(
			fmt.Errorf("stripe transfer: unparseable success response: %s", strings.TrimSpace(string(body))))
	}
	if out.Amount != cents || out.Currency != currency {
		return PayoutResult{}, payoutOutcomeUnknown(fmt.Errorf(
			"stripe transfer %s amount/currency mismatch: requested=%d usd response=%d %s",
			out.ID, cents, out.Amount, out.Currency))
	}
	return PayoutResult{Ref: out.ID, SentCents: out.Amount, Currency: out.Currency, CashMoved: true}, nil
}

// stripeIdempotencyKey builds the Stripe Idempotency-Key for a single payout.
//
// The key MUST be unique across distinct payouts and stable across a genuine
// retry of the SAME payout. payoutKey (the released ledger-entry id) is that
// discriminator: a distinct credit has a distinct id, and a retried release of
// the same row reuses its id. We still bind the key to supplier+cents so it can
// never be reused for a different supplier or amount.
//
// Keying on (supplier, cents) ALONE was a real-money bug: two separate credits of
// identical cents in different release cycles produced the same key, so Stripe
// replayed the first transfer as a no-op and the second payout was silently
// dropped. When payoutKey is empty we fall back to the legacy (supplier, cents)
// scheme — never worse than before, and never a fabricated key.
func stripeIdempotencyKey(supplierID uuid.UUID, cents int64, payoutKey string) string {
	key := "cx-" + supplierID.String() + "-" + strconv.FormatInt(cents, 10)
	if payoutKey != "" {
		key += "-" + payoutKey
	}
	return key
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

func (p *ManualExportPayout) Send(_ context.Context, supplierID uuid.UUID, cents int64, currency, payoutKey string) (PayoutResult, error) {
	if cents <= 0 {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("non-positive payout amount %d cents", cents))
	}
	if currency != "usd" {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("unsupported payout currency %q", currency))
	}
	if strings.TrimSpace(payoutKey) == "" {
		return PayoutResult{}, payoutDefinitelyNotSent(errors.New("manual export payout key is required"))
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	// The export is an outbox too. If the row reached disk but the process died
	// before returning, a lease retry must not append a second instruction.
	if existing, err := os.ReadFile(p.path); err == nil {
		for _, line := range strings.Split(strings.TrimSpace(string(existing)), "\n") {
			fields := strings.Split(line, ",")
			if len(fields) > 0 && fields[len(fields)-1] == payoutKey {
				expectedAmount := fmt.Sprintf("%.6f", float64(cents)/100)
				if len(fields) != 4 || fields[0] != supplierID.String() || fields[1] != expectedAmount {
					return PayoutResult{}, fmt.Errorf(
						"payout export key %s is already bound to a different instruction", payoutKey)
				}
				return PayoutResult{Ref: "manual-export:" + p.path, Currency: currency, CashMoved: false}, nil
			}
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return PayoutResult{}, payoutDefinitelyNotSent(
			fmt.Errorf("reading payout export %q for idempotency: %w", p.path, err))
	}
	_, statErr := os.Stat(p.path)
	created := errors.Is(statErr, os.ErrNotExist)
	if statErr != nil && !created {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("stating payout export %q: %w", p.path, statErr))
	}
	f, err := os.OpenFile(p.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return PayoutResult{}, payoutDefinitelyNotSent(fmt.Errorf("opening payout export %q: %w", p.path, err))
	}
	// CSV row: supplier_id,amount_usd,exported_at(RFC3339),payout_key. The operator settles
	// these out-of-band and reconciles against the ledger's audit view.
	if _, err := fmt.Fprintf(f, "%s,%.6f,%s,%s\n", supplierID, float64(cents)/100,
		time.Now().UTC().Format(time.RFC3339), payoutKey); err != nil {
		_ = f.Close()
		return PayoutResult{}, fmt.Errorf("writing payout export: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return PayoutResult{}, fmt.Errorf("syncing payout export %q: %w", p.path, err)
	}
	if err := f.Close(); err != nil {
		return PayoutResult{}, fmt.Errorf("closing payout export %q: %w", p.path, err)
	}
	if created {
		dir, err := os.Open(filepath.Dir(p.path))
		if err != nil {
			return PayoutResult{}, fmt.Errorf("opening payout export directory: %w", err)
		}
		if err := dir.Sync(); err != nil {
			_ = dir.Close()
			return PayoutResult{}, fmt.Errorf("syncing payout export directory: %w", err)
		}
		if err := dir.Close(); err != nil {
			return PayoutResult{}, fmt.Errorf("closing payout export directory: %w", err)
		}
	}
	// Export is durable coordination, not cash. The release worker records an
	// `exported` operation and never reports the supplier credit as paid.
	return PayoutResult{Ref: "manual-export:" + p.path, Currency: "usd", CashMoved: false}, nil
}
