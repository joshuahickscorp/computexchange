package main

import (
	"testing"
	"time"
)

// workers_liveness_test.go — unit tests for the ticker liveness guard (no DB).
// The staleness math is the load-bearing piece of /readyz, so it is proven in
// isolation: a fresh ticker is healthy, a recently-succeeded one is healthy, one
// that missed staleMultiple × its interval is stale, and a never-run ticker goes
// stale only after the grace window from the loop start.

func newLiveness() *tickerLiveness { return &tickerLiveness{entries: map[string]*tickerStat{}} }

func TestLivenessFreshTickerIsNotStale(t *testing.T) {
	l := newLiveness()
	l.register("payout-release", 60*time.Second)
	now := time.Now()
	// Just started, never succeeded, well within the grace budget: not stale.
	if stale := l.stale(now, now.Add(-1*time.Second)); len(stale) != 0 {
		t.Fatalf("fresh ticker should not be stale, got %v", stale)
	}
}

func TestLivenessRecentSuccessIsNotStale(t *testing.T) {
	l := newLiveness()
	l.register("stale-requeue", 30*time.Second)
	now := time.Now()
	l.markSuccess("stale-requeue", now.Add(-20*time.Second)) // < 3×30s
	if stale := l.stale(now, now.Add(-time.Hour)); len(stale) != 0 {
		t.Fatalf("recently-succeeded ticker should not be stale, got %v", stale)
	}
}

func TestLivenessMissedRunsIsStale(t *testing.T) {
	l := newLiveness()
	l.register("webhook-sweep", 20*time.Second)
	now := time.Now()
	// Last success 90s ago > 3×20s = 60s budget → stale.
	l.markSuccess("webhook-sweep", now.Add(-90*time.Second))
	stale := l.stale(now, now.Add(-time.Hour))
	if len(stale) != 1 || stale[0] != "webhook-sweep" {
		t.Fatalf("missed-run ticker should be stale, got %v", stale)
	}
}

func TestLivenessNeverRanGoesStaleAfterGrace(t *testing.T) {
	l := newLiveness()
	l.register("ledger-reconcile", 15*time.Minute)
	now := time.Now()
	budget := time.Duration(staleMultiple) * 15 * time.Minute

	// Started just inside the budget: a never-run ticker is still tolerated.
	if stale := l.stale(now, now.Add(-budget+time.Minute)); len(stale) != 0 {
		t.Fatalf("never-run ticker within grace should not be stale, got %v", stale)
	}
	// Started well past the budget with no success: now stale.
	if stale := l.stale(now, now.Add(-budget-time.Minute)); len(stale) != 1 {
		t.Fatalf("never-run ticker past grace should be stale, got %v", stale)
	}
}

func TestLivenessSnapshotCountsSeconds(t *testing.T) {
	l := newLiveness()
	l.register("dispute-resolve", 20*time.Second)
	now := time.Now()
	l.markSuccess("dispute-resolve", now.Add(-5*time.Second))
	snap := l.snapshot(now, now.Add(-time.Hour))
	if got := snap["dispute-resolve"]; got < 4.9 || got > 5.1 {
		t.Fatalf("snapshot seconds-since-success: want ~5, got %.3f", got)
	}
	// A never-succeeded ticker measures from `since`, not zero time.
	l.register("payout-release", 60*time.Second)
	snap = l.snapshot(now, now.Add(-42*time.Second))
	if got := snap["payout-release"]; got < 41.9 || got > 42.1 {
		t.Fatalf("never-run snapshot should measure from since: want ~42, got %.3f", got)
	}
}
