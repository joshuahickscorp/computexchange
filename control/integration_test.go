//go:build integration

package main

// integration_test.go — the brutal deterministic proof matrix. Runs the REAL
// control plane (handlers, store, verifier, background workers) against a REAL
// Postgres + REAL MinIO, with the supplier agent SIMULATED (we PUT result objects
// and POST commits directly) so the whole lifecycle is exercised without the
// Metal model download — that live path is proven separately by scripts/prove-local.sh.
//
// Gated behind `//go:build integration` so a plain `go test ./...` (no infra)
// stays green; prove-local and CI run it with `-tags integration` plus
// DATABASE_URL + S3_* pointed at a live stack. Missing infra under the tag is a
// loud failure, never a silent skip (BLACKHOLE: surface every failure).
//
// Each capability is one Test*; reset() truncates the volatile tables and
// restores the demo supplier between them so they are order-independent.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	itPool    *pgxpool.Pool
	itStore   *Store
	itStorage *Storage
	itServer  *Server
	itHTTP    *httptest.Server
)

// demo identities mirror seed.go (the seed runs in TestMain).
var (
	demoSupplierUUID = uuid.MustParse(demoSupplierID)
	demoWorkerUUID   = uuid.MustParse(demoWorkerID)
	demoBuyerUUID    = uuid.MustParse(demoBuyerID)
)

func TestMain(m *testing.M) {
	ctx := context.Background()
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		fmt.Fprintln(os.Stderr, "integration: DATABASE_URL unset — run via scripts/prove-local.sh (it provisions Postgres + MinIO)")
		os.Exit(2)
	}
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		fmt.Fprintf(os.Stderr, "integration: pgx pool: %v\n", err)
		os.Exit(2)
	}
	defer pool.Close()
	itPool = pool
	itStore = NewStore(pool)
	if err := itStore.Migrate(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "integration: migrate: %v\n", err)
		os.Exit(2)
	}
	if err := seedDemo(ctx, pool); err != nil {
		fmt.Fprintf(os.Stderr, "integration: seed: %v\n", err)
		os.Exit(2)
	}
	st, err := NewStorage(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "integration: storage (MinIO): %v\n", err)
		os.Exit(2)
	}
	itStorage = st
	itServer = NewServer(itStore, itStorage, NewVerifier(itStore), stubPayout{})
	itHTTP = httptest.NewServer(itServer.Routes())
	defer itHTTP.Close()
	os.Exit(m.Run())
}

// reset truncates the per-test volatile tables and restores the demo supplier to
// a clean, active, 0.90-reputation state so tests do not bleed into each other.
func reset(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	if _, err := itPool.Exec(ctx,
		`TRUNCATE tasks, jobs, webhooks, ledger_entries, benchmark_results, disputes, verification_events RESTART IDENTITY CASCADE`); err != nil {
		t.Fatalf("reset truncate: %v", err)
	}
	// Workers/worker_tokens are NOT truncated above (FK from worker_tokens). Drop
	// every NON-demo worker a prior test left behind so peer selection (tiebreak,
	// hedge, redundancy) is deterministic — each test starts with exactly the demo
	// worker plus whatever it inserts itself. Without this, a leftover same-class
	// worker leaks across tests and steals the pinned peer.
	if _, err := itPool.Exec(ctx, `DELETE FROM worker_tokens WHERE worker_id <> $1`, demoWorkerUUID); err != nil {
		t.Fatalf("reset worker_tokens: %v", err)
	}
	if _, err := itPool.Exec(ctx, `DELETE FROM workers WHERE id <> $1`, demoWorkerUUID); err != nil {
		t.Fatalf("reset workers: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE suppliers SET reputation = 0.90, status = 'active' WHERE id = $1`, demoSupplierUUID); err != nil {
		t.Fatalf("reset supplier: %v", err)
	}
	// Demo worker must exist + be live for poll claims.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET last_seen_at = now() WHERE id = $1`, demoWorkerUUID); err != nil {
		t.Fatalf("reset worker: %v", err)
	}
}

// --- HTTP helpers ---

type hdr struct{ k, v string }

func req(t *testing.T, method, path string, body any, headers ...hdr) (int, []byte) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		switch b := body.(type) {
		case string:
			rdr = strings.NewReader(b)
		case []byte:
			rdr = bytes.NewReader(b)
		default:
			j, _ := json.Marshal(b)
			rdr = bytes.NewReader(j)
		}
	}
	r, err := http.NewRequest(method, itHTTP.URL+path, rdr)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	for _, h := range headers {
		r.Header.Set(h.k, h.v)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatalf("do %s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, out
}

func buyerKey() hdr  { return hdr{"Authorization", "Bearer " + demoAPIKey} }
func adminKey() hdr  { return hdr{"Authorization", "Bearer " + demoAdminAPIKey} }
func workerTok() hdr { return hdr{"X-Worker-Token", demoWorkerToken} }
func jsonCT() hdr    { return hdr{"Content-Type", "application/json"} }

// embedResultJSON builds a deterministic embed result blob with n unit vectors.
func embedResultJSON(n int) []byte {
	vecs := make([][]float64, n)
	for i := range vecs {
		v := make([]float64, 384)
		v[i%384] = 1.0 // a distinct unit vector per record
		vecs[i] = v
	}
	b, _ := json.Marshal(map[string]any{"job_type": "embed", "model": "all-minilm-l6-v2", "dim": 384, "count": n, "vectors": vecs})
	return b
}

// submitEmbedJob submits an embed job with the given input lines + policy and
// returns the job id and task count.
func submitEmbedJob(t *testing.T, lines int, redFrac, honeyFrac float32, holdSecs uint32) (uuid.UUID, int) {
	t.Helper()
	var sb strings.Builder
	for i := 0; i < lines; i++ {
		fmt.Fprintf(&sb, `{"id":"r%d","text":"record %d"}`+"\n", i, i)
	}
	body := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"params":       map[string]any{"split_size": 1000},
		"constraints":  map[string]any{"min_memory_gb": 2},
		"verification": map[string]any{"redundancy_frac": redFrac, "honeypot_frac": honeyFrac, "payout_hold_secs": holdSecs},
		"tier":         "batch",
		"input":        sb.String(),
	}
	code, out := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("submit: want 202, got %d: %s", code, out)
	}
	var r JobSubmitResponse
	if err := json.Unmarshal(out, &r); err != nil {
		t.Fatalf("submit decode: %v (%s)", err, out)
	}
	return r.JobID, r.TaskCount
}

// --- 1. health + metrics ---

func TestHealthAndMetrics(t *testing.T) {
	reset(t)
	if code, _ := req(t, "GET", "/healthz", nil); code != 200 {
		t.Fatalf("healthz: want 200, got %d", code)
	}
	code, body := req(t, "GET", "/metrics", nil)
	if code != 200 {
		t.Fatalf("metrics: want 200, got %d", code)
	}
	for _, name := range []string{
		"cx_jobs_submitted_total", "cx_tasks_dispatched_total", "cx_tasks_completed_total",
		"cx_verification_mismatch_total", "cx_payouts_released_total", "cx_active_workers",
	} {
		if !strings.Contains(string(body), name) {
			t.Errorf("metrics missing %q", name)
		}
	}
}

// --- 2. auth: 401 / 403 paths, no silent bypass ---

func TestAuth(t *testing.T) {
	reset(t)
	cases := []struct {
		name   string
		method string
		path   string
		want   int
		hdrs   []hdr
	}{
		{"buyer no token", "GET", "/v1/models", 401, nil},
		{"buyer bad token", "GET", "/v1/models", 401, []hdr{{"Authorization", "Bearer nope"}}},
		{"buyer good token", "GET", "/v1/models", 200, []hdr{buyerKey()}},
		{"admin via non-admin key", "GET", "/admin/workers", 403, []hdr{buyerKey()}},
		{"admin via admin key", "GET", "/admin/workers", 200, []hdr{adminKey()}},
		{"worker no token", "GET", "/v1/worker/poll", 401, nil},
		{"worker bad token", "GET", "/v1/worker/poll", 401, []hdr{{"X-Worker-Token", "garbage"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			code, body := req(t, c.method, c.path, nil, c.hdrs...)
			if code != c.want {
				t.Fatalf("want %d, got %d: %s", c.want, code, body)
			}
		})
	}
}

// --- 3. malformed submissions all fail cleanly (4xx, no partial job) ---

func TestMalformedSubmissions(t *testing.T) {
	reset(t)
	bad := []struct {
		name string
		body string
	}{
		{"invalid json", `{`},
		{"missing job_type", `{"model":{"kind":"gguf","ref":"x"},"input":"{}"}`},
		{"bad tier", `{"job_type":{"type":"embed"},"tier":"bogus","input":"{\"a\":1}"}`},
		{"bad hw_class", `{"job_type":{"type":"embed"},"constraints":{"hw_classes":["nonsense"]},"input":"{\"a\":1}"}`},
		{"insecure webhook", `{"job_type":{"type":"embed"},"webhook_url":"http://x","input":"{\"a\":1}"}`},
		{"empty input", `{"job_type":{"type":"embed"},"input":""}`},
		{"missing input", `{"job_type":{"type":"embed"}}`},
	}
	for _, c := range bad {
		t.Run(c.name, func(t *testing.T) {
			code, body := req(t, "POST", "/v1/jobs", c.body, buyerKey(), jsonCT())
			if code < 400 || code >= 500 {
				t.Fatalf("want 4xx, got %d: %s", code, body)
			}
		})
	}
	// No jobs should have been created by any malformed request.
	var n int
	if err := itPool.QueryRow(context.Background(), `SELECT count(*) FROM jobs`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("malformed submissions leaked %d job rows", n)
	}
}

// --- 4. worker registration ---

func TestWorkerRegister(t *testing.T) {
	reset(t)
	cap := WorkerCapability{
		HWClass: "apple_silicon_max", MemoryGB: 64, MemoryBwGbps: 400,
		SupportedJobs: []string{"embed"}, AgentVersion: "test", OSVersion: "macOS",
	}
	code, body := req(t, "POST", "/v1/worker/register", cap, workerTok(), jsonCT())
	if code != 200 {
		t.Fatalf("register: want 200, got %d: %s", code, body)
	}
	var echoed WorkerCapability
	if err := json.Unmarshal(body, &echoed); err != nil {
		t.Fatalf("decode echo: %v", err)
	}
	if echoed.WorkerID != demoWorkerUUID {
		t.Fatalf("register bound wrong worker_id: %s", echoed.WorkerID)
	}
	// bad hw_class rejected
	if code, _ := req(t, "POST", "/v1/worker/register",
		WorkerCapability{HWClass: "frobnicator"}, workerTok(), jsonCT()); code != 400 {
		t.Fatalf("bad hw_class: want 400, got %d", code)
	}
}

// --- 5. MinIO object flow: put/get round-trip + presigned GET/PUT ---

func TestObjectFlow(t *testing.T) {
	reset(t)
	ctx := context.Background()
	key := "it/object-flow/" + uuid.NewString() + ".bin"
	payload := []byte("compute-exchange-object-flow-" + uuid.NewString())
	if err := itStorage.PutObject(ctx, key, payload, "application/octet-stream"); err != nil {
		t.Fatalf("put: %v", err)
	}
	got, err := itStorage.GetObject(ctx, key)
	if err != nil || !bytes.Equal(got, payload) {
		t.Fatalf("get round-trip: %v got=%q", err, got)
	}
	// Presigned GET is fetchable over plain HTTP.
	gurl, err := itStorage.PresignGet(ctx, key, time.Minute)
	if err != nil {
		t.Fatalf("presign get: %v", err)
	}
	resp, err := http.Get(gurl)
	if err != nil {
		t.Fatalf("http get presigned: %v", err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(b, payload) {
		t.Fatalf("presigned GET body mismatch: %q", b)
	}
	// Presigned PUT stores an object the control side can read back.
	pkey := "it/object-flow/put-" + uuid.NewString() + ".bin"
	purl, err := itStorage.PresignPut(ctx, pkey, time.Minute)
	if err != nil {
		t.Fatalf("presign put: %v", err)
	}
	put, _ := http.NewRequest("PUT", purl, bytes.NewReader(payload))
	if r, err := http.DefaultClient.Do(put); err != nil || r.StatusCode/100 != 2 {
		t.Fatalf("http put presigned: %v status=%v", err, r)
	}
	if got, err := itStorage.GetObject(ctx, pkey); err != nil || !bytes.Equal(got, payload) {
		t.Fatalf("get after presigned put: %v", err)
	}
}

// --- 6. embed happy path (agent simulated) → complete + ledger ---

func TestEmbedHappyPathSimulated(t *testing.T) {
	reset(t)
	ctx := context.Background()
	beforeSub := metrics.jobsSubmitted.Load()
	beforeDone := metrics.tasksCompleted.Load()

	if code, body := req(t, "POST", "/v1/worker/register",
		WorkerCapability{HWClass: "apple_silicon_max", MemoryGB: 64,
			SupportedJobs: []string{"embed"}, SupportedModels: []string{"all-minilm-l6-v2"}},
		workerTok(), jsonCT()); code != 200 {
		t.Fatalf("register: %d %s", code, body)
	}
	jobID, taskCount := submitEmbedJob(t, 1, 0, 0, 0)
	if taskCount != 1 {
		t.Fatalf("want 1 task, got %d", taskCount)
	}

	// Poll → claim the task.
	code, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	if code != 200 {
		t.Fatalf("poll: want 200, got %d: %s", code, body)
	}
	var disp TaskDispatch
	if err := json.Unmarshal(body, &disp); err != nil {
		t.Fatalf("dispatch decode: %v", err)
	}
	// The presigned input URL must actually serve the chunk.
	if r, err := http.Get(disp.InputURL); err != nil || r.StatusCode != 200 {
		t.Fatalf("fetch presigned input: %v status=%v", err, r)
	} else {
		r.Body.Close()
	}

	// Simulate the agent: PUT a result object at the canonical key, then commit.
	if err := itStorage.PutObject(ctx, disp.ResultKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatalf("put result: %v", err)
	}
	commit := TaskCommit{TaskID: disp.TaskID, ResultKey: disp.ResultKey, DurationMS: 12, TokensUsed: 8}
	if code, body := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("commit: want 204, got %d: %s", code, body)
	}

	// Job should be complete (hold 0, single task) and a supplier credit written.
	code, body = req(t, "GET", "/v1/jobs/"+jobID.String(), nil, buyerKey())
	if code != 200 {
		t.Fatalf("get job: %d %s", code, body)
	}
	var js JobStatus
	json.Unmarshal(body, &js)
	if js.Status != "complete" {
		t.Fatalf("job status: want complete, got %q", js.Status)
	}
	var credits int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries le JOIN tasks t ON t.id=le.task_id
		WHERE t.job_id=$1 AND le.kind='supplier_credit'`, jobID).Scan(&credits)
	if credits != 1 {
		t.Fatalf("want 1 supplier_credit, got %d", credits)
	}
	// Results endpoint returns presigned URLs.
	if code, body := req(t, "GET", "/v1/jobs/"+jobID.String()+"/results", nil, buyerKey()); code != 200 {
		t.Fatalf("results: %d %s", code, body)
	}
	if metrics.jobsSubmitted.Load() <= beforeSub || metrics.tasksCompleted.Load() <= beforeDone {
		t.Fatalf("metrics did not advance (sub %d->%d done %d->%d)",
			beforeSub, metrics.jobsSubmitted.Load(), beforeDone, metrics.tasksCompleted.Load())
	}
}

// --- 6b. quote-to-actual drift: a committed task records its real duration ---

// Plane D D6 / errata C-Errata-6: every COMMITTED task writes one task_durations row
// carrying the worker's reported wall-time, job_type, model_ref + split_size, so the
// Exchange Brain can learn an observed p90. A second (duplicate) commit is a 409 that
// records NOTHING — the duration is written inside the commit transaction, so a task
// that does not truly commit cannot poison the estimate. /admin/drift then reflects it.
func TestTaskDurationRecorded(t *testing.T) {
	reset(t)
	ctx := context.Background()
	itPool.Exec(ctx, `TRUNCATE task_durations`)
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE task_durations`) })

	if code, body := req(t, "POST", "/v1/worker/register",
		WorkerCapability{HWClass: "apple_silicon_max", MemoryGB: 64,
			SupportedJobs: []string{"embed"}, SupportedModels: []string{"all-minilm-l6-v2"}},
		workerTok(), jsonCT()); code != 200 {
		t.Fatalf("register: %d %s", code, body)
	}
	jobID, _ := submitEmbedJob(t, 1, 0, 0, 0)

	_, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	var disp TaskDispatch
	if err := json.Unmarshal(body, &disp); err != nil {
		t.Fatalf("dispatch decode: %v", err)
	}
	itStorage.PutObject(ctx, disp.ResultKey, embedResultJSON(1), "application/json")

	const wantDur = 1273
	commit := TaskCommit{TaskID: disp.TaskID, ResultKey: disp.ResultKey, DurationMS: wantDur, TokensUsed: 8}
	if code, b := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("commit: want 204, got %d: %s", code, b)
	}

	// Exactly one duration row for this job, with the worker's reported duration and
	// the job's (job_type, model_ref, split_size) threaded through from the commit.
	var rows int
	var durMs, splitSize int64
	var jobType, modelRef string
	if err := itPool.QueryRow(ctx,
		`SELECT count(*), COALESCE(max(duration_ms),0), COALESCE(max(split_size),0),
		        COALESCE(max(job_type),''), COALESCE(max(model_ref),'')
		   FROM task_durations WHERE job_id=$1`, jobID,
	).Scan(&rows, &durMs, &splitSize, &jobType, &modelRef); err != nil {
		t.Fatalf("read task_durations: %v", err)
	}
	if rows != 1 || durMs != wantDur {
		t.Fatalf("want 1 duration row of %dms, got rows=%d dur=%d", wantDur, rows, durMs)
	}
	if jobType != "embed" || modelRef != "all-minilm-l6-v2" || splitSize != 1000 {
		t.Fatalf("duration row context wrong: job_type=%q model=%q split=%d", jobType, modelRef, splitSize)
	}

	// A duplicate commit is a 409 and must NOT add a second row (no poisoning).
	if code, _ := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 409 {
		t.Fatalf("duplicate commit: want 409, got %d", code)
	}
	itPool.QueryRow(ctx, `SELECT count(*) FROM task_durations WHERE job_id=$1`, jobID).Scan(&rows)
	if rows != 1 {
		t.Fatalf("duplicate commit must not record a second duration row, got %d", rows)
	}

	// The admin drift rollup surfaces this (job_type, model) with its real actuals.
	code, drift := req(t, "GET", "/admin/drift", nil, adminKey())
	if code != 200 {
		t.Fatalf("GET /admin/drift: %d %s", code, drift)
	}
	var dr []DriftRow
	if err := json.Unmarshal(drift, &dr); err != nil {
		t.Fatalf("decode drift: %v\n%s", err, drift)
	}
	found := false
	for _, d := range dr {
		if d.JobType == "embed" && d.ModelRef == "all-minilm-l6-v2" {
			found = true
			if d.Samples < 1 || d.P90DurationMs != wantDur || d.AvgDurationMs != wantDur {
				t.Fatalf("drift row wrong: %+v", d)
			}
		}
	}
	if !found {
		t.Fatalf("drift rollup missing the embed/all-minilm-l6-v2 row: %s", drift)
	}
}

