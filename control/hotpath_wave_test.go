//go:build integration

package main

// hotpath_wave_test.go — real integration proofs for the Bundle A+C hot-path /
// Postgres-lifecycle rungs (docs/internal/CREED_AND_PATH_TO_TEN.md). Every test here
// runs against the REAL Postgres + MinIO the shared TestMain (integration_test.go)
// stands up — no mocks, no stand-in queries. Kept in its own file so it never
// collides with other agents editing integration_test.go concurrently.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

// TestRequeueTaskBacksOffAndExcludesFailedWorker proves Scheduling & Matching Engine
// 8->9 ("add backoff plus worker-exclusion to verification-requeue so a chunk that
// just failed verification doesn't immediately return to the same worker with no
// delay"): after RequeueTask, (1) the task is delayed by a real backoff (visible_at
// pushed into the future, not now()), (2) the worker that just failed it is recorded
// as excluded until a future instant, (3) the REAL claim path refuses to hand it back
// to that worker while excluded, (4) a DIFFERENT worker CAN claim it once visible, and
// (5) once the exclusion window elapses the original worker can retry (never starved).
func TestRequeueTaskBacksOffAndExcludesFailedWorker(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)

	// reset() restores the demo supplier's status/reputation and the worker's
	// last_seen_at, but NOT its supported_jobs/supported_models/effective_memory_gb —
	// a prior test may have mutated the demo worker to a different job type/model
	// (e.g. a batch_infer/llama hedging test), which would make an embed job below
	// silently ineligible. Pin the demo worker back to the seed's embed-capable state
	// so this test is self-contained regardless of run order.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, effective_memory_gb=64,
		        bw_gbps=400, last_seen_at=now(), throttled=false,
		        supported_jobs=ARRAY['embed','batch_infer'],
		        supported_models=ARRAY['all-minilm-l6-v2','llama-3.2-1b-instruct-q4']
		 WHERE id=$1`, demoWorkerUUID); err != nil {
		t.Fatalf("pin demo worker capabilities: %v", err)
	}

	// A second, identically-capable worker (reset() dropped it) so we can prove the
	// requeued task goes to SOMEONE ELSE while the failer is excluded. Same class +
	// supported jobs/models as the demo worker (seed.go), so eligibility is identical
	// and only the exclusion distinguishes them.
	worker2 := uuid.MustParse(demoWorkerID2)
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_max',64,400,now(),'seed',
		         ARRAY['embed','batch_infer'], ARRAY['all-minilm-l6-v2'], 0, true)
		 ON CONFLICT (id) DO UPDATE SET last_seen_at=now(), throttled=false`,
		worker2, demoSupplier2UUID); err != nil {
		t.Fatalf("seed worker2: %v", err)
	}

	// A single-task job with ONE task, currently 'running', claimed by the demo
	// worker — the exact state a task is in right after a commit whose honeypot then
	// fails verification (CommitTask leaves worker_id/claimed_by = the committer).
	jobID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/rq/in.jsonl','batch',1,0,2)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatalf("seed job: %v", err)
	}
	taskID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, worker_id, claimed_by, retry_count, visible_at)
		 VALUES ($1,$2,'running','jobs/rq/t0/in.jsonl','jobs/rq/t0/out.json',0,$3,$3,0, now())`,
		taskID, jobID, demoWorkerUUID); err != nil {
		t.Fatalf("seed task: %v", err)
	}

	before := time.Now()
	if err := itStore.RequeueTask(ctx, taskID); err != nil {
		t.Fatalf("RequeueTask: %v", err)
	}

	// (1)+(2): the row is retrying, claim cleared, retry bumped, visible_at in the
	// future (a REAL backoff, not now()), and the failer is excluded until later.
	var status string
	var claimedBy, workerID, excludedWorker *uuid.UUID
	var retryCount int
	var visibleAt, excludedUntil *time.Time
	if err := itPool.QueryRow(ctx,
		`SELECT status, claimed_by, worker_id, retry_count, visible_at, excluded_worker, excluded_until
		   FROM tasks WHERE id=$1`, taskID,
	).Scan(&status, &claimedBy, &workerID, &retryCount, &visibleAt, &excludedWorker, &excludedUntil); err != nil {
		t.Fatalf("read requeued task: %v", err)
	}
	if status != "retrying" {
		t.Fatalf("status: want retrying, got %q", status)
	}
	if claimedBy != nil || workerID != nil {
		t.Fatalf("claim not cleared: claimed_by=%v worker_id=%v", claimedBy, workerID)
	}
	if retryCount != 1 {
		t.Fatalf("retry_count: want 1, got %d", retryCount)
	}
	if visibleAt == nil || !visibleAt.After(before) {
		t.Fatalf("visible_at must be pushed into the future (a real backoff), got %v (test began %v)", visibleAt, before)
	}
	if excludedWorker == nil || *excludedWorker != demoWorkerUUID {
		t.Fatalf("excluded_worker must be the worker that just failed it (%v), got %v", demoWorkerUUID, excludedWorker)
	}
	if excludedUntil == nil || !excludedUntil.After(before) {
		t.Fatalf("excluded_until must be a future instant, got %v", excludedUntil)
	}
	// The exclusion window must outlast the visibility backoff, so a different worker
	// gets first crack once the task becomes visible (grace after visible_at).
	if !excludedUntil.After(*visibleAt) {
		t.Fatalf("excluded_until (%v) must be after visible_at (%v) so the failer stays excluded once the task becomes visible", excludedUntil, visibleAt)
	}

	// (3): make the task visible immediately (simulate the backoff having elapsed) but
	// KEEP the exclusion in force. The failer (demo worker) must NOT reclaim it.
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET visible_at = now() - interval '1 second' WHERE id=$1`, taskID); err != nil {
		t.Fatalf("make visible: %v", err)
	}
	c, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil {
		t.Fatalf("ClaimTask (failer): %v", err)
	}
	if c != nil {
		t.Fatalf("the just-failed worker must be excluded from reclaiming the task, but it claimed %s", c.TaskID)
	}

	// (4): a DIFFERENT worker claims it fine (it is visible and not excluded for them).
	c2, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: worker2, SupplierID: demoSupplier2UUID})
	if err != nil {
		t.Fatalf("ClaimTask (worker2): %v", err)
	}
	if c2 == nil || c2.TaskID != taskID {
		t.Fatalf("a different, non-excluded worker must be able to claim the requeued task, got %v", c2)
	}

	// (5): the exclusion EXPIRES — put the task back to retrying+visible with an
	// already-past excluded_until, and the ORIGINAL worker can now retry it (never
	// permanently starved on a thin/single-worker fleet).
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='retrying', claimed_by=NULL, claimed_at=NULL, worker_id=NULL,
		                  visible_at = now() - interval '1 second',
		                  excluded_until = now() - interval '1 second'
		   WHERE id=$1`, taskID); err != nil {
		t.Fatalf("expire exclusion: %v", err)
	}
	c3, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil {
		t.Fatalf("ClaimTask (failer, after expiry): %v", err)
	}
	if c3 == nil || c3.TaskID != taskID {
		t.Fatalf("once the exclusion window elapses the original worker must be able to retry (no permanent starvation), got %v", c3)
	}
}

// TestNoHedgePeerMetricFiresOnHeterogeneousFleet proves Scheduling & Matching Engine
// 7->8 ("Make heterogeneous-fleet degradation visible instead of silent"): when a
// hedge/tiebreak/redundancy peer is needed but no INDEPENDENT same-class peer exists
// on a fleet that DOES have live eligible-for-the-job-type supply of another class,
// the cx_no_hedge_peer counter increments — the operational signal that was silent
// before. Two real, contrasting cases against the real matcher + real DB:
//
//	(1) heterogeneous fleet (anchor apple_silicon_max, only a live nvidia_24g peer):
//	    SelectRedundancyPeer returns ErrNoSupply AND the counter ticks — the exact
//	    thin-mixed-fleet degradation the rung targets.
//	(2) a real same-class independent peer online: a peer is returned and the counter
//	    does NOT move — proving the signal is a genuine no-peer detector, not a
//	    rubber stamp on every peer search.
//	(3) empty fleet (no live eligible supply at all): ErrNoSupply but the counter
//	    does NOT move — an empty fleet is an already-obvious condition, not the
//	    "supply exists but not of the right class" heterogeneous degradation.
func TestNoHedgePeerMetricFiresOnHeterogeneousFleet(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)

	// Anchor = the demo worker, pinned to a known class/engine/build and embed-capable.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', engine='candle', build_hash='bh1',
		        memory_gb=64, effective_memory_gb=64, bw_gbps=400, last_seen_at=now(), throttled=false,
		        supported_jobs=ARRAY['embed'], supported_models=ARRAY['all-minilm-l6-v2']
		 WHERE id=$1`, demoWorkerUUID); err != nil {
		t.Fatalf("pin anchor: %v", err)
	}

	// (1) Heterogeneous: only a live nvidia_24g peer (different class) + a distinct
	// supplier. It IS eligible for the embed job type (so CandidateWorkers returns
	// it — the fleet has supply), but it is the WRONG class for a same-class peer.
	otherClass := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, engine, build_hash, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok, effective_memory_gb, throttled)
		 VALUES ($1,$2,'nvidia_24g','vllm','bh2',24,600,now(),'seed',
		         ARRAY['embed'], ARRAY['all-minilm-l6-v2'], 0, true, 24, false)`,
		otherClass, demoSupplier2UUID); err != nil {
		t.Fatalf("seed wrong-class peer: %v", err)
	}

	before := NoHedgePeerCount()
	_, err := itStore.SelectRedundancyPeer(ctx, "embed", "all-minilm-l6-v2", 2, demoWorkerUUID)
	if !errorsIsNoSupply(err) {
		t.Fatalf("heterogeneous fleet: want ErrNoSupply (no same-class peer), got %v", err)
	}
	if got := NoHedgePeerCount() - before; got != 1 {
		t.Fatalf("heterogeneous-fleet no-peer must tick the signal exactly once, got +%d", got)
	}

	// (2) Add a real, independent SAME-class peer (apple_silicon_max, same engine/build,
	// distinct supplier). Now a peer exists — the search succeeds and the counter holds.
	sameClass := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, engine, build_hash, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok, effective_memory_gb, throttled)
		 VALUES ($1,$2,'apple_silicon_max','candle','bh1',64,400,now(),'seed',
		         ARRAY['embed'], ARRAY['all-minilm-l6-v2'], 0, true, 64, false)`,
		sameClass, demoSupplier3UUID); err != nil {
		t.Fatalf("seed same-class peer: %v", err)
	}
	before2 := NoHedgePeerCount()
	peer, err := itStore.SelectRedundancyPeer(ctx, "embed", "all-minilm-l6-v2", 2, demoWorkerUUID)
	if err != nil {
		t.Fatalf("a real same-class independent peer is online — want a peer, got err %v", err)
	}
	if peer != sameClass {
		t.Fatalf("want the same-class peer %v, got %v", sameClass, peer)
	}
	if got := NoHedgePeerCount() - before2; got != 0 {
		t.Fatalf("a successful peer search must NOT tick the no-peer signal, got +%d", got)
	}

	// (3) Empty fleet: drop every non-anchor worker so there is NO live eligible
	// supply at all. ErrNoSupply, but the counter must NOT move (empty-fleet is a
	// distinct, already-obvious condition, not heterogeneous degradation).
	if _, err := itPool.Exec(ctx, `DELETE FROM workers WHERE id <> $1`, demoWorkerUUID); err != nil {
		t.Fatalf("empty the fleet: %v", err)
	}
	before3 := NoHedgePeerCount()
	_, err = itStore.SelectRedundancyPeer(ctx, "embed", "all-minilm-l6-v2", 2, demoWorkerUUID)
	if !errorsIsNoSupply(err) {
		t.Fatalf("empty fleet: want ErrNoSupply, got %v", err)
	}
	if got := NoHedgePeerCount() - before3; got != 0 {
		t.Fatalf("an EMPTY fleet must NOT tick the heterogeneous-degradation signal (candidates were empty), got +%d", got)
	}
}

