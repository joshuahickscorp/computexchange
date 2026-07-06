//go:build integration

package main

// planner_integration_test.go — LAYER 2 proof of the fan-out planner (Speed
// Lane wave 1B, planner.go): the REAL control plane — real Postgres, real
// MinIO, real HTTP submit → split → claim → commit → merge — driven by
// concurrent fake workers with DECLARED different rates. The workers fake the
// GPU (they sleep their declared per-chunk time), so this layer proves
// SCHEDULING wall-clock, never tok/s (that boundary is the goal prompt's own
// layer split; tok/s claims live only at L3, the owner-run multi-node runbook).
//
// The A/B switch is fanoutPlannerEnabled (planner.go): OFF reverts every
// wave-1B path (live sizing, planner ETA, endgame racing) to the exact
// pre-wave behavior, so BOTH modes run on one identical harness, same fleet,
// same job — the measured wall-clock difference is attributable to the wave.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

const (
	raceModel   = "llama-3.2-1b-instruct-q4" // seeded catalogue model (batch_infer)
	raceJobType = "batch_infer"
)

// raceWorker is one simulated supplier machine with a DECLARED rate: the
// benchmark tps it registers (feeding worker_tps_cache — what the planner
// reads) and the wall-clock it actually spends per chunk (what the measured
// job wall-clock feels).
type raceWorker struct {
	name     string
	workerID uuid.UUID
	supplier uuid.UUID
	token    string
	declTPS  float32
	perChunk time.Duration
}