// --- 7. duplicate commit is idempotent: second → 409, credited once ---

func TestDuplicateCommitIdempotent(t *testing.T) {
	reset(t)
	ctx := context.Background()
	req(t, "POST", "/v1/worker/register", WorkerCapability{HWClass: "apple_silicon_max", MemoryGB: 64,
		SupportedJobs: []string{"embed"}, SupportedModels: []string{"all-minilm-l6-v2"}}, workerTok(), jsonCT())
	jobID, _ := submitEmbedJob(t, 1, 0, 0, 0)

	_, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	var disp TaskDispatch
	json.Unmarshal(body, &disp)
	itStorage.PutObject(ctx, disp.ResultKey, embedResultJSON(1), "application/json")
	commit := TaskCommit{TaskID: disp.TaskID, ResultKey: disp.ResultKey}

	if code, _ := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("first commit not 204: %d", code)
	}
	if code, _ := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 409 {
		t.Fatalf("second commit: want 409, got %d", code)
	}
	var credits int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries le JOIN tasks t ON t.id=le.task_id
		WHERE t.job_id=$1 AND le.kind='supplier_credit'`, jobID).Scan(&credits)
	if credits != 1 {
		t.Fatalf("double-commit double-credited: %d supplier_credit rows", credits)
	}
}

// --- 8. redundancy verification: match passes, mismatch is penalized ---

func TestRedundancyVerify(t *testing.T) {
	t.Run("match", func(t *testing.T) {
		reset(t)
		info := &CommitTaskInfo{TaskID: uuid.New(), JobID: uuid.New(), WorkerID: demoWorkerUUID,
			SupplierID: demoSupplierUUID, jobType: "embed", HWClass: "apple_silicon_max"}
		out, err := itServer.verifier.verifyTaskResult(context.Background(), info,
			TaskCommit{TaskID: info.TaskID}, embedResultJSON(1), embedResultJSON(1))
		if err != nil || out != OutcomePass {
			t.Fatalf("match: out=%v err=%v (want pass)", out, err)
		}
		if rep := supplierRep(t); rep <= 0.90 {
			t.Fatalf("match should credit reputation, got %v", rep)
		}
	})
	t.Run("mismatch detected, primary provisionally trusted", func(t *testing.T) {
		reset(t)
		a := embedResultJSON(1)              // unit vector e0
		b := []byte(`{"vectors":[[0,1,0]]}`) // orthogonal → cosine 0 < 0.999
		info := &CommitTaskInfo{TaskID: uuid.New(), JobID: uuid.New(), WorkerID: demoWorkerUUID,
			SupplierID: demoSupplierUUID, jobType: "embed", HWClass: "apple_silicon_max"}
		out, err := itServer.verifier.verifyTaskResult(context.Background(), info,
			TaskCommit{TaskID: info.TaskID}, a, b)
		// A 2-way mismatch is DETECTED → pass_with_penalty (api.go bumps the
		// cx_verification_mismatch metric on this outcome). With no third opinion
		// the primary is provisionally trusted, so it earns NO success credit —
		// the honest difference from a clean match (which rises to ~0.902). The
		// hard dock+clawback of confirmed fraud is proven by the honeypot path.
		if err != nil || out != OutcomePassWithPenalty {
			t.Fatalf("mismatch: out=%v err=%v (want pass_with_penalty)", out, err)
		}
		if rep := supplierRep(t); rep < 0.899 || rep > 0.901 {
			t.Fatalf("mismatch must neither credit nor dock the provisional primary; want ~0.90, got %v", rep)
		}
	})
}

// --- 9. honeypot verification: pass credits, fraud claws back + requeues ---

func TestHoneypotVerify(t *testing.T) {
	ctx := context.Background()
	t.Run("pass", func(t *testing.T) {
		reset(t)
		info := &CommitTaskInfo{TaskID: uuid.New(), JobID: uuid.New(), WorkerID: demoWorkerUUID,
			SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotEmbedRef, jobType: "embed"}
		// known answer is {"vectors":[[1,0,0]]}; commit the same.
		out, err := itServer.verifier.verifyTaskResult(ctx, info,
			TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
		if err != nil || out != OutcomePass {
			t.Fatalf("honeypot pass: out=%v err=%v", out, err)
		}
	})
	t.Run("fraud", func(t *testing.T) {
		reset(t)
		// Real task + a prior credit so the clawback has something to reverse.
		jobID := uuid.New()
		taskID := uuid.New()
		mustJobTask(t, jobID, taskID, true /*honeypot*/, false, demoHoneypotEmbedRef)
		credit := -0.001 // buyer charge placeholder not needed; insert a positive credit
		_ = credit
		if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
			Kind: KindSupplierCredit, SupplierID: &demoSupplierUUID, TaskID: &taskID,
			AmountUSD: 0.005, PayoutStatus: PayoutHeld,
		}}); err != nil {
			t.Fatal(err)
		}
		info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
			SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotEmbedRef, jobType: "embed"}
		out, err := itServer.verifier.verifyTaskResult(ctx, info,
			TaskCommit{TaskID: taskID}, []byte(`{"vectors":[[0,1,0]]}`), nil) // wrong answer
		if err != nil || out != OutcomeFail {
			t.Fatalf("honeypot fraud: out=%v err=%v (want fail)", out, err)
		}
		// Clawback row written, original credit clawed back, task requeued, rep docked.
		var clawbacks int
		itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE task_id=$1 AND kind='clawback'`, taskID).Scan(&clawbacks)
		if clawbacks != 1 {
			t.Fatalf("want 1 clawback, got %d", clawbacks)
		}
		var status string
		itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&status)
		if status != "retrying" {
			t.Fatalf("fraud task should requeue (retrying), got %q", status)
		}
		if rep := supplierRep(t); rep > 0.80 {
			t.Fatalf("honeypot fraud should dock reputation hard (~0.75), got %v", rep)
		}
	})
}

// --- 10. stale running task requeue ---

func TestStaleRequeue(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/x/tasks/0/input.jsonl")
	// Claim it and backdate the claim past the stale timeout.
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='running', claimed_by=$2, claimed_at=now()-interval '2 hours', worker_id=$2 WHERE id=$1`,
		taskID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.requeueStaleTasks(ctx); err != nil {
		t.Fatalf("requeueStaleTasks: %v", err)
	}
	var status string
	var retry int
	itPool.QueryRow(ctx, `SELECT status, retry_count FROM tasks WHERE id=$1`, taskID).Scan(&status, &retry)
	if status != "queued" || retry != 1 {
		t.Fatalf("stale task not requeued: status=%q retry=%d", status, retry)
	}
}

// --- 11. failed job: retries exhausted → fail + buyer refund ---

func TestFailAndRefund(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/y/tasks/0/input.jsonl")
	// A prior buyer charge to refund.
	if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
		Kind: KindBuyerCharge, BuyerID: &demoBuyerUUID, TaskID: &taskID, AmountUSD: -0.01, PayoutStatus: PayoutReleased,
	}}); err != nil {
		t.Fatal(err)
	}
	// Running, stale, retries already exhausted.
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='running', claimed_by=$2, claimed_at=now()-interval '2 hours', worker_id=$2, retry_count=3 WHERE id=$1`,
		taskID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.requeueStaleTasks(ctx); err != nil {
		t.Fatalf("requeue: %v", err)
	}
	var tstatus, jstatus string
	itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&tstatus)
	itPool.QueryRow(ctx, `SELECT status FROM jobs WHERE id=$1`, jobID).Scan(&jstatus)
	if tstatus != "failed" || jstatus != "failed" {
		t.Fatalf("want failed task+job, got task=%q job=%q", tstatus, jstatus)
	}
	var refunds int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE buyer_id=$1 AND kind='refund'`, demoBuyerUUID).Scan(&refunds)
	if refunds != 1 {
		t.Fatalf("want 1 refund row, got %d", refunds)
	}
}

// --- 12. payout hold→ready, and the transfer is honestly blocked ---

func TestPayoutHoldToReadyAndBlocked(t *testing.T) {
	reset(t)
	ctx := context.Background()
	// stubPayout never fakes a transfer.
	if _, err := (stubPayout{}).Send(ctx, demoSupplierUUID, 1.0, uuid.NewString()); err == nil {
		t.Fatal("stubPayout.Send must return an error (no fake transfers)")
	}
	taskID := uuid.New()
	mustJobTask(t, uuid.New(), taskID, false, false, "jobs/z/tasks/0/input.jsonl")
	past := time.Now().Add(-time.Minute)
	if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
		Kind: KindSupplierCredit, SupplierID: &demoSupplierUUID, TaskID: &taskID,
		AmountUSD: 0.02, PayoutStatus: PayoutHeld, ReleaseAt: &past,
	}}); err != nil {
		t.Fatal(err)
	}
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.releasePayouts(ctx); err != nil {
		t.Fatalf("releasePayouts: %v", err)
	}
	var status, ref string
	itPool.QueryRow(ctx,
		`SELECT payout_status, COALESCE(payout_ref,'') FROM ledger_entries WHERE kind='supplier_credit' AND task_id=$1`,
		taskID).Scan(&status, &ref)
	if status != "ready" {
		t.Fatalf("held payout past its window should be 'ready', got %q", status)
	}
	if ref != "" || status == "released" {
		t.Fatalf("no rail configured: must NOT be released with a ref (status=%q ref=%q)", status, ref)
	}
}

// --- 13. webhook delivery with retries against a local receiver ---

func TestWebhookRetry(t *testing.T) {
	reset(t)
	ctx := context.Background()
	wk := NewWorkers(itStore, itStorage, stubPayout{})

	// Receiver fails twice, then 200 — exercises the backoff retry loop.
	var hits int32
	flaky := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&hits, 1) < 3 {
			w.WriteHeader(500)
			return
		}
		w.WriteHeader(200)
	}))
	defer flaky.Close()
	if err := wk.deliverWebhook(ctx, PendingWebhook{ID: uuid.New(), JobID: uuid.New(), URL: flaky.URL, Status: "complete"}); err != nil {
		t.Fatalf("flaky webhook should eventually deliver: %v", err)
	}
	if got := atomic.LoadInt32(&hits); got != 3 {
		t.Fatalf("want 3 delivery attempts, got %d", got)
	}

	// Always-500 receiver → delivery fails after all retries (never marked delivered).
	var fhits int32
	dead := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&fhits, 1)
		w.WriteHeader(500)
	}))
	defer dead.Close()
	if err := wk.deliverWebhook(ctx, PendingWebhook{ID: uuid.New(), JobID: uuid.New(), URL: dead.URL, Status: "complete"}); err == nil {
		t.Fatal("dead webhook must surface an error, not a fake success")
	}
	if got := atomic.LoadInt32(&fhits); got != 3 {
		t.Fatalf("dead receiver: want 3 attempts, got %d", got)
	}
}

// --- 14. full completion sweep delivers a registered webhook exactly once ---

func TestWebhookSweepExactlyOnce(t *testing.T) {
	reset(t)
	ctx := context.Background()
	var hits int32
	rcv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(200)
	}))
	defer rcv.Close()

	// A job with one already-complete task → completable by the sweep.
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/w/tasks/0/input.jsonl")
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running', task_count=1, tasks_done=1 WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET status='complete' WHERE id=$1`, taskID); err != nil {
		t.Fatal(err)
	}
	if _, err := itStore.InsertWebhook(ctx, demoBuyerUUID, &jobID, rcv.URL); err != nil {
		t.Fatal(err)
	}
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	// Two sweeps: the first finalizes + delivers, the second must NOT re-deliver.
	if err := wk.sweepAndDeliver(ctx); err != nil {
		t.Fatalf("sweep 1: %v", err)
	}
	if err := wk.sweepAndDeliver(ctx); err != nil {
		t.Fatalf("sweep 2: %v", err)
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Fatalf("webhook should fire exactly once, fired %d", got)
	}
	var jstatus string
	itPool.QueryRow(ctx, `SELECT status FROM jobs WHERE id=$1`, jobID).Scan(&jstatus)
	if jstatus != "complete" {
		t.Fatalf("sweep should finalize job, got %q", jstatus)
	}
}

