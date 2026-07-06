package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// billing.go — the BUYER side of money (payment.go is the supplier-payout side).
// Scaffolds Stripe: one customer per buyer, a SetupIntent to save a card, and an
// off-session charge when a job is billed. Gated on STRIPE_SECRET_KEY exactly like
// the payout rail — with no key, every call returns errBillingUnconfigured and
// NOTHING is charged or faked (BLACKHOLE: surface every failure, never a fake
// success). The internal ledger (payment.go) stays the source of truth for what is
// owed; this rail is the external money-in that reconciles against it.

var errBillingUnconfigured = fmt.Errorf("billing is not configured (set STRIPE_SECRET_KEY) — no charge is made or faked")

// stripeKey is the shared Stripe secret (same env var as the payout rail).
func stripeKey() string { return os.Getenv("STRIPE_SECRET_KEY") }

// stripeForm POSTs an x-www-form-urlencoded request to the Stripe API and decodes
// the JSON object. idemKey, when set, is sent as Idempotency-Key so a retried
// charge never double-bills. A missing key is the honest unconfigured error.
func stripeForm(ctx context.Context, path string, form url.Values, idemKey string) (map[string]any, error) {
	key := stripeKey()
	if key == "" {
		return nil, errBillingUnconfigured
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://api.stripe.com/v1/"+path, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if idemKey != "" {
		req.Header.Set("Idempotency-Key", idemKey)
	}
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("stripe %s (%d): %s", path, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("stripe %s: unparseable response", path)
	}
	return out, nil
}

// ensureStripeCustomer returns the buyer's Stripe customer id, creating + storing
// it on first use. Honest unconfigured error without a key.
func ensureStripeCustomer(ctx context.Context, store *Store, buyerID uuid.UUID) (string, error) {
	if cust, _, err := store.GetBillingCustomer(ctx, buyerID); err == nil && cust != "" {
		return cust, nil
	}
	out, err := stripeForm(ctx, "customers", url.Values{"metadata[buyer_id]": {buyerID.String()}}, "")
	if err != nil {
		return "", err
	}
	cust, _ := out["id"].(string)
	if cust == "" {
		return "", fmt.Errorf("stripe customer: no id in response")
	}
	if err := store.UpsertBillingCustomer(ctx, buyerID, cust); err != nil {
		return "", err
	}
	return cust, nil
}

// setupIntent creates a SetupIntent so the buyer's app (Stripe.js) can collect and
// save a card against their customer. Returns the client_secret.
func setupIntent(ctx context.Context, store *Store, buyerID uuid.UUID) (string, error) {
	cust, err := ensureStripeCustomer(ctx, store, buyerID)
	if err != nil {
		return "", err
	}
	out, err := stripeForm(ctx, "setup_intents", url.Values{"customer": {cust}, "payment_method_types[]": {"card"}}, "")
	if err != nil {
		return "", err
	}
	cs, _ := out["client_secret"].(string)
	if cs == "" {
		return "", fmt.Errorf("stripe setup_intent: no client_secret in response")
	}
	return cs, nil
}

// chargeBuyer makes an off-session charge for a billed job against the buyer's
// saved card. Scaffolded for the lifecycle to call once a card exists; it is NOT
// auto-wired into the commit path yet (that is the deliberate next step, gated on a
// real key + saved card so it can be verified, not assumed). idemKey makes a retry
// a no-op so a job is never double-charged.
func chargeBuyer(ctx context.Context, store *Store, buyerID uuid.UUID, usd float64, idemKey string) (string, error) {
	cents := int64(math.Round(usd * 100))
	if cents <= 0 {
		return "", fmt.Errorf("non-positive charge amount %.6f USD", usd)
	}
	cust, err := ensureStripeCustomer(ctx, store, buyerID)
	if err != nil {
		return "", err
	}
	_, pm, _ := store.GetBillingCustomer(ctx, buyerID)
	form := url.Values{
		"amount":      {strconv.FormatInt(cents, 10)},
		"currency":    {"usd"},
		"customer":    {cust},
		"confirm":     {"true"},
		"off_session": {"true"},
	}
	if pm != "" {
		form.Set("payment_method", pm)
	}
	out, err := stripeForm(ctx, "payment_intents", form, idemKey)
	if err != nil {
		return "", err
	}
	id, _ := out["id"].(string)
	return id, nil
}

// --- handlers ---

// handleBillingSetup returns a SetupIntent client_secret the buyer's app uses to
// save a card. 503 with an honest reason until STRIPE_SECRET_KEY is set.
func (s *Server) handleBillingSetup(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	cs, err := setupIntent(r.Context(), s.store, auth.BuyerID)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"client_secret": cs})
}

// handleBillingStatus reports whether billing is configured and whether THIS buyer
// has a customer + a saved card. Honest about the unconfigured state.
func (s *Server) handleBillingStatus(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	cust, pm, err := s.store.GetBillingCustomer(r.Context(), auth.BuyerID)
	writeJSON(w, http.StatusOK, map[string]any{
		"configured": stripeKey() != "",
		"connected":  err == nil && cust != "",
		"has_card":   pm != "",
	})
}

