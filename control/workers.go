package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"sync"
	"testing"
	"time"
)

// workers.go — the background loops that turn the job lifecycle from "rows in a
// table" into real, self-healing motion. Several tickers, all bound to a context
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
//     (SSRF-guarded + HMAC-signed; see deliverWebhook) with retries, then flagged
//     delivered.
//   - straggler-hedge (30s): a running primary past the hedge window gets one
//     duplicate copy on a distinct same-class peer so a slow worker cannot stall
//     the job tail.
//   - ledger-reconcile (15m): released supplier credits are audited against actual
//     Stripe transfers and any drift is logged — read-only, never moves money (see
//     reconcile.go).
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
	disputeInterval  = 20 * time.Second // buyer-dispute re-verification + resolution
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

// --- ticker liveness guard ---
//
// A background ticker that silently dies (a panic-free goroutine wedge, a
// deadlocked dependency) is invisible: the process stays "up" while payouts stop
// releasing, stale tasks never requeue, and webhooks never deliver. The liveness
// guard makes that failure observable. Each registered ticker records the wall
// time of its last SUCCESSFUL run (fn returned nil); the readiness probe and a
// hand-rolled metric expose how stale each one is, and /readyz fails when any
// ticker has not succeeded within a safe multiple of its own interval. This is
// the BLACKHOLE rule applied to the background loops: a wedged ticker surfaces
// loudly instead of rotting silently.

// staleMultiple is how many intervals a ticker may miss before it is judged
// stale. A ticker that runs every interval should succeed roughly every interval;
// 3× absorbs a slow DB / a transient error cycle or two without false alarms,
// while still catching a genuinely wedged loop within a few minutes.
const staleMultiple = 3

// tickerLiveness records, per ticker, its configured interval and the time of its
// last successful run. It is the shared source of truth for the readiness probe
// and the cx_ticker_seconds_since_success metric. Concurrency-safe: tick goroutines
// write last-success while the probe/metric read all entries.
type tickerLiveness struct {
	mu      sync.RWMutex
	entries map[string]*tickerStat
}

type tickerStat struct {
	interval    time.Duration
	lastSuccess time.Time // zero until the first successful run
}

// liveness is the package-global ticker registry. One value because the tickers
// are process-wide, mirroring how metrics is a package-global counter set.
var liveness = &tickerLiveness{entries: map[string]*tickerStat{}}

// register declares a ticker (name + interval) with no success yet. Called before
// the loop starts so the probe/metric can see a ticker that has never run as
// "never succeeded" rather than "absent". A duplicate name reuses its entry.
func (l *tickerLiveness) register(name string, interval time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.entries[name]; !ok {
		l.entries[name] = &tickerStat{interval: interval}
	}
}

// markSuccess records that name's fn just returned nil at t.
func (l *tickerLiveness) markSuccess(name string, t time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if e, ok := l.entries[name]; ok {
		e.lastSuccess = t
	}
}

// stale reports the names of every ticker that has not succeeded within
// staleMultiple × its interval as of now. A ticker that has NEVER succeeded is
// stale once staleMultiple × interval has elapsed since `since` (the process /
// loop start), so a just-started process is not instantly unready but a loop
// that never runs is caught. Returned names are the offenders, for the probe body.
func (l *tickerLiveness) stale(now, since time.Time) []string {
	l.mu.RLock()
	defer l.mu.RUnlock()
	var bad []string
	for name, e := range l.entries {
		budget := time.Duration(staleMultiple) * e.interval
		ref := e.lastSuccess
		if ref.IsZero() {
			ref = since // never succeeded → measure staleness from the loop start
		}
		if now.Sub(ref) > budget {
			bad = append(bad, name)
		}
	}
	return bad
}

// snapshot returns, per ticker, seconds since its last successful run as of now
// (seconds since `since` when it has never succeeded). Backs the metric.
func (l *tickerLiveness) snapshot(now, since time.Time) map[string]float64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make(map[string]float64, len(l.entries))
	for name, e := range l.entries {
		ref := e.lastSuccess
		if ref.IsZero() {
			ref = since
		}
		out[name] = now.Sub(ref).Seconds()
	}
	return out
}

// workersStartedAt is the loop start time the never-succeeded staleness budget is
// measured from. Set once when Run launches; the readiness probe reads it so a
// freshly-booted process is not judged stale before a ticker has had a chance to run.
var workersStartedAt = time.Now()

