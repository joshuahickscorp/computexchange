package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
	"time"
)

// The Stripe webhook gate: a correctly-signed payload verifies (within the replay
// window); a tampered signature or wrong secret must be rejected (no spoofed
// billing events). Uses verifyStripeSigAt with a fixed clock so "good" and
// "expired" are both deterministic, not races against real wall-clock time.
func TestVerifyStripeSig(t *testing.T) {
	secret, payload, ts := "whsec_test", []byte(`{"type":"setup_intent.succeeded"}`), "1700000000"
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(ts + "." + string(payload)))
	good := "t=" + ts + ",v1=" + hex.EncodeToString(mac.Sum(nil))
	tsTime := time.Unix(1700000000, 0)

	if !verifyStripeSigAt(payload, good, secret, tsTime) {
		t.Fatal("valid signature at its own timestamp was rejected")
	}
	if verifyStripeSigAt(payload, "t="+ts+",v1=deadbeef", secret, tsTime) {
		t.Fatal("forged signature was accepted")
	}
	if verifyStripeSigAt(payload, good, "wrong-secret", tsTime) {
		t.Fatal("wrong secret was accepted")
	}
	if verifyStripeSigAt(payload, "", secret, tsTime) {
		t.Fatal("empty signature header was accepted")
	}
}

// A correctly-signed payload whose own timestamp has drifted past the replay
// tolerance (either direction) must be rejected — the HMAC alone never expires,
// so this is the only thing standing between a captured request and an
// indefinitely-replayable one.
func TestVerifyStripeSigRejectsReplayOutsideTolerance(t *testing.T) {
	secret, payload, ts := "whsec_test", []byte(`{"type":"setup_intent.succeeded"}`), "1700000000"
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(ts + "." + string(payload)))
	sig := "t=" + ts + ",v1=" + hex.EncodeToString(mac.Sum(nil))
	tsTime := time.Unix(1700000000, 0)

	if !verifyStripeSigAt(payload, sig, secret, tsTime.Add(4*time.Minute)) {
		t.Fatal("a signature 4 minutes old (within the 5-minute tolerance) was rejected")
	}
	if verifyStripeSigAt(payload, sig, secret, tsTime.Add(10*time.Minute)) {
		t.Fatal("a signature replayed 10 minutes later (past tolerance) was accepted")
	}
	if verifyStripeSigAt(payload, sig, secret, tsTime.Add(-10*time.Minute)) {
		t.Fatal("a signature claiming to be 10 minutes in the FUTURE was accepted")
	}
}
