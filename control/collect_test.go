package main

import (
	"testing"
	"time"
)

// The immediate-vs-defer seam: below the threshold defers (batched later), at or
// above charges immediately. The boundary itself must charge — a job that hits
// the threshold exactly is not fee bleed.
func TestShouldDeferChargeDecision(t *testing.T) {
	cases := []struct {
		actual, threshold float64
		want              bool
	}{
		{0.01, 5.00, true}, // classic sub-threshold job → defer
		{4.999999, 5.00, true},
		{5.00, 5.00, false}, // boundary → charge immediately
		{5.000001, 5.00, false},
		{100.00, 5.00, false},
		{0.50, 0, false},    // threshold 0 = operator opted out of batching
		{2.00, 10.00, true}, // raised threshold widens the deferred band
	}
	for _, c := range cases {
		if got := shouldDeferCharge(c.actual, c.threshold); got != c.want {
			t.Fatalf("shouldDeferCharge(%v, %v): want %v, got %v", c.actual, c.threshold, c.want, got)
		}
	}
}

// The failed-single backoff seam: attempts × 30min, capped at 6h, never zero.
func TestChargeRetryBackoffSchedule(t *testing.T) {
	cases := []struct {
		attempts int
		want     time.Duration
	}{
		{0, 30 * time.Minute}, // defensive: pre-increment input still backs off
		{1, 30 * time.Minute},
		{2, 60 * time.Minute},
		{4, 2 * time.Hour},
		{12, 6 * time.Hour}, // exactly at the cap
		{13, 6 * time.Hour}, // beyond the cap stays capped
		{1000, 6 * time.Hour},
	}
	for _, c := range cases {
		if got := chargeRetryBackoff(c.attempts); got != c.want {
			t.Fatalf("chargeRetryBackoff(%d): want %s, got %s", c.attempts, c.want, got)
		}
	}
}

// The CX_CHARGE_MIN_USD env seam: unset/garbage → the 5.00 default; a real value
// is honored; a negative value clamps to 0 (charge everything immediately) — the
// threshold is never silently fabricated from a bad input.
func TestChargeMinUSDFromEnv(t *testing.T) {
	t.Setenv("CX_CHARGE_MIN_USD", "")
	if got := chargeMinUSD(); got != defaultChargeMinUSD {
		t.Fatalf("unset: want %v, got %v", defaultChargeMinUSD, got)
	}
	t.Setenv("CX_CHARGE_MIN_USD", "2.50")
	if got := chargeMinUSD(); got != 2.50 {
		t.Fatalf("2.50: want 2.50, got %v", got)
	}
	t.Setenv("CX_CHARGE_MIN_USD", "0")
	if got := chargeMinUSD(); got != 0 {
		t.Fatalf("0: want 0, got %v", got)
	}
	t.Setenv("CX_CHARGE_MIN_USD", "-3")
	if got := chargeMinUSD(); got != 0 {
		t.Fatalf("-3: want clamp to 0, got %v", got)
	}
	t.Setenv("CX_CHARGE_MIN_USD", "not-a-number")
	if got := chargeMinUSD(); got != defaultChargeMinUSD {
		t.Fatalf("garbage: want the %v default, got %v", defaultChargeMinUSD, got)
	}
}
