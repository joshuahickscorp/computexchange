package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"golang.org/x/sync/errgroup"
)

// workers.go — the background loops that turn the job lifecycle from "rows in a
// table" into real, self-healing motion. Several tickers, all bound to a context
// cancelled at shutdown:
//
//   - payout-release (60s): held supplier credits past their hold window are sent
//     via the Payout rail; definitely-unsent failures become operator-rearmable
//     `ready`, ambiguous responses remain `outcome_unknown` and retry only with
//     the same bounded-lifetime idempotency key, and exact cash facts become
//     `released` with the rail reference.
//   - stale-task requeue (30s): tasks claimed but never committed past a timeout
//     are pushed back to the queue with backoff, up to maxRetries, then failed
//     (settled at delivered work; completed chunks stay charged, the rest was
//     never charged — see failJobAndSettleOnce).
//   - job finalization (20s): jobs whose tasks are all done are merged and settled.
//   - webhook delivery (20s): terminal-job callbacks run through an independent,
//     leased, SSRF-guarded, HMAC-signed outbox with durable backoff.
//   - straggler-hedge (30s): a running primary past the hedge window gets one
//     duplicate copy on a distinct same-class peer so a slow worker cannot stall
//     the job tail.
//   - stuck-reaper (30s): the stuck-run watchdog's escalation ladder — a job past
//     its deadline with no progress is RESCUED on the first strike (unfinished
//     tasks requeued to a different machine) and KILLED on a repeat (checkpoint,
//     cancel, settle at completed work). See reapStuckJobs.
//   - dead-claim-rescue (30s): a running task whose claiming worker stopped
//     heartbeating is requeued immediately (dead machine = fast rescue, mildest
//     supplier dock on catalogue work). See rescueDeadClaims.
//   - ledger-reconcile (15m): released supplier credits are audited against actual
//     Stripe transfers and any drift is logged — read-only, never moves money (see
//     reconcile.go).
//   - charge-collect (60s): the money-truth collection sweep — retries attempting
//     charge batches under their frozen amount + stable idempotency key, forms new
//     per-buyer batches of sub-threshold deferred jobs, routes watchdog/fail-settled
//     terminal jobs into collection, backs off failed single-job charges, and
//     backfills real stripe_fee ledger rows (see collect.go).
//
// Every failure is logged, never swallowed; nothing here pretends success.

// Workers holds the dependencies the background loops need.
type Workers struct {
	store        *Store
	storage      *Storage
	payout       Payout
	client       *http.Client
	verification *VerificationProcessor
}

// NewWorkers wires the background-loop dependencies.
func NewWorkers(store *Store, storage *Storage, payout Payout) *Workers {
	return &Workers{
		store:        store,
		storage:      storage,
		payout:       payout,
		client:       newWebhookHTTPClient(),
		verification: NewVerificationProcessor(store, storage, NewVerifier(store).WithStorage(storage)),
	}
}

// loop tuning. Kept as named constants so the cadence is visible in one place.
const (
	payoutInterval     = 60 * time.Second
	payoutSendingLease = 5 * time.Minute
	// Stripe may prune idempotency keys after 24 hours. Stop automatic POST
	// resolution before that boundary; older unknowns require read-only/provider
	// evidence rather than risking a second transfer.
	payoutIdempotencyRetryWindow = 23 * time.Hour
	staleInterval                = 30 * time.Second
	pinnedTiebreakInterval       = 15 * time.Second
	pinnedTiebreakTimeout        = 2 * time.Minute
	finalizationInterval         = 20 * time.Second
	webhookInterval              = 20 * time.Second
	webhookDeliveryLease         = 2 * time.Minute
	webhookDeliveryConcurrency   = 8
	webhookDeliveryBatch         = 16 // 16/8 × 10s timeout stays below the 60s readiness window
	webhookDeliveryMaxAttempts   = 12
	hedgeInterval                = 30 * time.Second
	disputeInterval              = 20 * time.Second // buyer-dispute re-verification + resolution
	staleTaskTimeout             = 30 * time.Minute // claim older than this with no commit → stale
	staleBackoff                 = 1 * time.Minute  // delay before a requeued task is visible
	maxTaskRetries               = 3                // requeue this many times before failing
	sweepBatch                   = 100              // max rows handled per tick
	// budgetStopInterval (Control Plane Hot Path 7->8, docs/internal/
	// CREED_AND_PATH_TO_TEN.md "Move markBudgetStoppedJobs off the claim path onto
	// its own ticker (a 5-10s cadence is plenty)"): the Budget Governor's
	// paused_for_budget state-transition + one-time event sweep, formerly run
	// inside ClaimTask's transaction on every single claim. 7s sits in the
	// rung's stated 5-10s window.
	budgetStopInterval = 7 * time.Second
	// Straggler hedging: a running primary older than hedgeAfter (≈2× the
	// targetTaskSecs a chunk is sized for) gets one duplicate copy on a second
	// worker. Hedge sparingly — at most hedgeMaxInFlight per job and hedgeBatch
	// per tick — so hedging speeds the tail without doubling the whole fleet's load.
	hedgeAfter       = 90 * time.Second // 2 × ~45s target per-task time
	hedgeMaxInFlight = 4                // concurrent hedges per job
	hedgeBatch       = 20               // max new hedges per tick
	// Endgame racing (Speed Lane wave 1B, planner.go / raceEndgameTails): once a
	// job has ZERO unclaimed tasks left, its wall-clock IS the slowest running
	// chunk — so idle same-class spare capacity should duplicate that chunk
	// IMMEDIATELY, not after the general 90s hedge window. The sweep runs on its
	// own short cadence (the endgame is exactly when seconds matter, and the
	// candidate query is cheap — it prefilters to jobs with an empty queue);
	// endgameRaceMinRun is a small floor so a chunk that just started — and will
	// finish in a few seconds anyway — is not duplicated for nothing. Caps are
	// SHARED with hedging (hedgeMaxInFlight per job, hedgeBatch per tick, the
	// same one-hedge-per-chunk guard in SQL), so racing can never double-spend
	// the fleet beyond what hedging was already allowed.
	endgameRaceInterval = 5 * time.Second
	endgameRaceMinRun   = 10 * time.Second
	// Throttled-worker hedging (docs/internal/CREED_AND_PATH_TO_TEN.md, "Thermal
	// sustained-vs-peak throughput on fanless Apple Silicon" 7→8): a task whose
	// claiming worker's OWN most recent heartbeat reports throttled=true (memory
	// pressure OR a live sustained-throughput drop the agent detected mid-task —
	// see runners.rs's LiveThroughputMonitor) is hedged after this MUCH shorter
	// floor instead of waiting out the full hedgeAfter — a worker demonstrably
	// throttling right now is a stronger, earlier signal than plain elapsed time.
	// Never zero: a task that started a heartbeat-tick ago must get at least one
	// real chance to make progress before being duplicated.
	hedgeThrottledAfter = 15 * time.Second
	// Stuck-run watchdog: a running job past its deadline (buyer deadline_secs when
	// set, else stuckEtaFactor × the predicted eta_secs floored at eta+120s, else the
	// 24h wall-clock cap — see StuckRunningJobs) with NO task progress — no commit,
	// no fresh claim, no freshly-scheduled retry — within stuckProgressGrace is
	// judged STUCK, not slow. The watchdog is a REGULATOR with an escalation ladder,
	// not a kill switch: the FIRST verdict rescues (unfinished tasks requeued to a
	// different machine, watchdog_strikes → 1), a REPEAT verdict kills (completed
	// chunks checkpointed, the rest cancelled, the job settled at what actually ran).
	// Grace is 2 × hedgeAfter so hedging always gets its chance to rescue a
	// straggler before the watchdog rules.
	stuckInterval      = 30 * time.Second
	stuckEtaFactor     = 1.5
	stuckProgressGrace = 2 * hedgeAfter
	// Worker-liveness attribution: a running task whose claiming worker has neither
	// heartbeated nor held its claim for less than deadWorkerAfter is on a DEAD
	// machine — a certainty worth acting on immediately (per-task rescue, no job
	// strike), instead of waiting out the 30-min stale reaper. Heartbeats arrive
	// ~every 30s, so 180s is six missed beats: sleep, crash, or network loss, not jitter.
	deadWorkerAfter = 180 * time.Second
	// telemetryRetentionInterval + workerMemorySampleRetention (docs/
	// CREED_AND_PATH_TO_TEN.md, "Postgres data lifecycle" 3→4): the highest-write-
	// rate table in the schema (one row per worker per memory-reporting heartbeat)
	// had no retention at all before this — every real reader only ever looks at
	// the most recent memSampleWindow=20 rows per worker, so a 14-day window is
	// generous headroom above anything a real query needs. Once an hour is plenty
	// for a prune, not a hot-path operation.
	telemetryRetentionInterval  = 1 * time.Hour
	workerMemorySampleRetention = 14 * 24 * time.Hour
	// taskDurationRetention (docs/CREED_AND_PATH_TO_TEN.md, "Postgres data
	// lifecycle" 4→5): task_durations is pure internal telemetry (the drift/ETA
	// rollup), so it gets the same short window as worker_memory_samples.
	taskDurationRetention = 30 * 24 * time.Hour
	// jobEventRetention is deliberately much longer: job_events is buyer-VISIBLE
	// history (GET /v1/jobs/{id}/events), not telemetry — a buyer auditing an old
	// job may reasonably look months later.
	jobEventRetention = 180 * 24 * time.Hour
	// partitionRotationInterval (docs/internal/CREED_AND_PATH_TO_TEN.md, "Postgres data
	// lifecycle" 6->7): the cadence of the partition lifecycle job
	// (Store.RotateTelemetryPartitions) that CREATEs upcoming month partitions and
	// DROPs expired ones for the three now-partitioned telemetry tables. The unit of
	// work is a whole calendar month, so nothing needs doing more than a few times a
	// day; six-hourly is generous headroom that keeps a create-ahead month ready long
	// before any real insert could need it and drops an expired month within hours of
	// it becoming droppable, while being a rare, cheap metadata operation — never a
	// hot-path cost. (The hourly DELETE sweep still trims the sub-month tail; this job
	// removes whole expired months in O(1).)
	partitionRotationInterval = 6 * time.Hour
)

