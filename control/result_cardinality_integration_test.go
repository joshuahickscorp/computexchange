//go:build integration

package main

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/google/uuid"
)

func TestExactResultCardinalityRejectsShortArtifactWithoutSettlement(t *testing.T) {
	reset(t)
	ctx := context.Background()
	_, taskCount := submitEmbedJob(t, 2, 0, 0, 0)
	if taskCount != 1 {
		t.Fatalf("two-row exact fixture split into %d tasks", taskCount)
	}
	code, raw := req(t, http.MethodGet, "/v1/worker/poll", nil, workerTok())
	if code != http.StatusOK {
		t.Fatalf("poll exact-cardinality task: %d %s", code, raw)
	}
	var dispatch TaskDispatch
	if err := json.Unmarshal(raw, &dispatch); err != nil {
		t.Fatal(err)
	}
	var persisted int64
	if err := itPool.QueryRow(ctx,
		`SELECT expected_output_records FROM tasks WHERE id=$1`, dispatch.TaskID,
	).Scan(&persisted); err != nil || persisted != 2 {
		t.Fatalf("persisted exact task count=%d err=%v, want 2", persisted, err)
	}
	short := embedResultJSON(1)
	if err := itStorage.PutObject(ctx, dispatch.ResultKey, short, "application/json"); err != nil {
		t.Fatal(err)
	}
	commit := TaskCommit{TaskID: dispatch.TaskID, ResultKey: dispatch.ResultKey, DurationMS: 10}
	code, raw = req(t, http.MethodPost,
		"/v1/worker/task/"+dispatch.TaskID.String()+"/commit", commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("short exact-cardinality commit: %d %s", code, raw)
	}
	state := readVerificationProcessorState(t, dispatch.TaskID)
	if state.TaskStatus != "retrying" || state.WorkStatus != VerificationWorkTerminal ||
		state.TerminalOutcome != string(OutcomeFail) || state.VerdictRows != 1 ||
		state.DurationRows != 0 || state.LedgerRows != 0 {
		t.Fatalf("short result was payable or non-terminal: %+v", state)
	}
	work, err := itStore.VerificationWorkForAttempt(ctx, dispatch.TaskID, 0)
	if err != nil {
		t.Fatal(err)
	}
	info, _, err := commitInfoFromVerificationWork(work)
	if err != nil {
		t.Fatal(err)
	}
	if info.ExpectedOutputRecords != 2 || info.resultMaxBytes != verificationArtifactMaxBytesForRecords("embed", 2, 1000, 0) {
		t.Fatalf("attempt did not freeze exact count/cap: %+v", info)
	}
	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil {
		t.Fatal(err)
	}
	if plan.Decision.Failure == nil || plan.Decision.Failure.Kind != "artifact_invalid" ||
		plan.Decision.Failure.Code != resultValidationCount || len(plan.Settlement) != 0 {
		t.Fatalf("short-result durable plan is not typed/nonpayable: %+v", plan)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET expected_output_records=1 WHERE id=$1`, dispatch.TaskID,
	); err == nil {
		t.Fatal("immutable exact task count accepted an update")
	}
}

func TestFinalPartialChunkPersistsExactCountAndAcceptsOneRow(t *testing.T) {
	reset(t)
	ctx := context.Background()
	body := map[string]any{
		"job_type":    map[string]any{"type": "embed"},
		"model":       map[string]any{"kind": "hf", "ref": "all-minilm-l6-v2"},
		"params":      map[string]any{"split_size": 2},
		"constraints": map[string]any{"min_memory_gb": 2},
		"verification": map[string]any{
			"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true,
		},
		"tier":  "batch",
		"input": "{\"id\":\"a\",\"text\":\"a\"}\n{\"id\":\"b\",\"text\":\"b\"}\n{\"id\":\"c\",\"text\":\"c\"}\n",
	}
	code, raw := req(t, http.MethodPost, "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("submit partial-chunk fixture: %d %s", code, raw)
	}
	var submitted JobSubmitResponse
	if err := json.Unmarshal(raw, &submitted); err != nil {
		t.Fatal(err)
	}
	rows, err := itPool.Query(ctx, `
		SELECT id,result_key,expected_output_records
		  FROM tasks WHERE job_id=$1 AND is_honeypot=false AND is_redundancy=false
		 ORDER BY chunk_index`, submitted.JobID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var taskIDs []uuid.UUID
	var resultKeys []string
	var counts []int64
	for rows.Next() {
		var id uuid.UUID
		var key string
		var count int64
		if err := rows.Scan(&id, &key, &count); err != nil {
			t.Fatal(err)
		}
		taskIDs, resultKeys, counts = append(taskIDs, id), append(resultKeys, key), append(counts, count)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	rows.Close()
	if len(taskIDs) != 2 || counts[0] != 2 || counts[1] != 1 {
		t.Fatalf("primary exact chunk counts=%v, want [2 1]", counts)
	}
	finalTask, finalKey := taskIDs[1], resultKeys[1]
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET status='running',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, finalTask, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, submitted.JobID); err != nil {
		t.Fatal(err)
	}
	if err := itStorage.PutObject(ctx, finalKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatal(err)
	}
	info, err := itStore.CommitTask(ctx, finalTask, demoWorkerUUID,
		TaskCommit{TaskID: finalTask, ResultKey: finalKey, DurationMS: 10})
	if err != nil {
		t.Fatal(err)
	}
	if info.ExpectedOutputRecords != 1 || info.SplitSize != 2 ||
		info.resultMaxBytes != verificationArtifactMaxBytesForRecords("embed", 1, 2, 0) {
		t.Fatalf("final task attempt used split ceiling instead of exact count: %+v", info)
	}
	result, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, finalTask, 0)
	if err != nil || result.Pending || result.Outcome != OutcomePass {
		t.Fatalf("valid one-row final chunk = %+v err=%v", result, err)
	}
	state := readVerificationProcessorState(t, finalTask)
	if state.TaskStatus != "complete" || state.DurationRows != 1 || state.LedgerRows != 3 {
		t.Fatalf("valid final partial chunk did not settle once: %+v", state)
	}
}

func TestInitialVerificationClonesPersistHonestExactCardinality(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, _ := submitEmbedJob(t, 2, 1, 1, 0)
	rows, err := itPool.Query(ctx, `
		SELECT is_honeypot,is_redundancy,expected_output_records
		  FROM tasks WHERE job_id=$1 ORDER BY is_honeypot,is_redundancy`, jobID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var primary, redundancy, honeypot int64
	for rows.Next() {
		var isHoneypot, isRedundancy bool
		var expected int64
		if err := rows.Scan(&isHoneypot, &isRedundancy, &expected); err != nil {
			t.Fatal(err)
		}
		switch {
		case isHoneypot:
			honeypot = expected
		case isRedundancy:
			redundancy = expected
		default:
			primary = expected
		}
	}
	if primary != 2 || redundancy != primary || honeypot <= 0 {
		t.Fatalf("clone exact counts primary=%d redundancy=%d honeypot=%d", primary, redundancy, honeypot)
	}
}

func TestLegacyUnknownCardinalityStaysNullAndCannotBeRelabelledExact(t *testing.T) {
	reset(t)
	ctx := context.Background()
	_, taskIDs, _ := createFrozenEconomicTestJob(t, 1, 0, 0)
	var expected *int64
	if err := itPool.QueryRow(ctx,
		`SELECT expected_output_records FROM tasks WHERE id=$1`, taskIDs[0],
	).Scan(&expected); err != nil {
		t.Fatal(err)
	}
	if expected != nil {
		t.Fatalf("legacy/direct task was falsely labelled exact: %d", *expected)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET expected_output_records=1 WHERE id=$1`, taskIDs[0],
	); err == nil {
		t.Fatal("legacy NULL cardinality was relabelled exact after creation")
	}
}

