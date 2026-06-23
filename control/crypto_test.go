package main

import (
	"testing"

	"github.com/google/uuid"
)

// With a key set, a sealed token must round-trip and must NOT appear in plaintext.
func TestSealOpenRoundTrip(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "test-secret-key")
	sealed := sealToken("ghp_secrettoken123")
	if sealed == "ghp_secrettoken123" || len(sealed) < 5 || sealed[:4] != "enc:" {
		t.Fatalf("token not sealed: %q", sealed)
	}
	if got := openToken(sealed); got != "ghp_secrettoken123" {
		t.Fatalf("open = %q, want original", got)
	}
}

// With no key, sealing is honest plaintext (tagged) and still round-trips.
func TestSealNoKeyIsHonestPlaintext(t *testing.T) {
	t.Setenv("CX_TOKEN_KEY", "")
	sealed := sealToken("abc")
	if sealed != "plain:abc" {
		t.Fatalf("expected plain: marker, got %q", sealed)
	}
	if got := openToken(sealed); got != "abc" {
		t.Fatalf("open = %q", got)
	}
}

// Signed state must verify, and a tampered signature must be rejected.
func TestStateSignVerify(t *testing.T) {
	t.Setenv("CX_STATE_SECRET", "state-secret")
	id := uuid.New()
	st := signState(id)
	got, ok := verifyState(st)
	if !ok || got != id {
		t.Fatalf("verify failed for valid state")
	}
	if _, ok := verifyState(st + "x"); ok {
		t.Fatal("tampered state must not verify")
	}
	if _, ok := verifyState(id.String()); ok {
		t.Fatal("unsigned state must not verify when a secret is set")
	}
}
