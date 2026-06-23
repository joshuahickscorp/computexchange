package main

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"os"

	"github.com/google/uuid"
)

// connect.go — supplier-side Stripe Connect onboarding. Creates a supplier's Express
// account (Stripe-hosted KYC) + an onboarding link, and reports when the account can
// receive payouts. The payout TRANSFERS already exist (payment.go's StripePayout);
// this is the flow that gives each supplier the stripe_acct those transfers target.
// Gated on STRIPE_SECRET_KEY like the rest — an honest 503 without it, never faked.

// ensureConnectAccount returns the supplier's Express account id, creating + storing
// it on first use.
func ensureConnectAccount(ctx context.Context, store *Store, supplierID uuid.UUID) (string, error) {
	if acct, err := store.SupplierStripeAcct(ctx, supplierID); err == nil && acct != "" {
		return acct, nil
	}
	out, err := stripeForm(ctx, "accounts", url.Values{
		"type":                               {"express"},
		"capabilities[transfers][requested]": {"true"},
		"metadata[supplier_id]":              {supplierID.String()},
	}, "")
	if err != nil {
		return "", err
	}
	acct, _ := out["id"].(string)
	if acct == "" {
		return "", fmt.Errorf("stripe account: no id in response")
	}
	if err := store.SetSupplierStripeAcct(ctx, supplierID, acct); err != nil {
		return "", err
	}
	return acct, nil
}

// onboardingLink creates a Stripe-hosted onboarding URL for the account.
func onboardingLink(ctx context.Context, acct string) (string, error) {
	ret := os.Getenv("CX_CONNECT_RETURN_URL")
	if ret == "" {
		ret = "https://compute.exchange/earn?connected=1"
	}
	refresh := os.Getenv("CX_CONNECT_REFRESH_URL")
	if refresh == "" {
		refresh = ret
	}
	out, err := stripeForm(ctx, "account_links", url.Values{
		"account":     {acct},
		"refresh_url": {refresh},
		"return_url":  {ret},
		"type":        {"account_onboarding"},
	}, "")
	if err != nil {
		return "", err
	}
	link, _ := out["url"].(string)
	if link == "" {
		return "", fmt.Errorf("stripe account_link: no url in response")
	}
	return link, nil
}

// handleWorkerConnect ensures the supplier has a Connect account and returns an
// onboarding link to complete it (Stripe-hosted KYC). 503 until Stripe is configured.
func (s *Server) handleWorkerConnect(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	acct, err := ensureConnectAccount(r.Context(), s.store, auth.SupplierID)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	link, err := onboardingLink(r.Context(), acct)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"account": acct, "onboarding_url": link})
}

// handleWorkerConnectStatus reports whether the supplier's account can be paid yet
// (live from Stripe, so it reflects onboarding completion without a webhook).
func (s *Server) handleWorkerConnectStatus(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	acct, _ := s.store.SupplierStripeAcct(r.Context(), auth.SupplierID)
	if stripeKey() == "" || acct == "" {
		writeJSON(w, http.StatusOK, map[string]any{"configured": stripeKey() != "", "connected": false, "payouts_enabled": false})
		return
	}
	out, err := stripeGet(r.Context(), "accounts/"+acct)
	if err != nil {
		writeErr(w, http.StatusServiceUnavailable, err.Error())
		return
	}
	pe, _ := out["payouts_enabled"].(bool)
	writeJSON(w, http.StatusOK, map[string]any{"configured": true, "connected": true, "payouts_enabled": pe})
}
