//go:build integration

package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func insertRuntimeAuthorityFixtureJob(t *testing.T, jobType, modelRef string) uuid.UUID {
	t.Helper()
	ctx := context.Background()
	jobID := uuid.New()
	job := &jobRow{
		ID:                 jobID,
		BuyerID:            demoBuyerUUID,
		JobType:            jobType,
		ModelRef:           modelRef,
		InputRef:           "runtime-authority/" + jobID.String() + "/input.jsonl",
		OutputRef:          "runtime-authority/" + jobID.String() + "/output.json",
		Tier:               "batch",
		VerificationPolicy: []byte(`{}`),
		TaskCount:          1,
		MinMemoryGB:        1,
	}
	job.EconomicPlan = BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD:   1,
		InitialTaskCount: 1,
		SupplierShare:    supplierShareRate,
	}, testEconomicSchedule())
	job.EstimatedUSD = job.EconomicPlan.InitialBuyerChargeUSD
	tasks := []taskRow{{
		ID:        uuid.New(),
		JobID:     jobID,
		InputRef:  job.InputRef,
		ResultKey: job.OutputRef,
	}}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("create runtime-authority fixture job: %v", err)
	}
	return jobID
}

func authorizedCapabilityCount(t *testing.T, workerID uuid.UUID) int {
	t.Helper()
	var n int
	if err := itPool.QueryRow(context.Background(),
		`SELECT count(*) FROM worker_authorized_capabilities
		  WHERE worker_id = $1 AND matrix_sha256 = $2`,
		workerID, generatedRuntimeMatrixSHA256,
	).Scan(&n); err != nil {
		t.Fatalf("count worker authorized capabilities: %v", err)
	}
	return n
}

func TestRuntimeMatrixRejectsUnsupportedIngressBeforeWrites(t *testing.T) {
	reset(t)
	ctx := context.Background()
	if _, err := itPool.Exec(ctx, `TRUNCATE quotes`); err != nil {
		t.Fatal(err)
	}
	body := map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "llama-3.2-1b-instruct-q4"},
		"tier":         "batch",
		"verification": map[string]any{"skip_verification_floor": true},
		"input":        `{"id":"a","text":"must never be stored"}` + "\n",
	}
	for _, endpoint := range []string{"/v1/quote", "/v1/jobs"} {
		code, out := req(t, "POST", endpoint, body, buyerKey(), jsonCT())
		if code != http.StatusBadRequest || !strings.Contains(string(out), "runtime capability is not advertised") {
			t.Fatalf("%s must reject an unsupported Cartesian job/model cell: %d %s", endpoint, code, out)
		}
	}
	var jobs, quotes int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM jobs`).Scan(&jobs); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM quotes`).Scan(&quotes); err != nil {
		t.Fatal(err)
	}
	if jobs != 0 || quotes != 0 {
		t.Fatalf("rejected runtime cells must leave no job/quote rows, got jobs=%d quotes=%d", jobs, quotes)
	}
}

func TestRuntimeMatrixPublicCatalogHidesPendingModels(t *testing.T) {
	if err := itStore.ValidateAdvertisedRuntimeCatalog(context.Background()); err != nil {
		t.Fatalf("seeded production catalog must satisfy the runtime matrix: %v", err)
	}
	code, body := req(t, "GET", "/v1/models", nil, buyerKey())
	if code != http.StatusOK {
		t.Fatalf("GET /v1/models: %d %s", code, body)
	}
	var models []ModelInfo
	if err := json.Unmarshal(body, &models); err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, model := range models {
		seen[model.ID] = true
	}
	for _, required := range []string{"all-minilm-l6-v2", "llama-3.2-1b-instruct-q4", "whisper-tiny", "whisper-base"} {
		if !seen[required] {
			t.Errorf("production model %q missing from public catalog", required)
		}
	}
	for _, pending := range []string{"bge-small-en-v1.5", "qwen2.5-7b-instruct-q4"} {
		if seen[pending] {
			t.Errorf("hardware-pending model %q leaked into public catalog", pending)
		}
		code, out := req(t, "GET", "/v1/price-estimate?model="+pending+"&units=1000&tier=batch", nil, buyerKey())
		if code != http.StatusBadRequest || !strings.Contains(string(out), "not advertised") {
			t.Errorf("pending model %q price estimate must fail closed: %d %s", pending, code, out)
		}
	}
}

