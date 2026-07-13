package main

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestWebhookSigningSecretSealedRoundTripAndSignature(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "unit-test-webhook-token-key")
	secret, sealed, err := newWebhookSigningSecret()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(secret, webhookSigningSecretPrefix) {
		t.Fatalf("secret prefix = %q", secret)
	}
	if !strings.HasPrefix(sealed, "enc:") || strings.Contains(sealed, secret) {
		t.Fatalf("secret was not sealed at rest: %q", sealed)
	}
	opened, err := openWebhookSigningSecret(sealed)
	if err != nil || opened != secret {
		t.Fatalf("opened secret = %q, %v; want original", opened, err)
	}

	body := []byte(`{"delivery_id":"fixed","event":"job.completed"}`)
	now := time.Unix(1_750_000_000, 0)
	signature := signWebhookAt(secret, body, now)
	if !verifyStripeSigAt(body, signature, secret, now) {
		t.Fatal("per-registration signature did not verify")
	}
	if verifyStripeSigAt(body, signature, "another-buyer's-secret", now) {
		t.Fatal("signature verified under a different registration secret")
	}
}

func TestWebhookSigningSecretFailsClosedWithoutUsableKey(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "")
	if _, _, err := newWebhookSigningSecret(); !errors.Is(err, errWebhookSigningKeyUnavailable) {
		t.Fatalf("generation error = %v, want errWebhookSigningKeyUnavailable", err)
	}
	for _, stored := range []string{"", "plain:cx_whsec_legacy", "cx_whsec_raw", "enc:not-base64"} {
		if _, err := openWebhookSigningSecret(stored); !errors.Is(err, errWebhookSigningSecretInvalid) {
			t.Fatalf("open %q error = %v, want fail-closed invalid secret", stored, err)
		}
	}
}

type countingWebhookRoundTripper struct{ calls atomic.Int32 }

func (r *countingWebhookRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	r.calls.Add(1)
	return nil, errors.New("network must not be reached")
}

func TestLegacyWebhookFailsBeforeNetworkIO(t *testing.T) {
	roundTripper := &countingWebhookRoundTripper{}
	wk := &Workers{client: &http.Client{Transport: roundTripper}}
	err := wk.deliverWebhook(context.Background(), PendingWebhook{
		ID: uuid.New(), JobID: uuid.New(), URL: "https://hooks.example.test/event", Status: "complete",
	})
	if err == nil || !webhookFailureIsPermanent(err) || !errors.Is(err, errWebhookSigningSecretInvalid) {
		t.Fatalf("legacy delivery error = %v, want permanent signing-secret failure", err)
	}
	if got := roundTripper.calls.Load(); got != 0 {
		t.Fatalf("legacy unsigned webhook made %d network call(s)", got)
	}
}

func TestAtomicJobBoundaryRejectsPlainWebhookSecret(t *testing.T) {
	err := (&Store{}).CreateJobWithTasks(context.Background(), &jobRow{
		WebhookID: uuid.New(), WebhookURL: "https://hooks.example.test/event",
		WebhookSigningSecretSealed: "plain:cx_whsec_forbidden",
	}, nil)
	if err == nil || !strings.Contains(err.Error(), "encrypted signing secret") {
		t.Fatalf("plain job webhook boundary error = %v", err)
	}
}
