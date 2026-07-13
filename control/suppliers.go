package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// suppliers.go — authenticated, self-serve supplier onboarding.
//
// All three buyer-facing routes are scoped by the AuthResult installed by
// authBuyer. The request never supplies an email, supplier id, or tax identifier:
//
//   POST /v1/supplier/onboard {}       -> Stripe-hosted Connect onboarding URL
//   GET  /v1/supplier/status           -> this account's Connect status
//   POST /v1/supplier/worker-tokens {} -> one token for a new machine
//
// suppliers.owner_buyer_id is the authorization boundary. The supplier email is a
// display/contact value copied from the authenticated buyer account when the row is
// first created, never a lookup credential. Stripe Connect hosts identity, KYC, and
// tax collection; CX deliberately stores none of those plaintext identifiers.

// --- store layer: account ownership + payout readiness ---

var (
	errSupplierAccountRequired   = errors.New("supplier routes require a self-serve buyer account")
	errSupplierOwnershipConflict = errors.New("supplier ownership conflict")
	errSupplierBodyMustBeEmpty   = errors.New("supplier request must not contain identity or KYC fields")
)

// EnsureSupplierForBuyer returns the one supplier owned by buyerID, creating it
// from the buyer account's canonical email when necessary. It never claims an
// unowned legacy supplier at request time: the schema migration backfills only
// unambiguous one-to-one matches, while anything left unowned requires an explicit
// operator decision. That fail-closed rule prevents an account from taking over a
// historical supplier merely by presenting the same email string.
func (s *Store) EnsureSupplierForBuyer(ctx context.Context, buyerID uuid.UUID) (uuid.UUID, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return uuid.Nil, err
	}
	defer tx.Rollback(ctx)

	var email string
	err = tx.QueryRow(ctx,
		`SELECT lower(email) FROM buyers WHERE id = $1 FOR SHARE`, buyerID,
	).Scan(&email)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, errSupplierAccountRequired
	}
	if err != nil {
		return uuid.Nil, err
	}
	if !looksLikeEmail(email) {
		return uuid.Nil, errSupplierAccountRequired
	}

	// The owner id is authoritative even if a future account-email migration changes
	// the display email. Locking the row also serializes duplicate onboard/token calls.
	var supplierID uuid.UUID
	err = tx.QueryRow(ctx,
		`SELECT id FROM suppliers WHERE owner_buyer_id = $1 FOR UPDATE`, buyerID,
	).Scan(&supplierID)
	if err == nil {
		if err := tx.Commit(ctx); err != nil {
			return uuid.Nil, err
		}
		return supplierID, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, err
	}

	// An existing case-insensitive email match that was not safely backfilled is
	// intentionally not claimable here. This also catches a supplier owned elsewhere.
	var existingOwner *uuid.UUID
	err = tx.QueryRow(ctx,
		`SELECT id, owner_buyer_id
		   FROM suppliers
		  WHERE lower(email) = lower($1)
		  ORDER BY created_at, id
		  LIMIT 1
		  FOR UPDATE`, email,
	).Scan(&supplierID, &existingOwner)
	if err == nil {
		if existingOwner != nil && *existingOwner == buyerID {
			if err := tx.Commit(ctx); err != nil {
				return uuid.Nil, err
			}
			return supplierID, nil
		}
		return uuid.Nil, errSupplierOwnershipConflict
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, err
	}

	err = tx.QueryRow(ctx,
		`INSERT INTO suppliers (email, owner_buyer_id, status)
		 VALUES ($1, $2, 'pending')
		 ON CONFLICT DO NOTHING
		 RETURNING id`, email, buyerID,
	).Scan(&supplierID)
	if err == nil {
		if err := tx.Commit(ctx); err != nil {
			return uuid.Nil, err
		}
		return supplierID, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, err
	}

	// A concurrent request may have won either unique constraint while this
	// transaction waited. Only the same owner may reuse that result.
	err = tx.QueryRow(ctx,
		`SELECT id FROM suppliers WHERE owner_buyer_id = $1`, buyerID,
	).Scan(&supplierID)
	if err == nil {
		if err := tx.Commit(ctx); err != nil {
			return uuid.Nil, err
		}
		return supplierID, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, err
	}
	return uuid.Nil, errSupplierOwnershipConflict
}

