//go:build integration

package main

// latency_moat_test.go — real integration proofs for Bundle D+E+F:
//   - Data Moat 6->7: the per-(supplier, job_type) reliability admin view.
//   - Data Transfer 9->10 (code half): the transfer throughput/latency histograms.
//   - Performance Observability: the per-endpoint HTTP request-duration histogram.
//   - End-to-End Latency 8->8.5 & 8.5->9: cold-model hedge suppression + the
//     class-aware no-peer watchdog.
//
// Every fixture is real rows in the real Postgres + real objects in the real MinIO;
// the metrics are scraped off the real /metrics HTTP surface, never asserted from a
// diff. Gated behind //go:build integration like the rest of the matrix.

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// ─────────────────────────────────────────────────────────────────────────────
// Data Moat 6->7 — per-(supplier, job_type) reliability view.
// ─────────────────────────────────────────────────────────────────────────────

// TestSupplierReliabilityView proves the Data Moat 6->7 rung's proof artifact "a
// query or admin view showing per-supplier, per-job-type historical accuracy,
// derived from real completed verifications": it seeds REAL tasks (complete/failed)
// and REAL verification_events (honeypot pass/fail, redundancy match/mismatch)
// across two suppliers and two job types, then asserts the Store query AND the live
// GET /admin/moat/reliability HTTP surface report the exact rates those rows imply —
// including a nil rate where a denominator is genuinely zero (never a faked 1.0).
func TestSupplierReliabilityView(t *testing.T) {
	ctx := context.Background()
	reset(t)
	ensureExtraDemoSuppliers(t, ctx)

	// A second worker on the SECOND supplier, so task outcomes attribute to two
	// distinct suppliers (task completion is keyed by tasks.worker_id -> supplier).
	worker2 := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, last_seen_at, version)
		 VALUES ($1,$2,'apple_silicon_max',64,now(),'seed')`,
		worker2, demoSupplier2UUID); err != nil {
		t.Fatal(err)
	}

	// Supplier 1 (demo): job type "embed" — 3 tasks complete, 1 failed (75% completion);
	// 2 honeypot passes, 0 fails (100% honeypot, denom 2); 1 redundancy match, 1
	// mismatch (50% agreement). Also one "batch_infer" task complete but NO
	// verification of any kind, so its honeypot/redundancy rates must be nil.
	// Supplier 2: "embed" — 1 task complete, 0 failed (100% completion); NO honeypot
	// (nil), 3 redundancy matches, 0 mismatch (100% agreement).
	mkJob := func(supplierBuyer uuid.UUID, jobType string) uuid.UUID {
		id := uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done)
			 VALUES ($1,$2,'complete',$3,'m','in','batch',1,1)`,
			id, supplierBuyer, jobType); err != nil {
			t.Fatal(err)
		}
		return id
	}
	mkTask := func(jobID, worker uuid.UUID, status string) {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, worker_id, status, input_ref, result_key, chunk_index)
			 VALUES ($1,$2,$3,$4,'in','out',0)`,
			uuid.New(), jobID, worker, status); err != nil {
			t.Fatal(err)
		}
	}
	mkVE := func(jobID, supplier uuid.UUID, kind string) {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO verification_events (id, job_id, supplier_id, kind) VALUES ($1,$2,$3,$4)`,
			uuid.New(), jobID, supplier, kind); err != nil {
			t.Fatal(err)
		}
	}

	// Supplier 1 / embed.
	s1embed := mkJob(demoBuyerUUID, "embed")
	mkTask(s1embed, demoWorkerUUID, "complete")
	mkTask(s1embed, demoWorkerUUID, "complete")
	mkTask(s1embed, demoWorkerUUID, "complete")
	mkTask(s1embed, demoWorkerUUID, "failed")
	// A still-running task must NOT count in either numerator or denominator.
	mkTask(s1embed, demoWorkerUUID, "running")
	mkVE(s1embed, demoSupplierUUID, "honeypot_pass")
	mkVE(s1embed, demoSupplierUUID, "honeypot_pass")
	mkVE(s1embed, demoSupplierUUID, "redundancy_match")
	mkVE(s1embed, demoSupplierUUID, "redundancy_mismatch")
	// A cross-class forensic event must NOT be counted as a redundancy check.
	mkVE(s1embed, demoSupplierUUID, "redundancy_cross_class")

	// Supplier 1 / batch_infer — completion only, no verification.
	s1infer := mkJob(demoBuyerUUID, "batch_infer")
	mkTask(s1infer, demoWorkerUUID, "complete")

	// Supplier 2 / embed.
	s2embed := mkJob(demoBuyerUUID, "embed")
	mkTask(s2embed, worker2, "complete")
	mkVE(s2embed, demoSupplier2UUID, "redundancy_match")
	mkVE(s2embed, demoSupplier2UUID, "redundancy_match")
	mkVE(s2embed, demoSupplier2UUID, "redundancy_match")

	// --- Store query ---
	rel, err := itStore.SupplierReliability(ctx)
	if err != nil {
		t.Fatalf("SupplierReliability: %v", err)
	}
	get := func(sup uuid.UUID, jt string) *SupplierReliability {
		for i := range rel {
			if rel[i].SupplierID == sup && rel[i].JobType == jt {
				return &rel[i]
			}
		}
		return nil
	}

	// Supplier 1 / embed: completion 3/4=0.75, honeypot 2/2=1.0, redundancy 1/2=0.5.
	r := get(demoSupplierUUID, "embed")
	if r == nil {
		t.Fatalf("missing supplier1/embed cell: %+v", rel)
	}
	if r.TasksCompleted != 3 || r.TasksFailed != 1 {
		t.Fatalf("supplier1/embed task counts: want 3 complete/1 failed, got %d/%d", r.TasksCompleted, r.TasksFailed)
	}
	if r.CompletionRate == nil || *r.CompletionRate != 0.75 {
		t.Fatalf("supplier1/embed completion: want 0.75, got %v", r.CompletionRate)
	}
	if r.HoneypotRate == nil || *r.HoneypotRate != 1.0 {
		t.Fatalf("supplier1/embed honeypot: want 1.0 (2/2), got %v (passes=%d fails=%d)", r.HoneypotRate, r.HoneypotPasses, r.HoneypotFails)
	}
	if r.RedundancyMatches != 1 || r.RedundancyMismatches != 1 {
		t.Fatalf("supplier1/embed redundancy counts: want 1 match/1 mismatch (cross_class excluded), got %d/%d", r.RedundancyMatches, r.RedundancyMismatches)
	}
	if r.RedundancyRate == nil || *r.RedundancyRate != 0.5 {
		t.Fatalf("supplier1/embed redundancy: want 0.5, got %v", r.RedundancyRate)
	}

	// Supplier 1 / batch_infer: completion 1/1=1.0, honeypot nil, redundancy nil.
	ri := get(demoSupplierUUID, "batch_infer")
	if ri == nil {
		t.Fatalf("missing supplier1/batch_infer cell: %+v", rel)
	}
	if ri.CompletionRate == nil || *ri.CompletionRate != 1.0 {
		t.Fatalf("supplier1/batch_infer completion: want 1.0, got %v", ri.CompletionRate)
	}
	if ri.HoneypotRate != nil {
		t.Fatalf("supplier1/batch_infer honeypot: want nil (never honeypot-checked), got %v", *ri.HoneypotRate)
	}
	if ri.RedundancyRate != nil {
		t.Fatalf("supplier1/batch_infer redundancy: want nil (never redundancy-checked), got %v", *ri.RedundancyRate)
	}

	// Supplier 2 / embed: completion 1/1=1.0, honeypot nil, redundancy 3/3=1.0.
	r2 := get(demoSupplier2UUID, "embed")
	if r2 == nil {
		t.Fatalf("missing supplier2/embed cell: %+v", rel)
	}
	if r2.CompletionRate == nil || *r2.CompletionRate != 1.0 {
		t.Fatalf("supplier2/embed completion: want 1.0, got %v", r2.CompletionRate)
	}
	if r2.HoneypotRate != nil {
		t.Fatalf("supplier2/embed honeypot: want nil, got %v", *r2.HoneypotRate)
	}
	if r2.RedundancyRate == nil || *r2.RedundancyRate != 1.0 || r2.RedundancyMatches != 3 {
		t.Fatalf("supplier2/embed redundancy: want 1.0 (3/3), got %v (matches=%d)", r2.RedundancyRate, r2.RedundancyMatches)
	}

	// --- HTTP admin surface (auth-gated) ---
	if code, body := req(t, "GET", "/admin/moat/reliability", nil, buyerKey()); code != http.StatusForbidden {
		t.Fatalf("reliability via non-admin key: want 403, got %d: %s", code, body)
	}
	code, body := req(t, "GET", "/admin/moat/reliability", nil, adminKey())
	if code != http.StatusOK {
		t.Fatalf("GET /admin/moat/reliability: want 200, got %d: %s", code, body)
	}
	var httpRel []SupplierReliability
	if err := json.Unmarshal(body, &httpRel); err != nil {
		t.Fatalf("decode reliability: %v (body=%s)", err, body)
	}
	// The HTTP surface must carry the same three cells the Store query returned.
	if len(httpRel) < 3 {
		t.Fatalf("HTTP reliability: want >=3 cells, got %d: %s", len(httpRel), body)
	}
	var sawS1embed bool
	for _, c := range httpRel {
		if c.SupplierID == demoSupplierUUID && c.JobType == "embed" {
			sawS1embed = true
			if c.CompletionRate == nil || *c.CompletionRate != 0.75 || c.RedundancyRate == nil || *c.RedundancyRate != 0.5 {
				t.Fatalf("HTTP supplier1/embed rates wrong: %+v", c)
			}
		}
	}
	if !sawS1embed {
		t.Fatalf("HTTP reliability missing supplier1/embed: %s", body)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Data Transfer 9->10 (code half) — transfer throughput + latency histograms.
// ─────────────────────────────────────────────────────────────────────────────

// scrapeHistogramLine parses one Prometheus histogram line's value out of /metrics,
// matched by the full "name{labels}" prefix up to the value (e.g.
// `cx_transfer_bytes_count{direction="put"}`). Returns (value, true) if present.
func scrapeHistogramLine(t *testing.T, full string) (float64, bool) {
	t.Helper()
	code, body := req(t, "GET", "/metrics", nil)
	if code != 200 {
		t.Fatalf("GET /metrics: want 200, got %d", code)
	}
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, full+" ") {
			v, err := strconv.ParseFloat(strings.TrimSpace(line[len(full)+1:]), 64)
			if err != nil {
				t.Fatalf("parsing %q line %q: %v", full, line, err)
			}
			return v, true
		}
	}
	return 0, false
}

