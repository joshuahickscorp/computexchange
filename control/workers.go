package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"
)

// workers.go — the background loops that turn the job lifecycle from "rows in a
// table" into real, self-healing motion. Three tickers, all bound to a context
// cancelled at shutdown:
//
//   - payout-release (60s): held supplier credits past their hold window are sent
//     via the Payout rail; the stub errors honestly, so the row is marked 'ready'
//     (NEVER faked as transferred) and retried later; a real transfer marks
//     'released' with the rail reference.
//   - stale-task requeue (30s): tasks claimed but never committed past a timeout
//     are pushed back to the queue with backoff, up to maxRetries, then failed
//     (with a buyer refund).
//   - webhook delivery / job sweep (20s): jobs whose tasks are all done are
//     finalized to 'complete', and registered completion webhooks are POSTed once
//     with retries, then flagged delivered.
//
// Every failure is logged, never swallowed; nothing here pretends success.

// Workers holds the dependencies the background loops need.
type Workers struct {
	store   *Store
	storage *Storage
	payout  Payout
	client  *http.Client
}

// NewWorkers wires the background-loop dependencies.
func NewWorkers(store *Store, storage *Storage, payout Payout) *Workers {
	return &Workers{
		store:   store,
		storage: storage,
		payout:  payout,
		client:  &http.Client{Timeout: 10 * time.Second},
	}
}

// loop tuning. Kept as named constants so the cadence is visible in one place.
const (
	payoutInterval   = 60 * time.Second
	staleInterval    = 30 * time.Second
	webhookInterval  = 20 * time.Second
	hedgeInterval    = 30 * time.Second
	staleTaskTimeout = 30 * time.Minute // claim older than this with no commit → stale
	staleBackoff     = 1 * time.Minute  // delay before a requeued task is visible
	maxTaskRetries   = 3                // requeue this many times before failing
	sweepBatch       = 100              // max rows handled per tick
	// Straggler hedging: a running primary older than hedgeAfter (≈2× the
	// targetTaskSecs a chunk is sized for) gets one duplicate copy on a second
	// worker. Hedge sparingly — at most hedgeMaxInFlight per job and hedgeBatch
	// per tick — so hedging speeds the tail without doubling the whole fleet's load.
	hedgeAfter       = 90 * time.Second // 2 × ~45s target per-task time
	hedgeMaxInFlight = 4                // concurrent hedges per job
	hedgeBatch       = 20               // max new hedges per tick
)

// Run launches the three tickers and blocks until ctx is cancelled. Each tick
// runs in the foreground of its own goroutine (ticks never overlap themselves),
// and ctx cancellation stops all three promptly.
func (wk *Workers) Run(ctx context.Context) {
	go wk.tick(ctx, payoutInterval, "payout-release", wk.releasePayouts)
	go wk.tick(ctx, staleInterval, "stale-requeue", wk.requeueStaleTasks)
	go wk.tick(ctx, webhookInterval, "webhook-sweep", wk.sweepAndDeliver)
	go wk.tick(ctx, hedgeInterval, "straggler-hedge", wk.hedgeStragglers)
	<-ctx.Done()
	log.Print("workers: shutting down")
}

// tick runs fn every d until ctx is done. A fn error is logged, never fatal —
// one bad cycle must not kill the loop.
func (wk *Workers) tick(ctx context.Context, d time.Duration, name string, fn func(context.Context) error) {
	t := time.NewTicker(d)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := fn(ctx); err != nil {
				log.Printf("workers: %s: %v", name, err)
			}
		}
	}
}

