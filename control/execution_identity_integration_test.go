//go:build integration

package main

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
)

// A worker registration is live routing state: agents legitimately upgrade a
// build or even change execution lanes between jobs. That later state must never
// rewrite the identity/class of work that was already claimed. This test mutates
// every mutable axis after dispatch and proves commit, verification, settlement,
// and buyer receipts all remain bound to the claim-time tuple.
func TestClaimExecutionIdentitySurvivesWorkerMutation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)

	claimCapability := demoProductionCapability()
	claimCapability.BuildHash = "claim-build-v1"
	if err := itStore.UpsertWorker(ctx, claimCapability); err != nil {
		t.Fatalf("register claim build: %v", err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(), `
			UPDATE workers
			   SET supplier_id=$2,hw_class='apple_silicon_max',engine='candle',build_hash=''
			 WHERE id=$1`, demoWorkerUUID, demoSupplierUUID)
		_ = itStore.UpsertWorker(context.Background(), demoProductionCapability())
	})

	jobID, taskCount := submitEmbedJob(t, 1, 0, 0, 0)
	if taskCount != 1 {
		t.Fatalf("task count=%d, want 1", taskCount)
	}
	code, body := req(t, http.MethodGet, "/v1/worker/poll", nil, workerTok())
	if code != http.StatusOK {
		t.Fatalf("poll: status=%d body=%s", code, body)
	}
	var dispatch TaskDispatch
	if err := json.Unmarshal(body, &dispatch); err != nil {
		t.Fatalf("decode dispatch: %v", err)
	}

	var frozenWorker, frozenSupplier string
	var frozenHW, frozenEngine, frozenBuild string
	if err := itPool.QueryRow(ctx, `
		SELECT execution_worker_id::text,execution_supplier_id::text,
		       execution_hw_class,execution_engine,execution_build_hash
		  FROM tasks WHERE id=$1`, dispatch.TaskID).
		Scan(&frozenWorker, &frozenSupplier, &frozenHW, &frozenEngine, &frozenBuild); err != nil {
		t.Fatalf("read claim identity: %v", err)
	}
	if frozenWorker != demoWorkerUUID.String() || frozenSupplier != demoSupplierUUID.String() ||
		frozenHW != "apple_silicon_max" || frozenEngine != "candle" || frozenBuild != "claim-build-v1" {
		t.Fatalf("unexpected frozen claim tuple worker=%s supplier=%s class=%s/%s/%s",
			frozenWorker, frozenSupplier, frozenHW, frozenEngine, frozenBuild)
	}

	// Exercise the public registration mutation first, then simulate an
	// administrative supplier/lane reassignment. The task has already crossed the
	// claim boundary, so neither may affect its provenance.
	mutatedCapability := claimCapability
	mutatedCapability.BuildHash = "post-claim-build"
	if err := itStore.UpsertWorker(ctx, mutatedCapability); err != nil {
		t.Fatalf("mutate registered build: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE workers
		   SET supplier_id=$2,hw_class='cpu',engine='vllm',build_hash='post-claim-admin-build'
		 WHERE id=$1`, demoWorkerUUID, demoSupplier2UUID); err != nil {
		t.Fatalf("mutate live worker profile: %v", err)
	}

	if err := itStorage.PutObject(ctx, dispatch.ResultKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatalf("put result: %v", err)
	}
	commit := TaskCommit{TaskID: dispatch.TaskID, ResultKey: dispatch.ResultKey, DurationMS: 12, TokensUsed: 8}
	if code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+dispatch.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != http.StatusNoContent {
		t.Fatalf("commit after profile mutation: status=%d body=%s", code, body)
	}

	var workWorker, workSupplier, workHW, workEngine, workBuild string
	if err := itPool.QueryRow(ctx, `
		SELECT worker_id::text,supplier_id::text,input_snapshot->>'hw_class',
		       input_snapshot->>'engine',input_snapshot->>'build_hash'
		  FROM verification_work WHERE task_id=$1 AND attempt=0`, dispatch.TaskID).
		Scan(&workWorker, &workSupplier, &workHW, &workEngine, &workBuild); err != nil {
		t.Fatalf("read verification work: %v", err)
	}
	if workWorker != frozenWorker || workSupplier != frozenSupplier || workHW != frozenHW ||
		workEngine != frozenEngine || workBuild != frozenBuild {
		t.Fatalf("verification work drifted: got %s/%s/%s/%s/%s want %s/%s/%s/%s/%s",
			workWorker, workSupplier, workHW, workEngine, workBuild,
			frozenWorker, frozenSupplier, frozenHW, frozenEngine, frozenBuild)
	}

	var originalCredits, mutatedCredits int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE supplier_id=$2),
		       count(*) FILTER (WHERE supplier_id=$3)
		  FROM ledger_entries WHERE task_id=$1 AND kind='supplier_credit'`,
		dispatch.TaskID, demoSupplierUUID, demoSupplier2UUID).
		Scan(&originalCredits, &mutatedCredits); err != nil {
		t.Fatalf("read settlement attribution: %v", err)
	}
	if originalCredits != 1 || mutatedCredits != 0 {
		t.Fatalf("supplier credit attribution original=%d mutated=%d, want 1/0", originalCredits, mutatedCredits)
	}

	classes, err := itStore.JobVerificationClasses(ctx, jobID)
	if err != nil {
		t.Fatalf("verification classes: %v", err)
	}
	if len(classes) != 1 || classes[0] != "candle|claim-build-v1" {
		t.Fatalf("receipt classes=%v, want claim-time class", classes)
	}
	receipts, err := itStore.JobTaskReceipts(ctx, jobID)
	if err != nil {
		t.Fatalf("task receipts: %v", err)
	}
	if len(receipts) != 1 || receipts[0].WorkerClass != "candle|claim-build-v1" {
		t.Fatalf("task receipts=%+v, want claim-time class", receipts)
	}

	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET execution_build_hash='forged-after-complete' WHERE id=$1`, dispatch.TaskID); err == nil {
		t.Fatal("database accepted post-claim execution identity mutation")
	}
}