// TestTransferHistogramPopulatesFromRealTransfers proves the Data Transfer 9->10
// code-half rung: real control-side object-store transfers (storage.go
// PutObject/GetObject) populate the cx_transfer_duration_ms + cx_transfer_bytes
// histograms, labeled by direction — the real throughput/latency signal that
// replaces the bare resultMerges counter. Reads the /metrics counters before and
// after a real PUT + GET of a known-size object and asserts the put/get series each
// advanced by exactly one observation whose byte total grew by the object's real size.
func TestTransferHistogramPopulatesFromRealTransfers(t *testing.T) {
	ctx := context.Background()
	reset(t)

	putCountBefore, _ := scrapeHistogramLine(t, `cx_transfer_bytes_count{direction="put"}`)
	getCountBefore, _ := scrapeHistogramLine(t, `cx_transfer_bytes_count{direction="get"}`)
	putSumBefore, _ := scrapeHistogramLine(t, `cx_transfer_bytes_sum{direction="put"}`)
	getSumBefore, _ := scrapeHistogramLine(t, `cx_transfer_bytes_sum{direction="get"}`)
	// The duration histogram must exist for the same directions.
	putDurCountBefore, _ := scrapeHistogramLine(t, `cx_transfer_duration_ms_count{direction="put"}`)

	payload := make([]byte, 4096)
	for i := range payload {
		payload[i] = byte(i % 251)
	}
	key := "transfer-hist-test/obj.bin"
	if err := itStorage.PutObject(ctx, key, payload, "application/octet-stream"); err != nil {
		t.Fatalf("PutObject: %v", err)
	}
	got, err := itStorage.GetObject(ctx, key)
	if err != nil {
		t.Fatalf("GetObject: %v", err)
	}
	if len(got) != len(payload) {
		t.Fatalf("GetObject size: want %d, got %d", len(payload), len(got))
	}

	putCountAfter, ok := scrapeHistogramLine(t, `cx_transfer_bytes_count{direction="put"}`)
	if !ok {
		t.Fatal("cx_transfer_bytes_count{direction=put} not present after a real PutObject")
	}
	getCountAfter, ok := scrapeHistogramLine(t, `cx_transfer_bytes_count{direction="get"}`)
	if !ok {
		t.Fatal("cx_transfer_bytes_count{direction=get} not present after a real GetObject")
	}
	putSumAfter, _ := scrapeHistogramLine(t, `cx_transfer_bytes_sum{direction="put"}`)
	getSumAfter, _ := scrapeHistogramLine(t, `cx_transfer_bytes_sum{direction="get"}`)
	putDurCountAfter, ok := scrapeHistogramLine(t, `cx_transfer_duration_ms_count{direction="put"}`)
	if !ok {
		t.Fatal("cx_transfer_duration_ms_count{direction=put} not present after a real PutObject")
	}

	if putCountAfter-putCountBefore != 1 {
		t.Fatalf("put transfer count: want +1, got +%v", putCountAfter-putCountBefore)
	}
	if getCountAfter-getCountBefore != 1 {
		t.Fatalf("get transfer count: want +1, got +%v", getCountAfter-getCountBefore)
	}
	if putSumAfter-putSumBefore != float64(len(payload)) {
		t.Fatalf("put transfer bytes sum: want +%d, got +%v", len(payload), putSumAfter-putSumBefore)
	}
	if getSumAfter-getSumBefore != float64(len(payload)) {
		t.Fatalf("get transfer bytes sum: want +%d, got +%v", len(payload), getSumAfter-getSumBefore)
	}
	if putDurCountAfter-putDurCountBefore != 1 {
		t.Fatalf("put transfer duration count: want +1, got +%v", putDurCountAfter-putDurCountBefore)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Performance Observability — per-endpoint HTTP request-duration histogram.
// ─────────────────────────────────────────────────────────────────────────────

// TestHTTPRequestDurationHistogramPopulates proves the per-endpoint HTTP request
// duration histogram: a real request to a real route advances
// cx_http_request_duration_ms for THAT route's matched pattern label, and the label
// is the route PATTERN ("GET /v1/models"), not the raw path — so path-variable
// routes stay one bounded series. Uses the buyer-authed GET /v1/models (a cheap real
// handler) and asserts its endpoint-labeled count advances by exactly one per call.
func TestHTTPRequestDurationHistogramPopulates(t *testing.T) {
	reset(t)
	const endpoint = `cx_http_request_duration_ms_count{endpoint="GET /v1/models"}`

	before, _ := scrapeHistogramLine(t, endpoint)

	if code, body := req(t, "GET", "/v1/models", nil, buyerKey()); code != http.StatusOK {
		t.Fatalf("GET /v1/models: want 200, got %d: %s", code, body)
	}

	after, ok := scrapeHistogramLine(t, endpoint)
	if !ok {
		t.Fatalf("%s not present after a real request to GET /v1/models", endpoint)
	}
	if after-before != 1 {
		t.Fatalf("http request-duration count for GET /v1/models: want +1, got +%v", after-before)
	}

	// A second call advances it by exactly one more — confirms per-request, per-endpoint.
	req(t, "GET", "/v1/models", nil, buyerKey())
	after2, _ := scrapeHistogramLine(t, endpoint)
	if after2-after != 1 {
		t.Fatalf("second GET /v1/models: want +1 more, got +%v", after2-after)
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// End-to-End Latency 8.5->9 — the class-aware no-peer watchdog.
// ─────────────────────────────────────────────────────────────────────────────

// insertWedgedTask sets up a running PRIMARY task held by a heartbeating worker,
// started long enough ago to be past noPeerWatchdogAfter, on a job that is running.
// Returns the task id. Used by the no-peer watchdog tests.
func insertWedgedTask(t *testing.T, ctx context.Context, jobType, modelRef string, holder uuid.UUID) uuid.UUID {
	t.Helper()
	jobID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb)
		 VALUES ($1,$2,'running',$3,$4,'in','batch',1,0,0)`,
		jobID, demoBuyerUUID, jobType, modelRef); err != nil {
		t.Fatal(err)
	}
	taskID := uuid.New()
	longAgo := time.Now().Add(-2 * noPeerWatchdogAfter)
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, worker_id, claimed_by, claimed_at, started_at, input_ref, result_key, chunk_index, visible_at)
		 VALUES ($1,$2,'running',$3,$3,$4,$4,'in','out',0,$4)`,
		taskID, jobID, holder, longAgo); err != nil {
		t.Fatal(err)
	}
	return taskID
}

// TestNoPeerWatchdogRequeuesWedgedTask proves the End-to-End Latency 8.5->9 rung's
// proof artifact "a synthetic wedged-worker-with-no-peer scenario resolves in a
// bounded time well under 30 minutes, verified in a test": a running task held past
// noPeerWatchdogAfter by a STILL-HEARTBEATING worker (so the dead-claim rescue never
// touches it) with NO eligible same-class peer online is requeued by reapNoPeerWedged
// — instead of waiting out the 30-minute stale reaper. The negative control proves
// the watchdog is class-aware, not a blanket requeuer: when an eligible same-class
// peer DOES exist, hedging owns the straggler and the watchdog leaves it running.
func TestNoPeerWatchdogRequeuesWedgedTask(t *testing.T) {
	ctx := context.Background()
	wk := NewWorkers(itStore, itStorage, stubPayout{})

	t.Run("no eligible peer: wedged task requeued well under 30min", func(t *testing.T) {
		reset(t)
		// The demo worker holds the task and is heartbeating (reset() refreshes its
		// last_seen_at). It supports embed by seed; make sure it is registered so a
		// worker row + capability exists.
		if _, err := itPool.Exec(ctx,
			`UPDATE workers SET supported_jobs=ARRAY['embed'], supported_models=ARRAY['all-minilm-l6-v2'], last_seen_at=now() WHERE id=$1`,
			demoWorkerUUID); err != nil {
			t.Fatal(err)
		}
		taskID := insertWedgedTask(t, ctx, "embed", "all-minilm-l6-v2", demoWorkerUUID)

		before := metrics.noPeerRequeues.Load()
		if err := wk.reapNoPeerWedged(ctx); err != nil {
			t.Fatalf("reapNoPeerWedged: %v", err)
		}
		if got := metrics.noPeerRequeues.Load() - before; got != 1 {
			t.Fatalf("no-peer requeue metric: want +1, got +%d", got)
		}
		var status string
		var claimedBy *uuid.UUID
		if err := itPool.QueryRow(ctx,
			`SELECT status, claimed_by FROM tasks WHERE id=$1`, taskID).Scan(&status, &claimedBy); err != nil {
			t.Fatal(err)
		}
		if status != "retrying" {
			t.Fatalf("wedged task status after watchdog: want 'retrying', got %q", status)
		}
		if claimedBy != nil {
			t.Fatalf("wedged task claimed_by after watchdog: want NULL (unclaimed), got %v", *claimedBy)
		}
	})

	t.Run("eligible same-class peer exists: watchdog leaves the task to hedging", func(t *testing.T) {
		reset(t)
		ensureExtraDemoSuppliers(t, ctx)
		// Anchor worker (demo) plus a DISTINCT-supplier same-class peer that supports
		// the same job/model — a genuinely eligible redundancy/hedge peer.
		if _, err := itPool.Exec(ctx,
			`UPDATE workers SET hw_class='apple_silicon_max', engine='candle', build_hash='bh1',
			        supported_jobs=ARRAY['embed'], supported_models=ARRAY['all-minilm-l6-v2'], last_seen_at=now() WHERE id=$1`,
			demoWorkerUUID); err != nil {
			t.Fatal(err)
		}
		peer := uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO workers (id, supplier_id, hw_class, engine, build_hash, memory_gb, bw_gbps, last_seen_at, version,
			                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
			 VALUES ($1,$2,'apple_silicon_max','candle','bh1',64,400,now(),'seed',
			         ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true)`,
			peer, demoSupplier2UUID); err != nil {
			t.Fatal(err)
		}
		taskID := insertWedgedTask(t, ctx, "embed", "all-minilm-l6-v2", demoWorkerUUID)

		before := metrics.noPeerRequeues.Load()
		if err := wk.reapNoPeerWedged(ctx); err != nil {
			t.Fatalf("reapNoPeerWedged: %v", err)
		}
		if got := metrics.noPeerRequeues.Load() - before; got != 0 {
			t.Fatalf("with an eligible peer the watchdog must NOT requeue: metric moved +%d", got)
		}
		var status string
		if err := itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status != "running" {
			t.Fatalf("task with an eligible peer must stay running (hedging owns it), got %q", status)
		}
	})
}

// ─────────────────────────────────────────────────────────────────────────────
// End-to-End Latency 8->8.5 — cold-model hedge suppression.
// ─────────────────────────────────────────────────────────────────────────────

// insertStragglerFixture sets up a running PRIMARY task past hedgeAfter held by the
// demo worker, plus a DISTINCT-supplier eligible same-class peer — so a hedge WOULD
// fire absent the cold-model suppression. Optionally marks the model warm for the
// holder. Returns the straggler task id.
func insertStragglerFixture(t *testing.T, ctx context.Context, jobType, modelRef string, holderWarm bool) uuid.UUID {
	t.Helper()
	ensureExtraDemoSuppliers(t, ctx)
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', engine='candle', build_hash='bh1',
		        supported_jobs=ARRAY[$2], supported_models=ARRAY[$3], last_seen_at=now() WHERE id=$1`,
		demoWorkerUUID, jobType, modelRef); err != nil {
		t.Fatal(err)
	}
	peer := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, engine, build_hash, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_max','candle','bh1',64,400,now(),'seed',
		         ARRAY[$3],ARRAY[$4],0,true)`,
		peer, demoSupplier2UUID, jobType, modelRef); err != nil {
		t.Fatal(err)
	}
	jobID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb)
		 VALUES ($1,$2,'running',$3,$4,'in','batch',1,0,0)`,
		jobID, demoBuyerUUID, jobType, modelRef); err != nil {
		t.Fatal(err)
	}
	taskID := uuid.New()
	// started_at just past hedgeAfter (a straggler), but well within coldModelLoadAllowance.
	startedAt := time.Now().Add(-hedgeAfter - 5*time.Second)
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, worker_id, claimed_by, claimed_at, started_at, input_ref, result_key, chunk_index, visible_at)
		 VALUES ($1,$2,'running',$3,$3,$4,$4,'in','out',0,$4)`,
		taskID, jobID, demoWorkerUUID, startedAt); err != nil {
		t.Fatal(err)
	}
	if holderWarm {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO worker_model_state (worker_id, model_id, last_seen_warm) VALUES ($1,$2,now())`,
			demoWorkerUUID, modelRef); err != nil {
			t.Fatal(err)
		}
	}
	return taskID
}