// releasePayouts attempts to send every supplier credit whose hold has expired.
// The stub Payout always errors; we mark such rows 'ready' (the money is owed and
// queued, not sent) and leave them for a future real rail. A real transfer marks
// the row 'released' with the rail reference. We never write 'released' without a
// reference (BLACKHOLE: surface every failure — no faked transfer).
func (wk *Workers) releasePayouts(ctx context.Context) error {
	due, err := wk.store.DuePayouts(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, e := range due {
		ref, serr := wk.payout.Send(ctx, e.SupplierID, e.AmountUSD)
		if serr != nil {
			// Honest deferral: the hold is over and the credit is owed, but no
			// rail moved money. Mark 'ready' so earnings/audits can see it is
			// queued, and log the reason. Retried next cycle once a rail exists.
			if merr := wk.store.MarkPayout(ctx, e.ID, "ready", ""); merr != nil {
				return merr
			}
			log.Printf("workers: payout %s deferred ($%.6f to %s): %v", e.ID, e.AmountUSD, e.SupplierID, serr)
			continue
		}
		if merr := wk.store.MarkPayout(ctx, e.ID, PayoutReleased, ref); merr != nil {
			return merr
		}
		metrics.payoutsReleased.Add(1)
	}
	return nil
}

// requeueStaleTasks pushes tasks that were claimed but never committed past the
// timeout back onto the queue with a backoff, incrementing retry_count. Past
// maxTaskRetries a task is permanently failed and its job refunded.
func (wk *Workers) requeueStaleTasks(ctx context.Context) error {
	stale, err := wk.store.StaleRunningTasks(ctx, staleTaskTimeout, sweepBatch)
	if err != nil {
		return err
	}
	for _, t := range stale {
		if int(t.RetryCount) >= maxTaskRetries {
			if ferr := wk.store.FailTaskAndRefundJob(ctx, t.ID, t.JobID); ferr != nil {
				return ferr
			}
			log.Printf("workers: task %s failed after %d retries (job %s refunded)", t.ID, t.RetryCount, t.JobID)
			continue
		}
		if rerr := wk.store.RequeueStaleTask(ctx, t.ID, staleBackoff); rerr != nil {
			return rerr
		}
		log.Printf("workers: requeued stale task %s (retry %d)", t.ID, t.RetryCount+1)
	}
	return nil
}

// hedgeStragglers finds running primary tasks that have run past the hedge window
// and dispatches one duplicate copy of each to a distinct same-class peer, so a
// single slow worker cannot stall the job's tail. It hedges sparingly (the store
// query caps per-job in-flight hedges and per-tick volume) and skips a straggler
// when no distinct same-class peer is online (no hedge is better than a fake one).
// "First commit wins": when either copy commits, the commit path cancels the
// sibling (CancelStragglerSiblings) and the merge dedupes per chunk.
func (wk *Workers) hedgeStragglers(ctx context.Context) error {
	stragglers, err := wk.store.StragglerTasks(ctx, hedgeAfter, hedgeMaxInFlight, hedgeBatch)
	if err != nil {
		return err
	}
	for _, s := range stragglers {
		peer, perr := wk.store.SelectRedundancyPeerExcluding(ctx, s.JobType, s.ModelRef, s.MinMemGB, s.WorkerID, nil)
		if errors.Is(perr, ErrNoSupply) {
			continue // no distinct same-class worker free — leave it; the stale reaper still guards it
		}
		if perr != nil {
			return perr
		}
		if _, ierr := wk.store.InsertHedgeTask(ctx, s.JobID, s.TaskID, peer, s.InputRef, s.ChunkIndex); ierr != nil {
			return ierr
		}
		metrics.hedges.Add(1)
		log.Printf("workers: hedged straggler task %s (chunk %d of job %s) to peer %s", s.TaskID, s.ChunkIndex, s.JobID, peer)
	}
	return nil
}

// webhookPayload is the JSON body POSTed on job completion.
type webhookPayload struct {
	Event      string `json:"event"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
	ResultsURL string `json:"results_url"`
	TS         string `json:"ts"`
}

// sweepAndDeliver finalizes newly-complete jobs and then delivers any registered
// completion webhooks for complete/failed jobs not yet delivered, flagging each
// delivered so it fires exactly once. Finalization is merge-THEN-mark: for each
// job whose tasks are all done, the buyer-ready artifact is written to output_ref
// FIRST, and only on a successful merge is the job flipped to complete + settled.
// A merge failure leaves the job non-complete (and logged) so it is retried next
// sweep rather than published as complete with no/short output.
func (wk *Workers) sweepAndDeliver(ctx context.Context) error {
	finalizable, err := wk.store.FinalizableJobs(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, j := range finalizable {
		if j.OutputRef != "" {
			if _, merr := mergeJobResults(ctx, wk.store, wk.storage, j.ID); merr != nil {
				log.Printf("workers: merging job %s results: %v (left non-complete for retry)", j.ID, merr)
				continue // do NOT mark complete on a failed merge
			}
		}
		if cerr := wk.store.MarkJobComplete(ctx, j.ID); cerr != nil {
			log.Printf("workers: marking job %s complete: %v", j.ID, cerr)
			continue
		}
		// Close the buyer-visible timeline (Plane C/D). Best-effort.
		_ = wk.store.InsertJobEvent(ctx, j.ID, nil, "job_completed", "Job completed; results ready", nil)
		if serr := wk.store.SetJobActualUSD(ctx, j.ID); serr != nil {
			log.Printf("workers: settling job %s actual_usd: %v", j.ID, serr)
		}
	}

	pending, err := wk.store.PendingWebhooks(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, p := range pending {
		if derr := wk.deliverWebhook(ctx, p); derr != nil {
			// Logged, not fatal: an unreachable endpoint is the buyer's problem,
			// and we retry on the next sweep until it succeeds (delivered_at stays
			// NULL). We do NOT flag it delivered on failure.
			log.Printf("workers: webhook %s for job %s undelivered: %v", p.ID, p.JobID, derr)
			continue
		}
		if merr := wk.store.MarkWebhookDelivered(ctx, p.ID); merr != nil {
			return merr
		}
	}
	return nil
}

// deliverWebhook POSTs the completion payload with a few retries + backoff. A
// signed results URL is included when one can be minted (best-effort: its absence
// does not block delivery). Returns an error only after all attempts fail.
func (wk *Workers) deliverWebhook(ctx context.Context, p PendingWebhook) error {
	var resultsURL string
	if keys, err := wk.store.JobResultKeys(ctx, p.JobID); err == nil && len(keys) > 0 {
		if u, perr := wk.storage.PresignGet(ctx, keys[0], time.Hour); perr == nil {
			resultsURL = u
		}
	}
	body, _ := json.Marshal(webhookPayload{
		Event:      "job.completed",
		JobID:      p.JobID.String(),
		Status:     p.Status,
		ResultsURL: resultsURL,
		TS:         time.Now().UTC().Format(time.RFC3339),
	})

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.URL, bytes.NewReader(body))
		if err != nil {
			return err // a malformed URL will never succeed; do not retry
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := wk.client.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return nil
		}
		lastErr = errors.New("webhook endpoint returned " + resp.Status)
	}
	return lastErr
}