// --- 15. pricing + models from the DB catalogue ---

func TestPriceAndModels(t *testing.T) {
	reset(t)
	code, body := req(t, "GET", "/v1/models", nil, buyerKey())
	if code != 200 || !strings.Contains(string(body), "all-minilm-l6-v2") {
		t.Fatalf("models: %d %s", code, body)
	}
	code, body = req(t, "GET", "/v1/price-estimate?model=all-minilm-l6-v2&units=1000&tier=batch", nil, buyerKey())
	if code != 200 {
		t.Fatalf("price-estimate: %d %s", code, body)
	}
	var pe PriceEstimate
	json.Unmarshal(body, &pe)
	if pe.EstimateUSD <= 0 {
		t.Fatalf("estimate should be positive, got %v", pe.EstimateUSD)
	}
}

// --- 16. the hard filter (item A): a worker can NEVER claim a task it cannot run.
// Each sub-case makes the single live demo worker ineligible on exactly one axis
// and asserts the claim returns nothing; the control restores eligibility and the
// same worker then claims successfully. This proves the SQL filter, not just Match. ---

// Plane C (docs/PLANE_C.md §24-C1, §30): POST /v1/quote scans the input, returns a
// conservative cost/ETA/supply/risk band WITHOUT creating a job or spending, and
// PERSISTS the assumptions (the load-bearing rule: a later invoice can say what was
// believed). Proven end-to-end against live Postgres.
func TestQuoteEndpointPersistsAssumptions(t *testing.T) {
	ctx := context.Background()
	itPool.Exec(ctx, `TRUNCATE quotes`)

	// Inline JSONL: 2 valid records + 1 malformed (line 2) → the scanner must see it.
	body := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"tier":         "batch",
		"verification": map[string]any{"redundancy_frac": 0.0, "honeypot_frac": 0.0, "payout_hold_secs": 0},
		"input":        "{\"id\":\"a\",\"text\":\"hello\"}\n{bad json}\n{\"id\":\"c\",\"text\":\"world\"}\n",
	}
	status, out := req(t, "POST", "/v1/quote", body, buyerKey(), jsonCT())
	if status != 200 {
		t.Fatalf("POST /v1/quote -> %d: %s", status, out)
	}
	var q Quote
	if err := json.Unmarshal(out, &q); err != nil {
		t.Fatalf("decode quote: %v\n%s", err, out)
	}
	if q.QuoteID == "" || q.JobType != "embed" || q.Model != "all-minilm-l6-v2" {
		t.Fatalf("quote header wrong: %+v", q)
	}
	if q.Input.Records != 3 || q.Input.MalformedRecords != 1 || q.Input.FirstBadLine != 2 {
		t.Fatalf("scanner wrong: records=%d malformed=%d firstBad=%d", q.Input.Records, q.Input.MalformedRecords, q.Input.FirstBadLine)
	}
	if q.Execution.EstimatedTasks < 1 || q.Execution.RecommendedSplitSize < 1 {
		t.Fatalf("execution plan wrong: %+v", q.Execution)
	}
	if q.Cost.ExpectedUSD < 0 || q.Cost.MaxUSD < q.Cost.ExpectedUSD || q.Cost.MinUSD > q.Cost.ExpectedUSD {
		t.Fatalf("cost band incoherent: %+v", q.Cost)
	}
	if q.Time.P50Secs <= 0 || q.Time.P90Secs < q.Time.P50Secs {
		t.Fatalf("eta band incoherent: %+v", q.Time)
	}
	if q.Confidence.Score <= 0 || q.Confidence.Score > 1 || len(q.Confidence.Reasons) == 0 {
		t.Fatalf("confidence wrong: %+v", q.Confidence)
	}
	if !q.Budget.CancelBeforeExceeding || q.Budget.SuggestedMaxUSD < q.Cost.ExpectedUSD {
		t.Fatalf("budget suggestion wrong: %+v", q.Budget)
	}
	// A malformed record must surface a buyer-visible warning (never hidden).
	if len(q.Warnings) == 0 {
		t.Fatal("malformed input must produce a warning")
	}

	// The assumptions are persisted (PLANE_C §6): exactly one quotes row, matching.
	var n int
	var records int
	var costExpected float64
	if err := itPool.QueryRow(ctx,
		`SELECT count(*), COALESCE(max(records),0), COALESCE(max(cost_expected_usd),0) FROM quotes WHERE job_type='embed'`,
	).Scan(&n, &records, &costExpected); err != nil {
		t.Fatalf("read persisted quote: %v", err)
	}
	if n != 1 || records != 3 {
		t.Fatalf("quote not persisted correctly: rows=%d records=%d", n, records)
	}
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE quotes`) })
}

// Plane D D7 / errata C-Errata-4 (docs/PLANE_D.md §13): quote-to-submit binding.
// A buyer quotes, then submits carrying the quote_id; a matching, unexpired quote
// binds (jobs.quote_id set, a quote_bound event, invoice shows quoted-vs-actual),
// while an expired or mismatched quote is refused 409 with no job created. Proven
// end-to-end against live Postgres.
func TestQuoteBindingMatchAndExpiry(t *testing.T) {
	ctx := context.Background()
	itPool.Exec(ctx, `TRUNCATE quotes`)
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE quotes`) })

	// One input, quoted once; every submit below reuses these exact bytes so the
	// best-effort input_sha256 match holds (the only axis we vary is what we want to
	// fail on: model mismatch, then expiry).
	const input = "{\"id\":\"a\",\"text\":\"bind me to a price\"}\n{\"id\":\"b\",\"text\":\"so the invoice can prove it\"}\n"
	quoteBody := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"tier":         "batch",
		"verification": map[string]any{"redundancy_frac": 0.0, "honeypot_frac": 0.0, "payout_hold_secs": 0},
		"input":        input,
	}
	status, out := req(t, "POST", "/v1/quote", quoteBody, buyerKey(), jsonCT())
	if status != 200 {
		t.Fatalf("POST /v1/quote -> %d: %s", status, out)
	}
	var q Quote
	if err := json.Unmarshal(out, &q); err != nil {
		t.Fatalf("decode quote: %v\n%s", err, out)
	}
	if q.QuoteID == "" || q.ExpiresAt.IsZero() || q.InputSHA256 == "" {
		t.Fatalf("quote missing binding fields: id=%q expires=%v sha=%q", q.QuoteID, q.ExpiresAt, q.InputSHA256)
	}
	// The bare uuid (quotes.id / jobs.quote_id) is the "q_" handle minus its prefix;
	// it is NOT a wire field, so derive it from QuoteID rather than the (unexported,
	// unmarshal-skipped) q.bareID.
	wantBare, perr := uuid.Parse(strings.TrimPrefix(q.QuoteID, "q_"))
	if perr != nil {
		t.Fatalf("quote id %q not parseable: %v", q.QuoteID, perr)
	}

	// 1) Matching submit with the quote_id binds: 202, jobs.quote_id = the quote's
	//    bare uuid, a quote_bound event, and the invoice carries quoted_usd.
	bind := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"tier":         "batch",
		"verification": map[string]any{"redundancy_frac": 0.0, "honeypot_frac": 0.0, "payout_hold_secs": 0},
		"input":        input,
		"quote_id":     q.QuoteID,
	}
	code, body := req(t, "POST", "/v1/jobs", bind, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("bound submit: want 202, got %d: %s", code, body)
	}
	var jr JobSubmitResponse
	if err := json.Unmarshal(body, &jr); err != nil {
		t.Fatalf("bound submit decode: %v (%s)", err, body)
	}
	var boundID *uuid.UUID
	if err := itPool.QueryRow(ctx, `SELECT quote_id FROM jobs WHERE id=$1`, jr.JobID).Scan(&boundID); err != nil {
		t.Fatalf("read jobs.quote_id: %v", err)
	}
	if boundID == nil || *boundID != wantBare {
		t.Fatalf("jobs.quote_id=%v, want bound to quote %v", boundID, wantBare)
	}
	// The binding is on the buyer-visible timeline.
	var nBound int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM job_events WHERE job_id=$1 AND event='quote_bound'`, jr.JobID,
	).Scan(&nBound); err != nil {
		t.Fatalf("read quote_bound event: %v", err)
	}
	if nBound != 1 {
		t.Fatalf("want exactly 1 quote_bound event, got %d", nBound)
	}
	// The invoice now shows what the buyer was quoted next to what they were charged.
	icode, ibody := req(t, "GET", "/v1/jobs/"+jr.JobID.String()+"/invoice", nil, buyerKey())
	if icode != 200 {
		t.Fatalf("invoice: want 200, got %d: %s", icode, ibody)
	}
	var inv InvoiceView
	if err := json.Unmarshal(ibody, &inv); err != nil {
		t.Fatalf("invoice decode: %v (%s)", err, ibody)
	}
	if inv.QuotedUSD == nil {
		t.Fatalf("bound job invoice must include quoted_usd, got %s", ibody)
	}
	if *inv.QuotedUSD != q.Cost.ExpectedUSD {
		t.Fatalf("quoted_usd=%v, want the quote's expected %v", *inv.QuotedUSD, q.Cost.ExpectedUSD)
	}

	// 2) Mismatched submit: same quote_id, DIFFERENT model → 409, no job. The quote
	//    described all-minilm; submitting under another model is acting on a price the
	//    buyer was not given.
	mismatch := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "bge-small-en-v1.5"},
		"tier":         "batch",
		"verification": map[string]any{"redundancy_frac": 0.0, "honeypot_frac": 0.0, "payout_hold_secs": 0},
		"input":        input,
		"quote_id":     q.QuoteID,
	}
	mcode, mbody := req(t, "POST", "/v1/jobs", mismatch, buyerKey(), jsonCT())
	if mcode != http.StatusConflict {
		t.Fatalf("model-mismatch submit: want 409, got %d: %s", mcode, mbody)
	}
	if !strings.Contains(string(mbody), "does not match") {
		t.Fatalf("409 reason should explain the mismatch, got %s", mbody)
	}

	// 3) Expired submit: age the quote past its TTL in the DB, then re-submit the
	//    matching shape → 409 "quote expired".
	if _, err := itPool.Exec(ctx,
		`UPDATE quotes SET expires_at = now() - interval '1 minute' WHERE id=$1`, wantBare,
	); err != nil {
		t.Fatalf("expire quote: %v", err)
	}
	ecode, ebody := req(t, "POST", "/v1/jobs", bind, buyerKey(), jsonCT())
	if ecode != http.StatusConflict {
		t.Fatalf("expired submit: want 409, got %d: %s", ecode, ebody)
	}
	if !strings.Contains(string(ebody), "expired") {
		t.Fatalf("409 reason should say expired, got %s", ebody)
	}
}

// seedClaimedTask inserts a fresh job + one running task claimed by `worker`, for
// the fail-endpoint tests. Returns (jobID, taskID).
func seedClaimedTask(t *testing.T, worker uuid.UUID, retryCount int) (uuid.UUID, uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/x/in.jsonl','batch',1,0)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatalf("seed job: %v", err)
	}
	// claimed_by carries ownership (no FK); worker_id is a FK to workers, set only on
	// commit, so leave it NULL here — the fail path keys on claimed_by. This lets us
	// seed a task "owned" by an arbitrary (non-enrolled) worker id for the anti-spoof
	// test without tripping the worker_id foreign key.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, claimed_by, claimed_at, started_at,
		                    input_ref, result_key, retry_count, chunk_index)
		 VALUES ($1,$2,'running',$3,now(),now(),'jobs/x/t0/in.jsonl','jobs/x/t0/out.json',$4,0)`,
		taskID, jobID, worker, retryCount); err != nil {
		t.Fatalf("seed task: %v", err)
	}
	return jobID, taskID
}