// errorsIsNoSupply is a tiny local adapter (errors.Is against the package ErrNoSupply)
// so the test reads cleanly.
func errorsIsNoSupply(err error) bool { return errors.Is(err, ErrNoSupply) }

// TestRequeueBackoffGrowsWithRetries proves the backoff is genuinely exponential per
// retry (not a flat delay): a task requeued at retry_count=0 gets a shorter delay than
// one requeued at a higher retry_count, mirroring the stale-task requeue ladder's shape.
func TestRequeueBackoffGrowsWithRetries(t *testing.T) {
	// Pure function under test — deterministic, no DB needed, but kept in the
	// integration file since RequeueTask itself is DB-driven and proven above.
	d0 := requeueBackoff(0)
	d1 := requeueBackoff(1)
	d3 := requeueBackoff(3)
	if !(d0 < d1 && d1 < d3) {
		t.Fatalf("backoff must grow with retries: d0=%v d1=%v d3=%v", d0, d1, d3)
	}
	if d0 != requeueBackoffBase {
		t.Fatalf("first requeue delay should be the base (%v), got %v", requeueBackoffBase, d0)
	}
	// The cap holds: a pathological retry_count never exceeds the ceiling and never
	// overflows to a non-positive duration.
	if capped := requeueBackoff(100); capped != requeueBackoffCap {
		t.Fatalf("a huge retry_count must clamp to the cap (%v), got %v", requeueBackoffCap, capped)
	}
	if requeueBackoff(-5) != requeueBackoffBase {
		t.Fatalf("a negative retry_count must be treated as 0 (base delay)")
	}
}