func createExactDynamicCloneFixture(t *testing.T, expected int) (uuid.UUID, uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD: 1, InitialTaskCount: 1, ExtraTaskReserve: 1,
		SupplierShare: supplierShareRate,
	}, testEconomicSchedule())
	job := &jobRow{
		ID: jobID, BuyerID: demoBuyerUUID, JobType: "embed", ModelRef: "all-minilm-l6-v2",
		InputRef: "jobs/cardinality/input.jsonl", OutputRef: "jobs/cardinality/output.jsonl",
		Tier: "batch", VerificationPolicy: []byte(`{}`), TaskCount: 1, SplitSize: 1000,
		EstimatedUSD: plan.InitialBuyerChargeUSD, EconomicPlan: plan,
	}
	if err := itStore.CreateJobWithTasks(ctx, job, []taskRow{{
		ID: taskID, JobID: jobID, InputRef: "jobs/cardinality/chunk.jsonl",
		ResultKey: "jobs/cardinality/result.json", ExpectedOutputRecords: int64(expected),
	}}); err != nil {
		t.Fatal(err)
	}
	return jobID, taskID
}

func TestDynamicHedgeAndTiebreakClonesInheritAnchorCardinality(t *testing.T) {
	ctx := context.Background()
	for _, kind := range []string{"hedge", "tiebreak"} {
		t.Run(kind, func(t *testing.T) {
			reset(t)
			jobID, primaryID := createExactDynamicCloneFixture(t, 3)
			if kind == "hedge" {
				if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, jobID); err != nil {
					t.Fatal(err)
				}
				if _, err := itPool.Exec(ctx,
					`UPDATE tasks SET claimed_by=$2,claimed_at=now() WHERE id=$1`, primaryID, demoWorkerUUID); err != nil {
					t.Fatal(err)
				}
				if err := itStore.StartTask(ctx, primaryID, demoWorkerUUID); err != nil {
					t.Fatal(err)
				}
				clone, err := itStore.InsertHedgeTask(ctx, jobID, primaryID, demoWorkerUUID,
					"jobs/cardinality/chunk.jsonl", 0)
				if err != nil {
					t.Fatal(err)
				}
				assertExpectedOutputRecords(t, clone, 3)
				return
			}
			if _, err := itPool.Exec(ctx,
				`UPDATE jobs SET status='verifying',tasks_done=1 WHERE id=$1`, jobID); err != nil {
				t.Fatal(err)
			}
			if _, err := itPool.Exec(ctx,
				`UPDATE tasks SET claimed_by=$2,claimed_at=now() WHERE id=$1`, primaryID, demoWorkerUUID); err != nil {
				t.Fatal(err)
			}
			if err := itStore.StartTask(ctx, primaryID, demoWorkerUUID); err != nil {
				t.Fatal(err)
			}
			if _, err := itPool.Exec(ctx,
				`UPDATE tasks SET status='complete',completed_at=now(),result_ref=result_key WHERE id=$1`, primaryID); err != nil {
				t.Fatal(err)
			}
			clone, err := itStore.InsertTiebreakTask(ctx, jobID, primaryID, demoWorkerUUID,
				"jobs/cardinality/chunk.jsonl", 0)
			if err != nil {
				t.Fatal(err)
			}
			assertExpectedOutputRecords(t, clone, 3)
		})
	}
}

func assertExpectedOutputRecords(t *testing.T, taskID uuid.UUID, want int64) {
	t.Helper()
	var got int64
	if err := itPool.QueryRow(context.Background(),
		`SELECT expected_output_records FROM tasks WHERE id=$1`, taskID,
	).Scan(&got); err != nil || got != want {
		t.Fatalf("task %s expected output records=%d err=%v, want %d", taskID, got, err, want)
	}
}
