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

// suppliers.go — self-serve SUPPLIER onboarding (the buyer side is accounts.go).
//
// A prospective supplier has no credential yet, so these two handlers are unauthed
// and keyed by email (the worker-token Connect flow in connect.go is the
// authenticated counterpart once a supplier has a worker):
//
//   POST /v1/supplier/onboard {"email","tax_id","tax_country"}
//       -> {"onboarding_url", ...}    (Stripe Connect account-link)
//       -> 503                        (honest, when STRIPE_SECRET_KEY is unset)
//   GET  /v1/supplier/status?email=…  -> {"connect_status","payouts_enabled","tax_on_file"}
//
// Tax info (W-9/W-8BEN/T4A identifiers) is captured at onboard and persisted on the
// supplier row BEFORE any Stripe call, so the tax record exists even if Stripe is
// unconfigured (the boundary is honest: we tell the supplier payouts are not wired
// yet, but their tax + account intent is recorded). The Connect account-link and
// the account.updated webhook that flips payouts_enabled mirror billing.go's
// gating exactly — every Stripe path returns the same errBillingUnconfigured 503
// when the key is unset, NEVER a faked account or transfer (BLACKHOLE).

// --- store layer: supplier upsert + tax + payout readiness ---

// UpsertSupplierByEmail finds or creates a supplier by email and records its tax
// identifiers (tax_id / tax_country) — the W-9/W-8BEN/T4A info collected at
// onboarding. Idempotent on email (UNIQUE): a returning supplier updates its tax
// fields, a new one is created 'pending'. Returns the supplier id.
func (s *Store) UpsertSupplierByEmail(ctx context.Context, email, taxID, taxCountry string) (uuid.UUID, error) {
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`INSERT INTO suppliers (email, tax_id, tax_country, status)
		 VALUES (lower($1), NULLIF($2,''), NULLIF($3,''), 'pending')
		 ON CONFLICT (email) DO UPDATE SET
		   tax_id      = COALESCE(NULLIF(EXCLUDED.tax_id,''), suppliers.tax_id),
		   tax_country = COALESCE(NULLIF(EXCLUDED.tax_country,''), suppliers.tax_country)
		 RETURNING id`,
		email, taxID, taxCountry,
	).Scan(&id)
	return id, err
}

// SupplierStatusByEmail returns a supplier's Connect/tax state for the status
// endpoint: whether a Connect account exists, the cached payouts_enabled flag, and
// whether tax info is on file. errNotFound when no supplier has that email.
func (s *Store) SupplierStatusByEmail(ctx context.Context, email string) (acct string, payoutsEnabled, taxOnFile bool, err error) {
	var (
		acctP *string
		taxP  *string
	)
	err = s.pool.QueryRow(ctx,
		`SELECT stripe_acct, tax_id, COALESCE(payouts_enabled,false)
		   FROM suppliers WHERE email = lower($1)`,
		email,
	).Scan(&acctP, &taxP, &payoutsEnabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, false, errNotFound
	}
	if err != nil {
		return "", false, false, err
	}
	if acctP != nil {
		acct = *acctP
	}
	taxOnFile = taxP != nil && *taxP != ""
	return acct, payoutsEnabled, taxOnFile, nil
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

// supplierOnboardRequest is the POST /v1/supplier/onboard body.
type supplierOnboardRequest struct {
	Email      string `json:"email"`
	TaxID      string `json:"tax_id"`
	TaxCountry string `json:"tax_country"`
}

// handleSupplierOnboard captures a supplier's email + tax identifiers, creates (or
// reuses) a Stripe Connect Express account, persists stripe_acct, and returns a
// hosted onboarding_url to complete KYC. The tax fields are recorded BEFORE the
// Stripe call so the record stands even when Stripe is unconfigured; in that case
// every Connect call returns the honest 503 (errBillingUnconfigured), never a faked
// account or link (BLACKHOLE).
func (s *Server) handleSupplierOnboard(w http.ResponseWriter, r *http.Request) {
	var req supplierOnboardRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid onboard json: "+err.Error())
		return
	}
	email := normalizeEmail(req.Email)
	if !looksLikeEmail(email) {
		writeErr(w, http.StatusBadRequest, "a valid email is required")
		return
	}

	// Persist the supplier + tax info first (independent of Stripe). This is the
	// durable tax record; it exists even if the Connect call below 503s.
	supplierID, err := s.store.UpsertSupplierByEmail(r.Context(), email, req.TaxID, req.TaxCountry)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "recording supplier: "+err.Error())
		return
	}

	// Connect account + onboarding link (reuses connect.go's helpers, which are
	// gated on STRIPE_SECRET_KEY). With no key these return errBillingUnconfigured,
	// surfaced as the honest 503 — the supplier's tax + intent are still recorded.
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

// handleSupplierStatus reports a supplier's onboarding state by email. connect_status
// is "none" (no account yet), "pending" (account exists, not yet payout-ready), or
// "enabled" (Stripe says it can receive transfers). payouts_enabled is the cached
// webhook-driven flag; tax_on_file reflects whether tax identifiers are recorded.
// When Stripe is configured AND an account exists, we also refresh live so the
// status reflects completion even before the webhook lands.
func (s *Server) handleSupplierStatus(w http.ResponseWriter, r *http.Request) {
	email := normalizeEmail(r.URL.Query().Get("email"))
	if !looksLikeEmail(email) {
		writeErr(w, http.StatusBadRequest, "a valid ?email= is required")
		return
	}
	acct, payoutsEnabled, taxOnFile, err := s.store.SupplierStatusByEmail(r.Context(), email)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "no supplier with that email")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "supplier status: "+err.Error())
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
		"connect_status":  status,
		"payouts_enabled": payoutsEnabled,
		"tax_on_file":     taxOnFile,
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