// telemetryTables is every table sweepTelemetryRetention prunes, in a fixed
// display order — backs cx_telemetry_table_rows (Postgres Data Lifecycle 8->9).
var telemetryTables = []string{"worker_memory_samples", "task_durations", "job_events"}

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
	// failures counts every returned error from this ticker's fn, lifetime (not
	// reset on a later success). Postgres Data Lifecycle 8->9
	// (docs/internal/CREED_AND_PATH_TO_TEN.md): the staleness gauge above answers
	// "has this loop stopped entirely", but a ticker that fails on every run and
	// gets rescheduled just inside its stale budget would never trip that alert —
	// this answers the different, narrower question "is this specific sweep
	// actually succeeding", which is what a retention-job failure alert needs.
	failures int64
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

// markFailure records that name's fn just returned a non-nil error.
func (l *tickerLiveness) markFailure(name string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if e, ok := l.entries[name]; ok {
		e.failures++
	}
}

// failureSnapshot returns, per ticker, the lifetime count of failed runs. Backs
// cx_ticker_failures_total.
func (l *tickerLiveness) failureSnapshot() map[string]int64 {
	l.mu.RLock()
	defer l.mu.RUnlock()
	out := make(map[string]int64, len(l.entries))
	for name, e := range l.entries {
		out[name] = e.failures
	}
	return out
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

// workersStartedAtNano is the loop start time (unix nanos) the never-succeeded
// staleness budget is measured from. Written once when Run launches, read by the
// readiness probe and the metrics handler — which run on OTHER goroutines, so the
// value is an atomic (a bare time.Time write racing an HTTP-handler read is a data
// race the race detector rightly flags). Zero until Run runs — the watchdog sweeps
// read that zero as "not the real server" (tests drive them directly) and skip
// their startup grace, while a real process gets one observation window after
// downtime before judging anything stalled.
var workersStartedAtNano atomic.Int64

// workersStarted returns the loop start time, or the zero time before Run ran.
func workersStarted() time.Time {
	n := workersStartedAtNano.Load()
	if n == 0 {
		return time.Time{}
	}
	return time.Unix(0, n)
}

// Run launches the background tickers and blocks until ctx is cancelled. Each tick
// runs in the foreground of its own goroutine (ticks never overlap themselves),
// and ctx cancellation stops all promptly. Every ticker is registered with the
// liveness guard up front so a never-ran loop is observable, not merely absent.
func (wk *Workers) Run(ctx context.Context) {
	workersStartedAtNano.Store(time.Now().UnixNano())
	tickers := []struct {
		interval time.Duration
		name     string
		fn       func(context.Context) error
	}{
		{payoutInterval, "payout-release", wk.releasePayouts},
		{verificationRecoveryInterval, "verification-recovery", wk.recoverVerification},
		{pinnedTiebreakInterval, "pinned-tiebreak-recovery", wk.recoverPinnedTiebreaks},
		{staleInterval, "stale-requeue", wk.requeueStaleTasks},
		// Finalization and outbound delivery have independent failure/liveness
		// domains: a slow result merge or finalization backlog must not starve an
		// already-due webhook outbox page.
		{finalizationInterval, "job-finalize", wk.finalizeJobs},
		{webhookInterval, "webhook-sweep", wk.deliverPendingWebhooks},
		{hedgeInterval, "straggler-hedge", wk.hedgeStragglers},
		// Speed Lane wave 1B: the endgame race — duplicate the slowest running
		// chunks onto idle warm same-class capacity the moment a job's queue
		// empties, instead of waiting out the 90s hedge (raceEndgameTails).
		{endgameRaceInterval, "endgame-race", wk.raceEndgameTails},
		{stuckInterval, "stuck-reaper", wk.reapStuckJobs},
		{stuckInterval, "dead-claim-rescue", wk.rescueDeadClaims},
		{reconcileInterval, "ledger-reconcile", wk.reconcileLedger},
		{disputeInterval, "dispute-resolve", wk.resolveDisputes},
		{chargeCollectInterval, "charge-collect", wk.collectCharges},
		{telemetryRetentionInterval, "telemetry-retention", wk.sweepTelemetryRetention},
		{partitionRotationInterval, "partition-rotation", wk.rotateTelemetryPartitions},
		{budgetStopInterval, "budget-stop-sweep", wk.sweepBudgetStops},
		// End-to-End Latency 8.5->9 (latency_watchdog.go): the class-aware no-peer
		// watchdog — escapes a task wedged on a heartbeating worker with no eligible
		// same-class peer off the 30-minute stale-reaper path.
		{noPeerWatchdogInterval, "no-peer-watchdog", wk.reapNoPeerWedged},
	}
	var loops sync.WaitGroup
	loops.Add(len(tickers))
	for _, ticker := range tickers {
		t := ticker
		liveness.register(t.name, t.interval)
		go func() {
			defer loops.Done()
			wk.tick(ctx, t.interval, t.name, t.fn)
		}()
	}
	<-ctx.Done()
	log.Print("workers: shutting down; waiting for active sweeps")
	loops.Wait()
	log.Print("workers: all sweeps stopped")
}

const verificationRecoveryInterval = 2 * time.Second

func (wk *Workers) recoverVerification(ctx context.Context) error {
	return wk.verification.Drain(ctx, sweepBatch)
}

// recoverPinnedTiebreaks repairs a pre-claimed third-opinion task whose chosen
// peer never started it. It never drops the pin into the general queue: the
// replacement is selected through the same same-class, distinct-worker and
// distinct-supplier path as the original tiebreak, then revalidated by the
// transactional CAS. With no eligible replacement the existing row is left
// untouched and the next tick retries when independent supply appears.
func (wk *Workers) recoverPinnedTiebreaks(ctx context.Context) error {
	stale, err := wk.store.StalePinnedTiebreaks(ctx, pinnedTiebreakTimeout, sweepBatch)
	if err != nil {
		return err
	}
	for _, item := range stale {
		_, also, alsoSuppliers, err := wk.store.PinnedTiebreakExclusions(ctx, item)
		if err != nil {
			return err
		}
		// The generic redundancy selector deliberately knows only live runtime/class
		// eligibility. A job can impose additional claim-time gates (residency,
		// trusted tier, private pool, payout floor, budget). ReassignPinnedTiebreak
		// checks all of them transactionally. If the highest-ranked class peer fails
		// one, exclude that worker and try the next candidate in this same sweep;
		// otherwise every tick would choose the same attractive-but-unclaimable peer.
		candidates, err := wk.store.CandidateWorkers(ctx, item.JobType, item.ModelRef, item.MinMemoryGB)
		if err != nil {
			return err
		}
		excludedWorkers := make(map[uuid.UUID]bool, len(also)+1)
		for _, id := range also {
			excludedWorkers[id] = true
		}
		excludedWorkers[item.PinnedWorker] = true
		excludedSuppliers := make(map[uuid.UUID]bool, len(alsoSuppliers))
		for _, id := range alsoSuppliers {
			excludedSuppliers[id] = true
		}
		frozenCandidates := make([]MatchWorker, 0, len(candidates))
		for _, candidate := range candidates {
			if excludedWorkers[candidate.ID] || excludedSuppliers[candidate.SupplierID] ||
				candidate.HWClass != item.HWClass ||
				(item.Engine != "" && candidate.Engine != item.Engine) ||
				(item.BuildHash != "" && candidate.BuildHash != item.BuildHash) {
				continue
			}
			frozenCandidates = append(frozenCandidates, candidate)
		}
		for _, candidate := range rankPeersBySpeed(frozenCandidates, item.JobType) {
			peer := candidate.ID
			reassigned, err := wk.store.ReassignPinnedTiebreak(ctx, item, peer, pinnedTiebreakTimeout)
			if errors.Is(err, ErrNoSupply) {
				continue
			}
			if err != nil {
				return err
			}
			if reassigned {
				log.Printf("workers: reassigned stale pinned tiebreak %s for job %s from %s to %s",
					item.TaskID, item.JobID, item.PinnedWorker, peer)
			}
			break // reassigned, or another recovery/claim won the CAS
		}
	}
	return nil
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
				liveness.markFailure(name)
				continue
			}
			liveness.markSuccess(name, time.Now())
		}
	}
}