// --- Stripe webhooks + auto-charge ---

// stripeSigTolerance bounds how far a webhook's own claimed timestamp may drift
// from wall-clock "now" before it is rejected — Stripe's own client libraries
// default to 5 minutes for exactly this reason (Security Posture 6.5->7,
// docs/internal/CREED_AND_PATH_TO_TEN.md): without it, a signature is valid
// forever once computed, so a captured request body (e.g. from a proxy log, a
// misconfigured debug tool, or a compromised intermediary) stays replayable
// indefinitely — the HMAC alone never expires on its own.
const stripeSigTolerance = 5 * time.Minute

// verifyStripeSig checks a Stripe-Signature header (t=…,v1=…) against the webhook
// secret: real HMAC-SHA256 over "t.payload", constant-time compared, AND that the
// header's own claimed timestamp is within stripeSigTolerance of now.
func verifyStripeSig(payload []byte, sigHeader, secret string) bool {
	return verifyStripeSigAt(payload, sigHeader, secret, time.Now())
}

// verifyStripeSigAt is verifyStripeSig with an injectable clock, so replay-window
// behavior is unit-testable without a real wall-clock race.
func verifyStripeSigAt(payload []byte, sigHeader, secret string, now time.Time) bool {
	var t, v1 string
	for _, part := range strings.Split(sigHeader, ",") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) != 2 {
			continue
		}
		if kv[0] == "t" {
			t = kv[1]
		} else if kv[0] == "v1" && v1 == "" {
			v1 = kv[1]
		}
	}
	if t == "" || v1 == "" {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(t + "." + string(payload)))
	if !hmac.Equal([]byte(hex.EncodeToString(mac.Sum(nil))), []byte(v1)) {
		return false
	}
	tsSecs, err := strconv.ParseInt(t, 10, 64)
	if err != nil {
		return false
	}
	age := now.Sub(time.Unix(tsSecs, 0))
	if age < 0 {
		age = -age
	}
	return age <= stripeSigTolerance
}

