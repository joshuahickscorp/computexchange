package main

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"sync"
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
	// Stuck-run watchdog:
	stuckCancels atomic.Int64 // runs auto-cancelled past their deadline with no progress (checkpointed + partially settled; strike >= 1)
	stuckRescues atomic.Int64 // runs rescued on their FIRST stuck verdict (unfinished tasks requeued; strike 0 → 1)
	// ETA calibration loop:
	watchdogNearMiss atomic.Int64 // finalized jobs that finished LATE (realized > 1.2 × predicted eta_secs) — the data that tunes stuckEtaFactor
	// Payments, Payouts & Unit Economics 8->9 (docs/internal/CREED_AND_PATH_TO_TEN.md):
	// reconcileLedger's DRIFT findings were log-only — an operator had to grep for
	// "reconcile DRIFT" to notice one. A drift here is a genuine anomaly (unlike a
	// payout "deferred" pending a real rail, which is the expected common case
	// today and would just be alert noise), so it is the one payments event this
	// pass wires into Prometheus, not every log line indiscriminately.
	reconcileDrift atomic.Int64 // ledger-vs-Stripe discrepancies found by the reconcile audit
	// Thermal sustained-vs-peak throughput on fanless Apple Silicon 7->8
	// (docs/internal/CREED_AND_PATH_TO_TEN.md): a hedge triggered specifically
	// because the claiming worker's own heartbeat currently reports
	// throttled=true (memory pressure OR a live sustained-throughput drop),
	// via the short hedgeThrottledAfter floor — distinct from the pre-existing
	// elapsed-time-only cx_hedges_total, so an operator can see how often a
	// LIVE thermal/pressure signal is the thing that actually saved a job's tail.
	throttledHedges atomic.Int64
	// Control Plane Hot Path 8->9 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Get
	// result-commit off the S3 critical path"): a redundancy comparison that
	// trusted a worker/peer SHA-256 hash match instead of re-fetching the peer's
	// result object from S3 synchronously inside the commit request. Lets an
	// operator see the S3-GET savings actually landing in real traffic, not just
	// assumed from the code.
	hashTrustedRedundancy atomic.Int64
	// End-to-End Latency 8.5->9 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Close the
	// one real >30-minute path"): a task the class-aware no-peer watchdog requeued
	// because it was wedged on a heartbeating worker with NO eligible same-class
	// peer — the exact case that used to fall all the way through to the 30-minute
	// stale reaper. A climbing value is the operator's signal that the fleet is too
	// thin/heterogeneous for some job class (no peers to hedge to), the underlying
	// condition this watchdog papers over.
	noPeerRequeues atomic.Int64
	// End-to-End Latency 8->8.5 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Prevent
	// the cold-model hedge storm"): a straggler hedge the hedge path suppressed
	// because the slowness was an expected cold GGUF load (worker not yet warm on
	// the model, task still inside the cold-load allowance), not a wedged worker —
	// so a second, likely-also-cold worker was NOT sent a duplicate download. Lets
	// an operator see how often the cold-to-cold hedge storm is actually being
	// averted in real traffic, not just assumed from the code.
	coldModelHedgesSuppressed atomic.Int64
}

var metrics metricsState

// ─────────────────────────────────────────────────────────────────────────────
// Labeled in-process latency/size histograms.
//
// scheduler.go's claimHistogram is a single, UNLABELED, lock-free cumulative
// histogram for the one claim hot path. Two more rungs need histograms that are
// LABELED (one series per direction / per endpoint), which a fixed [N]atomic.Int64
// array can't express without knowing the label set up front:
//
//   - Data Transfer & Artifact I/O 9→10 (docs/internal/CREED_AND_PATH_TO_TEN.md):
//     "transfer throughput and latency are real, dashboarded metrics (not a bare
//     counter)". Until now the only transfer signal was cx_result_merges_total, a
//     bare count — nothing measured how long a transfer took or how big it was.
//   - Performance Observability & Regression Tracking (same doc): "no HTTP request
//     duration ... per-endpoint p99" — the exposition had a task-duration and a
//     claim-duration histogram but no HTTP-request-duration histogram at all.
//
// Both are the same shape: a duration distribution sliced by a small, low-
// cardinality label (transfer direction get/put; the request's route pattern).
// labeledHistogram is that shape — a mutex-guarded map from label value to a
// per-series bucket set. The mutex (not lock-free atomics) is deliberate: these
// observe sites are NOT the claim hot path (a transfer is already an S3 round trip
// dominated by network I/O; an HTTP request is already doing real handler work), so
// a single map lookup + slice increment under a short mutex is negligible next to
// the work being measured, and it buys arbitrary runtime label cardinality the
// atomic-array approach cannot give. Process-lifetime, like every other metric here.
// ─────────────────────────────────────────────────────────────────────────────