// hedgeCountForJob returns how many in-flight hedge tasks exist for a straggler's job.
func hedgeCountForJob(t *testing.T, ctx context.Context, taskID uuid.UUID) int {
	t.Helper()
	var n int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM tasks WHERE hedged_from IS NOT NULL
		   AND job_id = (SELECT job_id FROM tasks WHERE id=$1)`, taskID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// TestColdModelHedgeSuppressed proves the End-to-End Latency 8->8.5 rung's proof
// artifact "a fresh worker's first task on an uncached model no longer triggers a
// hedge to a second worker under the existing 90-second threshold": a straggler whose
// holder does NOT report the model warm (a cold GGUF load in progress) is NOT hedged,
// even though an eligible peer exists and it is past hedgeAfter — the cold-to-cold
// hedge storm is averted. The positive control proves this is model-warmth-specific:
// the SAME fixture with the model marked warm (a genuinely slow warm worker) DOES
// hedge, so suppression is not a blanket disable of hedging.
func TestColdModelHedgeSuppressed(t *testing.T) {
	ctx := context.Background()
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	// reset() does NOT truncate worker_model_state (it is maintained on write, not a
	// volatile per-test table), so the warm-model subtest's inserted row would leak a
	// fresh warm row into a later test's count (e.g. TestWorkerModelStateUpsert's
	// exact-2-rows assertion). Clean it up regardless of test ordering.
	t.Cleanup(func() { itPool.Exec(ctx, `DELETE FROM worker_model_state WHERE worker_id=$1`, demoWorkerUUID) })

	t.Run("cold model: spurious hedge suppressed", func(t *testing.T) {
		reset(t)
		taskID := insertStragglerFixture(t, ctx, "batch_infer", "llama-3.2-1b-instruct-q4", false)

		beforeSuppressed := metrics.coldModelHedgesSuppressed.Load()
		beforeHedges := metrics.hedges.Load()
		if err := wk.hedgeStragglers(ctx); err != nil {
			t.Fatalf("hedgeStragglers: %v", err)
		}
		if got := metrics.coldModelHedgesSuppressed.Load() - beforeSuppressed; got != 1 {
			t.Fatalf("cold-model suppression metric: want +1, got +%d", got)
		}
		if got := metrics.hedges.Load() - beforeHedges; got != 0 {
			t.Fatalf("a cold-model straggler must NOT hedge: hedges moved +%d", got)
		}
		if n := hedgeCountForJob(t, ctx, taskID); n != 0 {
			t.Fatalf("cold-model straggler produced %d hedge task(s); want 0", n)
		}
	})

	t.Run("warm model: genuinely slow worker still hedges", func(t *testing.T) {
		reset(t)
		taskID := insertStragglerFixture(t, ctx, "batch_infer", "llama-3.2-1b-instruct-q4", true)

		beforeSuppressed := metrics.coldModelHedgesSuppressed.Load()
		beforeHedges := metrics.hedges.Load()
		if err := wk.hedgeStragglers(ctx); err != nil {
			t.Fatalf("hedgeStragglers: %v", err)
		}
		if got := metrics.coldModelHedgesSuppressed.Load() - beforeSuppressed; got != 0 {
			t.Fatalf("a warm-model straggler must NOT be cold-suppressed: suppression moved +%d", got)
		}
		if got := metrics.hedges.Load() - beforeHedges; got != 1 {
			t.Fatalf("a warm-model straggler with an eligible peer must hedge: hedges moved +%d (want +1)", got)
		}
		if n := hedgeCountForJob(t, ctx, taskID); n != 1 {
			t.Fatalf("warm-model straggler produced %d hedge task(s); want 1", n)
		}
	})
}
