package main

import (
	"context"
	"errors"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// latency_watchdog.go — two End-to-End Latency escapes for the paths where a
// 1-second inference genuinely becomes minutes (docs/internal/CREED_AND_PATH_TO_TEN.md,
// "End-to-End Job Latency Decomposition"). Both live in their OWN file with their own
// ticker, deliberately separate from the general stuck-run watchdog in workers.go /
// the claim path in scheduler.go — they target two specific, narrow failure modes the
// existing sweeps do NOT cover:
//
//   8→8.5  Prevent the cold-model hedge storm. A fresh worker's FIRST task on an
//          uncached model spends its opening minutes downloading a multi-gigabyte GGUF
//          INSIDE the claimed task, which trips the 90-second straggler hedge and fans
//          a duplicate out to a SECOND, likely also-cold worker — doubling the cold
//          download for zero benefit. isColdModelStraggler tells the hedge path when a
//          straggler's slowness is an expected cold load (worker does not yet report
//          the model warm, and the task is still inside a cold-load allowance window)
//          so it suppresses that first, spurious hedge rather than storming.
//
//   8.5→9  Close the one real >30-minute path. A wedged-but-heartbeating worker
//          holding a task with NO eligible same-class peer online falls through
//          hedging entirely (hedgeStragglers skips a straggler with no distinct peer)
//          AND through the dead-claim rescue (its worker is still heartbeating, so it
//          is not "dead"), landing on the 30-minute stale reaper as its only escape.
//          reapNoPeerWedged is a shorter, class-aware watchdog specifically for that
//          no-peer case: it requeues such a task after noPeerWatchdogAfter (well under
//          30 minutes) so it can be re-claimed the instant capacity returns — the wedged
//          worker recovering, a same-class peer coming online, or simply a fresh attempt.

const (
	// noPeerWatchdogInterval is how often the no-peer watchdog sweeps. Matched to the
	// straggler-hedge cadence: the two run the same class-peer probe, and the watchdog
	// is the escape hatch for exactly the tasks hedging just declined to act on.
	noPeerWatchdogInterval = 30 * time.Second
	// noPeerWatchdogAfter is how long a running task may be held by a heartbeating
	// worker with NO eligible same-class peer before the watchdog requeues it. It is a
	// small multiple of hedgeAfter (90s): give hedging its full chance first (a
	// straggler that DID have a peer is already being duplicated and is not this
	// watchdog's business), then, if the no-peer straggler is still not making
	// progress, escape it far below the 30-minute staleTaskTimeout that is its only
	// alternative today. 5 minutes is ~3× hedgeAfter — comfortably past any legitimate
	// slow-but-progressing task, comfortably under the half-hour it replaces.
	noPeerWatchdogAfter = 5 * time.Minute
	// coldModelLoadAllowance is how long a straggler on a model its worker does not yet
	// report warm is treated as "still cold-loading", not "wedged" — so the hedge path
	// suppresses its first hedge instead of storming to a second cold worker. Sized to
	// cover a realistic multi-gigabyte GGUF fetch + load: comfortably longer than the
	// 90-second hedgeAfter (the whole point — the download legitimately exceeds it),
	// bounded so a genuinely wedged worker on a cold model is not shielded forever (once
	// past this, the straggler hedges and, failing a peer, the no-peer watchdog above
	// catches it on the normal schedule).
	coldModelLoadAllowance = 6 * time.Minute
)

// isColdModelStraggler reports whether a straggler's slowness is explained by an
// expected cold model load rather than a wedged worker — the signal the hedge path
// uses to suppress the spurious cold-to-cold hedge (End-to-End Latency 8→8.5). It is
// true only when BOTH hold: the job has a real model_ref, the CLAIMING worker does
// NOT currently report that model warm (no fresh worker_model_state row), AND the
// task has been running less than coldModelLoadAllowance (still plausibly inside the
// GGUF download+load, not stuck). A blank model_ref (a job type with no model to
// download) is never cold-shielded. Read-only over worker_model_state — the same
// table + 60s freshness window the scheduler's warm-routing already uses, so "warm"
// here means exactly what it means to the claim path.
//
// This is a HINT that biases hedging, never a correctness gate: if it is wrong (the
// worker really is wedged on a cold model), the straggler simply hedges once the
// coldModelLoadAllowance elapses, and the no-peer watchdog still guards it — the worst
// case is the pre-existing behavior delayed by the allowance, never a lost task.
func (s *Store) isColdModelStraggler(ctx context.Context, taskID uuid.UUID) (bool, error) {
	var cold bool
	err := s.pool.QueryRow(ctx, `
		SELECT
			COALESCE(j.model_ref,'') <> ''
			AND t.started_at IS NOT NULL
			AND t.started_at > now() - make_interval(secs => $2)
			AND NOT EXISTS (
				SELECT 1 FROM worker_model_state wms
				WHERE wms.worker_id = t.worker_id
				  AND wms.model_id = j.model_ref
				  AND wms.last_seen_warm > now() - interval '60 seconds'
			)
		FROM tasks t JOIN jobs j ON j.id = t.job_id
		WHERE t.id = $1`,
		taskID, coldModelLoadAllowance.Seconds()).Scan(&cold)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil // task vanished (committed/cancelled between find and check) — not cold, not an error
	}
	return cold, err
}