// histSeries is one label value's cumulative-bucket accumulator. buckets[i] counts
// observations that fall in bucket i (NON-cumulative in storage; the exposition
// accumulates them into the Prometheus le<= convention on scrape, exactly like
// claimHistogram.snapshot does). overflow counts observations past the last bound.
type histSeries struct {
	buckets  []int64
	overflow int64
	sum      float64
	count    int64
}

// labeledHistogram is a set of histSeries keyed by a label value, all sharing one
// fixed bucket-boundary slice. Safe for concurrent observe/snapshot under its mutex.
type labeledHistogram struct {
	bounds []float64 // fixed upper bounds, ascending
	mu     sync.Mutex
	series map[string]*histSeries
}

func newLabeledHistogram(bounds []float64) *labeledHistogram {
	return &labeledHistogram{bounds: bounds, series: map[string]*histSeries{}}
}

// observe records one value against a label. value is in the histogram's unit
// (milliseconds for the latency histograms, bytes for the size histogram).
func (h *labeledHistogram) observe(label string, value float64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	s := h.series[label]
	if s == nil {
		s = &histSeries{buckets: make([]int64, len(h.bounds))}
		h.series[label] = s
	}
	s.count++
	s.sum += value
	placed := false
	for i, bound := range h.bounds {
		if value <= bound {
			s.buckets[i]++
			placed = true
			break
		}
	}
	if !placed {
		s.overflow++
	}
}

// histSnapshot is one label's snapshot: cumulative bucket counts (Prometheus le<=
// convention — bucket[i] counts every observation <= bounds[i]), plus total count
// and sum. Independent of the live map so the exposition never holds the lock while
// formatting.
type histSnapshot struct {
	label      string
	cumulative []int64
	count      int64
	sum        float64
}

// snapshot returns every label's cumulative-bucket snapshot, sorted by label for a
// stable exposition order. Takes the lock once, copies out, releases — the
// formatting loop in handleMetrics runs lock-free.
func (h *labeledHistogram) snapshot() []histSnapshot {
	h.mu.Lock()
	labels := make([]string, 0, len(h.series))
	for l := range h.series {
		labels = append(labels, l)
	}
	sort.Strings(labels)
	out := make([]histSnapshot, 0, len(labels))
	for _, l := range labels {
		s := h.series[l]
		cumulative := make([]int64, len(h.bounds))
		var running int64
		for i := range h.bounds {
			running += s.buckets[i]
			cumulative[i] = running
		}
		out = append(out, histSnapshot{label: l, cumulative: cumulative, count: s.count, sum: s.sum})
	}
	h.mu.Unlock()
	return out
}

// transferLatencyBucketsMs are the upper bounds (milliseconds) for
// cx_transfer_duration_ms — the wall time of one control-side object-store transfer
// (a PutObject/GetObject in storage.go). Spans a sub-ms in-region hit through the
// multi-second transfer a large merged artifact can take.
var transferLatencyBucketsMs = []float64{1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 15000}

// transferSizeBucketsBytes are the upper bounds (bytes) for cx_transfer_bytes — the
// size of one control-side object-store transfer. Spans a tiny result JSON through a
// multi-hundred-MB merged artifact, powers-of-ten-ish so a throughput read (bytes ÷
// duration) has resolution across the whole real range.
var transferSizeBucketsBytes = []float64{
	1 << 10, 4 << 10, 16 << 10, 64 << 10, 256 << 10,
	1 << 20, 4 << 20, 16 << 20, 64 << 20, 256 << 20, 1 << 30,
}

// httpDurationBucketsMs are the upper bounds (milliseconds) for
// cx_http_request_duration_ms — one real HTTP handler's wall time, per endpoint.
// Finer at the low end (a warm read is single-digit ms) through the seconds a large
// submit or a long-poll-adjacent handler can take.
var httpDurationBucketsMs = []float64{1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 15000}

// transferLatency / transferSize record every control-side object-store transfer
// (storage.go's PutObject/GetObject), labeled by direction (get|put). Data Transfer
// & Artifact I/O 9→10: the first real transfer-throughput + transfer-latency signal,
// replacing the bare cx_result_merges_total counter.
var (
	transferLatency = newLabeledHistogram(transferLatencyBucketsMs)
	transferSize    = newLabeledHistogram(transferSizeBucketsBytes)
)