// Plane C/D D0 (docs/PLANE_C_ERRATA.md C-Errata-1, docs/PLANE_D.md §6): a worker
// that KNOWS a retryable task cannot complete reports it immediately; the task is
// requeued in SECONDS (not the 30-min stale timeout), recorded in task_failures,
// and surfaced in the buyer's job_events timeline.
func TestFailEndpointRequeuesImmediately(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE task_failures, job_events`)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	jobID, taskID := seedClaimedTask(t, demoWorkerUUID, 0)

	body := map[string]any{
		"class": "oom", "message": "next-token alloc exceeded effective memory",
		"backend": "batch_infer", "model": "llama-3.2-1b-instruct-q4", "duration_ms": 1273,
		"memory": map[string]any{"total_gb": 64, "available_gb": 2.1, "effective_gb": 0, "reserved_headroom_gb": 8},
	}
	status, out := req(t, "POST", "/v1/worker/task/"+taskID.String()+"/fail", body, workerTok(), jsonCT())
	if status != 200 {
		t.Fatalf("fail -> %d: %s", status, out)
	}
	var resp struct {
		Outcome string `json:"outcome"`
	}
	json.Unmarshal(out, &resp)
	if resp.Outcome != "requeued" {
		t.Fatalf("outcome=%q, want requeued", resp.Outcome)
	}
	// Task is claimable again NOW (retrying, unclaimed, retry++), not stranded.
	var tstatus string
	var claimedBy *uuid.UUID
	var retry int16
	if err := itPool.QueryRow(ctx, `SELECT status, claimed_by, retry_count FROM tasks WHERE id=$1`, taskID).
		Scan(&tstatus, &claimedBy, &retry); err != nil {
		t.Fatal(err)
	}
	if tstatus != "retrying" || claimedBy != nil || retry != 1 {
		t.Fatalf("task not requeued: status=%s claimed=%v retry=%d", tstatus, claimedBy, retry)
	}
	// A typed failure row with the memory snapshot persisted.
	var nfail int
	itPool.QueryRow(ctx, `SELECT count(*) FROM task_failures WHERE task_id=$1 AND failure_class='oom' AND retryable AND memory IS NOT NULL`, taskID).Scan(&nfail)
	if nfail != 1 {
		t.Fatalf("expected 1 oom task_failures row with memory, got %d", nfail)
	}
	// Buyer-visible timeline has task_failed + task_requeued.
	_, evOut := req(t, "GET", "/v1/jobs/"+jobID.String()+"/events", nil, buyerKey())
	var events []map[string]any
	json.Unmarshal(evOut, &events)
	gotFailed, gotRequeued := false, false
	for _, e := range events {
		switch e["event"] {
		case "task_failed":
			gotFailed = true
		case "task_requeued":
			gotRequeued = true
		}
	}
	if !gotFailed || !gotRequeued {
		t.Fatalf("event timeline missing task_failed/task_requeued: %s", evOut)
	}
}

// Buyer-bad-input fails TERMINALLY and immediately (no retry of bad data, no
// charge), with a job_failed event — the opposite of letting it drain budget.
func TestFailEndpointBadInputTerminal(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE task_failures, job_events`)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	jobID, taskID := seedClaimedTask(t, demoWorkerUUID, 0)

	body := map[string]any{"class": "bad_input", "message": "missing required field 'text'"}
	status, out := req(t, "POST", "/v1/worker/task/"+taskID.String()+"/fail", body, workerTok(), jsonCT())
	if status != 200 {
		t.Fatalf("fail -> %d: %s", status, out)
	}
	var resp struct {
		Outcome string `json:"outcome"`
	}
	json.Unmarshal(out, &resp)
	if resp.Outcome != "failed" {
		t.Fatalf("bad_input outcome=%q, want failed (terminal)", resp.Outcome)
	}
	var tstatus, jstatus string
	itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&tstatus)
	itPool.QueryRow(ctx, `SELECT status FROM jobs WHERE id=$1`, jobID).Scan(&jstatus)
	if tstatus != "failed" || jstatus != "failed" {
		t.Fatalf("bad_input should fail task+job terminally: task=%s job=%s", tstatus, jstatus)
	}
	// job_failed event present.
	_, evOut := req(t, "GET", "/v1/jobs/"+jobID.String()+"/events", nil, buyerKey())
	if !bytesContains(evOut, "job_failed") {
		t.Fatalf("expected job_failed event, got %s", evOut)
	}
}

// Only the claiming worker may fail a task (anti-spoof).
func TestFailEndpointOnlyClaimingWorker(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE task_failures, job_events`)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	// Task claimed by a DIFFERENT worker than the demo token authenticates as.
	_, taskID := seedClaimedTask(t, uuid.New(), 0)
	status, _ := req(t, "POST", "/v1/worker/task/"+taskID.String()+"/fail",
		map[string]any{"class": "oom"}, workerTok(), jsonCT())
	if status != 409 {
		t.Fatalf("non-claiming worker fail -> %d, want 409", status)
	}
}

// When several tasks of one job fail terminally, the job must be flipped + refunded
// EXACTLY once — never double-refunded (a money-correctness invariant).
func TestFailEndpointRefundsJobOnce(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE task_failures, job_events`)
		itPool.Exec(ctx, `DELETE FROM ledger_entries WHERE buyer_id=$1`, demoBuyerUUID)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	jobID := uuid.New()
	task1, task2 := uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/x/in.jsonl','batch',2,0)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	for _, tid := range []uuid.UUID{task1, task2} {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, claimed_by, claimed_at, started_at, input_ref, result_key, retry_count, chunk_index)
			 VALUES ($1,$2,'running',$3,now(),now(),'jobs/x/t/in.jsonl','jobs/x/t/out.json',0,0)`,
			tid, jobID, demoWorkerUUID); err != nil {
			t.Fatal(err)
		}
	}
	// A prior buyer_charge (debit) on task1, so there is something to refund.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO ledger_entries (kind, buyer_id, task_id, amount_usd, payout_status)
		 VALUES ('buyer_charge',$1,$2,-0.50,'pending')`, demoBuyerUUID, task1); err != nil {
		t.Fatal(err)
	}

	// Fail BOTH tasks terminally (bad_input). The job is refunded ONCE.
	for _, tid := range []uuid.UUID{task1, task2} {
		st, out := req(t, "POST", "/v1/worker/task/"+tid.String()+"/fail",
			map[string]any{"class": "bad_input", "message": "x"}, workerTok(), jsonCT())
		if st != 200 {
			t.Fatalf("fail %s -> %d: %s", tid, st, out)
		}
	}
	var nrefund int
	var refundSum float64
	itPool.QueryRow(ctx, `SELECT count(*), COALESCE(SUM(amount_usd),0) FROM ledger_entries WHERE kind='refund' AND buyer_id=$1`, demoBuyerUUID).
		Scan(&nrefund, &refundSum)
	if nrefund != 1 || refundSum < 0.49 || refundSum > 0.51 {
		t.Fatalf("expected exactly one refund of ~0.50, got %d rows summing %.2f (double refund?)", nrefund, refundSum)
	}
	var nJobFailed int
	itPool.QueryRow(ctx, `SELECT count(*) FROM job_events WHERE job_id=$1 AND event='job_failed'`, jobID).Scan(&nJobFailed)
	if nJobFailed != 1 {
		t.Fatalf("expected exactly one job_failed event, got %d", nJobFailed)
	}
}

func bytesContains(b []byte, sub string) bool {
	return strings.Contains(string(b), sub)
}

// Budget Governor (Plane C §12 / Plane D §14 D8): a job with a tiny max_usd whose
// next task's PROJECTED charge (already-charged + one task's estimate) would breach
// the cap must NOT have that task dispatched. The cap PREVENTS dispatch — the task
// stays queued, budget_state flips to paused_for_budget, and a budget_stopped event
// fires once. No refund, no over-charge: the money math only GATES, it never moves
// money. Then raising the cap lets the same worker claim the same task, proving it
// was the budget gate (not any other hard filter) that held it back.
func TestBudgetCapPausesDispatch(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE job_events`)
		itPool.Exec(ctx, `DELETE FROM ledger_entries WHERE buyer_id=$1`, demoBuyerUUID)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	reset(t) // demo worker live + eligible (apple_silicon_max, supports embed)

	jobID := uuid.New()
	doneTask, queuedTask := uuid.New(), uuid.New()
	// estimated_usd 1.00 over task_count 2 ⇒ per-task estimate 0.50. Cap 0.60.
	// One task already charged 0.50 ⇒ projecting one MORE = 0.50 + 0.50 = 1.00 > 0.60,
	// so the queued task must be refused. budget_state starts at the default 'tracking'.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
		                   task_count, tasks_done, min_memory_gb, estimated_usd, max_usd, budget_state)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/x/in.jsonl','batch',
		         2,1,2,1.00,0.60,'tracking')`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	// task1: already complete + charged (the spent half of the budget).
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, worker_id, input_ref, result_key, chunk_index, completed_at)
		 VALUES ($1,$2,'complete',$3,'jobs/x/t0/in.jsonl','jobs/x/t0/out.json',0, now())`,
		doneTask, jobID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	// task2: queued + claimable on every axis EXCEPT the budget gate.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
		 VALUES ($1,$2,'queued','jobs/x/t1/in.jsonl','jobs/x/t1/out.json',1, now())`,
		queuedTask, jobID); err != nil {
		t.Fatal(err)
	}
	// The real buyer_charge debit (-0.50) the projection reads (same ledger shape
	// refundJobChargesTx sums). This is what makes the next dispatch breach the cap.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO ledger_entries (kind, buyer_id, task_id, amount_usd, payout_status)
		 VALUES ('buyer_charge',$1,$2,-0.50,'released')`, demoBuyerUUID, doneTask); err != nil {
		t.Fatal(err)
	}

	wauth := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}

	// 1) The cap PREVENTS dispatch: the claim returns nothing.
	c, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask: %v", err)
	}
	if c != nil {
		t.Fatalf("budget cap should block dispatch, but task %s was claimed (over-cap dispatch)", c.TaskID)
	}

	// 2) The queued task is untouched (still queued, unclaimed) — not failed, not refunded.
	var tstatus string
	var claimedBy *uuid.UUID
	itPool.QueryRow(ctx, `SELECT status, claimed_by FROM tasks WHERE id=$1`, queuedTask).Scan(&tstatus, &claimedBy)
	if tstatus != "queued" || claimedBy != nil {
		t.Fatalf("queued task must stay queued+unclaimed under budget pause, got status=%s claimed_by=%v", tstatus, claimedBy)
	}

	// 3) budget_state flipped to paused_for_budget.
	var bstate string
	itPool.QueryRow(ctx, `SELECT budget_state FROM jobs WHERE id=$1`, jobID).Scan(&bstate)
	if bstate != "paused_for_budget" {
		t.Fatalf("budget_state = %q, want paused_for_budget", bstate)
	}

	// 4) Exactly one budget_stopped event (poll again — it must NOT re-emit).
	if _, err := itStore.ClaimTask(ctx, wauth); err != nil {
		t.Fatalf("second ClaimTask: %v", err)
	}
	var nStopped int
	itPool.QueryRow(ctx, `SELECT count(*) FROM job_events WHERE job_id=$1 AND event='budget_stopped'`, jobID).Scan(&nStopped)
	if nStopped != 1 {
		t.Fatalf("expected exactly one budget_stopped event, got %d (re-emitted on repeat poll?)", nStopped)
	}

	// 5) No money moved: the cap GATES, it never refunds. There must be zero refund rows.
	var nRefund int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE kind='refund' AND buyer_id=$1`, demoBuyerUUID).Scan(&nRefund)
	if nRefund != 0 {
		t.Fatalf("budget pause must not refund (cap prevents dispatch, never moves money), got %d refund rows", nRefund)
	}

	// 6) Raising the cap above the projection lets the SAME worker claim the SAME task,
	// proving the budget gate (not another filter) was what held it back.
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET max_usd=10.00 WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}
	c2, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask after raising cap: %v", err)
	}
	if c2 == nil || c2.TaskID != queuedTask {
		t.Fatalf("raising the cap should release the queued task, but it was not claimed (got %v)", c2)
	}
}

