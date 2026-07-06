package main

import (
	"context"
	"log"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// notify.go — wake-on-work for the claim long-poll (docs/CREED_AND_PATH_TO_TEN.md,
// "Control plane hot path" 6→7). db/schema.sql (via Store.Migrate) installs a
// STATEMENT-level trigger on `tasks` that calls `pg_notify('cx_task_available', '')`
// whenever a task is inserted or its status/visible_at changes. This file holds ONE
// dedicated connection LISTENing on that channel for the life of the process and
// broadcasts each notification to every currently-waiting claimWithWait goroutine, so
// an idle long-poll wakes on real work arriving instead of re-attempting a full
// ClaimTask transaction every 250ms regardless of whether anything changed.

// taskWake is the process-wide broadcaster every claimWithWait call waits on,
// mirroring how `liveness` (workers.go) is a package-global registry for the same
// "one value, process-wide, not per-request" reason.
var taskWake = newWaker()

// waker implements the standard "close a channel to broadcast, then replace it"
// pattern: Wait() returns the CURRENT channel; every waiter's channel closes
// simultaneously on the next Broadcast(), waking them all, and a fresh channel is
// swapped in for the next generation. No waiter can miss a broadcast that happens
// after it calls Wait() (it already holds the channel that will close), and a
// broadcast with zero waiters is simply a no-op close/replace — cheap either way.
// The mutex is load-bearing, not decorative: `ch` is read from every waiting
// claimWithWait goroutine and written from the single notify-listener goroutine
// (notify.go's listenOnce), and Go's memory model gives NO visibility guarantee
// across goroutines without a happens-before edge — an unsynchronized read could
// observe an arbitrarily stale channel reference forever, silently breaking the
// wake (this exact bug shipped in the first version of this file and was caught by
// timing a real long-poll against a real submit, not by code review).
type waker struct {
	mu sync.Mutex
	ch chan struct{}
}

func newWaker() *waker {
	return &waker{ch: make(chan struct{})}
}

// Wait returns the CURRENT channel, which closes on the next Broadcast call.
func (w *waker) Wait() <-chan struct{} {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.ch
}

// Broadcast wakes every current waiter and prepares the next generation.
func (w *waker) Broadcast() {
	w.mu.Lock()
	old := w.ch
	w.ch = make(chan struct{})
	w.mu.Unlock()
	close(old)
}

// startTaskWakeListener holds one dedicated pool connection LISTENing on
// cx_task_available for the life of ctx, broadcasting on every notification. It
// never returns while ctx is live: a dropped connection (network blip, Postgres
// restart) is logged and retried after a short backoff rather than left silently
// dead — a wedged listener would make claimWithWait fall back to its 250ms safety
// net forever, which is degraded (not broken), but should still be visible in logs.
// Run this in its own goroutine; it blocks until ctx is cancelled.
func startTaskWakeListener(ctx context.Context, pool *pgxpool.Pool) {
	backoff := time.Second
	const maxBackoff = 30 * time.Second
	for {
		if ctx.Err() != nil {
			return
		}
		if err := listenOnce(ctx, pool); err != nil && ctx.Err() == nil {
			log.Printf("notify: LISTEN cx_task_available: %v (retrying in %s)", err, backoff)
			select {
			case <-ctx.Done():
				return
			case <-time.After(backoff):
			}
			if backoff *= 2; backoff > maxBackoff {
				backoff = maxBackoff
			}
			continue
		}
		backoff = time.Second // a clean run (ctx cancelled) resets backoff; unreachable on real drop
	}
}

// listenOnce acquires one connection, issues LISTEN, and blocks on
// WaitForNotification until ctx is cancelled or the connection errors. The
// connection is held OUTSIDE the pool's normal rotation for its entire lifetime
// (Acquire without a matching Release until this function returns) — a LISTENing
// connection cannot be safely reused for ordinary pooled queries in between.
func listenOnce(ctx context.Context, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()

	if _, err := conn.Exec(ctx, "LISTEN cx_task_available"); err != nil {
		return err
	}
	log.Print("notify: listening for cx_task_available")
	for {
		if _, err := conn.Conn().WaitForNotification(ctx); err != nil {
			return err
		}
		taskWake.Broadcast()
	}
}