// SupplierStatusForBuyer returns only the supplier owned by buyerID. A valid
// account with no supplier is errNotFound; a legacy/API-key identity with no buyer
// account row is errSupplierAccountRequired.
func (s *Store) SupplierStatusForBuyer(ctx context.Context, buyerID uuid.UUID) (supplierID uuid.UUID, acct string, payoutsEnabled bool, err error) {
	var acctP *string
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(s.id, '00000000-0000-0000-0000-000000000000'::uuid),
		        s.stripe_acct,
		        COALESCE(s.payouts_enabled, false)
		   FROM buyers b
		   LEFT JOIN suppliers s ON s.owner_buyer_id = b.id
		  WHERE b.id = $1`, buyerID,
	).Scan(&supplierID, &acctP, &payoutsEnabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, "", false, errSupplierAccountRequired
	}
	if err != nil {
		return uuid.Nil, "", false, err
	}
	if supplierID == uuid.Nil {
		return uuid.Nil, "", false, errNotFound
	}
	if acctP != nil {
		acct = *acctP
	}
	return supplierID, acct, payoutsEnabled, nil
}

// CreateWorkerTokenForBuyer adds a defense-in-depth ownership check immediately
// before minting. The handler already obtained supplierID through
// EnsureSupplierForBuyer, but this prevents a future caller from turning the raw
// CreateWorkerToken primitive into a cross-account route by mistake.
func (s *Store) CreateWorkerTokenForBuyer(ctx context.Context, buyerID, workerID, supplierID uuid.UUID) (string, error) {
	var owned bool
	if err := s.pool.QueryRow(ctx,
		`SELECT EXISTS (
		   SELECT 1 FROM suppliers WHERE id = $1 AND owner_buyer_id = $2
		 )`, supplierID, buyerID,
	).Scan(&owned); err != nil {
		return "", err
	}
	if !owned {
		return "", errSupplierOwnershipConflict
	}
	return s.CreateWorkerToken(ctx, workerID, supplierID)
}

// SetSupplierPayoutsEnabledByAcct flips the cached payouts_enabled flag for the
// supplier owning a Stripe Connect account id. Driven by the account.updated
// webhook. A no-op (no error) when no supplier has that account.
func (s *Store) SetSupplierPayoutsEnabledByAcct(ctx context.Context, acct string, enabled bool) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE suppliers SET payouts_enabled = $2 WHERE stripe_acct = $1`, acct, enabled)
	return err
}

// --- HTTP handlers ---

// decodeEmptySupplierBody accepts an omitted body or one empty JSON object. Any
// field is rejected so old clients cannot accidentally send email/tax identifiers
// that CX must not receive or retain.
func decodeEmptySupplierBody(r *http.Request) error {
	if r.Body == nil {
		return nil
	}
	dec := json.NewDecoder(io.LimitReader(r.Body, 4097))
	var body map[string]json.RawMessage
	if err := dec.Decode(&body); errors.Is(err, io.EOF) {
		return nil
	} else if err != nil {
		return err
	}
	if body == nil || len(body) != 0 {
		return errSupplierBodyMustBeEmpty
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("supplier request must contain one JSON object")
		}
		return err
	}
	return nil
}

func writeSupplierStoreError(w http.ResponseWriter, action string, err error) {
	switch {
	case errors.Is(err, errSupplierAccountRequired):
		writeErr(w, http.StatusForbidden, errSupplierAccountRequired.Error())
	case errors.Is(err, errSupplierOwnershipConflict):
		writeErr(w, http.StatusConflict, "supplier ownership requires operator review")
	default:
		writeErr(w, http.StatusInternalServerError, action+": "+err.Error())
	}
}

// handleSupplierOnboard creates (or reuses) this buyer account's supplier, creates
// a Stripe Connect Express account, and returns Stripe's hosted onboarding URL.
// CX never receives KYC or tax identifiers. With no Stripe key, the owned supplier
// intent remains recorded and the Connect boundary returns an honest 503.
func (s *Server) handleSupplierOnboard(w http.ResponseWriter, r *http.Request) {
	if err := decodeEmptySupplierBody(r); err != nil {
		writeErr(w, http.StatusBadRequest, "supplier identity comes from the authenticated account and KYC is collected by Stripe; send an empty JSON object")
		return
	}
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	supplierID, err := s.store.EnsureSupplierForBuyer(r.Context(), auth.BuyerID)
	if err != nil {
		writeSupplierStoreError(w, "recording supplier", err)
		return
	}

	acct, err := ensureConnectAccount(r.Context(), s.store, supplierID)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	link, err := onboardingLink(r.Context(), acct)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"supplier_id":    supplierID,
		"account":        acct,
		"onboarding_url": link,
	})
}