// httpRequestDuration records every real HTTP handler's wall time, labeled by the
// matched route pattern (Performance Observability: the missing per-endpoint request
// duration histogram). The observe site is the observe() middleware in api.go, which
// already times each request — this turns that measurement into a real distribution.
var httpRequestDuration = newLabeledHistogram(httpDurationBucketsMs)

// observeTransfer records one completed control-side object-store transfer: its wall
// time and byte count, labeled by direction ("get"|"put"). Called from storage.go's
// PutObject/GetObject on success only — a failed transfer moved no complete object,
// so counting it would poison the throughput read (Data Transfer 9→10). Zero-byte or
// negative sizes are clamped out (a stat-only call never reaches here).
func observeTransfer(direction string, bytes int, d time.Duration) {
	if bytes <= 0 {
		return
	}
	transferLatency.observe(direction, float64(d)/float64(time.Millisecond))
	transferSize.observe(direction, float64(bytes))
}

// observeHTTPRequest records one completed HTTP request's handler wall time against
// its endpoint label (the matched route pattern, e.g. "POST /v1/jobs" — never the
// raw path, so /v1/jobs/{id} does not explode into one series per job id). Called
// from api.go's observe() middleware.
func observeHTTPRequest(endpoint string, d time.Duration) {
	httpRequestDuration.observe(endpoint, float64(d)/float64(time.Millisecond))
}