// handleStripeWebhook receives Stripe events (unauthed — verified by signature) and
// reconciles billing state: a saved card sets the buyer's default payment method so
// the auto-charge can run. Gated on STRIPE_WEBHOOK_SECRET (honest 503 without it).
func (s *Server) handleStripeWebhook(w http.ResponseWriter, r *http.Request) {
	secret := os.Getenv("STRIPE_WEBHOOK_SECRET")
	if secret == "" {
		writeErr(w, http.StatusServiceUnavailable, "stripe webhooks not configured (set STRIPE_WEBHOOK_SECRET)")
		return
	}
	payload, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if !verifyStripeSig(payload, r.Header.Get("Stripe-Signature"), secret) {
		writeErr(w, http.StatusBadRequest, "invalid stripe signature")
		return
	}
	var ev struct {
		Type string `json:"type"`
		Data struct {
			Object map[string]any `json:"object"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &ev); err != nil {
		writeErr(w, http.StatusBadRequest, "unparseable webhook body")
		return
	}
	switch ev.Type {
	case "setup_intent.succeeded", "payment_method.attached":
		obj := ev.Data.Object
		cust, _ := obj["customer"].(string)
		pm, _ := obj["payment_method"].(string)
		if pm == "" {
			pm, _ = obj["id"].(string) // payment_method.attached: the object IS the PM
		}
		if cust != "" && pm != "" {
			if err := s.store.SetBillingPMByCustomer(r.Context(), cust, pm); err != nil {
				log.Printf("billing webhook: set PM for customer %s: %v", cust, err)
			}
		}
	}
	w.WriteHeader(http.StatusOK)
}

// chargeForJob bills the buyer's saved card for a completed job's actual cost.
// GATED: with no Stripe key OR no saved card it is a no-op — the internal ledger
// still records what is owed, so nothing is faked and the proven lifecycle is
// unchanged. Idempotent by job id so a re-finalize never double-charges. The
// actual decision + charge live in chargeOrDeferJob so the charge-collect sweep
// (collect.go) can route watchdog/fail-settled jobs through the SAME logic.
func (s *Server) chargeForJob(ctx context.Context, jobID uuid.UUID) {
	chargeOrDeferJob(ctx, s.store, jobID)
}

// chargeOrDeferJob is the single immediate-or-defer charge decision for one
// settled job. Below the CX_CHARGE_MIN_USD batching threshold the job is marked
// 'deferred' — deliberately NOT charged alone (Stripe's ~30¢ fixed fee would eat
// a sub-threshold charge), left for the charge-collect sweep to batch per buyer.
// At or above the threshold it charges immediately, exactly as before: no key →
// no-op; no saved card → 'no_payment_method' (owed, surfaced on the timeline);
// a Stripe failure → 'failed' (retried with backoff by the sweep). The Stripe
// idempotency key stays "job-"+jobID so ANY retry of the same job — including a
// retry after an ambiguous network timeout — can never double-charge.
func chargeOrDeferJob(ctx context.Context, store *Store, jobID uuid.UUID) {
	if stripeKey() == "" {
		return
	}
	buyerID, usd, err := store.JobChargeInfo(ctx, jobID)
	if err != nil || usd <= 0 {
		return
	}
	// DOUBLE-CHARGE GUARD: only a job whose charge was never decided may be decided
	// here. finalizeJobIfDone re-runs on late sibling commits (hedge/redundancy) and
	// would otherwise re-decide a job that is already charged or deferred — worst
	// case flipping 'charged' back to 'deferred' and re-charging it under a NEW
	// batch idempotency key, which Stripe cannot dedupe. Every later transition is
	// owned by the charge-collect sweep, never by a re-finalize.
	if st, serr := store.JobChargeStatus(ctx, jobID); serr != nil || st != "not_attempted" {
		return
	}
	if shouldDeferCharge(usd, chargeMinUSD()) {
		if _, derr := store.MarkJobDeferred(ctx, jobID); derr != nil {
			log.Printf("billing: deferring sub-threshold charge for job %s: %v (stays owed in the ledger)", jobID, derr)
		}
		return
	}
	cust, pm, err := store.GetBillingCustomer(ctx, buyerID)
	if err != nil || cust == "" || pm == "" {
		_ = store.SetChargeStatus(ctx, jobID, "no_payment_method")
		// Surface the silent debt on the buyer's timeline (best-effort): the work ran
		// and is owed, but there was no saved card to charge off-session.
		_ = store.InsertJobEvent(ctx, jobID, nil, "charge_failed",
			"Job complete but no saved payment method · amount is owed and will be charged once a card is on file", nil)
		return // no saved card → nothing to charge off-session (still owed in the ledger)
	}
	// Freeze the attempted amount BEFORE the charge: retries must replay the SAME
	// (key, amount) pair. If actual_usd drifted after a failed attempt (a late
	// sibling commit re-settling), reusing the key with a different amount is a
	// permanent Stripe idempotency_error loop — and a double charge once the key
	// expires. The frozen figure is what every retry charges.
	if ferr := store.FreezeChargeAmount(ctx, jobID, usd); ferr != nil {
		log.Printf("billing: freezing charge amount for job %s: %v (charge deferred to the sweep)", jobID, ferr)
		return
	}
	pi, err := chargeBuyer(ctx, store, buyerID, usd, "job-"+jobID.String())
	if err != nil {
		_ = store.SetChargeStatus(ctx, jobID, "failed")
		// Make the charge failure visible to the buyer (best-effort), not just a log line.
		_ = store.InsertJobEvent(ctx, jobID, nil, "charge_failed",
			"Charge for this job failed · amount is owed and will be reconciled", nil)
		log.Printf("billing: charge for job %s failed (owed, will reconcile): %v", jobID, err)
		return
	}
	if serr := store.SetJobCharged(ctx, jobID, pi); serr != nil {
		log.Printf("billing: marking job %s charged (pi %s): %v", jobID, pi, serr)
		return
	}
	// Record the REAL Stripe fee (never estimated). A fetch failure is logged and
	// left to the charge-collect sweep's backfill scan — the charge itself stands.
	if ferr := recordStripeFee(ctx, store, buyerID, pi); ferr != nil {
		log.Printf("billing: stripe fee for job %s (pi %s) not recorded yet: %v (backfilled by the charge-collect sweep)", jobID, pi, ferr)
	}
}

// recordStripeFee fetches the REAL processing fee of a successful PaymentIntent
// (latest_charge.balance_transaction.fee, integer cents) and inserts exactly one
// negative 'stripe_fee' ledger row with payout_ref = the PI id. Idempotent: a PI
// that already has its fee row is a clean no-op (INSERT-if-absent, backed by the
// ledger_stripe_fee_ref_uniq index), so the finalize path and the backfill sweep
// can both call this without double-counting. A missing/not-yet-settled
// balance_transaction is an error (retried by the backfill scan) — the fee is
// never guessed.
func recordStripeFee(ctx context.Context, store *Store, buyerID uuid.UUID, pi string) error {
	if pi == "" {
		return fmt.Errorf("no payment intent id to fetch a fee for")
	}
	out, err := stripeGet(ctx, "payment_intents/"+pi+"?expand[]=latest_charge.balance_transaction")
	if err != nil {
		return err
	}
	lc, _ := out["latest_charge"].(map[string]any)
	bt, _ := lc["balance_transaction"].(map[string]any)
	feeCents, ok := bt["fee"].(float64) // JSON number; Stripe fees are integer cents
	if !ok {
		return fmt.Errorf("payment intent %s: latest_charge.balance_transaction.fee absent (not settled yet?) — fee not recorded, never estimated", pi)
	}
	return store.InsertStripeFee(ctx, buyerID, pi, feeCents/100)
}

// stripeGet does a GET against the Stripe API (used by the Connect status check).
func stripeGet(ctx context.Context, path string) (map[string]any, error) {
	key := stripeKey()
	if key == "" {
		return nil, errBillingUnconfigured
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.stripe.com/v1/"+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return nil, fmt.Errorf("stripe GET %s (%d): %s", path, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("stripe GET %s: unparseable response", path)
	}
	return out, nil
}