// handleCreateWorkerToken mints one worker token for a new machine under the
// authenticated account's supplier. It may create that supplier first, but cannot
// select or mutate one by email or supplier id supplied by the caller.
func (s *Server) handleCreateWorkerToken(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Pragma", "no-cache")
	if err := decodeEmptySupplierBody(r); err != nil {
		writeErr(w, http.StatusBadRequest, "supplier identity comes from the authenticated account; send an empty JSON object")
		return
	}
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	supplierID, err := s.store.EnsureSupplierForBuyer(r.Context(), auth.BuyerID)
	if err != nil {
		writeSupplierStoreError(w, "recording supplier", err)
		return
	}
	workerID := uuid.New()
	token, err := s.store.CreateWorkerTokenForBuyer(r.Context(), auth.BuyerID, workerID, supplierID)
	if err != nil {
		writeSupplierStoreError(w, "minting worker token", err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"supplier_id":  supplierID,
		"worker_id":    workerID,
		"worker_token": token,
	})
}

// handleSupplierStatus reports only the authenticated account's supplier.
// connect_status is "none" (no Connect account), "pending" (account exists but is
// not payout-ready), or "enabled". When Stripe is configured, the cached readiness
// is refreshed live. No email selector or local "tax on file" claim exists.
func (s *Server) handleSupplierStatus(w http.ResponseWriter, r *http.Request) {
	if _, supplied := r.URL.Query()["email"]; supplied {
		writeErr(w, http.StatusBadRequest, "email is not accepted; supplier status is scoped to the authenticated account")
		return
	}
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	supplierID, acct, payoutsEnabled, err := s.store.SupplierStatusForBuyer(r.Context(), auth.BuyerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "no supplier for this account")
		return
	}
	if err != nil {
		writeSupplierStoreError(w, "supplier status", err)
		return
	}

	// Best-effort live refresh: if Stripe is configured and the account exists, ask
	// Stripe directly so completion shows without waiting for the webhook. A Stripe
	// error here is non-fatal — we fall back to the cached flag, never fake it.
	if stripeKey() != "" && acct != "" {
		if out, gerr := stripeGet(r.Context(), "accounts/"+acct); gerr == nil {
			if pe, ok := out["payouts_enabled"].(bool); ok {
				payoutsEnabled = pe
				_ = s.store.SetSupplierPayoutsEnabledByAcct(r.Context(), acct, pe) // keep cache fresh
			}
		}
	}

	status := "none"
	if acct != "" {
		if payoutsEnabled {
			status = "enabled"
		} else {
			status = "pending"
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"supplier_id":     supplierID,
		"connect_status":  status,
		"payouts_enabled": payoutsEnabled,
		"kyc_provider":    "stripe_connect",
	})
}

// handleConnectWebhook receives Stripe Connect events (account.updated) and flips
// the cached payouts_enabled flag for the affected connected account. Like the
// buyer-side billing webhook it is unauthed but signature-verified, and gated on a
// secret — CX_CONNECT_WEBHOOK_SECRET, falling back to STRIPE_WEBHOOK_SECRET so a
// single endpoint secret works for both directions. Honest 503 when no secret is set.
func (s *Server) handleConnectWebhook(w http.ResponseWriter, r *http.Request) {
	secret := os.Getenv("CX_CONNECT_WEBHOOK_SECRET")
	if secret == "" {
		secret = os.Getenv("STRIPE_WEBHOOK_SECRET")
	}
	if secret == "" {
		writeErr(w, http.StatusServiceUnavailable, "connect webhooks not configured (set CX_CONNECT_WEBHOOK_SECRET or STRIPE_WEBHOOK_SECRET)")
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
	if ev.Type == "account.updated" {
		obj := ev.Data.Object
		acct, _ := obj["id"].(string)
		pe, _ := obj["payouts_enabled"].(bool)
		if acct != "" {
			if err := s.store.SetSupplierPayoutsEnabledByAcct(r.Context(), acct, pe); err != nil {
				// Surface, do not swallow: the webhook will be retried by Stripe.
				writeErr(w, http.StatusInternalServerError, "updating payout readiness: "+err.Error())
				return
			}
		}
	}
	w.WriteHeader(http.StatusOK)
}