// releasePayouts attempts to send every supplier credit whose hold has expired.
// ClaimPayout first reserves exact cents from the causally linked canonical buyer
// collection (or an explicit capped subsidy pool), then CAS-claims held->sending
// with a durable operation. An unfunded/partially funded row becomes explicit
// awaiting_funding debt and no provider call occurs. A clawback can therefore
// either win before the provider
// call or leave a crossed/unknown transfer visibly reversal_required; completion
// can never overwrite it. `ready` remains an explicit operator-owned state (the
// admin release action re-arms it to held) so definitely-unsent failures are not
// hammered every minute.
func (wk *Workers) releasePayouts(ctx context.Context) error {
	finishAttempt := func(claimed DueHeldEntry, resolvingUnknown bool) error {
		result, sendErr := wk.payout.Send(ctx, claimed.SupplierID, claimed.RequestedCents,
			claimed.Currency, claimed.ID.String())
		if sendErr != nil {
			var state string
			var stateErr error
			if resolvingUnknown || !errors.Is(sendErr, errPayoutDefinitelyNotSent) {
				state, stateErr = wk.store.MarkPayoutOutcomeUnknown(ctx, claimed.ID, sendErr)
			} else {
				state, stateErr = wk.store.DeferPayout(ctx, claimed.ID, sendErr)
			}
			if stateErr != nil {
				return stateErr
			}
			log.Printf("workers: payout %s unresolved in state %s ($%.6f to %s): %v",
				claimed.ID, state, claimed.AmountUSD, claimed.SupplierID, sendErr)
			return nil
		}
		state, err := wk.store.FinalizePayout(ctx, claimed.ID, result)
		if err != nil {
			return err
		}
		switch state {
		case PayoutReleased:
			metrics.payoutsReleased.Add(1)
		case PayoutReversalRequired:
			log.Printf("workers: payout %s crossed the provider boundary during clawback; external reversal required (not implemented)", claimed.ID)
		case PayoutExported:
			log.Printf("workers: payout %s exported for manual settlement; no supplier cash is reported moved", claimed.ID)
		}
		return nil
	}

	// A process can die after the durable held->sending claim and before (or after)
	// the provider response. Expired work becomes outcome_unknown, never ready.
	if _, err := wk.store.RecoverStalePayoutOperations(ctx, payoutSendingLease, sweepBatch); err != nil {
		return err
	}
	unknown, err := wk.store.ClaimOutcomeUnknownPayouts(
		ctx, payoutSendingLease, payoutIdempotencyRetryWindow, sweepBatch)
	if err != nil {
		return err
	}
	for _, claimed := range unknown {
		if err := finishAttempt(claimed, true); err != nil {
			return err
		}
	}
	due, err := wk.store.DuePayouts(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, e := range due {
		claimed, ok, cerr := wk.store.ClaimPayout(ctx, e.ID)
		if cerr != nil {
			return cerr
		}
		if !ok {
			continue // clawback or another release worker won the CAS
		}
		// e.ID is the supplier-credit row's stable, unique payout
		// key the rail uses for idempotency. Distinct credits (distinct ids) never
		// collide even at identical cents; a retried release of the same row reuses
		// its id, so a genuine retry stays a no-op.
		if err := finishAttempt(claimed, false); err != nil {
			return err
		}
	}
	return nil
}

// requeueStaleTasks pushes tasks that were claimed but never committed past the
// timeout back onto the queue with a backoff, incrementing retry_count. Past
// maxTaskRetries a task is permanently failed and its job settled at the completed
// work (delivered chunks stay charged, un-run work was never charged), with the
// completed chunks checkpointed into output_ref BEFORE the flip.
func (wk *Workers) requeueStaleTasks(ctx context.Context) error {
	stale, err := wk.store.StaleRunningTasks(ctx, staleTaskTimeout, sweepBatch)
	if err != nil {
		return err
	}
	for _, t := range stale {
		if int(t.RetryCount) >= maxTaskRetries {
			// Merge-before-mark on the fail path: checkpoint what completed so the
			// buyer keeps it (best-effort — a merge failure never blocks the fail).
			checkpointBeforeFail(ctx, wk.store, wk.storage, t.JobID)
			if ferr := wk.store.FailTaskAndSettleJob(ctx, t.ID, t.JobID); ferr != nil {
				return ferr
			}
			log.Printf("workers: task %s failed after %d retries (job %s settled at completed work)", t.ID, t.RetryCount, t.JobID)
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
			peer, perr := wk.store.SelectRedundancyPeerExcluding(ctx, target.JobType, target.ModelRef, target.MinMemGB, target.AnchorWorker, nil, nil)
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
	stragglers, err := wk.store.StragglerTasks(ctx, hedgeAfter, hedgeThrottledAfter, hedgeMaxInFlight, hedgeBatch)
	if err != nil {
		return err
	}
	for _, s := range stragglers {
		// End-to-End Latency 8->8.5 (latency_watchdog.go, "Prevent the cold-model hedge
		// storm"): a fresh worker's FIRST task on an uncached model is slow because it
		// is downloading a multi-gigabyte GGUF inside the claimed task — hedging it to a
		// second, likely also-cold worker just doubles that download for nothing. When
		// the straggler's slowness is an expected cold load (worker does not yet report
		// the model warm, task still inside coldModelLoadAllowance), suppress this first
		// hedge. NEVER suppress a THROTTLED-worker hedge: that is a LIVE distress signal
		// (memory pressure / measured throughput drop), not a cold load, so it must still
		// hedge on its short floor. The suppression is bounded — once past the allowance,
		// the same straggler hedges normally, and the no-peer watchdog still guards it —
		// so a genuinely wedged cold-model worker is delayed, never shielded forever.
		if !s.ThrottledHedge {
			cold, cerr := wk.store.isColdModelStraggler(ctx, s.TaskID)
			if cerr != nil {
				return cerr
			}
			if cold {
				log.Printf("workers: cold-model straggler task %s (chunk %d of job %s, model %s) — suppressing spurious hedge (worker %s still loading an uncached model, not wedged)", s.TaskID, s.ChunkIndex, s.JobID, s.ModelRef, s.WorkerID)
				metrics.coldModelHedgesSuppressed.Add(1)
				continue
			}
		}
		representedSuppliers, rerr := wk.representedChunkSuppliers(ctx, s.JobID, s.ChunkIndex)
		if rerr != nil {
			return rerr
		}
		peer, perr := wk.store.SelectRedundancyPeerExcluding(ctx, s.JobType, s.ModelRef, s.MinMemGB,
			s.WorkerID, nil, representedSuppliers)
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
		if s.ThrottledHedge {
			// Distinct counter (docs/internal/CREED_AND_PATH_TO_TEN.md, "Thermal
			// sustained-vs-peak throughput on fanless Apple Silicon" 7→8): lets an
			// operator see how often a LIVE throttle signal — not just elapsed
			// time — is the thing that actually triggered a hedge.
			metrics.throttledHedges.Add(1)
			log.Printf("workers: hedged THROTTLED-worker straggler task %s (chunk %d of job %s) to peer %s (worker %s reporting throttled=true)", s.TaskID, s.ChunkIndex, s.JobID, peer, s.WorkerID)
		} else {
			log.Printf("workers: hedged straggler task %s (chunk %d of job %s) to peer %s", s.TaskID, s.ChunkIndex, s.JobID, peer)
		}
	}
	return nil
}

// raceEndgameTails is the ENDGAME RACE sweep (Speed Lane wave 1B — the tail
// half of the fan-out planner, planner.go). When a job has zero unclaimed
// tasks, at least one running task, and idle eligible same-class capacity
// exists, the slowest running chunks are duplicated onto the FASTEST idle warm
// peers immediately — the buyer's wall-clock at that point is exactly the
// straggling chunk, and the pre-wave machinery would sit on its hands for the
// full 90s hedgeAfter window (reactive-only tail: hedge 90s → no-peer watchdog
// 5min → stale reaper 30min). Everything downstream is the PROVEN hedge
// machinery reused verbatim: InsertHedgeTask pins the duplicate (pre-claimed,
// pinned poll branch), first-commit-wins cancels the loser
// (CancelStragglerSiblings — now in both directions, see its PATCH note), and
// the merge dedupes per chunk (JobMergeInputs DISTINCT ON) — no second dedupe
// invented. Cold-model suppression is respected on the straggler side
// (isColdModelStraggler — a chunk slow because its worker is still cold-loading
// must not be raced; the duplicate would just wait out the same load
// elsewhere), and SelectEndgameRacePeer refuses cold peers on the dispatch
// side, so a cold load is never raced from either end. Gated on
// fanoutPlannerEnabled (CX_DISABLE_FANOUT_PLANNER reverts to the exact
// pre-wave tail behavior — also the L2 proof's A/B switch).
func (wk *Workers) raceEndgameTails(ctx context.Context) error {
	if !fanoutPlannerEnabled.Load() {
		return nil
	}
	tails, err := wk.store.EndgameTailTasks(ctx, endgameRaceMinRun, hedgeMaxInFlight, hedgeBatch)
	if err != nil {
		return err
	}
	for _, s := range tails {
		cold, cerr := wk.store.isColdModelStraggler(ctx, s.TaskID)
		if cerr != nil {
			return cerr
		}
		if cold {
			log.Printf("workers: endgame race: task %s (chunk %d of job %s, model %s) is a cold-model straggler — not raced (duplicate would pay the same cold load)", s.TaskID, s.ChunkIndex, s.JobID, s.ModelRef)
			continue
		}
		representedSuppliers, rerr := wk.representedChunkSuppliers(ctx, s.JobID, s.ChunkIndex)
		if rerr != nil {
			return rerr
		}
		peer, perr := wk.selectEndgameRacePeerExcluding(ctx, s.JobType, s.ModelRef, s.MinMemGB,
			s.WorkerID, representedSuppliers)
		if errors.Is(perr, ErrNoSupply) {
			continue // no idle warm same-class peer — the ordinary hedge path still guards this chunk
		}
		if perr != nil {
			return perr
		}
		if _, ierr := wk.store.InsertHedgeTask(ctx, s.JobID, s.TaskID, peer, s.InputRef, s.ChunkIndex); ierr != nil {
			return ierr
		}
		metrics.hedges.Add(1) // an endgame race IS a hedge dispatch (same machinery, same cap)
		// Speed Lane wave 1B: also count the endgame-race SUBSET of that hedge, at
		// the exact same event site as metrics.hedges above — i.e. only once the
		// duplicate is REALLY inserted (InsertHedgeTask returned no error), never on
		// a candidate merely considered (cold-model skip and no-idle-peer skip both
		// `continue` above without reaching here). cx_hedges_total cannot tell an
		// operator whether a hedge came from the ordinary 90s straggler path or from
		// the seconds-matter endgame race; this splits that out.
		metrics.endgameRaces.Add(1)
		log.Printf("workers: endgame race: duplicated slowest running task %s (chunk %d of job %s, type %s) onto idle fastest same-class peer %s — zero unclaimed tasks left, worker %s is the job's wall-clock (fan-out planner wave 1B)",
			s.TaskID, s.ChunkIndex, s.JobID, s.JobType, peer, s.WorkerID)
	}
	return nil
}

// representedChunkSuppliers returns every supplier already attached to execution
// of a buyer chunk. Current task projections cover running/completed and pinned
// work, verification_work freezes committed identities across retries, and history
// preserves failed tiebreak executions whose task projection was cleared. A hedge
// selector passes this complete set to prunePeers so it cannot duplicate work onto
// a supplier that already owns a redundancy/tiebreak vote for the chunk.
//
// This is one READ COMMITTED statement with a deterministic result order. A claim
// that starts strictly after this snapshot is a future execution, not evidence the
// selector ignored; supplier-normalized voting remains the authoritative race-safe
// settlement fence in either ordering.
func (wk *Workers) representedChunkSuppliers(ctx context.Context, jobID uuid.UUID, chunkIndex int) ([]uuid.UUID, error) {
	rows, err := wk.store.pool.Query(ctx, `
		SELECT DISTINCT supplier_id
		  FROM (
		    SELECT t.execution_supplier_id AS supplier_id
		      FROM tasks t
		     WHERE t.job_id=$1 AND COALESCE(t.chunk_index,0)=$2
		       AND NOT COALESCE(t.is_honeypot,false)
		       AND t.execution_supplier_id IS NOT NULL
		    UNION ALL
		    -- An unstarted pin is live routing state rather than execution history;
		    -- reserve its currently assigned supplier until claim-time revalidation.
		    SELECT w.supplier_id
		      FROM tasks t JOIN workers w ON w.id=t.claimed_by
		     WHERE t.job_id=$1 AND COALESCE(t.chunk_index,0)=$2
		       AND NOT COALESCE(t.is_honeypot,false)
		       AND t.status IN ('queued','retrying') AND t.started_at IS NULL
		    UNION ALL
		    SELECT vw.supplier_id
		      FROM verification_work vw JOIN tasks t ON t.id=vw.task_id
		     WHERE t.job_id=$1 AND COALESCE(t.chunk_index,0)=$2
		       AND NOT COALESCE(t.is_honeypot,false)
		    UNION ALL
		    SELECT history.supplier_id
		      FROM task_execution_history history JOIN tasks t ON t.id=history.task_id
		     WHERE t.job_id=$1 AND COALESCE(t.chunk_index,0)=$2
		       AND NOT COALESCE(t.is_honeypot,false)
		  ) represented
		 ORDER BY supplier_id`, jobID, chunkIndex)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var suppliers []uuid.UUID
	for rows.Next() {
		var supplierID uuid.UUID
		if err := rows.Scan(&supplierID); err != nil {
			return nil, err
		}
		suppliers = append(suppliers, supplierID)
	}
	return suppliers, rows.Err()
}

// selectEndgameRacePeerExcluding preserves SelectEndgameRacePeer's idle+warm
// latency gates while additionally excluding suppliers already represented on the
// chunk. The ordinary hedge path can pass that exclusion through the existing
// selector directly; the specialized endgame selector has no exclusion parameter,
// so the production sweep performs the same pure ranking here.
func (wk *Workers) selectEndgameRacePeerExcluding(ctx context.Context, jobType, modelRef string,
	minMemGB float32, anchor uuid.UUID, representedSuppliers []uuid.UUID) (uuid.UUID, error) {
	primary, err := wk.store.GetWorkerProfile(ctx, anchor)
	if err != nil {
		return uuid.Nil, err
	}
	candidates, err := wk.store.CandidateWorkers(ctx, jobType, modelRef, minMemGB)
	if err != nil {
		return uuid.Nil, err
	}
	pruned := prunePeers(candidates, anchor, primary.SupplierID, nil, representedSuppliers)
	ranked, err := Match(MatchTask{
		JobType: jobType, MinMemoryGB: minMemGB,
		HWClasses: []string{primary.HWClass}, PinEngine: primary.Engine,
		PinBuildHash: primary.BuildHash, Tier: "batch",
	}, pruned)
	if err != nil {
		return uuid.Nil, err
	}
	ids := make([]uuid.UUID, len(ranked))
	for i, worker := range ranked {
		ids[i] = worker.ID
	}
	busy, err := wk.store.BusyWorkerIDs(ctx, ids)
	if err != nil {
		return uuid.Nil, err
	}
	idle := make([]MatchWorker, 0, len(ranked))
	for _, worker := range ranked {
		if busy[worker.ID] || (modelRef != "" && !worker.Warm) {
			continue
		}
		idle = append(idle, worker)
	}
	if len(idle) == 0 {
		return uuid.Nil, ErrNoSupply
	}
	return rankPeersBySpeed(idle, jobType)[0].ID, nil
}

// reapStuckJobs is the stuck-run watchdog — a REGULATOR with an escalation
// ladder, not a kill switch. A running job past its deadline (buyer deadline_secs
// when set, else the floored ETA multiple, else the 24h cap — StuckRunningJobs
// owns the geometry) with no task progress within stuckProgressGrace escalates
// one rung per verdict:
//
//   - STRIKE 0 → RESCUE: every unfinished task is requeued for a different
//     machine (claim cleared, small visibility backoff, retry_count untouched —
//     the stall is not the task's fault), watchdog_strikes flips to 1, and the
//     buyer is told honestly what happened and what happens next.
//   - STRIKE 1+ → KILL: checkpoint first (merge-before-mark, same discipline as
//     completion — a failed merge leaves the job untouched for retry, never
//     cancel-then-lose), cancel the job + its unfinished tasks (CancelStuckJob),
//     settle actual_usd at the completed work (per-task charges settle at commit,
//     so the un-run remainder was never charged and completed tasks stay charged —
//     the suppliers earned them), and hand the buyer presigned URLs to any
//     mid-chunk partial checkpoint documents the agents uploaded (never merged
//     into the verified artifact, never money — just not lost).
//
// Both rungs are idempotent under concurrent sweeps: the strike flip and the
// cancel are guarded UPDATEs, and the loser of either race touches nothing.
func (wk *Workers) reapStuckJobs(ctx context.Context) error {
	// Startup grace: right after control-plane downtime every claim and heartbeat
	// looks ancient — the plane simply was not there to observe them — so every
	// running job would be falsely judged stalled. Observe for one grace window
	// before judging. workersStarted() is zero when a test drives this sweep
	// directly (Run never ran), so tests are unaffected.
	if ws := workersStarted(); !ws.IsZero() && time.Since(ws) < stuckProgressGrace {
		return nil
	}
	stuck, err := wk.store.StuckRunningJobs(ctx, stuckEtaFactor, stuckProgressGrace, deadWorkerAfter, sweepBatch)
	if err != nil {
		return err
	}
	for _, j := range stuck {
		// Attribution for the buyer text + logs: a dead claiming worker means the
		// MACHINE is stuck; a live-but-silent one makes the workload the suspect.
		cause := "the workload made no progress"
		if j.DeadClaim {
			cause = "the machine running it went unresponsive"
		}

		if j.Strikes == 0 {
			// RESCUE (strike 0): move the unfinished work, warn the buyer, arm the ladder.
			flipped, rerr := wk.store.RescueStuckJob(ctx, j.ID, staleBackoff)
			if rerr != nil {
				log.Printf("workers: rescuing stuck job %s: %v", j.ID, rerr)
				continue
			}
			if !flipped {
				continue // progressed, went terminal, or a concurrent sweep already rescued
			}
			msg := fmt.Sprintf(
				"Run stalled past its deadline (%s); unfinished work was moved to a different machine. "+
					"If it stalls again it will be cancelled with completed work checkpointed.", cause)
			_ = wk.store.InsertJobEvent(ctx, j.ID, nil, "job_stuck_rescued", msg, nil)
			metrics.stuckRescues.Add(1)
			log.Printf("workers: stuck job %s rescued (strike 1 · %d/%d tasks done · dead_claim=%v)",
				j.ID, j.TasksDone, j.TaskCount, j.DeadClaim)
			continue
		}

		// KILL (strike >= 1): checkpoint, cancel, settle, tell the buyer.
		if j.OutputRef != "" && j.TasksDone > 0 {
			if _, merr := mergeJobResults(ctx, wk.store, wk.storage, j.ID); merr != nil {
				log.Printf("workers: stuck job %s: checkpoint merge failed: %v (left running for retry)", j.ID, merr)
				continue // do NOT cancel work we could not checkpoint
			}
		}
		flipped, cerr := wk.store.CancelStuckJob(ctx, j.ID)
		if cerr != nil {
			log.Printf("workers: cancelling stuck job %s: %v", j.ID, cerr)
			continue
		}
		if !flipped {
			continue // progressed or went terminal since selection — leave it alone
		}
		// Settle at what actually ran (sum of per-task charges on completed tasks).
		if serr := wk.store.SetJobActualUSD(ctx, j.ID); serr != nil {
			log.Printf("workers: settling stuck job %s actual_usd: %v", j.ID, serr)
		}
		msg := fmt.Sprintf(
			"Run stuck · stalled past its deadline with no progress (%s) and was cancelled automatically. "+
				"You are charged only for the %d of %d tasks that completed; the rest was never charged. "+
				"Completed results are checkpointed and downloadable.",
			cause, j.TasksDone, j.TaskCount)
		_ = wk.store.InsertJobEvent(ctx, j.ID, nil, "job_stuck_cancelled", msg, wk.stuckPartialDetail(ctx, j.ID))
		metrics.stuckCancels.Add(1)
		log.Printf("workers: stuck job %s cancelled (strike %d · %d/%d tasks done · eta %ds · dead_claim=%v · grace %s)",
			j.ID, j.Strikes, j.TasksDone, j.TaskCount, j.EtaSecs, j.DeadClaim, stuckProgressGrace)
	}
	return nil
}

// stuckPartialDetail checks each cancelled primary task's "<result_key>.partial"
// object — the agent's optional mid-chunk checkpoint document (shared wire
// contract: TaskDispatch.partial_put_url) — and returns a job_events detail JSON
// {"partial_urls": [...]} of presigned GET URLs for the ones that actually exist,
// so the buyer can retrieve mid-chunk progress from a killed run. Partial objects
// are NEVER merged into the verified artifact and NEVER affect money — unverified
// work is not paid and not verified; the URLs only keep it from being lost.
// Returns nil (no detail) when none exist; every stat/presign failure is logged,
// never papered over as "no partials".
func (wk *Workers) stuckPartialDetail(ctx context.Context, jobID uuid.UUID) []byte {
	keys, err := wk.store.CancelledTaskResultKeys(ctx, jobID)
	if err != nil {
		log.Printf("workers: stuck job %s: listing cancelled result keys: %v (no partial_urls detail)", jobID, err)
		return nil
	}
	var urls []string
	for _, k := range keys {
		pk := k + ".partial"
		exists, serr := wk.storage.ObjectExists(ctx, pk)
		if serr != nil {
			log.Printf("workers: stuck job %s: checking partial %q: %v", jobID, pk, serr)
			continue
		}
		if !exists {
			continue
		}
		u, perr := wk.storage.PresignGet(ctx, pk, time.Hour)
		if perr != nil {
			log.Printf("workers: stuck job %s: presigning partial %q: %v", jobID, pk, perr)
			continue
		}
		urls = append(urls, u)
	}
	if len(urls) == 0 {
		return nil
	}
	detail, _ := json.Marshal(map[string]any{"partial_urls": urls})
	return detail
}

// rescueDeadClaims is the worker-liveness rescue: a running task whose claiming
// worker has been silent past deadWorkerAfter (and whose claim is at least that
// old) sits on a DEAD machine — waiting for its commit is hopeless, so the task is
// requeued immediately (rescue mechanics: claim cleared, small backoff, no retry
// burned, and NO job strike — the job did nothing wrong) instead of aging into the
// 30-min stale reaper or dragging its whole job to the watchdog. For catalogue
// work (job_type <> 'custom') the wedged worker's supplier takes the MILDEST
// reputation dock (DockReputationMild — never a quarantine from here): a machine
// that goes dark mid-claim is a soft reliability signal, and custom jobs are
// exempt because their buyer-defined runtime proves nothing about the machine.
func (wk *Workers) rescueDeadClaims(ctx context.Context) error {
	// Startup grace — the same reasoning as reapStuckJobs: after control-plane
	// downtime every heartbeat looks ancient because nobody was listening. One
	// observation window before judging any machine dead. A zero workersStarted()
	// (tests driving the sweep directly) skips the grace.
	if ws := workersStarted(); !ws.IsZero() && time.Since(ws) < stuckProgressGrace {
		return nil
	}
	dead, err := wk.store.DeadClaimedTasks(ctx, deadWorkerAfter, sweepBatch)
	if err != nil {
		return err
	}
	for _, d := range dead {
		rescued, rerr := wk.store.RescueRunningTask(ctx, d.TaskID, staleBackoff)
		if rerr != nil {
			return rerr
		}
		if !rescued {
			continue // committed/failed between selection and here — leave it alone
		}
		taskID := d.TaskID
		_ = wk.store.InsertJobEvent(ctx, d.JobID, &taskID, "task_rescued_dead_worker",
			"Chunk moved to a different machine: the machine running it stopped responding. "+
				"No retry was counted against the task.", nil)
		if d.JobType != "custom" && d.SupplierID != uuid.Nil {
			if derr := wk.store.DockReputationMild(ctx, d.SupplierID, EventThermalThrottle); derr != nil {
				log.Printf("workers: docking supplier %s for dead claim on task %s: %v", d.SupplierID, d.TaskID, derr)
			}
		}
		log.Printf("workers: rescued task %s (job %s) from dead worker %s", d.TaskID, d.JobID, d.WorkerID)
	}
	return nil
}

// checkpointBeforeFail attempts the partial checkpoint merge for a job that is
// about to be terminally failed — the same merge-before-mark discipline the
// watchdog's kill path uses, so a buyer keeps every delivered chunk even when the
// job dies on the fail path. Strictly best-effort: no output_ref or no completed
// chunks is a clean no-op, and a lookup/merge failure is LOGGED and the fail
// proceeds — a failed job must not be blocked forever on a merge.
func checkpointBeforeFail(ctx context.Context, store *Store, storage *Storage, jobID uuid.UUID) {
	outputRef, done, err := store.JobCheckpointInfo(ctx, jobID)
	if err != nil {
		log.Printf("fail-path checkpoint: job %s lookup: %v (proceeding with the fail)", jobID, err)
		return
	}
	if outputRef == "" || done == 0 {
		return // nowhere to write, or nothing completed to checkpoint
	}
	if _, err := mergeJobResults(ctx, store, storage, jobID); err != nil {
		log.Printf("fail-path checkpoint: job %s merge: %v (proceeding with the fail — completed chunks remain per-task objects)", jobID, err)
	}
}

// recordEtaCalibration feeds the watchdog's calibration loop at a finalize site:
// one eta_calibration row pairing the submit-time prediction with the realized
// wall-clock, plus the cx_watchdog_near_miss_total counter when the job finished
// but LATE (realized > 1.2 × predicted) — exactly the observations that tune
// stuckEtaFactor, with no value fabricated (a job without a prediction records
// nothing). Best-effort: a calibration failure is logged and never fails the
// finalize it rides on.
func recordEtaCalibration(ctx context.Context, store *Store, jobID uuid.UUID) {
	predicted, realized, err := store.RecordEtaCalibration(ctx, jobID)
	if err != nil {
		log.Printf("eta calibration for job %s: %v (finalize unaffected)", jobID, err)
		return
	}
	if predicted > 0 && float64(realized) > 1.2*float64(predicted) {
		metrics.watchdogNearMiss.Add(1)
	}
}

// webhookPayload is the JSON body POSTed on job completion.
type webhookPayload struct {
	DeliveryID string `json:"delivery_id"`
	Event      string `json:"event"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
	ResultsURL string `json:"results_url"`
	TS         string `json:"ts"`
}

// sweepAndDeliver is the direct-call lifecycle helper retained for integration
// proofs. Production runs finalizeJobs and deliverPendingWebhooks as independent
// tickers, so a slow merge cannot starve already-due outbound deliveries.
func (wk *Workers) sweepAndDeliver(ctx context.Context) error {
	if err := wk.finalizeJobs(ctx); err != nil {
		return err
	}
	return wk.deliverPendingWebhooks(ctx)
}

// finalizeJobs completes newly-finished jobs. Finalization is merge-THEN-mark:
// the buyer-ready artifact is written first, and only a successful merge permits
// the terminal state + settlement. A merge failure remains retryable.
func (wk *Workers) finalizeJobs(ctx context.Context) error {
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
		if cerr := wk.store.CompleteJobEconomics(ctx, j.ID); cerr != nil {
			log.Printf("workers: completing/settling job %s: %v", j.ID, cerr)
			continue
		}
		// Close the buyer-visible timeline (Plane C/D). Best-effort.
		_ = wk.store.InsertJobEvent(ctx, j.ID, nil, "job_completed", "Job completed; results ready", nil)
		// Feed the ETA calibration loop (predicted vs realized; best-effort).
		recordEtaCalibration(ctx, wk.store, j.ID)
	}
	return nil
}

// deliverPendingWebhooks claims one bounded due page and attempts each row once.
// Durable leases, backoff, dead-letter state, and a concurrency limit isolate
// poison/slow destinations from both later rows and job finalization.
func (wk *Workers) deliverPendingWebhooks(ctx context.Context) error {
	pending, err := wk.store.ClaimPendingWebhooks(ctx, webhookDeliveryBatch, webhookDeliveryLease)
	if err != nil {
		return err
	}
	var deliveries errgroup.Group
	deliveries.SetLimit(webhookDeliveryConcurrency)
	for _, pendingWebhook := range pending {
		p := pendingWebhook
		deliveries.Go(func() error {
			derr := wk.deliverWebhook(ctx, p)
			if derr == nil {
				if err := wk.store.MarkWebhookDelivered(ctx, p.ID, p.LeaseToken); err != nil {
					return fmt.Errorf("marking webhook %s delivered: %w", p.ID, err)
				}
				return nil
			}

			nextAttempt := p.Attempts + 1
			backoff := webhookRetryBackoff(nextAttempt)
			attempts, dead, err := wk.store.MarkWebhookFailed(
				ctx, p.ID, p.LeaseToken, derr, webhookFailureIsPermanent(derr),
				backoff, webhookDeliveryMaxAttempts,
			)
			if err != nil {
				return fmt.Errorf("recording webhook %s failure: %w", p.ID, err)
			}
			if dead {
				log.Printf("workers: webhook %s for job %s dead-lettered after %d failed attempt(s): %v",
					p.ID, p.JobID, attempts, derr)
			} else {
				log.Printf("workers: webhook %s for job %s attempt %d failed; retry in %s: %v",
					p.ID, p.JobID, attempts, backoff, derr)
			}
			return nil
		})
	}
	return deliveries.Wait()
}

// deliverWebhook performs exactly one outbox attempt. Retry/backoff/dead-letter
// state is durable in webhooks; keeping retries out of this call prevents one slow
// endpoint from holding a worker goroutine for several consecutive timeouts. A
// signed results URL is included when one can be minted (best-effort: its absence
// does not block delivery). Three hardening steps apply to the POST:
//
//   - the transport resolves once, rejects every non-public answer, then dials only
//     that frozen set while preserving the registered Host/TLS SNI;
//   - redirects are refused, so a public endpoint cannot bounce the signed payload
//     or its result URL to a private address or a different origin;
//   - per-registration HMAC signature: an "X-CX-Signature" header
//     ("t=<unix>,v1=<hex>", the Stripe-like scheme this codebase already verifies in
//     verifyStripeSig) lets the buyer authenticate the body without sharing a key
//     with another buyer. Missing/unopenable legacy keys fail before any network I/O.
//
// Success is at-least-once: a crash after the receiver accepts but before
// MarkWebhookDelivered commits causes a replay with the same delivery_id.
func (wk *Workers) deliverWebhook(ctx context.Context, p PendingWebhook) error {
	secret, err := openWebhookSigningSecret(p.SigningSecretSealed)
	if err != nil {
		return permanentWebhookFailure(fmt.Errorf("webhook signing secret unavailable: %w", err))
	}
	var resultsURL string
	if keys, err := wk.store.JobResultKeys(ctx, p.JobID); err == nil && len(keys) > 0 {
		if u, perr := wk.storage.PresignGet(ctx, keys[0], time.Hour); perr == nil {
			resultsURL = u
		}
	}
	// Event mirrors the job's terminal status: "job.completed" stays verbatim for
	// complete jobs (the value existing consumers already match on), while failed
	// and stuck-cancelled jobs report themselves honestly as job.failed /
	// job.cancelled instead of borrowing the completion event. A cancelled job
	// still gets a results_url when any chunk completed (best-effort above) — and
	// its checkpoint artifact sits at output_ref, merged before the watchdog cancels.
	event := "job." + p.Status
	if p.Status == "complete" {
		event = "job.completed"
	}
	body, _ := json.Marshal(webhookPayload{
		DeliveryID: p.ID.String(),
		Event:      event,
		JobID:      p.JobID.String(),
		Status:     p.Status,
		ResultsURL: resultsURL,
		TS:         time.Now().UTC().Format(time.RFC3339),
	})
	sig := signWebhook(secret, body)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.URL, bytes.NewReader(body))
	if err != nil {
		return permanentWebhookFailure(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-CX-Delivery-ID", p.ID.String())
	req.Header.Set("X-CX-Signature", sig)
	resp, err := wk.client.Do(req)
	if err != nil {
		if resp != nil && resp.Body != nil {
			resp.Body.Close()
		}
		return err
	}
	resp.Body.Close()
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	statusErr := fmt.Errorf("webhook endpoint returned %s", resp.Status)
	if webhookHTTPStatusIsRetryable(resp.StatusCode) {
		return statusErr
	}
	return permanentWebhookFailure(statusErr)
}

// sweepTelemetryRetention prunes worker_memory_samples rows older than
// workerMemorySampleRetention (docs/CREED_AND_PATH_TO_TEN.md, "Postgres data
// lifecycle" 3→4 — the first production DELETE this codebase has ever had on its
// highest-write-rate table). Logs the real row count pruned so an operator can see
// this is actually running and actually shrinking the table, not just ticking.
func (wk *Workers) sweepTelemetryRetention(ctx context.Context) error {
	n, err := wk.store.DeleteOldWorkerMemorySamples(ctx, time.Now().Add(-workerMemorySampleRetention))
	if err != nil {
		return err
	}
	if n > 0 {
		log.Printf("workers: telemetry-retention: pruned %d worker_memory_samples row(s) older than %s", n, workerMemorySampleRetention)
	}
	// task_durations (docs/CREED_AND_PATH_TO_TEN.md, "Postgres data lifecycle"
	// 4→5): the other unbounded append-only table the audit named, alongside
	// worker_memory_samples above.
	td, err := wk.store.DeleteOldTaskDurations(ctx, time.Now().Add(-taskDurationRetention))
	if err != nil {
		return err
	}
	if td > 0 {
		log.Printf("workers: telemetry-retention: pruned %d task_durations row(s) older than %s", td, taskDurationRetention)
	}
	// job_events: buyer-visible history, so a much longer window than the two
	// pure-telemetry tables above.
	je, err := wk.store.DeleteOldJobEvents(ctx, time.Now().Add(-jobEventRetention))
	if err != nil {
		return err
	}
	if je > 0 {
		log.Printf("workers: telemetry-retention: pruned %d job_events row(s) older than %s", je, jobEventRetention)
	}
	return nil
}

// rotateTelemetryPartitions runs the partition lifecycle job (Postgres Data Lifecycle
// 6->7, docs/internal/CREED_AND_PATH_TO_TEN.md): for each partitioned telemetry table
// it CREATEs the upcoming month partitions (so a real insert always finds a home) and
// DROPs any month partition whose entire range is older than that table's retention
// window. A dropped partition is an O(1) metadata operation that reclaims a whole
// month's heap + indexes at once — the replacement for the O(rows) DELETE burst the
// retention sweep otherwise pays. Idempotent and cheap: creating an existing partition
// is a no-op, and a quiet table with nothing to drop drops nothing. Logs only when it
// actually created or dropped something, so a healthy steady state stays quiet.
func (wk *Workers) rotateTelemetryPartitions(ctx context.Context) error {
	created, dropped, err := wk.store.RotateTelemetryPartitions(ctx, time.Now())
	if err != nil {
		return err
	}
	if created > 0 || dropped > 0 {
		log.Printf("workers: partition-rotation: created %d upcoming partition(s), dropped %d expired partition(s)", created, dropped)
	}
	return nil
}

// sweepBudgetStops runs the Budget Governor's paused_for_budget state-transition
// sweep (Store.SweepBudgetStops / markBudgetStoppedJobs, control/scheduler.go) on
// its own ticker.
//
// PATCH (Control plane hot path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md "Get
// the correlated-subquery cost out of the transactional hot path"): this used to
// run INSIDE ClaimTask's own transaction, on EVERY single claim attempt (claimed
// or not) — a scan over every capped job in the system, each paying its own
// correlated budget-projected-spend subquery, on the single hottest transactional
// path in the system. It is now a short, independent transaction on its own
// budgetStopInterval cadence, exactly like every other background sweep in this
// file. The claim's own per-task budget enforcement (a capped job's task is never
// dispatched past its cap) is a SEPARATE, unaffected predicate baked directly into
// ClaimTaskSQL's hard filter — moving this sweep off the hot path changes only how
// promptly the VISIBLE budget_state flips to 'paused_for_budget' and the one-time
// budget_stopped event fires (bounded by budgetStopInterval), never whether an
// over-cap task can be claimed.
func (wk *Workers) sweepBudgetStops(ctx context.Context) error {
	stopped, err := wk.store.SweepBudgetStops(ctx)
	if err != nil {
		return err
	}
	// Advance the counter only after the sweep's own transaction commits (Plane D
	// D21): a rolled-back sweep must never inflate cx_budget_stops_total. The
	// state transition inside markBudgetStoppedJobs's UPDATE is itself the guard
	// against double-counting a job across repeated ticks.
	if stopped > 0 {
		metrics.budgetStops.Add(int64(stopped))
	}
	return nil
}