// TestClaimLoad100kConcurrent is a REAL concurrent-poller load test at the scale
// Scheduling & Matching 6->6.5 asks for (100k-task queue / 500 registered workers /
// N concurrent pollers), producing a real claims/sec + p50/p90/p99 claim-latency
// report. It drives the EXACT shipped Store.ClaimTask (the /v1/worker/poll hot path,
// with the cx_claim_duration histogram inside it) — not a stand-in — with each
// goroutine a DISTINCT registered worker identity across many suppliers, against a
// single real 100k-row tasks queue, so the load is genuinely fleet-wide contention,
// not one worker in a loop.
//
// It is gated behind CX_CLAIM_LOAD=1 (and a poller count via CX_CLAIM_POLLERS,
// default 500) because seeding 100k tasks + 500 workers and draining them is heavy —
// it is a load benchmark, not a per-commit correctness gate. Run it explicitly:
//
//	CX_CLAIM_LOAD=1 go test -tags integration -run TestClaimLoad100kConcurrent -v ./control
//
// The measured numbers land in docs/load-test-reports/ (committed by hand from the
// test's logged output — the same convention as the 50k-load and adversarial reports).
func TestClaimLoad100kConcurrent(t *testing.T) {
	if os.Getenv("CX_CLAIM_LOAD") != "1" {
		t.Skip("heavy load test — set CX_CLAIM_LOAD=1 to run (seeds 100k tasks / 500 workers)")
	}
	reset(t)
	ctx := context.Background()

	const nWorkers = 500
	const nSuppliers = 50
	const nTasks = 100_000
	// Poller concurrency: a fixed count of concurrent claim loops. Deliberately
	// modest by default — the integration pool caps connections (~pool size), so far
	// more pollers than connections just measures pool queuing, not claim cost. 50
	// concurrent claim loops fully saturate the DB at this scale; the throughput
	// number is bounded by the DB + pool, not the poller count (the 50k report found
	// the same: 10 vs 100 pollers gave the same fleet-wide claims/sec). Override via
	// CX_CLAIM_POLLERS.
	pollers := 50
	if v := os.Getenv("CX_CLAIM_POLLERS"); v != "" {
		fmt.Sscanf(v, "%d", &pollers)
	}
	// Fixed measurement window (seconds) — measure steady-state claims/sec + p99
	// under sustained load rather than timing a full 100k drain (which at any real
	// claims/sec is minutes and dominated by wall-clock, not the per-claim cost this
	// report is about). Matches the 50k report's fixed-window methodology.
	windowSecs := 60
	if v := os.Getenv("CX_CLAIM_WINDOW"); v != "" {
		fmt.Sscanf(v, "%d", &windowSecs)
	}
	tag := fmt.Sprintf("claimload-%d", time.Now().Unix())

	// Seed: nSuppliers active suppliers, nWorkers workers across them (varied hw_class
	// so cheaper_class_online has real candidates), a benchmark_results + worker_tps_cache
	// row per worker, worker_model_state for a third, and nTasks queued across ~50-task
	// jobs (so job_dispatched_count/fairness has per-job variety, not one giant job).
	t.Logf("seeding %d suppliers / %d workers / %d tasks ...", nSuppliers, nWorkers, nTasks)
	seed := fmt.Sprintf(`
INSERT INTO suppliers (id, email, reputation, status, completed_tasks, data_country)
  SELECT gen_random_uuid(), '%[1]s-sup-'||g||'@load.local', 0.3+(g%%7)::real/10.0, 'active', (g*37)%%5000, 'US'
  FROM generate_series(1,%[2]d) g;
INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, supported_jobs, supported_models, min_payout_usd_hr, effective_memory_gb, throttled, thermal_ok)
  SELECT gen_random_uuid(),
         (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%' ORDER BY email OFFSET (g%%%[3]d) LIMIT 1),
         (ARRAY['cpu','apple_silicon_base','apple_silicon_pro','nvidia_24g','apple_silicon_max','nvidia_48g','nvidia_80g','apple_silicon_ultra','nvidia_180g'])[1+(g%%9)],
         16+(g%%8)*8, 200+(g%%5)*100, now()-(g%%30||' seconds')::interval,
         ARRAY['embed','batch_infer'], ARRAY['all-minilm-l6-v2'], 0, 16+(g%%8)*8, false, true
  FROM generate_series(1,%[4]d) g;
INSERT INTO worker_tps_cache (worker_id, job_type, tps)
  SELECT id, 'embed', 20+(random()*180)::real FROM workers WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%')
  ON CONFLICT (worker_id, job_type) DO NOTHING;
INSERT INTO worker_model_state (worker_id, model_id, last_seen_warm)
  SELECT id, 'all-minilm-l6-v2', now()-interval '5 seconds' FROM workers
   WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%') AND (('x'||substr(id::text,1,8))::bit(32)::bigint %% 3)=0;
INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb, estimated_usd, max_usd)
  SELECT gen_random_uuid(), '%[6]s', 'running', 'embed', 'all-minilm-l6-v2', 'jobs/%[1]s/'||g||'/in.jsonl', 'batch', 50, 0, 2, 5.00,
         CASE WHEN g%%3=0 THEN 50.00 ELSE NULL END
  FROM generate_series(1, %[5]d) g;
INSERT INTO tasks (id, job_id, status, input_ref, chunk_index, visible_at)
  SELECT gen_random_uuid(), j.id, 'queued', 'jobs/%[1]s/t'||row_number() OVER (PARTITION BY j.id)||'/in.jsonl',
         (row_number() OVER (PARTITION BY j.id))::int, now()
  FROM (SELECT id FROM jobs WHERE input_ref LIKE 'jobs/%[1]s/%%') j, generate_series(1,50) t;
ANALYZE suppliers; ANALYZE workers; ANALYZE worker_tps_cache; ANALYZE worker_model_state; ANALYZE jobs; ANALYZE tasks;`,
		tag, nSuppliers, nSuppliers, nWorkers, nTasks/50, demoBuyerUUID.String())
	// No bind params: the seed is a multi-statement script (pgx simple protocol),
	// and the only interpolated value is the fixed demo buyer UUID, inlined above.
	if _, err := itPool.Exec(ctx, seed); err != nil {
		t.Fatalf("seed load fleet+queue: %v", err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(), fmt.Sprintf(`
			DELETE FROM tasks WHERE job_id IN (SELECT id FROM jobs WHERE input_ref LIKE 'jobs/%[1]s/%%');
			DELETE FROM jobs WHERE input_ref LIKE 'jobs/%[1]s/%%';
			DELETE FROM worker_model_state WHERE worker_id IN (SELECT id FROM workers WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%'));
			DELETE FROM worker_tps_cache WHERE worker_id IN (SELECT id FROM workers WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%'));
			DELETE FROM workers WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE '%[1]s-%%');
			DELETE FROM suppliers WHERE email LIKE '%[1]s-%%';`, tag))
	})

	var queueDepth, liveWorkers int
	itPool.QueryRow(ctx, `SELECT count(*) FROM tasks WHERE status='queued' AND claimed_by IS NULL AND job_id IN (SELECT id FROM jobs WHERE input_ref LIKE 'jobs/'||$1||'/%')`, tag).Scan(&queueDepth)
	itPool.QueryRow(ctx, `SELECT count(*) FROM workers WHERE supplier_id IN (SELECT id FROM suppliers WHERE email LIKE $1||'-%')`, tag).Scan(&liveWorkers)
	t.Logf("seeded: %d claimable tasks / %d workers", queueDepth, liveWorkers)

	// Pull `pollers` distinct real worker identities (id + supplier) to drive.
	type ident struct{ w, s uuid.UUID }
	rows, err := itPool.Query(ctx,
		`SELECT w.id, w.supplier_id FROM workers w
		   WHERE w.supplier_id IN (SELECT id FROM suppliers WHERE email LIKE $1||'-%')
		   ORDER BY w.id LIMIT $2`, tag, pollers)
	if err != nil {
		t.Fatalf("load worker identities: %v", err)
	}
	var idents []ident
	for rows.Next() {
		var id ident
		if err := rows.Scan(&id.w, &id.s); err != nil {
			t.Fatalf("scan ident: %v", err)
		}
		idents = append(idents, id)
	}
	rows.Close()
	if len(idents) == 0 {
		t.Fatal("no worker identities to poll with")
	}

	// Each poller goroutine claims in a tight loop for the fixed window, recording
	// per-claim latency. This is the real ClaimTask transaction (Begin→claim→Commit),
	// the exact /v1/worker/poll hot path, under genuine fleet-wide concurrency.
	windowCtx, cancel := context.WithTimeout(ctx, time.Duration(windowSecs)*time.Second)
	defer cancel()
	var (
		mu       sync.Mutex
		latMs    []float64
		claimed  atomic.Int64
		empty    atomic.Int64
		errCount atomic.Int64
		wg       sync.WaitGroup
	)
	start := time.Now()
	for i := 0; i < pollers; i++ {
		id := idents[i%len(idents)]
		wg.Add(1)
		go func(id ident) {
			defer wg.Done()
			auth := WorkerAuth{WorkerID: id.w, SupplierID: id.s}
			for windowCtx.Err() == nil {
				t0 := time.Now()
				c, err := itStore.ClaimTask(windowCtx, auth)
				elapsed := float64(time.Since(t0).Microseconds()) / 1000.0
				if err != nil {
					// A window-deadline cancellation is expected teardown, not a claim error.
					if windowCtx.Err() != nil {
						return
					}
					errCount.Add(1)
					return
				}
				if c == nil {
					empty.Add(1)
					continue // no eligible task this instant; keep polling within the window
				}
				claimed.Add(1)
				mu.Lock()
				latMs = append(latMs, elapsed)
				mu.Unlock()
			}
		}(id)
	}
	wg.Wait()
	wall := time.Since(start)

	sort.Float64s(latMs)
	pct := func(p float64) float64 {
		if len(latMs) == 0 {
			return 0
		}
		i := int(p / 100.0 * float64(len(latMs)))
		if i >= len(latMs) {
			i = len(latMs) - 1
		}
		return latMs[i]
	}
	nClaimed := claimed.Load()
	if nClaimed == 0 {
		t.Fatal("no tasks were claimed — the load harness claimed nothing")
	}
	cps := float64(nClaimed) / wall.Seconds()
	t.Logf("========== CLAIM LOAD REPORT (100k queue / %d workers / %d pollers / %ds window) ==========", liveWorkers, pollers, windowSecs)
	t.Logf("queue depth (start): %d", queueDepth)
	t.Logf("wall: %s   claimed: %d   empty-returns: %d   errors: %d", wall.Round(time.Millisecond), nClaimed, empty.Load(), errCount.Load())
	t.Logf("throughput: %.1f claims/sec (fleet-wide, %d concurrent pollers)", cps, pollers)
	t.Logf("claim latency ms: p50=%.2f  p90=%.2f  p99=%.2f  min=%.2f  max=%.2f",
		pct(50), pct(90), pct(99), latMs[0], latMs[len(latMs)-1])
	t.Logf("=======================================================================")

	if errCount.Load() > 0 {
		t.Fatalf("%d claim errors during load (a real error, not empty-queue)", errCount.Load())
	}
}