// writeLabeledHistogram emits one labeledHistogram's snapshot in Prometheus text
// exposition, with a single label named labelKey (e.g. "direction"/"endpoint"). The
// bucket bounds are formatted per the histogram's own unit; the +Inf line closes
// each series (count includes the overflow the finite buckets don't). Kept next to
// the histogram type so the exposition and the accumulator can never drift on order.
func writeLabeledHistogram(w http.ResponseWriter, name, help, labelKey string, h *labeledHistogram) {
	fmt.Fprintf(w, "# HELP %s %s\n", name, help)
	fmt.Fprintf(w, "# TYPE %s histogram\n", name)
	for _, s := range h.snapshot() {
		for i, cumulative := range s.cumulative {
			fmt.Fprintf(w, "%s_bucket{%s=%q,le=%q} %d\n",
				name, labelKey, s.label, strconv.FormatFloat(h.bounds[i], 'f', -1, 64), cumulative)
		}
		fmt.Fprintf(w, "%s_bucket{%s=%q,le=\"+Inf\"} %d\n", name, labelKey, s.label, s.count)
		fmt.Fprintf(w, "%s_sum{%s=%q} %s\n", name, labelKey, s.label, strconv.FormatFloat(s.sum, 'f', -1, 64))
		fmt.Fprintf(w, "%s_count{%s=%q} %d\n", name, labelKey, s.label, s.count)
	}
}

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
	writeCounter(w, "cx_stuck_cancelled_total", "Stuck runs auto-cancelled by the watchdog (repeat stall past the deadline; checkpointed + settled at completed work).", metrics.stuckCancels.Load())
	writeCounter(w, "cx_stuck_rescued_total", "Stuck runs rescued by the watchdog's first strike (unfinished tasks requeued to a different machine).", metrics.stuckRescues.Load())
	writeCounter(w, "cx_watchdog_near_miss_total", "Jobs that finalized LATE (realized > 1.2x the predicted eta_secs) — calibration data for the watchdog's ETA factor.", metrics.watchdogNearMiss.Load())
	writeCounter(w, "cx_reconcile_drift_total", "Ledger-vs-Stripe discrepancies found by the reconcile audit (a genuine anomaly, not routine).", metrics.reconcileDrift.Load())
	writeCounter(w, "cx_throttled_hedges_total", "Straggler hedges triggered by a worker's live throttled=true heartbeat (memory pressure or a detected sustained throughput drop), ahead of the elapsed-time hedge/stale-worker thresholds.", metrics.throttledHedges.Load())
	writeCounter(w, "cx_hash_trusted_redundancy_total", "Redundancy comparisons that trusted a worker/peer SHA-256 match instead of re-fetching the peer's result object from S3 inside the commit request.", metrics.hashTrustedRedundancy.Load())
	writeCounter(w, "cx_no_peer_requeues_total", "Wedged tasks requeued by the class-aware no-peer watchdog (heartbeating worker holding a task with no eligible same-class peer), escaping the 30-minute stale reaper.", metrics.noPeerRequeues.Load())
	writeCounter(w, "cx_cold_model_hedges_suppressed_total", "Straggler hedges suppressed because the slowness was an expected cold GGUF load (worker not yet warm on the model), averting a spurious cold-to-cold hedge storm.", metrics.coldModelHedgesSuppressed.Load())
	writeCounter(w, "cx_no_hedge_peer_total", "Dispatch-time heterogeneous-fleet degradation: a redundancy/hedge peer was needed but no eligible same-class peer existed on the fleet (silent loss of hedging + warm-routing). Alerted on by monitoring/alerts.yml.", NoHedgePeerCount())

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
	for name, secs := range liveness.snapshot(now, workersStarted()) {
		fmt.Fprintf(w, "cx_ticker_seconds_since_success{ticker=%q} %.3f\n", name, secs)
	}

	// Postgres Data Lifecycle 8->9 (docs/internal/CREED_AND_PATH_TO_TEN.md): a
	// ticker can keep "succeeding often enough" to dodge the staleness gauge above
	// while still failing most individual runs (e.g. a lock contention error that
	// clears every third tick) — this is the narrower, direct signal an operator
	// alerts on for "is the retention sweep specifically healthy", not just alive.
	fmt.Fprintf(w, "# HELP cx_ticker_failures_total Lifetime count of a background ticker's fn returning an error.\n")
	fmt.Fprintf(w, "# TYPE cx_ticker_failures_total counter\n")
	for name, n := range liveness.failureSnapshot() {
		fmt.Fprintf(w, "cx_ticker_failures_total{ticker=%q} %d\n", name, n)
	}

	// The bloat-ratio half of the same rung: real row counts for the append-only
	// tables the telemetry-retention ticker prunes, so a sweep that is technically
	// "succeeding" (0 rows this pass, or erroring silently below the query layer)
	// but losing the race against insert volume is still visible as a climbing
	// gauge, not just inferred from the sweep's own self-report.
	fmt.Fprintf(w, "# HELP cx_telemetry_table_rows Live row count of a retention-swept telemetry table.\n")
	fmt.Fprintf(w, "# TYPE cx_telemetry_table_rows gauge\n")
	if counts, err := s.store.TelemetryTableCounts(ctx); err != nil {
		fmt.Fprintf(w, "# cx_telemetry_table_rows unavailable: %s\n", err.Error())
	} else {
		for _, table := range telemetryTables {
			fmt.Fprintf(w, "cx_telemetry_table_rows{table=%q} %d\n", table, counts[table])
		}
	}

	// Task-duration histogram (docs/CREED_AND_PATH_TO_TEN.md, "Performance
	// observability" 6→6.5): the first real latency distribution this exposition
	// has ever had — until now /metrics was 16 counters + 3 gauges, no histograms
	// anywhere, so a p90/p99 regression had no automated detection path. Computed
	// straight from task_durations (the same table the drift/ETA rollup reads); a
	// query error surfaces as a comment, never a fabricated empty histogram.
	fmt.Fprintf(w, "# HELP cx_task_duration_ms Committed task wall-time (ms), by job_type.\n")
	fmt.Fprintf(w, "# TYPE cx_task_duration_ms histogram\n")
	if hist, err := s.store.TaskDurationHistogram(ctx); err != nil {
		fmt.Fprintf(w, "# cx_task_duration_ms unavailable: %s\n", err.Error())
	} else {
		for _, row := range hist {
			for i, cumulative := range row.Buckets {
				fmt.Fprintf(w, "cx_task_duration_ms_bucket{job_type=%q,le=%q} %d\n",
					row.JobType, strconv.FormatFloat(taskDurationBucketsMs[i], 'f', -1, 64), cumulative)
			}
			fmt.Fprintf(w, "cx_task_duration_ms_bucket{job_type=%q,le=\"+Inf\"} %d\n", row.JobType, row.Count)
			fmt.Fprintf(w, "cx_task_duration_ms_sum{job_type=%q} %d\n", row.JobType, row.SumMs)
			fmt.Fprintf(w, "cx_task_duration_ms_count{job_type=%q} %d\n", row.JobType, row.Count)
		}
	}

	// Claim-latency histogram (Scalability headroom 4.5->5, docs/internal/
	// CREED_AND_PATH_TO_TEN.md "Measure instead of estimate"): an in-process,
	// zero-DB-cost cumulative histogram over every real ClaimTask call this
	// process has served (see scheduler.go's claimDuration). This is what the
	// bench-claim-load harness scrapes to compute p50/p90/p99 under synthetic
	// load; it is also live in production for the exact same hot path.
	fmt.Fprintf(w, "# HELP cx_claim_duration_ms ClaimTask (the /v1/worker/poll hot path) wall time, ms.\n")
	fmt.Fprintf(w, "# TYPE cx_claim_duration_ms histogram\n")
	{
		buckets, count, sumMs := claimDuration.snapshot()
		for i, cumulative := range buckets {
			fmt.Fprintf(w, "cx_claim_duration_ms_bucket{le=%q} %d\n",
				strconv.FormatFloat(claimDurationBucketsMs[i], 'f', -1, 64), cumulative)
		}
		fmt.Fprintf(w, "cx_claim_duration_ms_bucket{le=\"+Inf\"} %d\n", count)
		fmt.Fprintf(w, "cx_claim_duration_ms_sum %d\n", sumMs)
		fmt.Fprintf(w, "cx_claim_duration_ms_count %d\n", count)
	}

	// End-to-End Job Latency Decomposition 7->7.5 (docs/internal/CREED_AND_PATH_TO_TEN.md):
	// the first breakdown of WHERE a completed task's total wall-time actually
	// went — queue-wait (idle-fleet pickup cost), dispatch overhead (claimed but
	// not yet started, e.g. a cold model load), and run (the actual work, incl.
	// verification + commit) — computed straight from timestamps every task
	// already carries, zero new instrumentation.
	fmt.Fprintf(w, "# HELP cx_latency_phase_ms p50/p90 milliseconds per end-to-end latency phase, by job_type.\n")
	fmt.Fprintf(w, "# TYPE cx_latency_phase_ms gauge\n")
	if phases, err := s.store.LatencyPhaseDecomposition(ctx); err != nil {
		fmt.Fprintf(w, "# cx_latency_phase_ms unavailable: %s\n", err.Error())
	} else {
		for _, p := range phases {
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"queue_wait\",quantile=\"0.5\"} %.3f\n", p.JobType, p.QueueWaitP50Ms)
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"queue_wait\",quantile=\"0.9\"} %.3f\n", p.JobType, p.QueueWaitP90Ms)
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"dispatch_overhead\",quantile=\"0.5\"} %.3f\n", p.JobType, p.DispatchOverheadP50Ms)
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"dispatch_overhead\",quantile=\"0.9\"} %.3f\n", p.JobType, p.DispatchOverheadP90Ms)
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"run\",quantile=\"0.5\"} %.3f\n", p.JobType, p.RunP50Ms)
			fmt.Fprintf(w, "cx_latency_phase_ms{job_type=%q,phase=\"run\",quantile=\"0.9\"} %.3f\n", p.JobType, p.RunP90Ms)
		}
	}

	// Transfer throughput + latency (Data Transfer & Artifact I/O 9->10,
	// docs/internal/CREED_AND_PATH_TO_TEN.md): every control-side object-store
	// transfer (storage.go PutObject/GetObject) records its wall time AND its byte
	// count here, labeled by direction. This is the real transfer-throughput signal
	// the facet's own "where we stand" note said did not exist ("the only metric that
	// exists is a bare resultMerges counter") — a dashboard reads bytes ÷ duration for
	// throughput and the latency histogram directly for the p90/p99 transfer time.
	writeLabeledHistogram(w, "cx_transfer_duration_ms",
		"Control-side object-store transfer wall time (ms), by direction (get|put).",
		"direction", transferLatency)
	writeLabeledHistogram(w, "cx_transfer_bytes",
		"Control-side object-store transfer size (bytes), by direction (get|put).",
		"direction", transferSize)

	// Per-endpoint HTTP request duration (Performance Observability & Regression
	// Tracking, docs/internal/CREED_AND_PATH_TO_TEN.md, "no HTTP request duration ...
	// per-endpoint p99"): the observe() middleware in api.go already times every real
	// handler; this exposes that as a real distribution labeled by the matched route
	// pattern, so a per-endpoint p99 regression finally has an automated detection
	// path instead of only a human noticing a slow request in the access log.
	writeLabeledHistogram(w, "cx_http_request_duration_ms",
		"HTTP handler wall time (ms), by matched endpoint (method + route pattern).",
		"endpoint", httpRequestDuration)
}

// writeCounter emits one counter metric in text exposition format.
func writeCounter(w http.ResponseWriter, name, help string, v int64) {
	fmt.Fprintf(w, "# HELP %s %s\n", name, help)
	fmt.Fprintf(w, "# TYPE %s counter\n", name)
	fmt.Fprintf(w, "%s %d\n", name, v)
}
