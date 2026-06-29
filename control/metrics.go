package main

import (
	"context"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"
)

// metrics.go — hand-rolled Prometheus text exposition (NO client dependency).
//
// The counters are process-lifetime atomics bumped at the event sites (job
// submit, dispatch, commit, mismatch, payout). active_workers is a gauge read
// live from the DB on scrape. The exposition format is the documented
// text-format v0.0.4: `# HELP`, `# TYPE`, then `name value\n`. Hand-rolling it is
// a handful of lines and saves the whole prometheus/client_golang dependency
// tree (BLACKHOLE: own the trivial, never the treacherous — text exposition is
// trivial).

// metrics holds the atomic counters. One value, package-global, because the
// counters are inherently process-wide; threading a struct through every handler
// to increment a number would be ceremony.
type metricsState struct {
	jobsSubmitted        atomic.Int64
	tasksDispatched      atomic.Int64
	tasksCompleted       atomic.Int64
	verificationMismatch atomic.Int64
	payoutsReleased      atomic.Int64
	// Turbo additions:
	quarantines  atomic.Int64 // suppliers auto-quarantined (Verification V2 / fraud)
	hedges       atomic.Int64 // straggler hedge tasks inserted
	resultMerges atomic.Int64 // buyer-ready artifacts merged
	tiebreaks    atomic.Int64 // third-worker tiebreak tasks inserted
	// Plane C/D additions:
	taskFailures atomic.Int64 // typed task failures reported via POST /v1/worker/task/{id}/fail
	// Plane D D21 additions (docs/PLANE_D.md §27):
	quotes           atomic.Int64 // POST /v1/quote priced + persisted (every quote the Brain issued)
	budgetStops      atomic.Int64 // capped jobs paused before breach (Budget Governor stop, §14 D8)
	longPollTimeouts atomic.Int64 // worker long-poll waits that returned empty on timeout (§7 D1; 0 until the long-poll slice lands)
}

var metrics metricsState

// handleMetrics writes the Prometheus exposition. active_workers is read from the
// store live; a DB error there surfaces as a gauge omission with a comment rather
// than a fabricated zero (BLACKHOLE: surface every failure).
func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")

	writeCounter(w, "cx_jobs_submitted_total", "Jobs accepted by POST /v1/jobs.", metrics.jobsSubmitted.Load())
	writeCounter(w, "cx_tasks_dispatched_total", "Tasks claimed by workers via poll.", metrics.tasksDispatched.Load())
	writeCounter(w, "cx_tasks_completed_total", "Tasks committed and verified.", metrics.tasksCompleted.Load())
	writeCounter(w, "cx_verification_mismatch_total", "Redundancy/honeypot mismatches detected.", metrics.verificationMismatch.Load())
	writeCounter(w, "cx_payouts_released_total", "Supplier payouts released or marked ready.", metrics.payoutsReleased.Load())
	writeCounter(w, "cx_quarantines_total", "Suppliers auto-quarantined on fraud / low reputation.", metrics.quarantines.Load())
	writeCounter(w, "cx_hedges_total", "Straggler hedge tasks inserted.", metrics.hedges.Load())
	writeCounter(w, "cx_tiebreaks_total", "Third-worker redundancy tiebreak tasks inserted.", metrics.tiebreaks.Load())
	writeCounter(w, "cx_result_merges_total", "Buyer-ready result artifacts merged.", metrics.resultMerges.Load())
	writeCounter(w, "cx_task_failures_total", "Typed task failures reported via the immediate fail endpoint.", metrics.taskFailures.Load())
	writeCounter(w, "cx_quotes_total", "Quotes priced and persisted via POST /v1/quote.", metrics.quotes.Load())
	writeCounter(w, "cx_budget_stops_total", "Capped jobs paused before breach by the Budget Governor.", metrics.budgetStops.Load())
	writeCounter(w, "cx_long_poll_timeouts_total", "Worker long-poll waits that returned empty on timeout.", metrics.longPollTimeouts.Load())

	// Bound the metric DB queries; widened to 10s so a slow scrape under load still
	// completes rather than truncating the exposition.
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	fmt.Fprintf(w, "# HELP cx_active_workers Workers seen within the last 60s.\n")
	fmt.Fprintf(w, "# TYPE cx_active_workers gauge\n")
	if n, err := s.store.ActiveWorkerCount(ctx); err != nil {
		fmt.Fprintf(w, "# cx_active_workers unavailable: %s\n", err.Error())
	} else {
		fmt.Fprintf(w, "cx_active_workers %d\n", n)
	}

	// Queue depth: claimable tasks broken down by job tier and by job type. A DB
	// error surfaces as a comment, never a fabricated zero (BLACKHOLE).
	fmt.Fprintf(w, "# HELP cx_queue_depth Claimable (queued/retrying, visible, unclaimed) tasks.\n")
	fmt.Fprintf(w, "# TYPE cx_queue_depth gauge\n")
	if rows, err := s.store.QueueDepth(ctx); err != nil {
		fmt.Fprintf(w, "# cx_queue_depth unavailable: %s\n", err.Error())
	} else {
		for _, qd := range rows {
			fmt.Fprintf(w, "cx_queue_depth{tier=%q,job_type=%q} %d\n", qd.Tier, qd.JobType, qd.Count)
		}
	}

	// Background-ticker liveness: seconds since each ticker last succeeded (since the
	// loop start when it has never run). A monotonically climbing value for a ticker
	// is the signature of a wedged background loop · alert on it crossing a multiple of
	// the ticker's interval. /readyz fails on the same staleness (see handleReadyz).
	now := time.Now()
	fmt.Fprintf(w, "# HELP cx_ticker_seconds_since_success Seconds since a background ticker last completed a successful run.\n")
	fmt.Fprintf(w, "# TYPE cx_ticker_seconds_since_success gauge\n")
	for name, secs := range liveness.snapshot(now, workersStartedAt) {
		fmt.Fprintf(w, "cx_ticker_seconds_since_success{ticker=%q} %.3f\n", name, secs)
	}
}

// writeCounter emits one counter metric in text exposition format.
func writeCounter(w http.ResponseWriter, name, help string, v int64) {
	fmt.Fprintf(w, "# HELP %s %s\n", name, help)
	fmt.Fprintf(w, "# TYPE %s counter\n", name)
	fmt.Fprintf(w, "%s %d\n", name, v)
}