// setupRaceFleet inserts the suppliers + worker tokens directly (the same
// shape seed.go uses), then drives the REAL registration + heartbeat HTTP
// paths so benchmark_results, worker_tps_cache and worker_model_state are
// populated by the production code, not by test fixture SQL.
func setupRaceFleet(t *testing.T, ctx context.Context) []*raceWorker {
	t.Helper()
	fleet := []*raceWorker{
		{name: "fastA", declTPS: 200, perChunk: 500 * time.Millisecond},
		{name: "fastB", declTPS: 180, perChunk: 500 * time.Millisecond},
		{name: "slow", declTPS: 12, perChunk: 30 * time.Second},
	}
	for i, w := range fleet {
		w.workerID = uuid.New()
		w.supplier = uuid.New()
		w.token = fmt.Sprintf("race-worker-%d-%s", i, w.workerID)
		if _, err := itPool.Exec(ctx,
			`INSERT INTO suppliers (id, email, reputation, status, data_country)
			 VALUES ($1, $2, 0.90, 'active', 'US')`,
			w.supplier, fmt.Sprintf("race-%s-%s@computexchange.test", w.name, w.workerID)); err != nil {
			t.Fatalf("insert supplier %s: %v", w.name, err)
		}
		// A minimal worker row must exist before its token (FK), exactly like
		// seed.go's ordering; the REAL registration below upserts the actual
		// capability + benchmarks over it.
		if _, err := itPool.Exec(ctx,
			`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, last_seen_at)
			 VALUES ($1, $2, 'apple_silicon_pro', 36, now())`,
			w.workerID, w.supplier); err != nil {
			t.Fatalf("insert worker %s: %v", w.name, err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
			 VALUES ($1, $2, $3, false)`,
			hashKey(w.token), w.workerID, w.supplier); err != nil {
			t.Fatalf("insert token %s: %v", w.name, err)
		}
		// REAL registration path: populates workers + benchmark_results (incl.
		// the measured load_ms) + worker_tps_cache in UpsertWorker's transaction.
		code, body := req(t, "POST", "/v1/worker/register", WorkerCapability{
			HWClass: "apple_silicon_pro", MemoryGB: 36,
			SupportedJobs: []string{raceJobType}, SupportedModels: []string{raceModel},
			Benchmarks: []BenchResult{{ModelID: raceModel, JobType: raceJobType,
				TPS: w.declTPS, ThermalOK: true, LoadMS: 2500}},
		}, hdr{"X-Worker-Token", w.token}, jsonCT())
		if code != 200 {
			t.Fatalf("register %s: %d %s", w.name, code, body)
		}
		sendRaceHeartbeat(t, w) // WARM the model (worker_model_state) via the real heartbeat path
	}
	return fleet
}

func sendRaceHeartbeat(t *testing.T, w *raceWorker) {
	t.Helper()
	code, body := req(t, "POST", "/v1/worker/heartbeat", Heartbeat{
		WorkerID: w.workerID, AvailableMemoryGB: 30, EffectiveMemoryGB: 30,
		LoadedModels: []string{raceModel},
	}, hdr{"X-Worker-Token", w.token}, jsonCT())
	if code != 204 {
		t.Fatalf("heartbeat %s: %d %s", w.name, code, body)
	}
}

// submitRaceJob submits an n-line batch_infer job through the real HTTP path.
// splitSize 0 omits the param (adaptive sizing decides); >0 pins it.
func submitRaceJob(t *testing.T, n, splitSize int) JobSubmitResponse {
	t.Helper()
	var sb strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&sb, `{"prompt":"p%d"}`+"\n", i)
	}
	body := map[string]any{
		"job_type":     map[string]any{"type": raceJobType},
		"model":        map[string]any{"kind": "gguf", "ref": raceModel},
		"constraints":  map[string]any{"min_memory_gb": 8},
		"verification": map[string]any{"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true},
		"tier":         "batch",
		"input":        sb.String(),
	}
	if splitSize > 0 {
		body["params"] = map[string]any{"split_size": splitSize}
	}
	code, out := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("submit: want 202, got %d: %s", code, out)
	}
	var resp JobSubmitResponse
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatalf("submit decode: %v (%s)", err, out)
	}
	return resp
}

// deleteJobRows removes a probe job entirely (events → tasks → job) so it
// never pollutes the queue depth or duration history of the runs after it.
func deleteJobRows(t *testing.T, ctx context.Context, jobID uuid.UUID) {
	t.Helper()
	for _, q := range []string{
		`DELETE FROM job_events WHERE job_id = $1`,
		`DELETE FROM tasks WHERE job_id = $1`,
		`DELETE FROM jobs WHERE id = $1`,
	} {
		if _, err := itPool.Exec(ctx, q, jobID); err != nil {
			t.Fatalf("cleanup %q: %v", q, err)
		}
	}
}

// runSimWorker is the fake supplier agent loop: poll → fetch input → sleep the
// DECLARED per-chunk time → PUT a well-formed batch_infer result → commit.
// A 409 on commit is a lost first-commit-wins race (the pre-existing loser
// contract) — expected for the raced straggler, never an error. Runs until ctx
// is cancelled. t.Errorf (goroutine-safe) reports real failures.
func runSimWorker(ctx context.Context, t *testing.T, w *raceWorker, wg *sync.WaitGroup) {
	defer wg.Done()
	client := &http.Client{Timeout: 10 * time.Second}
	lastHB := time.Now()
	do := func(method, path string, payload any) (int, []byte) {
		var rdr io.Reader
		if payload != nil {
			j, _ := json.Marshal(payload)
			rdr = strings.NewReader(string(j))
		}
		r, _ := http.NewRequestWithContext(ctx, method, itHTTP.URL+path, rdr)
		r.Header.Set("X-Worker-Token", w.token)
		r.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(r)
		if err != nil {
			return 0, nil // ctx cancelled / transient — loop decides
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, b
	}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		if time.Since(lastHB) > 5*time.Second {
			do("POST", "/v1/worker/heartbeat", Heartbeat{
				WorkerID: w.workerID, AvailableMemoryGB: 30, EffectiveMemoryGB: 30,
				LoadedModels: []string{raceModel},
			})
			lastHB = time.Now()
		}
		code, body := do("GET", "/v1/worker/poll", nil)
		if code != 200 {
			select {
			case <-ctx.Done():
				return
			case <-time.After(150 * time.Millisecond):
			}
			continue
		}
		var disp TaskDispatch
		if err := json.Unmarshal(body, &disp); err != nil {
			t.Errorf("%s: dispatch decode: %v", w.name, err)
			return
		}
		// Count the chunk's input lines so the result is well-formed for merge.
		lines := 1
		if in, err := http.Get(disp.InputURL); err == nil {
			b, _ := io.ReadAll(in.Body)
			in.Body.Close()
			lines = 0
			for _, ln := range strings.Split(string(b), "\n") {
				if strings.TrimSpace(ln) != "" {
					lines++
				}
			}
		}
		// The DECLARED rate: this worker takes perChunk of wall-clock per chunk.
		select {
		case <-ctx.Done():
			return
		case <-time.After(w.perChunk):
		}
		comps := make([]string, lines)
		for i := range comps {
			comps[i] = fmt.Sprintf("completion by %s", w.name)
		}
		res, _ := json.Marshal(map[string]any{"job_type": raceJobType, "completions": comps})
		if err := itStorage.PutObject(ctx, disp.ResultKey, res, "application/json"); err != nil {
			if ctx.Err() == nil {
				t.Errorf("%s: put result: %v", w.name, err)
			}
			return
		}
		commit := TaskCommit{TaskID: disp.TaskID, ResultKey: disp.ResultKey,
			DurationMS: uint64(w.perChunk.Milliseconds()), TokensUsed: 8}
		if code, b := do("POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit); code != 204 && code != 409 && ctx.Err() == nil {
			// 204 = committed; 409 = lost first-commit-wins (raced straggler) — both real contracts.
			t.Errorf("%s: commit: %d %s", w.name, code, b)
			return
		}
	}
}

// waitJobComplete polls the real buyer status endpoint until the job is
// complete, returning the measured wall-clock since start. Fails the test
// (and returns) after timeout.
func waitJobComplete(t *testing.T, jobID uuid.UUID, start time.Time, timeout time.Duration) time.Duration {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		code, body := req(t, "GET", "/v1/jobs/"+jobID.String(), nil, buyerKey())
		if code != 200 {
			t.Fatalf("get job: %d %s", code, body)
		}
		var js JobStatus
		if err := json.Unmarshal(body, &js); err != nil {
			t.Fatalf("status decode: %v", err)
		}
		switch js.Status {
		case "complete":
			return time.Since(start)
		case "failed", "cancelled":
			t.Fatalf("job %s went %s instead of complete", jobID, js.Status)
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("job %s not complete within %s", jobID, timeout)
	return 0
}

// TestFanoutPlannerLiveSizingETAAndEndgameRace is the L2 proof, in three acts
// on ONE harness (real PG + MinIO + HTTP control plane, three concurrent fake
// workers with declared rates 200/180/12 tps):
//
//	A. SIZING+ETA PROBES — the same 6-record generative submit, planner OFF
//	   then ON: OFF reproduces the pre-wave static split (1 chunk) and the
//	   blunt wave ETA; ON must split into >=2 chunks (the planner width floor
//	   making fan-out achievable at all) and produce a heterogeneity-aware ETA
//	   that reflects the slow node (strictly above the blunt estimate).
//	B. MEASURED BASELINE — planner OFF, 6 chunks, all three workers polling:
//	   the slow worker claims a chunk and the job's wall-clock IS its 30s
//	   declared time (the pre-wave tail: the 90s hedge never fires).
//	C. MEASURED ENDGAME RACE — planner ON, identical job + fleet: once the
//	   queue empties, raceEndgameTails duplicates the slow chunk onto the
//	   FASTEST idle warm peer, first-commit-wins cancels the straggler, the
//	   merge dedupes, and the measured wall-clock beats the baseline by the
//	   tail it cut.
func TestFanoutPlannerLiveSizingETAAndEndgameRace(t *testing.T) {
	reset(t)
	ctx := context.Background()

	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })

	// Deterministic history: the drift p90 and any stale demo-worker rate rows
	// would perturb the ETA/sizing probes (task_durations and worker_tps_cache
	// are deliberately durable across reset — see their own comments).
	if _, err := itPool.Exec(ctx,
		`DELETE FROM task_durations WHERE job_type=$1 AND model_ref=$2`, raceJobType, raceModel); err != nil {
		t.Fatalf("clean task_durations: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`DELETE FROM worker_tps_cache WHERE worker_id=$1`, demoWorkerUUID); err != nil {
		t.Fatalf("clean demo tps cache: %v", err)
	}

	fleet := setupRaceFleet(t, ctx)

	// ---- Act A: sizing + ETA probes (no workers polling yet) ----
	fanoutPlannerEnabled.Store(false)
	probeA := submitRaceJob(t, 6, 0)
	if probeA.TaskCount != 1 {
		t.Fatalf("baseline static sizing: want 1 chunk for 6 generative records (static map 4 items/s × 45s), got %d", probeA.TaskCount)
	}
	if probeA.ETASecs != 45 {
		t.Fatalf("baseline blunt ETA: want 45s (1 wave × static 45s/task), got %d", probeA.ETASecs)
	}
	deleteJobRows(t, ctx, probeA.JobID)

	fanoutPlannerEnabled.Store(true)
	probeB := submitRaceJob(t, 6, 0)
	if probeB.TaskCount < 2 || probeB.TaskCount > 3 {
		t.Fatalf("planner live sizing: want the width floor to split 6 records into 2-3 chunks (fan-out achievable), got %d", probeB.TaskCount)
	}
	if probeB.ETASecs <= probeA.ETASecs {
		t.Fatalf("planner ETA must reflect the heterogeneous fleet (slow node + indivisible tasks): got %ds, baseline %ds", probeB.ETASecs, probeA.ETASecs)
	}
	if probeB.ETASecs > 150 {
		t.Fatalf("planner ETA implausibly large: %ds", probeB.ETASecs)
	}
	t.Logf("L2 probes: task_count baseline=%d planner=%d; eta baseline=%ds planner=%ds",
		probeA.TaskCount, probeB.TaskCount, probeA.ETASecs, probeB.ETASecs)
	deleteJobRows(t, ctx, probeB.JobID)

	// ---- Acts B + C: measured wall-clock, same harness, toggle A/B ----
	// split_size=1 pins 6 chunks for BOTH modes so the measured difference is
	// the ENDGAME RACE alone (sizing was proven in act A; here it is held
	// constant deliberately).
	wk := NewWorkers(itStore, itStorage, stubPayout{})

	runMode := func(mode string, enabled bool) (time.Duration, uuid.UUID) {
		fanoutPlannerEnabled.Store(enabled)
		wctx, cancel := context.WithCancel(ctx)
		defer cancel()
		var wg sync.WaitGroup
		for _, w := range fleet {
			wg.Add(1)
			go runSimWorker(wctx, t, w, &wg)
		}
		// The endgame sweep on a tight test cadence (prod: its own 5s ticker in
		// Workers.Run — the sweep FUNCTION is the real production code either
		// way; with the planner disabled it is a no-op by the same gate
		// production honors).
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-wctx.Done():
					return
				case <-time.After(500 * time.Millisecond):
					if err := wk.raceEndgameTails(wctx); err != nil && wctx.Err() == nil {
						t.Errorf("raceEndgameTails: %v", err)
					}
				}
			}
		}()
		start := time.Now()
		job := submitRaceJob(t, 6, 1)
		if job.TaskCount != 6 {
			t.Fatalf("%s: want 6 pinned chunks, got %d", mode, job.TaskCount)
		}
		elapsed := waitJobComplete(t, job.JobID, start, 70*time.Second)
		cancel()
		wg.Wait()
		t.Logf("L2 MEASURED %s wall-clock: %s (job %s)", mode, elapsed.Round(10*time.Millisecond), job.JobID)
		return elapsed, job.JobID
	}

	baseline, baseJob := runMode("baseline (planner OFF)", false)
	raced, raceJob := runMode("planner+endgame (ON)", true)

	// (a) the measured wall-clock win, with generous margins against CI jitter:
	// baseline is pinned to the slow worker's declared 30s chunk; the raced run
	// must land near minRun(10s)+sweep+fast-dupe, far under the baseline.
	if baseline < 25*time.Second {
		t.Fatalf("baseline sanity: expected the 30s straggler to dominate, measured %s", baseline)
	}
	if raced > 20*time.Second {
		t.Fatalf("endgame race did not cut the tail: measured %s (baseline %s)", raced, baseline)
	}
	if raced > baseline*6/10 {
		t.Fatalf("want raced < 0.6×baseline: raced %s baseline %s", raced, baseline)
	}

	// (b) the race demonstrably FIRED through the real machinery: a hedge-task
	// row exists on the raced job only, pinned to the FASTEST idle warm peer
	// (fastA, 200 tps — the rankPeersBySpeed contract), the raced straggler was
	// cancelled by first-commit-wins, and the duplicate committed.
	var baseHedges int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM tasks WHERE job_id=$1 AND hedged_from IS NOT NULL`, baseJob).Scan(&baseHedges); err != nil {
		t.Fatalf("count baseline hedges: %v", err)
	}
	if baseHedges != 0 {
		t.Fatalf("baseline must have no hedges/races (90s window never reached), got %d", baseHedges)
	}
	var dupe, original uuid.UUID
	var dupeStatus string
	var dupeWorker uuid.UUID
	err := itPool.QueryRow(ctx,
		`SELECT id, hedged_from, status, claimed_by FROM tasks
		 WHERE job_id=$1 AND hedged_from IS NOT NULL`, raceJob).Scan(&dupe, &original, &dupeStatus, &dupeWorker)
	if err != nil {
		t.Fatalf("endgame race row missing on raced job: %v", err)
	}
	if dupeStatus != "complete" {
		t.Fatalf("race duplicate must have committed (first commit wins), status=%s", dupeStatus)
	}
	if dupeWorker != fleet[0].workerID {
		t.Fatalf("race must pin the FASTEST idle warm peer (fastA %s), got %s", fleet[0].workerID, dupeWorker)
	}
	var origStatus string
	if err := itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, original).Scan(&origStatus); err != nil {
		t.Fatalf("original straggler row: %v", err)
	}
	if origStatus != "failed" {
		t.Fatalf("first-commit-wins must cancel the raced straggler, status=%s", origStatus)
	}

	// (c) the merge deduped: exactly 6 result lines, none from the slow worker
	// (its chunk was raced and its own commit lost), artifact readable.
	out, err := itStorage.GetObject(ctx, fmt.Sprintf("jobs/%s/output.jsonl", raceJob))
	if err != nil {
		t.Fatalf("merged output: %v", err)
	}
	nLines := 0
	for _, ln := range strings.Split(string(out), "\n") {
		if strings.TrimSpace(ln) != "" {
			nLines++
		}
	}
	if nLines != 6 {
		t.Fatalf("merge dedupe: want exactly 6 result lines, got %d:\n%s", nLines, out)
	}
	t.Logf("L2 MEASURED: baseline %s → planner+endgame %s (%.1fx wall-clock cut), race fired once, merge deduped to %d lines",
		baseline.Round(10*time.Millisecond), raced.Round(10*time.Millisecond),
		float64(baseline)/float64(raced), nLines)
}