// The cap must hold under CONCURRENCY: an in-flight (claimed, running, not-yet-
// committed) task counts toward projected exposure, so a second task is NOT
// dispatched when the first running task already commits the cap. Without counting
// in-flight work, both would claim before either charged and overshoot the cap.
func TestBudgetCapCountsInflightTasks(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `DELETE FROM ledger_entries WHERE buyer_id=$1`, demoBuyerUUID)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	reset(t)

	jobID := uuid.New()
	t1, t2 := uuid.New(), uuid.New()
	// per-task estimate = 1.00/2 = 0.50; cap 0.60. NO prior charge: one running task
	// (0.50) + the candidate (0.50) = 1.00 > 0.60 ⇒ the 2nd must be refused.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
		                   task_count, tasks_done, min_memory_gb, estimated_usd, max_usd, budget_state)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/x/in.jsonl','batch',2,0,2,1.00,0.60,'tracking')`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	for _, tid := range []uuid.UUID{t1, t2} {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/t/in.jsonl','jobs/x/t/out.json',0, now())`,
			tid, jobID); err != nil {
			t.Fatal(err)
		}
	}
	wauth := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}

	// First claim succeeds (nothing charged/running yet, 1 candidate within cap).
	c1, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask 1: %v", err)
	}
	if c1 == nil {
		t.Fatal("first task should be claimable under the cap")
	}
	// It is now 'running' (claimed, uncommitted). The SECOND claim must be refused:
	// the in-flight task's estimate + the candidate's estimate breaches the cap.
	c2, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask 2: %v", err)
	}
	if c2 != nil {
		t.Fatalf("budget cap overshoot: a 2nd task (%s) was dispatched while an in-flight task already commits the cap", c2.TaskID)
	}
}

func TestClaimHardFilter(t *testing.T) {
	ctx := context.Background()
	wauth := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}

	// setJob inserts one queued primary task on a fresh job with the given
	// constraints, returning the task id. The demo worker is apple_silicon_max,
	// 64GB, supports embed + all-minilm-l6-v2, min_payout 0, supplier US/active.
	setJob := func(t *testing.T, jobType, modelRef string, minMem float32, hwClasses, residency []string, offered float64) {
		t.Helper()
		if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
			t.Fatal(err)
		}
		jobID, taskID := uuid.New(), uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
			                   task_count, tasks_done, min_memory_gb, hw_classes, data_residency, offered_rate_usd_hr)
			 VALUES ($1,$2,'queued',$3,$4,'jobs/x/input.jsonl','batch',1,0,$5,$6,$7,$8)`,
			jobID, demoBuyerUUID, jobType, modelRef, minMem,
			nullStrSlice(hwClasses), nullStrSlice(residency), offered); err != nil {
			t.Fatalf("insert job: %v", err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/tasks/0/input.jsonl','jobs/x/tasks/0/result.json',0, now())`,
			taskID, jobID); err != nil {
			t.Fatalf("insert task: %v", err)
		}
	}
	// restoreWorker resets the demo worker + supplier to the fully-eligible baseline.
	restoreWorker := func(t *testing.T) {
		t.Helper()
		if _, err := itPool.Exec(ctx,
			`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, last_seen_at=now(),
			   supported_jobs=ARRAY['embed','batch_infer'], supported_models=ARRAY['all-minilm-l6-v2'],
			   min_payout_usd_hr=0, thermal_ok=true,
			   throttled=false, effective_memory_gb=NULL, available_memory_gb=NULL,
			   reserved_headroom_gb=NULL WHERE id=$1`, demoWorkerUUID); err != nil {
			t.Fatal(err)
		}
		if _, err := itPool.Exec(ctx,
			`UPDATE suppliers SET status='active', data_country='US' WHERE id=$1`, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
	}
	claims := func(t *testing.T) bool {
		t.Helper()
		c, err := itStore.ClaimTask(ctx, wauth)
		if err != nil {
			t.Fatalf("ClaimTask: %v", err)
		}
		return c != nil
	}

	cases := []struct {
		name    string
		breakIt func(t *testing.T) // make the worker/supplier ineligible
	}{
		{"unsupported job type", func(t *testing.T) {
			setJob(t, "rerank", "", 0, nil, nil, 1)
			// worker supports embed/batch_infer, NOT rerank
			itPool.Exec(ctx, `UPDATE workers SET supported_jobs=ARRAY['embed'] WHERE id=$1`, demoWorkerUUID)
		}},
		{"unsupported model", func(t *testing.T) {
			setJob(t, "embed", "some-other-model", 0, nil, nil, 1)
		}},
		{"insufficient memory", func(t *testing.T) {
			setJob(t, "embed", "", 999, nil, nil, 1)
		}},
		{"wrong hw_class", func(t *testing.T) {
			setJob(t, "embed", "", 0, []string{"apple_silicon_ultra"}, nil, 1)
		}},
		{"data residency mismatch", func(t *testing.T) {
			setJob(t, "embed", "", 0, nil, []string{"DE"}, 1) // supplier is US
		}},
		{"offered rate below worker floor", func(t *testing.T) {
			setJob(t, "embed", "", 0, nil, nil, 0.5)
			itPool.Exec(ctx, `UPDATE workers SET min_payout_usd_hr=10 WHERE id=$1`, demoWorkerUUID)
		}},
		{"supplier quarantined", func(t *testing.T) {
			setJob(t, "embed", "", 0, nil, nil, 1)
			itPool.Exec(ctx, `UPDATE suppliers SET status='suspended' WHERE id=$1`, demoSupplierUUID)
		}},
		{"worker throttled (memory pressure)", func(t *testing.T) {
			setJob(t, "embed", "", 0, nil, nil, 1)
			// Worker is healthy on every axis but is pausing for memory pressure —
			// the safe-dispatch filter must not hand it work.
			itPool.Exec(ctx, `UPDATE workers SET throttled=true WHERE id=$1`, demoWorkerUUID)
		}},
		{"effective memory below job min", func(t *testing.T) {
			// Total memory (64) clears the 32GB floor, but the live effective pool
			// after headroom is only 8GB — the claim must use effective, not total.
			setJob(t, "embed", "", 32, nil, nil, 1)
			itPool.Exec(ctx, `UPDATE workers SET effective_memory_gb=8 WHERE id=$1`, demoWorkerUUID)
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			restoreWorker(t)
			c.breakIt(t)
			if claims(t) {
				t.Fatalf("worker claimed a task it is ineligible for (%s)", c.name)
			}
			// Restoring eligibility lets the SAME worker claim the SAME task — proving
			// the rejection was the filter, not an unrelated empty queue. Make the
			// worker maximally capable and strip every job constraint so the claim
			// must succeed on whatever axis was broken above.
			restoreWorker(t)
			if _, err := itPool.Exec(ctx,
				`UPDATE workers SET supported_jobs=ARRAY['embed','batch_infer','rerank','json_extraction','batch_classification'],
				   supported_models=ARRAY['all-minilm-l6-v2','some-other-model'], min_payout_usd_hr=0 WHERE id=$1`,
				demoWorkerUUID); err != nil {
				t.Fatal(err)
			}
			if _, err := itPool.Exec(ctx,
				`UPDATE jobs SET min_memory_gb=0, hw_classes=NULL, data_residency=NULL, offered_rate_usd_hr=1`); err != nil {
				t.Fatal(err)
			}
			if !claims(t) {
				t.Fatalf("eligible worker failed to claim after restore (%s)", c.name)
			}
		})
	}

	// Reputation gate (Elite tier, research §6.4): a job with a high min_reputation is
	// NOT claimable by a low-reputation supplier, and becomes claimable once the
	// supplier's reputation clears the bar — the supplier-stickiness moat, in SQL.
	t.Run("reputation_gate", func(t *testing.T) {
		restoreWorker(t)
		if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
			t.Fatal(err)
		}
		jobID, taskID := uuid.New(), uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
			                   task_count, tasks_done, min_memory_gb, offered_rate_usd_hr, min_reputation)
			 VALUES ($1,$2,'queued','embed','all-minilm-l6-v2','jobs/x/input.jsonl','batch',1,0,0,1,0.90)`,
			jobID, demoBuyerUUID); err != nil {
			t.Fatalf("insert job: %v", err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/tasks/0/input.jsonl','jobs/x/tasks/0/result.json',0, now())`,
			taskID, jobID); err != nil {
			t.Fatalf("insert task: %v", err)
		}
		if _, err := itPool.Exec(ctx, `UPDATE suppliers SET reputation=0.50 WHERE id=$1`, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
		if claims(t) {
			t.Fatal("0.50-reputation supplier must NOT claim a min_reputation=0.90 job")
		}
		// Clear the bar; reset the task (a rejected claim leaves it queued) and retry.
		if _, err := itPool.Exec(ctx, `UPDATE tasks SET claimed_by=NULL, started_at=NULL, status='queued' WHERE id=$1`, taskID); err != nil {
			t.Fatal(err)
		}
		if _, err := itPool.Exec(ctx, `UPDATE suppliers SET reputation=0.95 WHERE id=$1`, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
		if !claims(t) {
			t.Fatal("0.95-reputation supplier SHOULD claim a min_reputation=0.90 job")
		}
		itPool.Exec(ctx, `UPDATE suppliers SET reputation=0.90 WHERE id=$1`, demoSupplierUUID)
	})

	// Private Deployment (research §3): a private_pool job is NOT claimable by an
	// unbound supplier, and becomes claimable once the buyer binds them to their pool.
	t.Run("private_pool", func(t *testing.T) {
		restoreWorker(t)
		if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
			t.Fatal(err)
		}
		itPool.Exec(ctx, `DELETE FROM private_pool_members WHERE buyer_id=$1`, demoBuyerUUID)
		jobID, taskID := uuid.New(), uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
			                   task_count, tasks_done, min_memory_gb, offered_rate_usd_hr, private_pool)
			 VALUES ($1,$2,'queued','embed','all-minilm-l6-v2','jobs/x/input.jsonl','batch',1,0,0,1,true)`,
			jobID, demoBuyerUUID); err != nil {
			t.Fatalf("insert job: %v", err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/tasks/0/input.jsonl','jobs/x/tasks/0/result.json',0, now())`,
			taskID, jobID); err != nil {
			t.Fatalf("insert task: %v", err)
		}
		if claims(t) {
			t.Fatal("unbound supplier must NOT claim a private_pool job")
		}
		if _, err := itPool.Exec(ctx, `UPDATE tasks SET claimed_by=NULL, started_at=NULL, status='queued' WHERE id=$1`, taskID); err != nil {
			t.Fatal(err)
		}
		if err := itStore.AddPrivatePoolMember(ctx, demoBuyerUUID, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
		if !claims(t) {
			t.Fatal("bound supplier SHOULD claim the buyer's private_pool job")
		}
		itPool.Exec(ctx, `DELETE FROM private_pool_members WHERE buyer_id=$1`, demoBuyerUUID)
	})
}