// Run launches the background tickers and blocks until ctx is cancelled. Each tick
// runs in the foreground of its own goroutine (ticks never overlap themselves),
// and ctx cancellation stops all promptly. Every ticker is registered with the
// liveness guard up front so a never-ran loop is observable, not merely absent.
func (wk *Workers) Run(ctx context.Context) {
	workersStartedAt = time.Now()
	tickers := []struct {
		interval time.Duration
		name     string
		fn       func(context.Context) error
	}{
		{payoutInterval, "payout-release", wk.releasePayouts},
		{staleInterval, "stale-requeue", wk.requeueStaleTasks},
		{webhookInterval, "webhook-sweep", wk.sweepAndDeliver},
		{hedgeInterval, "straggler-hedge", wk.hedgeStragglers},
		{reconcileInterval, "ledger-reconcile", wk.reconcileLedger},
		{disputeInterval, "dispute-resolve", wk.resolveDisputes},
	}
	for _, t := range tickers {
		liveness.register(t.name, t.interval)
		go wk.tick(ctx, t.interval, t.name, t.fn)
	}
	<-ctx.Done()
	log.Print("workers: shutting down")
}

// tick runs fn every d until ctx is done. A fn error is logged, never fatal —
// one bad cycle must not kill the loop. A successful run (fn returned nil) records
// the ticker's last-success time so the liveness guard can detect a wedged loop.
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
				continue
			}
			liveness.markSuccess(name, time.Now())
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
		// e.ID is the released supplier-credit row's id: the stable, unique payout
		// key the rail uses for idempotency. Distinct credits (distinct ids) never
		// collide even at identical cents; a retried release of the same row reuses
		// its id, so a genuine retry stays a no-op.
		ref, serr := wk.payout.Send(ctx, e.SupplierID, e.AmountUSD, e.ID.String())
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
		// Exponential backoff by prior retries (1m, 2m, 4m, 8m, 16m capped) so a
		// systematically-broken worker isn't re-handed the same task on a tight loop.
		shift := t.RetryCount
		if shift > 4 {
			shift = 4
		}
		backoff := staleBackoff << uint(shift)
		if rerr := wk.store.RequeueStaleTask(ctx, t.ID, backoff); rerr != nil {
			return rerr
		}
		log.Printf("workers: requeued stale task %s (retry %d, backoff %s)", t.ID, t.RetryCount+1, backoff)
	}
	return nil
}

