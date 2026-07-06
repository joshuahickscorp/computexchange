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

// TestLivenessFailureSnapshotCounts proves cx_ticker_failures_total's backing
// mechanism (Postgres Data Lifecycle 8->9): a ticker that fails must count each
// failure independently of whether/when it later succeeds, and an unregistered
// or never-failed ticker must never appear as a spurious nonzero count.
func TestLivenessFailureSnapshotCounts(t *testing.T) {
	l := newLiveness()
	l.register("telemetry-retention", time.Hour)
	l.register("payout-release", 60*time.Second)

	if got := l.failureSnapshot()["telemetry-retention"]; got != 0 {
		t.Fatalf("freshly registered ticker should have 0 failures, got %d", got)
	}

	l.markFailure("telemetry-retention")
	l.markFailure("telemetry-retention")
	if got := l.failureSnapshot()["telemetry-retention"]; got != 2 {
		t.Fatalf("want 2 failures after two markFailure calls, got %d", got)
	}
	// A subsequent success must NOT reset the lifetime failure count — the
	// counter is cumulative, matching Prometheus counter semantics (only ever
	// resets on process restart), not a "currently failing" gauge.
	l.markSuccess("telemetry-retention", time.Now())
	if got := l.failureSnapshot()["telemetry-retention"]; got != 2 {
		t.Fatalf("a later success must not reset the failure count, got %d", got)
	}
	l.markFailure("telemetry-retention")
	if got := l.failureSnapshot()["telemetry-retention"]; got != 3 {
		t.Fatalf("want 3 failures after a third markFailure call, got %d", got)
	}
	// A different, never-failed ticker must read 0, not leak the other's count.
	if got := l.failureSnapshot()["payout-release"]; got != 0 {
		t.Fatalf("unrelated ticker should have 0 failures, got %d", got)
	}
	// An unregistered name is simply absent from the snapshot (no map entry to
	// mutate), never a false positive.
	l.markFailure("no-such-ticker")
	if _, ok := l.failureSnapshot()["no-such-ticker"]; ok {
		t.Fatal("markFailure on an unregistered name must not create a phantom entry")
	}
}