// NoPeerWedged is a running task past noPeerWatchdogAfter whose claiming worker is
// still heartbeating (so the dead-claim rescue will not touch it) and which the
// caller has confirmed has no eligible same-class peer. Carries the identity the
// watchdog needs to requeue it and log the escape.
type NoPeerWedged struct {
	TaskID   uuid.UUID
	JobID    uuid.UUID
	WorkerID uuid.UUID
	JobType  string
	ModelRef string
	MinMemGB float32
}

// NoPeerWedgedCandidates finds the tasks the no-peer watchdog might rescue: running
// PRIMARY tasks (not honeypot, not redundancy, not themselves a hedge) that have run
// past `after`, whose job is still running, and whose claiming worker is STILL
// HEARTBEATING (last_seen_at within deadAfter — a genuinely dead worker's task is the
// dead-claim rescue's job, not this watchdog's). It deliberately does NOT itself
// decide "no peer" — that is the class-aware SelectRedundancyPeerExcluding probe the
// caller runs per candidate, exactly as hedgeStragglers does, so the watchdog and the
// hedge path can never disagree about what "eligible same-class peer" means. Ordered
// oldest-start first, capped at limit.
func (s *Store) NoPeerWedgedCandidates(ctx context.Context, after, deadAfter time.Duration, limit int) ([]NoPeerWedged, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.job_id, t.worker_id, j.job_type, COALESCE(j.model_ref,''),
		        COALESCE(j.min_memory_gb,0)
		 FROM tasks t
		 JOIN jobs j    ON j.id = t.job_id
		 JOIN workers w ON w.id = t.worker_id
		 WHERE t.status = 'running'
		   AND t.is_honeypot = false AND t.is_redundancy = false
		   AND t.hedged_from IS NULL
		   AND t.started_at IS NOT NULL
		   AND t.started_at < now() - make_interval(secs => $1)
		   AND j.status = 'running'
		   -- worker is STILL heartbeating: a dead worker's task belongs to the
		   -- dead-claim rescue, not this watchdog (that path docks + requeues faster).
		   AND w.last_seen_at IS NOT NULL
		   AND w.last_seen_at > now() - make_interval(secs => $2)
		 ORDER BY t.started_at ASC
		 LIMIT $3`,
		after.Seconds(), deadAfter.Seconds(), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []NoPeerWedged
	for rows.Next() {
		var n NoPeerWedged
		if err := rows.Scan(&n.TaskID, &n.JobID, &n.WorkerID, &n.JobType, &n.ModelRef, &n.MinMemGB); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

// reapNoPeerWedged is the class-aware no-peer watchdog ticker (End-to-End Latency
// 8.5→9). For each candidate (a running straggler held by a live worker), it runs the
// SAME class-peer probe hedging uses: if a distinct same-class peer IS online, this is
// not a no-peer wedge — hedging will (or already did) duplicate it, so the watchdog
// leaves it alone. Only when the probe returns ErrNoSupply — genuinely no eligible
// peer — does the watchdog requeue the task, unclaiming it from the wedged worker and
// making it visible immediately so any returning capacity can pick it up, decades
// before the 30-minute stale reaper would have. Idempotent under concurrent sweeps
// (RequeueTask flips the row's status/claim; a second sweep finds it no longer
// 'running'). A per-task error is logged and skipped so one bad row never stalls the
// rest; a query-level failure surfaces as the tick's error for the liveness guard.
func (wk *Workers) reapNoPeerWedged(ctx context.Context) error {
	// Startup grace mirrors the other watchdogs: right after control-plane start,
	// workers have not re-heartbeated and peers legitimately look absent, so a task
	// in flight across a restart must not be misjudged as no-peer-wedged. A test
	// driving the sweep directly (workersStarted() zero) skips the grace.
	if started := workersStarted(); !started.IsZero() && time.Since(started) < deadWorkerAfter {
		return nil
	}
	candidates, err := wk.store.NoPeerWedgedCandidates(ctx, noPeerWatchdogAfter, deadWorkerAfter, sweepBatch)
	if err != nil {
		return err
	}
	for _, c := range candidates {
		// The exact same class-peer probe hedgeStragglers uses. A found peer means
		// hedging owns this straggler (not the watchdog); only ErrNoSupply — no
		// distinct, independent same-class worker free — is the no-peer wedge.
		_, perr := wk.store.SelectRedundancyPeerExcluding(ctx, c.JobType, c.ModelRef, c.MinMemGB, c.WorkerID, nil, nil)
		if perr == nil {
			continue // a peer exists → hedging handles it, not this watchdog
		}
		if !errors.Is(perr, ErrNoSupply) {
			return perr // a real error probing supply — surface it, don't misread as no-peer
		}
		// Genuinely no eligible same-class peer: escape the task off the 30-minute
		// stale-reaper path by requeueing it now (unclaim from the wedged worker,
		// visible immediately). retry_count is bumped by RequeueTask so an endlessly
		// wedging chunk still eventually exhausts retries like any other requeue.
		if rerr := wk.store.RequeueTask(ctx, c.TaskID); rerr != nil {
			log.Printf("workers: no-peer watchdog: requeue task %s (chunk of job %s): %v", c.TaskID, c.JobID, rerr)
			continue
		}
		metrics.noPeerRequeues.Add(1)
		log.Printf("workers: no-peer watchdog: requeued wedged task %s (job %s, type %s) — held by heartbeating worker %s past %s with no eligible same-class peer; escaped ahead of the %s stale reaper",
			c.TaskID, c.JobID, c.JobType, c.WorkerID, noPeerWatchdogAfter, staleTaskTimeout)
	}
	return nil
}