// resolveDisputes drives the buyer-dispute lifecycle — the local execution of optimistic
// verification (docs/PRODUCTION_AUDIT.md §2.3). For each open dispute it dispatches an
// INDEPENDENT re-verification of the disputed job's primary result, reusing the proven
// redundancy path: a DIFFERENT same-class supplier re-runs the chunk (InsertTiebreakTask),
// and the existing verifier compares + clawbacks on a real mismatch. The dispute is then
// resolved off that OBJECTIVE verdict — upheld if the original was clawed back, rejected if
// the re-run agreed. When no distinct same-class supplier is free it surfaces 'no_peer'
// (the honest boundary: re-verification needs a second supplier, like cross-machine
// redundancy) and retries on a later tick. It NEVER moves money itself — clawback/refund
// flow only through the existing verifier on a confirmed mismatch. (The OPTIMIZED resolver
// — operator-level bisection, Verde/TAO — remains the frontier; this is the full-rerun baseline.)
func (wk *Workers) resolveDisputes(ctx context.Context) error {
	active, err := wk.store.ActiveDisputes(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, d := range active {
		switch d.Status {
		case "open", "no_peer":
			target, ok, terr := wk.store.ReverifyTarget(ctx, d.JobID)
			if terr != nil {
				return terr
			}
			if !ok {
				// No completed primary result to re-verify (e.g. the job never finished).
				if serr := wk.store.SetDisputeStatus(ctx, d.ID, "unresolvable"); serr != nil {
					return serr
				}
				continue
			}
			peer, perr := wk.store.SelectRedundancyPeerExcluding(ctx, target.JobType, target.ModelRef, target.MinMemGB, target.AnchorWorker, nil)
			if perr != nil {
				// No distinct same-class supplier free → surface the boundary, retry later.
				if serr := wk.store.SetDisputeStatus(ctx, d.ID, "no_peer"); serr != nil {
					return serr
				}
				continue
			}
			reverifyID, ierr := wk.store.InsertTiebreakTask(ctx, d.JobID, target.TaskID, peer, target.InputRef, target.ChunkIndex)
			if ierr != nil {
				return ierr
			}
			if serr := wk.store.SetDisputeReverifying(ctx, d.ID, reverifyID); serr != nil {
				return serr
			}
			_ = wk.store.InsertJobEvent(ctx, d.JobID, nil, "dispute_reverifying", "Dispute: independent re-verification dispatched", nil)
		case "reverifying":
			pending, perr := wk.store.JobHasPendingTasks(ctx, d.JobID)
			if perr != nil {
				return perr
			}
			if pending {
				continue // wait until the re-verify (and any cascaded tiebreak) settles
			}
			verdict, text := "rejected", "Dispute rejected: independent re-verification agreed with the original result"
			if target, ok, terr := wk.store.ReverifyTarget(ctx, d.JobID); terr != nil {
				return terr
			} else if ok {
				clawed, cerr := wk.store.TaskHasClawback(ctx, target.TaskID)
				if cerr != nil {
					return cerr
				}
				if clawed {
					verdict, text = "resolved", "Dispute upheld: independent re-verification found the original result was wrong (clawed back)"
				}
			}
			if serr := wk.store.SetDisputeStatus(ctx, d.ID, verdict); serr != nil {
				return serr
			}
			_ = wk.store.InsertJobEvent(ctx, d.JobID, nil, "dispute_"+verdict, text, nil)
		}
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
// does not block delivery). Two hardening steps run before the POST:
//
//   - SSRF guard: the destination host is resolved and the POST is refused if ANY
//     resolved address is loopback / private / link-local / unspecified. A buyer
//     webhook is an arbitrary buyer-supplied URL; without this it could be aimed at
//     an internal service (metadata endpoint, the DB, a neighbour) and the control
//     plane would dutifully POST to it. A blocked host fails the delivery (not
//     retried — re-resolving the same name will block again) and is surfaced.
//   - HMAC signature: when CX_WEBHOOK_SECRET is set, an "X-CX-Signature" header
//     ("t=<unix>,v1=<hex>", the Stripe-like scheme this codebase already verifies in
//     verifyStripeSig) lets the buyer authenticate the body. Without the secret the
//     POST still goes out unsigned and the skip is logged honestly — never silently.
//
// Returns an error only after all attempts fail (or the guard refuses the host).
func (wk *Workers) deliverWebhook(ctx context.Context, p PendingWebhook) error {
	if err := guardWebhookHost(ctx, p.URL); err != nil {
		return err // do NOT retry: an SSRF-blocked / unresolvable host stays blocked
	}

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
	sig := signWebhook(body) // "" when CX_WEBHOOK_SECRET is unset (skip logged once below)
	if sig == "" {
		log.Printf("workers: webhook %s for job %s sent UNSIGNED (CX_WEBHOOK_SECRET unset)", p.ID, p.JobID)
	}

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
		if sig != "" {
			req.Header.Set("X-CX-Signature", sig)
		}
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

// signWebhook returns the "X-CX-Signature" value for body: "t=<unix>,v1=<hex>",
// where the hex is HMAC-SHA256(CX_WEBHOOK_SECRET, "<t>.<body>"). This is the exact
// Stripe-like scheme verifyStripeSig already verifies on the inbound side, so a
// buyer can reuse one HMAC verifier for both directions. Returns "" when the secret
// is unset (the caller logs the skip and sends unsigned — never a faked signature).
func signWebhook(body []byte) string {
	secret := os.Getenv("CX_WEBHOOK_SECRET")
	if secret == "" {
		return ""
	}
	t := strconv.FormatInt(time.Now().Unix(), 10)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(t + "." + string(body)))
	return "t=" + t + ",v1=" + hex.EncodeToString(mac.Sum(nil))
}

// guardWebhookHost refuses a webhook destination that resolves to a non-public
// address, the SSRF defence for buyer-supplied URLs. It parses the URL, rejects a
// non-http(s) scheme, resolves the host, and fails if ANY resolved IP is loopback,
// private (10/8, 172.16/12, 192.168/16), link-local (incl. the 169.254 cloud
// metadata range), or unspecified — so a buyer webhook can never be aimed at an
// internal service. All resolved addresses must be public: a name that maps to even
// one internal IP is refused (a DNS-rebinding attempt cannot smuggle one through).
//
// The block is relaxed only when private destinations are explicitly allowed
// (allowPrivateWebhookHosts): an operator opt-in (CX_WEBHOOK_ALLOW_PRIVATE) for a
// trusted internal receiver, and the in-process test harness (testing.Testing()),
// whose httptest receivers bind to loopback. The shipped server binary has neither,
// so it stays locked down by default — the guard is real, never faked off.
func guardWebhookHost(ctx context.Context, raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("webhook url parse: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("webhook url scheme %q not allowed", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return errors.New("webhook url has no host")
	}
	if allowPrivateWebhookHosts() {
		return nil
	}
	ips, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return fmt.Errorf("webhook host resolve %q: %w", host, err)
	}
	if len(ips) == 0 {
		return fmt.Errorf("webhook host %q resolved to no addresses", host)
	}
	for _, ip := range ips {
		if isInternalIP(ip.IP) {
			return fmt.Errorf("webhook host %q resolves to non-public address %s (refused: SSRF guard)", host, ip.IP)
		}
	}
	return nil
}

// allowPrivateWebhookHosts reports whether webhook delivery to private/loopback
// destinations is permitted. False in the shipped server (secure by default); true
// under `go test` (the in-process httptest harness uses loopback) or when an
// operator sets CX_WEBHOOK_ALLOW_PRIVATE for a deliberately-internal receiver.
func allowPrivateWebhookHosts() bool {
	return testing.Testing() || os.Getenv("CX_WEBHOOK_ALLOW_PRIVATE") != ""
}

// isInternalIP reports whether ip is in a range a buyer webhook must never reach:
// loopback, RFC1918 private, link-local (unicast + the 169.254/multicast metadata
// range), or the unspecified address. Public addresses return false.
func isInternalIP(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified()
}