// TestSchedulerExplain proves GET /admin/scheduler/explain (Plane D §17 D11): for a
// worker, it runs the SAME hard-filter predicates as ClaimTask against the claimable
// queue and reports COUNTS of why work was rejected. The core assertion: seed a job
// the demo worker cannot run on exactly ONE axis and the matching reason count is
// >=1 while eligible=0 (the queue HAS work, just none this worker may take) — making
// "nothing eligible" visible instead of looking like a slow worker. Also checks the
// empty-queue no_queued_tasks path, the eligible path, and the endpoint's auth/404.
func TestSchedulerExplain(t *testing.T) {
	ctx := context.Background()

	// Reset the demo worker + supplier to the fully-eligible baseline (apple_silicon_max,
	// 64GB, supports embed/batch_infer + all-minilm-l6-v2, min_payout 0, US/active).
	restoreWorker := func(t *testing.T) {
		t.Helper()
		if _, err := itPool.Exec(ctx,
			`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, last_seen_at=now(),
			   supported_jobs=ARRAY['embed','batch_infer'], supported_models=ARRAY['all-minilm-l6-v2'],
			   min_payout_usd_hr=0, thermal_ok=true,
			   throttled=false, effective_memory_gb=NULL, available_memory_gb=NULL,
			   reserved_headroom_gb=NULL WHERE id=$1`, demoWorkerUUID); err != nil {
			t.Fatal(err)
		}
		if _, err := itPool.Exec(ctx,
			`UPDATE suppliers SET status='active', data_country='US' WHERE id=$1`, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
	}
	// seedJob inserts one queued primary task on a fresh job with the given
	// constraints (mirrors TestClaimHardFilter's setJob), returning the job id.
	seedJob := func(t *testing.T, jobType, modelRef string, minMem float32, hwClasses, residency []string, offered float64) uuid.UUID {
		t.Helper()
		if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
			t.Fatal(err)
		}
		jobID, taskID := uuid.New(), uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
			                   task_count, tasks_done, min_memory_gb, hw_classes, data_residency, offered_rate_usd_hr)
			 VALUES ($1,$2,'queued',$3,$4,'jobs/x/input.jsonl','batch',1,0,$5,$6,$7,$8)`,
			jobID, demoBuyerUUID, jobType, modelRef, minMem,
			nullStrSlice(hwClasses), nullStrSlice(residency), offered); err != nil {
			t.Fatalf("insert job: %v", err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/tasks/0/input.jsonl','jobs/x/tasks/0/result.json',0, now())`,
			taskID, jobID); err != nil {
			t.Fatalf("insert task: %v", err)
		}
		return jobID
	}
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`) })

	// Each case breaks exactly ONE hard-filter axis on an otherwise-claimable job and
	// names the reason field that must then be >=1 (and eligible must be 0).
	cases := []struct {
		name   string
		seed   func(t *testing.T)
		reason func(e *SchedulerExplanation) int
	}{
		{"hw_class mismatch", func(t *testing.T) {
			seedJob(t, "embed", "", 0, []string{"apple_silicon_ultra"}, nil, 1) // worker is _max
		}, func(e *SchedulerExplanation) int { return e.HWClassMismatch }},
		{"memory mismatch", func(t *testing.T) {
			seedJob(t, "embed", "", 999, nil, nil, 1) // worker has 64GB
		}, func(e *SchedulerExplanation) int { return e.MemoryMismatch }},
		{"job_type mismatch", func(t *testing.T) {
			seedJob(t, "rerank", "", 0, nil, nil, 1) // worker supports embed/batch_infer, not rerank
		}, func(e *SchedulerExplanation) int { return e.JobTypeMismatch }},
		{"model mismatch", func(t *testing.T) {
			seedJob(t, "embed", "some-other-model", 0, nil, nil, 1)
		}, func(e *SchedulerExplanation) int { return e.ModelMismatch }},
		{"residency mismatch", func(t *testing.T) {
			seedJob(t, "embed", "", 0, nil, []string{"DE"}, 1) // supplier is US
		}, func(e *SchedulerExplanation) int { return e.ResidencyMismatch }},
		{"payout floor", func(t *testing.T) {
			seedJob(t, "embed", "", 0, nil, nil, 0.5)
			itPool.Exec(ctx, `UPDATE workers SET min_payout_usd_hr=10 WHERE id=$1`, demoWorkerUUID)
		}, func(e *SchedulerExplanation) int { return e.PayoutFloor }},
		{"supplier inactive", func(t *testing.T) {
			seedJob(t, "embed", "", 0, nil, nil, 1)
			itPool.Exec(ctx, `UPDATE suppliers SET status='suspended' WHERE id=$1`, demoSupplierUUID)
		}, func(e *SchedulerExplanation) int { return e.SupplierInactive }},
		{"throttled", func(t *testing.T) {
			seedJob(t, "embed", "", 0, nil, nil, 1)
			itPool.Exec(ctx, `UPDATE workers SET throttled=true WHERE id=$1`, demoWorkerUUID)
		}, func(e *SchedulerExplanation) int { return e.Throttled }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			restoreWorker(t)
			c.seed(t)
			exp, err := itStore.SchedulerExplain(ctx, demoWorkerUUID)
			if err != nil {
				t.Fatalf("SchedulerExplain: %v", err)
			}
			if got := c.reason(exp); got < 1 {
				t.Fatalf("%s: expected the matching reason count >=1, got %d (%+v)", c.name, got, exp)
			}
			if exp.Eligible != 0 {
				t.Fatalf("%s: a job broken on this axis must NOT be eligible, got eligible=%d", c.name, exp.Eligible)
			}
			if exp.NoQueuedTasks != 0 {
				t.Fatalf("%s: a job IS queued, so no_queued_tasks must be 0, got %d", c.name, exp.NoQueuedTasks)
			}
		})
	}

	// Empty queue → no_queued_tasks=1 and every other bucket 0: the "nothing to do"
	// case the endpoint exists to distinguish from a worker that cannot keep up.
	t.Run("empty queue", func(t *testing.T) {
		restoreWorker(t)
		if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
			t.Fatal(err)
		}
		exp, err := itStore.SchedulerExplain(ctx, demoWorkerUUID)
		if err != nil {
			t.Fatalf("SchedulerExplain: %v", err)
		}
		if exp.NoQueuedTasks != 1 || exp.Eligible != 0 {
			t.Fatalf("empty queue: want no_queued_tasks=1 eligible=0, got %+v", exp)
		}
	})

	// A fully-compatible queued job is counted eligible (the worker CAN claim it) —
	// the same predicates ClaimTask uses, read-only.
	t.Run("eligible", func(t *testing.T) {
		restoreWorker(t)
		seedJob(t, "embed", "all-minilm-l6-v2", 2, nil, nil, 1)
		exp, err := itStore.SchedulerExplain(ctx, demoWorkerUUID)
		if err != nil {
			t.Fatalf("SchedulerExplain: %v", err)
		}
		if exp.Eligible < 1 {
			t.Fatalf("a fully-compatible job must be eligible, got %+v", exp)
		}
		if exp.NoQueuedTasks != 0 {
			t.Fatalf("eligible: no_queued_tasks must be 0, got %d", exp.NoQueuedTasks)
		}
	})

	// The HTTP surface: admin-only, 400 without worker_id, 404 for an unknown worker,
	// 200 with the JSON body for the demo worker (one eligible job seeded above).
	t.Run("http endpoint", func(t *testing.T) {
		restoreWorker(t)
		seedJob(t, "embed", "all-minilm-l6-v2", 2, nil, nil, 1)

		path := "/admin/scheduler/explain?worker_id=" + demoWorkerUUID.String()
		if code, _ := req(t, "GET", path, nil, buyerKey()); code != 403 {
			t.Fatalf("non-admin must be 403, got %d", code)
		}
		if code, _ := req(t, "GET", "/admin/scheduler/explain", nil, adminKey()); code != 400 {
			t.Fatalf("missing worker_id must be 400, got %d", code)
		}
		if code, _ := req(t, "GET", "/admin/scheduler/explain?worker_id="+uuid.New().String(), nil, adminKey()); code != 404 {
			t.Fatalf("unknown worker must be 404, got %d", code)
		}
		code, body := req(t, "GET", path, nil, adminKey())
		if code != 200 {
			t.Fatalf("admin explain: %d %s", code, body)
		}
		var exp SchedulerExplanation
		if err := json.Unmarshal(body, &exp); err != nil {
			t.Fatalf("decode explain body: %v (%s)", err, body)
		}
		if exp.WorkerID != demoWorkerUUID {
			t.Fatalf("explain echoed wrong worker_id: %s", exp.WorkerID)
		}
		if exp.Eligible < 1 {
			t.Fatalf("seeded a compatible job; expected eligible>=1, got %+v", exp)
		}
	})
}

// Plane B (docs/PLANE_B.md §5 "Routing proof"): a co-located cluster registers as
// ONE apple_silicon_cluster worker advertising the SUMMED member memory. A job
// whose min_memory_gb exceeds any single Mac's capacity must route ONLY to that
// cluster — and the SAME job, on the SAME unchanged claim filter, still routes a
// small job to a single Mac. This proves the summed-memory abstraction needs NO
// scheduler change (the existing ClaimTask memory filter does all the work).
func TestClusterSummedMemoryRouting(t *testing.T) {
	ctx := context.Background()
	clusterID := uuid.New()
	// A 4-Mac cluster registering as one worker with ~1800 GB summed usable memory.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_cluster',1800,now(),'cluster',
		         ARRAY['embed','batch_infer'], ARRAY['all-minilm-l6-v2'], 0, true)`,
		clusterID, demoSupplierUUID); err != nil {
		t.Fatalf("seed cluster worker: %v", err)
	}
	// Restore the world to just-the-demo-worker so later tests' supply is unchanged.
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
		itPool.Exec(ctx, `DELETE FROM benchmark_results WHERE worker_id=$1`, clusterID)
		itPool.Exec(ctx, `DELETE FROM workers WHERE id=$1`, clusterID)
	})
	// Demo single Mac at its 64 GB baseline, fully eligible.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, last_seen_at=now(),
		   supported_jobs=ARRAY['embed','batch_infer'], supported_models=ARRAY['all-minilm-l6-v2'],
		   min_payout_usd_hr=0, throttled=false, effective_memory_gb=NULL WHERE id=$1`, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	itPool.Exec(ctx, `UPDATE suppliers SET status='active', data_country='US' WHERE id=$1`, demoSupplierUUID)

	// One queued embed task on a fresh job needing minMem GB (hw_classes NULL, so
	// ONLY memory differentiates — isolating the summed-memory routing).
	submit := func(minMem float32) {
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
		jobID, taskID := uuid.New(), uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
			                   task_count, tasks_done, min_memory_gb)
			 VALUES ($1,$2,'queued','embed','all-minilm-l6-v2','jobs/x/in.jsonl','batch',1,0,$3)`,
			jobID, demoBuyerUUID, minMem); err != nil {
			t.Fatalf("insert job: %v", err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued','jobs/x/t0/in.jsonl','jobs/x/t0/out.json',0, now())`,
			taskID, jobID); err != nil {
			t.Fatalf("insert task: %v", err)
		}
	}
	claims := func(auth WorkerAuth) bool {
		c, err := itStore.ClaimTask(ctx, auth)
		if err != nil {
			t.Fatalf("ClaimTask: %v", err)
		}
		return c != nil
	}
	single := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}
	cluster := WorkerAuth{WorkerID: clusterID, SupplierID: demoSupplierUUID}

	// A 200 GB-min job: above any single Mac (64), within the cluster (1800). The
	// single Mac is filtered out; the SAME queued task is then claimed by the cluster.
	submit(200)
	if claims(single) {
		t.Fatal("single Mac (64GB) must NOT claim a 200GB-min job — summed memory is the whole point")
	}
	if !claims(cluster) {
		t.Fatal("cluster (1800GB summed) must claim the 200GB-min job (no scheduler change needed)")
	}
	// The unchanged filter still routes a small job to the single Mac.
	submit(0)
	if !claims(single) {
		t.Fatal("single Mac must still claim a 0GB-min job (routing unbroken for small work)")
	}
}

// --- 17. auto-quarantine: a honeypot fail suspends the supplier + stamps it ---

func TestAutoQuarantineOnHoneypotFail(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, true /*honeypot*/, false, demoHoneypotEmbedRef)
	info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
		SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotEmbedRef, jobType: "embed"}
	// Wrong answer → honeypot fail → quarantine.
	out, err := itServer.verifier.verifyTaskResult(ctx, info,
		TaskCommit{TaskID: taskID}, []byte(`{"vectors":[[0,1,0]]}`), nil)
	if err != nil || out != OutcomeFail {
		t.Fatalf("honeypot fail: out=%v err=%v", out, err)
	}
	var status string
	var quarantinedAt *time.Time
	itPool.QueryRow(ctx, `SELECT status, quarantined_at FROM suppliers WHERE id=$1`, demoSupplierUUID).
		Scan(&status, &quarantinedAt)
	if status != "suspended" {
		t.Fatalf("honeypot fail must auto-quarantine (suspend), got status %q", status)
	}
	if quarantinedAt == nil {
		t.Fatal("quarantined_at must be stamped on auto-quarantine")
	}
	// A quarantined supplier's worker can no longer claim work (the s.status gate).
	if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
		t.Fatal(err)
	}
	jid, tid := uuid.New(), uuid.New()
	mustJobTask(t, jid, tid, false, false, "jobs/q/tasks/0/input.jsonl")
	itPool.Exec(ctx, `UPDATE tasks SET status='queued', claimed_by=NULL, worker_id=NULL WHERE id=$1`, tid)
	itPool.Exec(ctx, `UPDATE workers SET supported_jobs=ARRAY['embed'], supported_models=ARRAY['all-minilm-l6-v2'] WHERE id=$1`, demoWorkerUUID)
	c, cerr := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if cerr != nil {
		t.Fatalf("claim: %v", cerr)
	}
	if c != nil {
		t.Fatal("a quarantined supplier's worker must not be able to claim a task")
	}
}

// --- 18. real 3-way tiebreak: two disagree → a third is dispatched and pinned;
// when it commits, the majority wins and the loser is docked ---

