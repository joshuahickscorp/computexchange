//go:build integration

package main

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestWorkerLeadershipIsExclusiveAndTransfers(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	otherPool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		t.Fatalf("second leader pool: %v", err)
	}
	defer otherPool.Close()

	first, acquired, err := tryAcquireWorkerLeadership(ctx, itPool)
	if err != nil || !acquired {
		t.Fatalf("first leadership acquired=%v err=%v", acquired, err)
	}
	second, acquired, err := tryAcquireWorkerLeadership(ctx, otherPool)
	if err != nil {
		t.Fatalf("contended leadership: %v", err)
	}
	if acquired || second != nil {
		if second != nil {
			releaseWorkerLeadership(second)
		}
		releaseWorkerLeadership(first)
		t.Fatal("second replica acquired leadership while first held the session lock")
	}

	releaseWorkerLeadership(first)
	second, acquired, err = tryAcquireWorkerLeadership(ctx, otherPool)
	if err != nil || !acquired {
		t.Fatalf("leadership did not transfer acquired=%v err=%v", acquired, err)
	}
	releaseWorkerLeadership(second)
}

func TestWorkerLeadershipTransfersWhenSessionDies(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	otherPool, err := pgxpool.New(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		t.Fatalf("second leader pool: %v", err)
	}
	defer otherPool.Close()

	first, acquired, err := tryAcquireWorkerLeadership(ctx, itPool)
	if err != nil || !acquired {
		t.Fatalf("first leadership acquired=%v err=%v", acquired, err)
	}
	// Simulate an abrupt process/session loss: no explicit advisory unlock. A
	// session lock must disappear when its PostgreSQL connection closes.
	raw := first.Hijack()
	if err := raw.Close(ctx); err != nil {
		t.Fatalf("close leader session: %v", err)
	}

	second, acquired, err := tryAcquireWorkerLeadership(ctx, otherPool)
	if err != nil || !acquired {
		t.Fatalf("leadership did not transfer after session death acquired=%v err=%v", acquired, err)
	}
	releaseWorkerLeadership(second)
}

func TestReadyzFailsWhenEnabledWorkerElectionStopsProgressing(t *testing.T) {
	setWorkerElectionReadinessEnabled(true)
	t.Cleanup(func() { setWorkerElectionReadinessEnabled(false) })

	code, body := req(t, "GET", "/readyz", nil)
	if code != 503 || !strings.Contains(string(body), "worker election") {
		t.Fatalf("unobserved election readiness = %d %s, want 503 election reason", code, body)
	}
	markWorkerElectionObserved(time.Now())
	code, body = req(t, "GET", "/readyz", nil)
	if code != 200 {
		t.Fatalf("recently observed election readiness = %d %s, want 200", code, body)
	}
}
