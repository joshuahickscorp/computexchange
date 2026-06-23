package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

// The Stripe webhook gate: a correctly-signed payload verifies; a tampered
// signature or wrong secret must be rejected (no spoofed billing events).
func TestVerifyStripeSig(t *testing.T) {
	secret, payload, ts := "whsec_test", []byte(`{"type":"setup_intent.succeeded"}`), "1700000000"
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(ts + "." + string(payload)))
	good := "t=" + ts + ",v1=" + hex.EncodeToString(mac.Sum(nil))

	if !verifyStripeSig(payload, good, secret) {
		t.Fatal("valid signature was rejected")
	}
	if verifyStripeSig(payload, "t="+ts+",v1=deadbeef", secret) {
		t.Fatal("forged signature was accepted")
	}
	if verifyStripeSig(payload, good, "wrong-secret") {
		t.Fatal("wrong secret was accepted")
	}
	if verifyStripeSig(payload, "", secret) {
		t.Fatal("empty signature header was accepted")
	}
}