func TestTiebreakThreeWay(t *testing.T) {
	reset(t)
	ctx := context.Background()
	// A second worker on the same supplier (same hw class) so a distinct peer exists.
	peerWorker := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_max',64,400,now(),'seed',
		         ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true)
		 ON CONFLICT (id) DO UPDATE SET last_seen_at=now(), supported_jobs=ARRAY['embed']`,
		peerWorker, demoSupplierUUID); err != nil {
		t.Fatal(err)
	}
	defer itPool.Exec(ctx, `DELETE FROM worker_tokens WHERE worker_id=$1`, peerWorker)

	// A THIRD distinct same-class worker. A real tiebreak excludes BOTH workers
	// whose results disagreed, so it can only be pinned here.
	tiebreakPeer := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_max',64,400,now(),'seed',
		         ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true)`,
		tiebreakPeer, demoSupplierUUID); err != nil {
		t.Fatal(err)
	}

	// One job, one chunk, two committed PRIMARY-ish results that disagree. We model
	// the chunk with a primary task (worker A) and a redundancy task (worker B) over
	// the same chunk_index, both complete with divergent embeddings.
	jobID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/t/input.jsonl','batch',2,2,2)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	primary, redun := uuid.New(), uuid.New()
	aKey := "jobs/t/tasks/0/result.json"
	bKey := "jobs/t/redundancy/0/result.json"
	itStorage.PutObject(ctx, aKey, embedResultJSON(1), "application/json")              // e0
	itStorage.PutObject(ctx, bKey, []byte(`{"vectors":[[0,1,0]]}`), "application/json") // e1 ≠ e0
	for _, r := range []struct {
		id     uuid.UUID
		worker uuid.UUID
		redun  bool
		key    string
	}{{primary, demoWorkerUUID, false, aKey}, {redun, peerWorker, true, bKey}} {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, is_redundancy, input_ref, result_key, result_ref, chunk_index, worker_id, claimed_by, completed_at)
			 VALUES ($1,$2,'complete',$3,'jobs/t/tasks/0/input.jsonl',$4,$4,0,$5,$5, now())`,
			r.id, jobID, r.redun, r.key, r.worker); err != nil {
			t.Fatal(err)
		}
	}

	// Verifier WITH storage so the 3-way machinery runs.
	v := NewVerifier(itStore).WithStorage(itStorage)
	info := &CommitTaskInfo{TaskID: redun, JobID: jobID, WorkerID: peerWorker,
		SupplierID: demoSupplierUUID, IsRedundancy: true, jobType: "embed",
		ModelRef: "all-minilm-l6-v2", MinMemoryGB: 2, ChunkIndex: 0,
		InputRef: "jobs/t/tasks/0/input.jsonl"}
	// The committing redundancy result is bKey; the peer present is aKey → mismatch.
	out, err := v.verifyTaskResult(ctx, info, TaskCommit{TaskID: redun},
		[]byte(`{"vectors":[[0,1,0]]}`), embedResultJSON(1))
	if err != nil || out != OutcomePassWithPenalty {
		t.Fatalf("2-way mismatch should dispatch a tiebreak (pass_with_penalty), got out=%v err=%v", out, err)
	}
	// A pinned tiebreak task must now exist for the chunk, claimed by the peer.
	var tbID, tbClaimed uuid.UUID
	var tbStatus string
	if err := itPool.QueryRow(ctx,
		`SELECT id, COALESCE(claimed_by,'00000000-0000-0000-0000-000000000000'), status FROM tasks
		 WHERE job_id=$1 AND is_redundancy=true AND hedged_from IS NOT NULL`, jobID).
		Scan(&tbID, &tbClaimed, &tbStatus); err != nil {
		t.Fatalf("expected a pinned tiebreak task: %v", err)
	}
	if tbClaimed == uuid.Nil {
		t.Fatal("tiebreak task must be pinned (pre-claimed) to a chosen peer")
	}
	if tbClaimed == demoWorkerUUID || tbClaimed == peerWorker {
		t.Fatalf("tiebreak peer must be distinct from the two that disagreed, got %s", tbClaimed)
	}
	if tbClaimed != tiebreakPeer {
		t.Fatalf("tiebreak must be pinned to the only eligible third worker %s, got %s", tiebreakPeer, tbClaimed)
	}
}

// --- 19. straggler hedging: a long-running primary gets one hedge to a peer,
// and the winner's commit cancels the loser (first commit wins) ---

func TestStragglerHedge(t *testing.T) {
	reset(t)
	ctx := context.Background()
	// A distinct same-class peer to receive the hedge.
	peer := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		                      supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,'apple_silicon_max',64,400,now(),'seed',ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true)
		 ON CONFLICT (id) DO UPDATE SET last_seen_at=now()`, peer, demoSupplierUUID); err != nil {
		t.Fatal(err)
	}
	jobID, slow := uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier, task_count, tasks_done, min_memory_gb)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/h/input.jsonl','batch',1,0,2)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	// Running primary, started well past the hedge window.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, worker_id, claimed_by, claimed_at, started_at)
		 VALUES ($1,$2,'running','jobs/h/tasks/0/input.jsonl','jobs/h/tasks/0/result.json',0,$3,$3, now()-interval '10 minutes', now()-interval '10 minutes')`,
		slow, jobID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.hedgeStragglers(ctx); err != nil {
		t.Fatalf("hedgeStragglers: %v", err)
	}
	var hedgeID, hedgeClaimed uuid.UUID
	if err := itPool.QueryRow(ctx,
		`SELECT id, COALESCE(claimed_by,'00000000-0000-0000-0000-000000000000') FROM tasks
		 WHERE job_id=$1 AND hedged_from=$2 AND is_redundancy=false`, jobID, slow).
		Scan(&hedgeID, &hedgeClaimed); err != nil {
		t.Fatalf("expected a hedge task for the straggler: %v", err)
	}
	if hedgeClaimed != peer {
		t.Fatalf("hedge must be pinned to the distinct peer, got %s", hedgeClaimed)
	}
	// Hedging is once-only: a second sweep must not add another hedge.
	if err := wk.hedgeStragglers(ctx); err != nil {
		t.Fatal(err)
	}
	var nHedges int
	itPool.QueryRow(ctx, `SELECT count(*) FROM tasks WHERE job_id=$1 AND hedged_from=$2`, jobID, slow).Scan(&nHedges)
	if nHedges != 1 {
		t.Fatalf("straggler must be hedged at most once, got %d hedges", nHedges)
	}
	// First commit wins: the original primary commits → the hedge is cancelled.
	itStorage.PutObject(ctx, "jobs/h/tasks/0/result.json", embedResultJSON(1), "application/json")
	if err := itStore.CancelStragglerSiblings(ctx, jobID, 0, slow); err != nil {
		t.Fatal(err)
	}
	var hedgeStatus string
	itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, hedgeID).Scan(&hedgeStatus)
	if hedgeStatus != "failed" {
		t.Fatalf("losing hedge should be cancelled (failed), got %q", hedgeStatus)
	}
}

// --- helpers shared by the direct-call tests ---

// mustJobTask inserts a minimal job + one task so FK-bound side effects (ledger,
// requeue, clawback) have real rows to act on.
func mustJobTask(t *testing.T, jobID, taskID uuid.UUID, honeypot, redundancy bool, inputRef string) {
	t.Helper()
	ctx := context.Background()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, input_ref, tier, task_count, tasks_done)
		 VALUES ($1,$2,'running','embed','jobs/x/input.jsonl','batch',1,0)`, jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert job: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, is_honeypot, is_redundancy, input_ref, result_key, visible_at)
		 VALUES ($1,$2,'running',$3,$4,$5,$6, now())`,
		taskID, jobID, honeypot, redundancy, inputRef, "jobs/x/tasks/0/result.json"); err != nil {
		t.Fatalf("insert task: %v", err)
	}
}

func supplierRep(t *testing.T) float32 {
	t.Helper()
	var rep float32
	if err := itPool.QueryRow(context.Background(),
		`SELECT reputation FROM suppliers WHERE id=$1`, demoSupplierUUID).Scan(&rep); err != nil {
		t.Fatal(err)
	}
	return rep
}

// Plane D D4 (docs/PLANE_D.md §10): a heartbeat carrying live memory must (1) append
// a rolling worker_memory_samples row with the REPORTED values (not just overwrite
// the latest-beat columns), (2) surface a recent avg_available_gb on GET /admin/workers,
// and (3) feed MedianEffectiveMemoryGB so quote risk sees the typical eligible box.
// Real telemetry end-to-end — the sample mirrors exactly what the agent sent.
func TestMemorySampleRecorded(t *testing.T) {
	reset(t)
	ctx := context.Background()
	t.Cleanup(func() { itPool.Exec(ctx, `DELETE FROM worker_memory_samples WHERE worker_id=$1`, demoWorkerUUID) })

	// Demo worker eligible for an (embed, all-minilm-l6-v2) job so the median query —
	// which uses the SAME supported-job/model + active + not-throttled + live filter
	// the claim does — counts this worker's latest sample.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, last_seen_at=now(),
		   supported_jobs=ARRAY['embed','batch_infer'], supported_models=ARRAY['all-minilm-l6-v2'],
		   min_payout_usd_hr=0, throttled=false, effective_memory_gb=NULL, available_memory_gb=NULL,
		   reserved_headroom_gb=NULL WHERE id=$1`, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	itPool.Exec(ctx, `UPDATE suppliers SET status='active' WHERE id=$1`, demoSupplierUUID)

	// A real heartbeat over the wire (the SAME path the agent uses), carrying live
	// memory. effective 40 / available 48; not throttled.
	hb := Heartbeat{
		WorkerID:          demoWorkerUUID,
		Timestamp:         uint64(time.Now().Unix()),
		AvailableMemoryGB: 48,
		EffectiveMemoryGB: 40,
		Throttled:         false,
	}
	if code, body := req(t, "POST", "/v1/worker/heartbeat", hb, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("heartbeat -> %d, want 204; body=%s", code, body)
	}

	// (1) Exactly one sample row, holding the reported values.
	var n int
	var availGB, effGB float32
	var throttled bool
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_memory_samples WHERE worker_id=$1`, demoWorkerUUID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("expected exactly 1 memory sample after one heartbeat, got %d", n)
	}
	if err := itPool.QueryRow(ctx,
		`SELECT available_gb, effective_gb, throttled FROM worker_memory_samples
		  WHERE worker_id=$1 ORDER BY created_at DESC LIMIT 1`, demoWorkerUUID).
		Scan(&availGB, &effGB, &throttled); err != nil {
		t.Fatal(err)
	}
	if availGB != 48 || effGB != 40 || throttled {
		t.Fatalf("sample mismatch: available=%v effective=%v throttled=%v (want 48/40/false)", availGB, effGB, throttled)
	}

	// (2) GET /admin/workers surfaces the rolling average for this worker.
	code, body := req(t, "GET", "/admin/workers", nil, adminKey())
	if code != 200 {
		t.Fatalf("admin/workers -> %d; body=%s", code, body)
	}
	var workers []AdminWorker
	if err := json.Unmarshal(body, &workers); err != nil {
		t.Fatalf("decode admin/workers: %v; body=%s", err, body)
	}
	found := false
	for _, w := range workers {
		if w.ID == demoWorkerUUID {
			found = true
			if w.MemorySamples != 1 || w.AvgAvailableGB != 48 {
				t.Fatalf("admin worker avg_available_gb=%v samples=%d, want 48/1", w.AvgAvailableGB, w.MemorySamples)
			}
		}
	}
	if !found {
		t.Fatalf("demo worker missing from admin/workers; body=%s", body)
	}

	// (3) The median feeds quote risk: an eligible (embed, all-minilm) query returns
	// this worker's reported effective memory.
	median, ok, err := itStore.MedianEffectiveMemoryGB(ctx, "embed", "all-minilm-l6-v2")
	if err != nil {
		t.Fatalf("MedianEffectiveMemoryGB: %v", err)
	}
	if !ok || median != 40 {
		t.Fatalf("median effective memory ok=%v median=%v, want true/40", ok, median)
	}

	// A pre-throttling beat (no memory fields) must NOT add a diluting all-NULL row.
	if code, _ := req(t, "POST", "/v1/worker/heartbeat",
		Heartbeat{WorkerID: demoWorkerUUID, Timestamp: uint64(time.Now().Unix())}, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("bare heartbeat -> %d, want 204", code)
	}
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_memory_samples WHERE worker_id=$1`, demoWorkerUUID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("a memory-less heartbeat must not write a sample; count=%d", n)
	}
}

// Plane D D3 (docs/PLANE_D.md §9): a heartbeat carrying loaded_models must upsert one
// worker_model_state row per warm model id (last_seen_warm = now()), refresh — not
// duplicate — on the next beat, and feed both the scheduler's warm re-rank
// (CandidateWorkers sets Warm) and the quote's warm supply count
// (WarmEligibleWorkerCount). Real warm telemetry end-to-end: a row exists only because
// the agent reported that id warm. A pre-warm beat (no loaded_models) writes nothing.
func TestWorkerModelStateUpsert(t *testing.T) {
	reset(t)
	ctx := context.Background()
	t.Cleanup(func() { itPool.Exec(ctx, `DELETE FROM worker_model_state WHERE worker_id=$1`, demoWorkerUUID) })

	// Demo worker eligible (embed, all-minilm-l6-v2): the warm-count + candidate
	// queries use the same supported-job/model + active + not-throttled + live filter.
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET hw_class='apple_silicon_max', memory_gb=64, last_seen_at=now(),
		   supported_jobs=ARRAY['embed','batch_infer'], supported_models=ARRAY['all-minilm-l6-v2'],
		   min_payout_usd_hr=0, throttled=false, effective_memory_gb=NULL WHERE id=$1`, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	itPool.Exec(ctx, `UPDATE suppliers SET status='active' WHERE id=$1`, demoSupplierUUID)

	// A real heartbeat over the wire (the SAME path the agent uses) reporting one warm
	// model. An unrelated, non-eligible id is included to prove the row is upserted
	// verbatim (warm state is recorded; eligibility filtering happens at read time).
	hb := Heartbeat{
		WorkerID:     demoWorkerUUID,
		Timestamp:    uint64(time.Now().Unix()),
		LoadedModels: []string{"all-minilm-l6-v2", "whisper-tiny"},
	}
	if code, body := req(t, "POST", "/v1/worker/heartbeat", hb, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("heartbeat -> %d, want 204; body=%s", code, body)
	}

	// One row per reported id, each fresh.
	var rows int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_model_state
		  WHERE worker_id=$1 AND last_seen_warm > now() - interval '60 seconds'`, demoWorkerUUID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 2 {
		t.Fatalf("expected 2 fresh warm rows after one heartbeat, got %d", rows)
	}

	// The eligible (embed, all-minilm) job sees this worker as warm supply.
	warm, err := itStore.WarmEligibleWorkerCount(ctx, "embed", "all-minilm-l6-v2", 1)
	if err != nil {
		t.Fatalf("WarmEligibleWorkerCount: %v", err)
	}
	if warm != 1 {
		t.Fatalf("warm eligible count = %d, want 1", warm)
	}
	// A model the worker has NOT reported warm is not warm supply (no fabrication).
	if cold, err := itStore.WarmEligibleWorkerCount(ctx, "batch_infer", "llama-3.2-1b-instruct-q4", 1); err != nil {
		t.Fatalf("WarmEligibleWorkerCount(cold): %v", err)
	} else if cold != 0 {
		t.Fatalf("a never-reported model must have 0 warm workers, got %d", cold)
	}

	// CandidateWorkers (the scheduler's ranking view) marks this worker Warm for the
	// model it reported, and NOT for one it did not — the warm re-rank input.
	cands, err := itStore.CandidateWorkers(ctx, "embed", "all-minilm-l6-v2", 1)
	if err != nil {
		t.Fatalf("CandidateWorkers: %v", err)
	}
	foundWarm := false
	for _, c := range cands {
		if c.ID == demoWorkerUUID {
			foundWarm = c.Warm
		}
	}
	if !foundWarm {
		t.Fatal("demo worker should be marked Warm for the model it reported warm")
	}
	coldCands, err := itStore.CandidateWorkers(ctx, "embed", "some-other-model", 1)
	if err != nil {
		t.Fatalf("CandidateWorkers(cold): %v", err)
	}
	for _, c := range coldCands {
		if c.ID == demoWorkerUUID && c.Warm {
			t.Fatal("worker must NOT be Warm for a model it never reported")
		}
	}

	// Re-send the same beat: the upsert refreshes last_seen_warm, never duplicates
	// (PRIMARY KEY (worker_id, model_id)).
	if code, _ := req(t, "POST", "/v1/worker/heartbeat", hb, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("second heartbeat -> %d, want 204", code)
	}
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_model_state WHERE worker_id=$1`, demoWorkerUUID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 2 {
		t.Fatalf("re-reporting the same models must upsert, not duplicate; rows=%d", rows)
	}

	// A pre-warm beat (no loaded_models) writes nothing new and leaves the rows intact.
	if code, _ := req(t, "POST", "/v1/worker/heartbeat",
		Heartbeat{WorkerID: demoWorkerUUID, Timestamp: uint64(time.Now().Unix())}, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("bare heartbeat -> %d, want 204", code)
	}
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM worker_model_state WHERE worker_id=$1`, demoWorkerUUID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 2 {
		t.Fatalf("a loaded_models-less heartbeat must not change warm rows; rows=%d", rows)
	}
}

// --- long-poll worker dispatch (Plane D §7 D1) ---

// seedClaimableEmbedTask inserts a running job + one queued, fully-claimable embed
// task for the demo worker and returns the task id. Mirrors the budget-cap test's
// seeding shape (no max_usd, so no budget gate); the demo worker after reset() is
// apple_silicon_max / supports embed+all-minilm / active / live, so this task is
// claimable on every axis.
func seedClaimableEmbedTask(t *testing.T, ctx context.Context) (jobID, taskID uuid.UUID) {
	t.Helper()
	jobID, taskID = uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
		                   task_count, tasks_done, min_memory_gb, estimated_usd)
		 VALUES ($1,$2,'running','embed','all-minilm-l6-v2','jobs/lp/in.jsonl','batch',1,0,2,0.01)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatalf("seed job: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, status, input_ref, result_key, chunk_index, visible_at)
		 VALUES ($1,$2,'queued','jobs/lp/t0/in.jsonl','jobs/lp/t0/out.json',0, now())`,
		taskID, jobID); err != nil {
		t.Fatalf("seed task: %v", err)
	}
	return jobID, taskID
}

// TestLongPollReturnsOnNewTask proves the server-side wait wakes on real work: a
// poll started with ?wait_ms returns a task that becomes claimable mid-wait, and it
// returns FAST (well under the wait budget) rather than after the full timeout —
// the latency win D1 is built for.
func TestLongPollReturnsOnNewTask(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE tasks, jobs, job_events CASCADE`) })
	reset(t) // demo worker live + eligible; queue empty so the poll has to wait first

	// Insert the claimable task ~400ms into the wait, from another goroutine. The
	// poll is in-flight by then (it started with an empty queue), so it must pick the
	// task up on its next ~250ms re-attempt — not on the initial claim.
	const waitMS = 6000
	insertAfter := 400 * time.Millisecond
	var taskID uuid.UUID
	go func() {
		time.Sleep(insertAfter)
		_, taskID = seedClaimableEmbedTask(t, ctx)
	}()

	start := time.Now()
	code, body := req(t, "GET", "/v1/worker/poll?wait_ms="+strconv.Itoa(waitMS), nil, workerTok())
	elapsed := time.Since(start)
	if code != http.StatusOK {
		t.Fatalf("long-poll: want 200 (task delivered mid-wait), got %d: %s", code, body)
	}
	var disp TaskDispatch
	if err := json.Unmarshal(body, &disp); err != nil {
		t.Fatalf("dispatch decode: %v (%s)", err, body)
	}
	// The wait must have ended on the INSERT, not on the timeout: a couple of
	// re-attempt ticks after the ~400ms insert, far below the 6s budget.
	if elapsed > 3*time.Second {
		t.Fatalf("long-poll took %v — it timed out instead of waking on the new task", elapsed)
	}
	// And it must hand back the task we inserted (claimed + dispatched), not a phantom.
	if taskID != (uuid.UUID{}) && disp.TaskID != taskID {
		t.Fatalf("long-poll returned task %s, want the just-inserted %s", disp.TaskID, taskID)
	}
	var claimedBy *uuid.UUID
	itPool.QueryRow(ctx, `SELECT claimed_by FROM tasks WHERE id=$1`, disp.TaskID).Scan(&claimedBy)
	if claimedBy == nil || *claimedBy != demoWorkerUUID {
		t.Fatalf("delivered task must be claimed by the demo worker, claimed_by=%v", claimedBy)
	}
}