func TestWorkerRegistrationPersistsSevenExactCellsNotCartesianProduct(t *testing.T) {
	reset(t)
	cap := demoProductionCapability()
	code, body := req(t, http.MethodPost, "/v1/worker/register", cap, workerTok(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("register full production projection: %d %s", code, body)
	}

	var declaredJobs, declaredModels int
	if err := itPool.QueryRow(context.Background(),
		`SELECT cardinality(supported_jobs), cardinality(supported_models)
		   FROM workers WHERE id = $1`, demoWorkerUUID,
	).Scan(&declaredJobs, &declaredModels); err != nil {
		t.Fatal(err)
	}
	if declaredJobs*declaredModels != 24 {
		t.Fatalf("fixture must expose the old 6x4 Cartesian hazard, got %dx%d", declaredJobs, declaredModels)
	}
	if got := authorizedCapabilityCount(t, demoWorkerUUID); got != 7 {
		t.Fatalf("normalized authority rows=%d, want the 7 generated cells, never 24", got)
	}

	rows, err := itPool.Query(context.Background(),
		`SELECT cell_id, runtime_id, job_type, model_ref, model_kind, matrix_sha256
		   FROM worker_authorized_capabilities WHERE worker_id = $1`, demoWorkerUUID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() {
		var cellID, runtimeID, jobType, modelRef, modelKind, matrixSHA string
		if err := rows.Scan(&cellID, &runtimeID, &jobType, &modelRef, &modelKind, &matrixSHA); err != nil {
			t.Fatal(err)
		}
		if matrixSHA != generatedRuntimeMatrixSHA256 {
			t.Fatalf("cell %q bound stale matrix %q", cellID, matrixSHA)
		}
		got[cellID+"\x00"+runtimeID+"\x00"+jobType+"\x00"+modelRef+"\x00"+modelKind] = true
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	for _, want := range generatedAdvertisedRuntimeCapabilities {
		key := want.ID + "\x00" + want.Runtime + "\x00" + want.Job + "\x00" + want.Model + "\x00" + want.ModelKind
		if !got[key] {
			t.Errorf("generated production cell not persisted: %s", want.ID)
		}
	}
	if got["candle-metal-minilm-embed\x00candle_metal\x00embed\x00llama-3.2-1b-instruct-q4\x00gguf"] {
		t.Fatal("unsupported Cartesian tuple entered normalized authority")
	}
}

func TestLegacyArrayOnlyWorkerIsInertUntilReregistration(t *testing.T) {
	reset(t)
	ctx := context.Background()
	if _, err := itPool.Exec(ctx,
		`DELETE FROM worker_authorized_capabilities WHERE worker_id = $1`, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	// Replaying the production migration must not infer authority from legacy arrays.
	if err := itStore.Migrate(ctx); err != nil {
		t.Fatalf("repeat migrate: %v", err)
	}
	if got := authorizedCapabilityCount(t, demoWorkerUUID); got != 0 {
		t.Fatalf("migration backfilled %d capability rows from legacy arrays", got)
	}
	insertRuntimeAuthorityFixtureJob(t, "embed", "all-minilm-l6-v2")

	var arraysStillClaimSupport bool
	if err := itPool.QueryRow(ctx,
		`SELECT (supported_jobs @> ARRAY['embed']) AND (supported_models @> ARRAY['all-minilm-l6-v2'])
		   FROM workers WHERE id = $1`, demoWorkerUUID,
	).Scan(&arraysStillClaimSupport); err != nil {
		t.Fatal(err)
	}
	if !arraysStillClaimSupport {
		t.Fatal("legacy fixture must retain apparently compatible arrays")
	}
	if n, err := itStore.EligibleWorkerCount(ctx, "embed", "all-minilm-l6-v2", 1); err != nil || n != 0 {
		t.Fatalf("array-only legacy worker counted as supply: n=%d err=%v", n, err)
	}
	if candidates, err := itStore.CandidateWorkers(ctx, "embed", "all-minilm-l6-v2", 1); err != nil || len(candidates) != 0 {
		t.Fatalf("array-only legacy worker entered routing candidates: len=%d err=%v", len(candidates), err)
	}
	claimed, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil {
		t.Fatalf("legacy inert claim: %v", err)
	}
	if claimed != nil {
		t.Fatalf("array-only legacy worker claimed task %s", claimed.TaskID)
	}
	code, body := req(t, http.MethodPost, "/v1/worker/register", demoProductionCapability(), workerTok(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("re-register legacy worker: %d %s", code, body)
	}
	claimed, err = itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil {
		t.Fatalf("claim after re-registration: %v", err)
	}
	if claimed == nil {
		t.Fatal("worker stayed inert after exact-capability re-registration")
	}
	if claimed.RuntimeCellID != "candle-metal-minilm-embed" ||
		claimed.RuntimeID != "candle_metal" ||
		claimed.RuntimeMatrixSHA != generatedRuntimeMatrixSHA256 || claimed.ModelKind != "hf" {
		t.Fatalf("claim did not freeze exact runtime/catalog authority: %+v", claimed)
	}
	var cellID, runtimeID, matrixSHA, modelKind string
	if err := itPool.QueryRow(ctx,
		`SELECT runtime_cell_id,runtime_id,runtime_matrix_sha256,model_kind
		   FROM tasks WHERE id=$1`, claimed.TaskID,
	).Scan(&cellID, &runtimeID, &matrixSHA, &modelKind); err != nil {
		t.Fatal(err)
	}
	if cellID != claimed.RuntimeCellID || runtimeID != claimed.RuntimeID ||
		matrixSHA != claimed.RuntimeMatrixSHA || modelKind != claimed.ModelKind {
		t.Fatalf("persisted runtime authority drifted from dispatch: %q %q %q %q", cellID, runtimeID, matrixSHA, modelKind)
	}
	receipts, err := itStore.JobTaskReceipts(ctx, claimed.JobID)
	if err != nil || len(receipts) != 1 {
		t.Fatalf("runtime-bound task receipt: len=%d err=%v", len(receipts), err)
	}
	if receipts[0].RuntimeCellID != cellID || receipts[0].RuntimeID != runtimeID ||
		receipts[0].RuntimeMatrixSHA != matrixSHA || receipts[0].ModelKind != modelKind {
		t.Fatalf("receipt omitted frozen runtime authority: %+v", receipts[0])
	}
}

func TestClaimUsesGeneratedWireKindNotMutableCatalog(t *testing.T) {
	reset(t)
	ctx := context.Background()
	code, body := req(t, http.MethodPost, "/v1/worker/register", demoProductionCapability(), workerTok(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("register generated authority: %d %s", code, body)
	}
	insertRuntimeAuthorityFixtureJob(t, "embed", "all-minilm-l6-v2")

	// Simulate post-start catalog drift to another otherwise valid wire family. The
	// claim must continue using the generated value persisted at registration, never
	// reinterpret this matrix SHA through today's mutable catalog row.
	if _, err := itPool.Exec(ctx,
		`UPDATE models SET kind='gguf' WHERE id='all-minilm-l6-v2'`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(),
			`UPDATE models SET kind='embed' WHERE id='all-minilm-l6-v2'`)
	})

	claimed, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil || claimed == nil {
		t.Fatalf("claim with drifted catalog: claimed=%+v err=%v", claimed, err)
	}
	if claimed.ModelKind != "hf" {
		t.Fatalf("claim followed mutable catalog kind: got %q, want generated hf", claimed.ModelKind)
	}
}

func TestUnsupportedCartesianTupleCannotCountRouteOrClaim(t *testing.T) {
	reset(t)
	ctx := context.Background()
	insertRuntimeAuthorityFixtureJob(t, "embed", "llama-3.2-1b-instruct-q4")

	var oldArraysWouldMatch bool
	if err := itPool.QueryRow(ctx,
		`SELECT (supported_jobs @> ARRAY['embed']) AND (supported_models @> ARRAY['llama-3.2-1b-instruct-q4'])
		   FROM workers WHERE id = $1`, demoWorkerUUID,
	).Scan(&oldArraysWouldMatch); err != nil {
		t.Fatal(err)
	}
	if !oldArraysWouldMatch {
		t.Fatal("fixture no longer demonstrates the former Cartesian false positive")
	}
	if n, err := itStore.EligibleWorkerCount(ctx, "embed", "llama-3.2-1b-instruct-q4", 1); err != nil || n != 0 {
		t.Fatalf("unsupported tuple counted as supply: n=%d err=%v", n, err)
	}
	if rows, err := itStore.FleetRateSnapshot(ctx, "embed", "llama-3.2-1b-instruct-q4", 1); err != nil || len(rows) != 0 {
		t.Fatalf("unsupported tuple entered planner fleet: len=%d err=%v", len(rows), err)
	}
	if candidates, err := itStore.CandidateWorkers(ctx, "embed", "llama-3.2-1b-instruct-q4", 1); err != nil || len(candidates) != 0 {
		t.Fatalf("unsupported tuple entered peer routing: len=%d err=%v", len(candidates), err)
	}
	claimed, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID})
	if err != nil {
		t.Fatalf("unsupported tuple claim: %v", err)
	}
	if claimed != nil {
		t.Fatalf("unsupported tuple was claimed: %+v", claimed)
	}
	explain, err := itStore.SchedulerExplain(ctx, demoWorkerUUID)
	if err != nil {
		t.Fatalf("scheduler explain: %v", err)
	}
	if explain.Eligible != 0 || explain.ModelMismatch != 1 {
		t.Fatalf("unsupported exact tuple must be a model mismatch, got %+v", explain)
	}
}

func TestWorkerReregistrationAtomicallyReplacesExactRowsIdempotently(t *testing.T) {
	reset(t)
	register := func(cap WorkerCapability) {
		t.Helper()
		code, body := req(t, http.MethodPost, "/v1/worker/register", cap, workerTok(), jsonCT())
		if code != http.StatusOK {
			t.Fatalf("register: %d %s", code, body)
		}
	}

	full := demoProductionCapability()
	register(full)
	register(full)
	if got := authorizedCapabilityCount(t, demoWorkerUUID); got != 7 {
		t.Fatalf("repeating full registration changed row count: %d", got)
	}

	subset := demoProductionCapability()
	subset.SupportedJobs = []string{"embed"}
	subset.SupportedModels = []string{"all-minilm-l6-v2"}
	register(subset)
	register(subset)
	if got := authorizedCapabilityCount(t, demoWorkerUUID); got != 1 {
		t.Fatalf("subset re-registration must replace seven rows with one, got %d", got)
	}
	var cellID, matrixSHA string
	if err := itPool.QueryRow(context.Background(),
		`SELECT cell_id, matrix_sha256 FROM worker_authorized_capabilities WHERE worker_id = $1`,
		demoWorkerUUID,
	).Scan(&cellID, &matrixSHA); err != nil {
		t.Fatal(err)
	}
	if cellID != "candle-metal-minilm-embed" || matrixSHA != generatedRuntimeMatrixSHA256 {
		t.Fatalf("wrong subset authority: cell=%q matrix=%q", cellID, matrixSHA)
	}

	register(full)
	if got := authorizedCapabilityCount(t, demoWorkerUUID); got != 7 {
		t.Fatalf("restoring full registration produced %d rows, want 7", got)
	}
}

func TestETAFallbackUsesExactCurrentEligibleWorkers(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// Force the blunt fallback whose former global ActiveWorkerCount divisor let
	// live-but-inert rows understate ETA. Restore the process-wide test switch even
	// if an assertion fails.
	plannerWasEnabled := fanoutPlannerEnabled.Load()
	fanoutPlannerEnabled.Store(false)
	t.Cleanup(func() { fanoutPlannerEnabled.Store(plannerWasEnabled) })

	legacyID, staleID, lowMemoryID := uuid.New(), uuid.New(), uuid.New()
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(),
			`DELETE FROM workers WHERE id = ANY($1::uuid[])`,
			[]uuid.UUID{legacyID, staleID, lowMemoryID})
	})

	// The demo worker becomes a live legacy array-only row. Add a second array-only
	// row and a third row carrying a syntactically valid but stale matrix SHA.
	// All three would have entered the old global live-worker divisor.
	if _, err := itPool.Exec(ctx,
		`DELETE FROM worker_authorized_capabilities WHERE worker_id = $1`, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	for _, workerID := range []uuid.UUID{legacyID, staleID} {
		if _, err := itPool.Exec(ctx,
			`INSERT INTO workers
			   (id, supplier_id, hw_class, engine, memory_gb, bw_gbps, last_seen_at, version,
			    supported_jobs, supported_models, min_payout_usd_hr, thermal_ok, throttled)
			 VALUES ($1,$2,'apple_silicon_max','candle',64,400,now(),'legacy-fixture',
			         ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true,false)`,
			workerID, demoSupplierUUID); err != nil {
			t.Fatal(err)
		}
	}
	staleSHA := strings.Repeat("0", 64)
	if staleSHA == generatedRuntimeMatrixSHA256 {
		t.Fatal("stale SHA fixture unexpectedly equals the current runtime matrix")
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_authorized_capabilities
		   (worker_id, cell_id, runtime_id, job_type, model_ref, model_kind, matrix_sha256)
		 VALUES ($1,'candle-metal-minilm-embed','candle_metal','embed','all-minilm-l6-v2','hf',$2)`,
		staleID, staleSHA); err != nil {
		t.Fatal(err)
	}

	// A fourth worker has a CURRENT exact cell but only 1 GB effective memory. It
	// proves the requested min-memory floor participates in the ETA denominator.
	lowMemoryCap := demoProductionCapability()
	lowMemoryCap.WorkerID = lowMemoryID
	lowMemoryCap.SupportedJobs = []string{"embed"}
	lowMemoryCap.SupportedModels = []string{"all-minilm-l6-v2"}
	if err := itStore.UpsertWorker(ctx, lowMemoryCap); err != nil {
		t.Fatalf("register low-memory control: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET effective_memory_gb = 1 WHERE id = $1`, lowMemoryID); err != nil {
		t.Fatal(err)
	}

	if live, err := itStore.ActiveWorkerCount(ctx); err != nil || live != 4 {
		t.Fatalf("fixture must expose four globally live rows: live=%d err=%v", live, err)
	}
	if eligible, err := itStore.EligibleWorkerCount(ctx, "embed", "all-minilm-l6-v2", 2); err != nil || eligible != 0 {
		t.Fatalf("legacy/stale/under-memory rows must provide zero exact supply: eligible=%d err=%v", eligible, err)
	}
	if eligibleWithoutFloor, err := itStore.EligibleWorkerCount(ctx, "embed", "all-minilm-l6-v2", 0); err != nil || eligibleWithoutFloor != 1 {
		t.Fatalf("low-memory control must be current authority excluded only by the 2GB floor: eligible=%d err=%v", eligibleWithoutFloor, err)
	}

	const taskCount = 12
	p90ms, _, err := itStore.HistoricalP90DurationMs(ctx, "embed", "all-minilm-l6-v2")
	if err != nil {
		t.Fatal(err)
	}
	perTaskSecs := perTaskSecsFromP90(p90ms)
	etaWithoutAuthority, conservative, plannerBacked := itServer.etaBandSecs(
		ctx, "embed", "all-minilm-l6-v2", 2, taskCount,
	)
	if plannerBacked || conservative != 0 {
		t.Fatalf("forced fallback unexpectedly planner-backed: eta=%d conservative=%d backed=%v",
			etaWithoutAuthority, conservative, plannerBacked)
	}
	if want := taskCount * perTaskSecs; etaWithoutAuthority != want {
		t.Fatalf("inert live workers reduced fallback ETA: got=%d want serial=%d", etaWithoutAuthority, want)
	}

	// Re-register the three capable rows through the production store transaction.
	// Only now may they enter the divisor. The under-memory current row remains
	// excluded, so the exact eligible count is three rather than four.
	for _, workerID := range []uuid.UUID{demoWorkerUUID, legacyID, staleID} {
		cap := demoProductionCapability()
		cap.WorkerID = workerID
		cap.SupportedJobs = []string{"embed"}
		cap.SupportedModels = []string{"all-minilm-l6-v2"}
		if err := itStore.UpsertWorker(ctx, cap); err != nil {
			t.Fatalf("re-register exact ETA worker %s: %v", workerID, err)
		}
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE workers SET effective_memory_gb = NULL, throttled = false
		  WHERE id = ANY($1::uuid[])`,
		[]uuid.UUID{demoWorkerUUID, legacyID, staleID}); err != nil {
		t.Fatal(err)
	}
	if eligible, err := itStore.EligibleWorkerCount(ctx, "embed", "all-minilm-l6-v2", 2); err != nil || eligible != 3 {
		t.Fatalf("re-registration must create exactly three eligible workers: eligible=%d err=%v", eligible, err)
	}
	etaWithAuthority, conservative, plannerBacked := itServer.etaBandSecs(
		ctx, "embed", "all-minilm-l6-v2", 2, taskCount,
	)
	if plannerBacked || conservative != 0 {
		t.Fatalf("forced fallback unexpectedly planner-backed after registration: eta=%d conservative=%d backed=%v",
			etaWithAuthority, conservative, plannerBacked)
	}
	wantParallel := ((taskCount + 3 - 1) / 3) * perTaskSecs
	if etaWithAuthority != wantParallel || etaWithAuthority >= etaWithoutAuthority {
		t.Fatalf("authorized re-registration did not control ETA divisor: before=%d after=%d want=%d",
			etaWithoutAuthority, etaWithAuthority, wantParallel)
	}
}