// TestLongPollTimesOutCleanly proves an idle wait ends correctly: with nothing
// claimable, a poll with ?wait_ms holds open for ~wait_ms, then returns 204 (not an
// error, not a stuck connection) and bumps cx_long_poll_timeouts_total exactly once.
func TestLongPollTimesOutCleanly(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`) })
	reset(t)
	// Empty, claimable queue guaranteed: drop every task so the wait can never find work.
	if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
		t.Fatalf("clear queue: %v", err)
	}

	before := metrics.longPollTimeouts.Load()
	const waitMS = 700 // small but real: keeps the test fast while exercising the full wait

	start := time.Now()
	code, body := req(t, "GET", "/v1/worker/poll?wait_ms="+strconv.Itoa(waitMS), nil, workerTok())
	elapsed := time.Since(start)
	if code != http.StatusNoContent {
		t.Fatalf("idle long-poll: want 204 after the wait, got %d: %s", code, body)
	}
	// It must have actually WAITED (not returned 204 instantly): at least most of the
	// budget elapsed. Lower-bounded loosely to stay robust on a busy CI box.
	if elapsed < 500*time.Millisecond {
		t.Fatalf("idle long-poll returned in %v — it did not hold open for ~%dms", elapsed, waitMS)
	}
	// And it must not have hung far past the budget (the deadline fires, no leak).
	if elapsed > 5*time.Second {
		t.Fatalf("idle long-poll took %v — the wait deadline did not fire", elapsed)
	}
	if got := metrics.longPollTimeouts.Load(); got != before+1 {
		t.Fatalf("cx_long_poll_timeouts_total = %d, want %d (one timed-out empty return)", got, before+1)
	}
}

// TestPollNoWaitUnchanged guards backwards compatibility: a poll WITHOUT wait_ms
// (the pre-D1 single-shot contract an older agent speaks) still returns 204
// immediately on an empty queue and 200 on the next claimable task — and never
// counts as a long-poll timeout.
func TestPollNoWaitUnchanged(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() { itPool.Exec(ctx, `TRUNCATE tasks, jobs, job_events CASCADE`) })
	reset(t)
	if _, err := itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`); err != nil {
		t.Fatalf("clear queue: %v", err)
	}

	before := metrics.longPollTimeouts.Load()
	// Empty queue, no wait_ms → immediate 204 (single-shot), and no timeout counted.
	start := time.Now()
	if code, body := req(t, "GET", "/v1/worker/poll", nil, workerTok()); code != http.StatusNoContent {
		t.Fatalf("no-wait empty poll: want 204, got %d: %s", code, body)
	}
	if elapsed := time.Since(start); elapsed > 300*time.Millisecond {
		t.Fatalf("no-wait poll took %v — it must not wait", elapsed)
	}
	if got := metrics.longPollTimeouts.Load(); got != before {
		t.Fatalf("a no-wait poll must NOT bump cx_long_poll_timeouts_total: %d -> %d", before, got)
	}

	// With work present, no-wait still claims it on the spot.
	_, taskID := seedClaimableEmbedTask(t, ctx)
	code, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	if code != http.StatusOK {
		t.Fatalf("no-wait poll with work: want 200, got %d: %s", code, body)
	}
	var disp TaskDispatch
	if err := json.Unmarshal(body, &disp); err != nil {
		t.Fatalf("dispatch decode: %v (%s)", err, body)
	}
	if disp.TaskID != taskID {
		t.Fatalf("no-wait poll returned %s, want %s", disp.TaskID, taskID)
	}
}

// --- Compute Autopilot pipeline: output->input chaining ---

// driveOneTask claims the next queued task, PUTs an embed result, and commits it — the
// minimal worker loop for a single-task job (committing the final task finalizes the job
// synchronously, which is what fires advancePipeline).
func driveOneTask(t *testing.T, ctx context.Context) {
	t.Helper()
	code, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	if code != 200 {
		t.Fatalf("poll: want 200, got %d: %s", code, body)
	}
	var disp TaskDispatch
	if err := json.Unmarshal(body, &disp); err != nil {
		t.Fatalf("dispatch decode: %v", err)
	}
	if err := itStorage.PutObject(ctx, disp.ResultKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatalf("put result: %v", err)
	}
	commit := TaskCommit{TaskID: disp.TaskID, ResultKey: disp.ResultKey, DurationMS: 10, TokensUsed: 8}
	if code, b := req(t, "POST", "/v1/worker/task/"+disp.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != 204 {
		t.Fatalf("commit: want 204, got %d: %s", code, b)
	}
}

// TestPipelineChaining exercises the user-defined pipeline orchestration end to end: a
// two-stage pipeline launches stage 0 on the supplied input, and when stage 0 completes the
// completion hook (advancePipeline) must auto-submit stage 1 on stage 0's merged output, and
// the pipeline must report complete only once every stage has completed. Both stages are
// embed so the merge format is identical — this tests the ORCHESTRATION (the chain fires,
// links, advances, and derives status), not cross-op data semantics.
func TestPipelineChaining(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// 1. Submit a 2-stage pipeline: stage 0 reads the input, stage 1 chains on stage 0.
	body := map[string]any{
		"name": "embed then re-embed",
		"stages": []map[string]any{
			{"op": "embed", "model": "all-minilm-l6-v2", "from": "input"},
			{"op": "embed", "model": "all-minilm-l6-v2", "from": "previous"},
		},
		"input": `{"id":"r0","text":"hello pipeline"}` + "\n",
	}
	code, out := req(t, "POST", "/v1/pipelines", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("create pipeline: want 202, got %d: %s", code, out)
	}
	var cr struct {
		PipelineID string           `json:"pipeline_id"`
		Launched   []map[string]any `json:"launched"`
	}
	if err := json.Unmarshal(out, &cr); err != nil {
		t.Fatalf("create decode: %v (%s)", err, out)
	}
	if cr.PipelineID == "" || len(cr.Launched) != 1 {
		t.Fatalf("want pipeline_id + exactly 1 launched stage (stage 0), got %s", out)
	}

	// 2. Drive stage 0's job to completion → the completion hook chains stage 1.
	driveOneTask(t, ctx)

	// 3. Stage 0 must be complete and stage 1 must now be chained (have a job).
	code, out = req(t, "GET", "/v1/pipelines/"+cr.PipelineID, nil, buyerKey())
	if code != 200 {
		t.Fatalf("get pipeline: %d %s", code, out)
	}
	var pv PipelineView
	if err := json.Unmarshal(out, &pv); err != nil {
		t.Fatalf("pipeline decode: %v (%s)", err, out)
	}
	if len(pv.Stages) != 2 {
		t.Fatalf("want 2 stages, got %d (%s)", len(pv.Stages), out)
	}
	if pv.Stages[0].Status != "complete" {
		t.Fatalf("stage 0: want complete, got %q", pv.Stages[0].Status)
	}
	if pv.Stages[1].JobID == "" {
		t.Fatalf("stage 1 was not chained (no job_id) after stage 0 completed: %s", out)
	}
	if pv.Status != "running" {
		t.Fatalf("pipeline overall: want running (stage 1 not done), got %q", pv.Status)
	}

	// 4. Drive stage 1 to completion → the whole pipeline is complete.
	driveOneTask(t, ctx)
	code, out = req(t, "GET", "/v1/pipelines/"+cr.PipelineID, nil, buyerKey())
	if code != 200 {
		t.Fatalf("get pipeline (2): %d %s", code, out)
	}
	if err := json.Unmarshal(out, &pv); err != nil {
		t.Fatalf("pipeline decode (2): %v (%s)", err, out)
	}
	if pv.Status != "complete" {
		t.Fatalf("pipeline: want complete, got %q (stages: %s)", pv.Status, out)
	}
}

// TestPipelineValidation rejects malformed pipelines honestly.
func TestPipelineValidation(t *testing.T) {
	reset(t)
	cases := []struct {
		name string
		body map[string]any
	}{
		{"no stages", map[string]any{"name": "x", "stages": []map[string]any{}, "input": "x\n"}},
		{"unknown op", map[string]any{"name": "x", "stages": []map[string]any{{"op": "mine_bitcoin", "model": "m", "from": "input"}}, "input": "x\n"}},
		{"stage 0 from previous", map[string]any{"name": "x", "stages": []map[string]any{{"op": "embed", "model": "m", "from": "previous"}}, "input": "x\n"}},
		{"no input", map[string]any{"name": "x", "stages": []map[string]any{{"op": "embed", "model": "m", "from": "input"}}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			code, out := req(t, "POST", "/v1/pipelines", c.body, buyerKey(), jsonCT())
			if code != http.StatusBadRequest {
				t.Fatalf("%s: want 400, got %d: %s", c.name, code, out)
			}
		})
	}
}

// TestFileDispute proves the buyer-dispute seam (optimistic-verification / payout-
// guarantee foundation): the owning buyer can file a dispute (202 + an id + the honest
// boundary note), and a buyer can NOT dispute a job that is not theirs (404, nothing
// recorded). Resolution by optimistic recompute is intentionally NOT exercised — it is
// the external/future work behind the seam.
func TestFileDispute(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, input_ref, task_count, tasks_done)
		 VALUES ($1,$2,'complete','embed','jobs/x/input.jsonl',1,1)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert job: %v", err)
	}

	// Owner files a dispute → 202 with an id + status open.
	code, body := req(t, "POST", "/v1/jobs/"+jobID.String()+"/dispute",
		map[string]string{"reason": "result looks wrong"}, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("file dispute: want 202, got %d: %s", code, body)
	}
	var got struct {
		DisputeID string `json:"dispute_id"`
		Status    string `json:"status"`
	}
	if err := json.Unmarshal(body, &got); err != nil || got.DisputeID == "" || got.Status != "open" {
		t.Fatalf("dispute response missing id/status: %s", body)
	}
	var n int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM disputes WHERE job_id=$1`, jobID).Scan(&n); err != nil || n != 1 {
		t.Fatalf("dispute not recorded: n=%d err=%v", n, err)
	}

	// A buyer cannot dispute a job that is not theirs (random/unknown id) → 404, and
	// nothing is recorded (the INSERT...SELECT...WHERE EXISTS yields no row).
	other := uuid.New()
	code2, _ := req(t, "POST", "/v1/jobs/"+other.String()+"/dispute",
		map[string]string{"reason": "x"}, buyerKey(), jsonCT())
	if code2 != http.StatusNotFound {
		t.Fatalf("dispute on non-owned job: want 404, got %d", code2)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM disputes WHERE job_id=$1`, other).Scan(&n); err != nil || n != 0 {
		t.Fatalf("non-owned dispute must not be recorded: n=%d err=%v", n, err)
	}
}

// TestDisputeResolverNoPeer proves the optimistic-verification resolver's honest
// boundary: when the only same-class supplier for the disputed job IS the original
// (no distinct peer to independently re-run on), the resolver surfaces 'no_peer' and
// retries later — it never fakes a resolution. The agree/disagree verdict paths flow
// through the existing redundancy verifier (covered by the tiebreak tests).
func TestDisputeResolverNoPeer(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, input_ref, task_count, tasks_done)
		 VALUES ($1,$2,'complete','embed','jobs/x/input.jsonl',1,1)`, jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert job: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO tasks (id, job_id, worker_id, status, input_ref, result_key, chunk_index, completed_at)
		 VALUES ($1,$2,$3,'complete','jobs/x/tasks/0/input.jsonl','jobs/x/tasks/0/result.json',0, now())`,
		taskID, jobID, demoWorkerUUID); err != nil {
		t.Fatalf("insert task: %v", err)
	}
	code, body := req(t, "POST", "/v1/jobs/"+jobID.String()+"/dispute",
		map[string]string{"reason": "x"}, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("file dispute: %d %s", code, body)
	}

	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.resolveDisputes(ctx); err != nil {
		t.Fatalf("resolveDisputes: %v", err)
	}
	var status string
	if err := itPool.QueryRow(ctx, `SELECT status FROM disputes WHERE job_id=$1`, jobID).Scan(&status); err != nil {
		t.Fatalf("read dispute: %v", err)
	}
	if status != "no_peer" {
		t.Fatalf("want 'no_peer' (no distinct same-class supplier to re-verify), got %q", status)
	}
}

// TestVerificationReceiptSurfaced proves the verification RECEIPT path end to end: a real
// verification outcome records an append-only verification_events row, and the buyer
// job-status (GetJob) surfaces the aggregate counts + the honest derived label +
// charge_status. This is the moat made visible — exercised against live Postgres.
func TestVerificationReceiptSurfaced(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO jobs (id, buyer_id, status, job_type, input_ref, task_count, tasks_done)
		 VALUES ($1,$2,'complete','embed','jobs/x/input.jsonl',1,1)`, jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert job: %v", err)
	}
	// A redundancy MATCH (two agreeing results) records a redundancy_match event for the job.
	info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
		SupplierID: demoSupplierUUID, jobType: "embed", HWClass: "apple_silicon_max"}
	if out, err := itServer.verifier.verifyTaskResult(ctx, info,
		TaskCommit{TaskID: taskID}, embedResultJSON(1), embedResultJSON(1)); err != nil || out != OutcomePass {
		t.Fatalf("verify match: out=%v err=%v", out, err)
	}
	var n int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM verification_events WHERE job_id=$1 AND kind='redundancy_match'`, jobID).Scan(&n); err != nil || n < 1 {
		t.Fatalf("want >=1 redundancy_match verification_event, got n=%d err=%v", n, err)
	}
	// The buyer job-status surfaces the receipt aggregate, the honest label, and charge_status.
	jv, err := itStore.GetJob(ctx, jobID, demoBuyerUUID)
	if err != nil {
		t.Fatalf("GetJob: %v", err)
	}
	if jv.Verification.RedundancyMatched < 1 || jv.Verification.Checked < 1 {
		t.Fatalf("receipt aggregate missing: %+v", jv.Verification)
	}
	if jv.Verification.Label != "verified" {
		t.Fatalf("want label 'verified' (redundancy matched), got %q", jv.Verification.Label)
	}
	if jv.ChargeStatus == "" {
		t.Fatalf("charge_status should surface a default state, got empty")
	}
}
