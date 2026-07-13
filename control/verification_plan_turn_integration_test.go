//go:build integration

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
)

func registerIndependentEmbedWorker(t *testing.T, supplierID uuid.UUID) uuid.UUID {
	t.Helper()
	capability := demoProductionCapability()
	capability.WorkerID = uuid.New()
	capability.SupplierID = supplierID
	capability.AgentVersion = "verification-plan-turn-test"
	if err := itStore.UpsertWorker(context.Background(), capability); err != nil {
		t.Fatalf("register independent embed worker: %v", err)
	}
	return capability.WorkerID
}

func alternateEmbedResultJSON() []byte {
	vector := make([]float64, 384)
	vector[1] = 1
	body, _ := json.Marshal(map[string]any{
		"job_type": "embed", "model": "all-minilm-l6-v2", "dim": 384,
		"count": 1, "vectors": [][]float64{vector},
	})
	return body
}

// TestPersistedChunkPlanOwnsTurnAcrossRecovery is the regression for the
// process-death gap between immutable-plan persistence and its terminal apply.
// A primary persists a no-peer pass while a disagreeing redundancy upload is
// already verifying, then loses its request context. The sibling must not create
// or apply another no-peer plan: it waits for the abandoned owner to settle and
// only then plans against that completed sealed result, exposing the mismatch and
// dispatching an independent tiebreak.
func TestPersistedChunkPlanOwnsTurnAcrossRecovery(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	ensureExtraDemoSuppliers(t, ctx)
	peerWorker := registerIndependentEmbedWorker(t, demoSupplier2UUID)
	tiebreakWorker := registerIndependentEmbedWorker(t, demoSupplier3UUID)

	fixture := newVerificationProcessorFixture(t)
	rootID := fixture.Dispatch.TaskID
	peerID := uuid.New()
	peerKey := fmt.Sprintf("jobs/%s/redundancy/%s/result.json", fixture.Dispatch.JobID, peerID)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO tasks
		 (id,job_id,status,is_honeypot,is_redundancy,retry_count,input_ref,result_key,
		  chunk_index,worker_id,claimed_by,claimed_at,started_at,visible_at,
		  economic_buyer_charge_usd,economic_supplier_payout_usd,
		  execution_worker_id,execution_supplier_id,execution_hw_class,
		  execution_engine,execution_build_hash)
		SELECT $1,job_id,'running',false,true,0,input_ref,$2,COALESCE(chunk_index,0),
		       $3,$3,now(),now(),now(),economic_buyer_charge_usd,economic_supplier_payout_usd,
		       w.id,w.supplier_id,w.hw_class,w.engine,w.build_hash
		  FROM tasks t JOIN workers w ON w.id=$3 WHERE t.id=$4`,
		peerID, peerKey, peerWorker, rootID); err != nil {
		t.Fatalf("insert disagreeing redundancy task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET task_count=2 WHERE id=$1`, fixture.Dispatch.JobID); err != nil {
		t.Fatalf("include redundancy in parent count: %v", err)
	}
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("put primary result: %v", err)
	}
	if err := itStorage.PutObject(ctx, peerKey, alternateEmbedResultJSON(), "application/json"); err != nil {
		t.Fatalf("put disagreeing redundancy result: %v", err)
	}
	if _, err := itStore.CommitTask(ctx, rootID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("commit primary verification work: %v", err)
	}
	peerCommit := fixture.Commit
	peerCommit.TaskID, peerCommit.ResultKey = peerID, peerKey
	if _, err := itStore.CommitTask(ctx, peerID, peerWorker, peerCommit); err != nil {
		t.Fatalf("commit redundancy verification work: %v", err)
	}

	ownerCtx, cancelOwner := context.WithCancel(ctx)
	probe := &decisionFenceProbe{reached: make(chan struct{}), release: make(chan struct{})}
	ownerDone := make(chan error, 1)
	go func() {
		_, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
			WithRecoveryProbe(probe).ProcessAttempt(ownerCtx, rootID, 0)
		ownerDone <- err
	}()
	select {
	case <-probe.reached:
	case <-ctx.Done():
		t.Fatal("primary did not persist its no-peer plan")
	}
	cancelOwner()
	select {
	case err := <-ownerDone:
		if err == nil || (!errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded)) {
			t.Fatalf("abandoned plan owner returned %v, want canceled before apply", err)
		}
	case <-ctx.Done():
		t.Fatal("abandoned plan owner did not release its lease")
	}
	rootWork, err := itStore.VerificationWorkForAttempt(ctx, rootID, 0)
	if err != nil {
		t.Fatal(err)
	}
	rootPlan, err := itStore.VerificationWorkPlan(ctx, rootWork.ID)
	if err != nil || rootPlan.Decision.Outcome != OutcomePass {
		t.Fatalf("abandoned primary plan = %+v err=%v, want persisted no-peer pass", rootPlan, err)
	}
	if rootWork.Status == VerificationWorkTerminal {
		t.Fatal("primary unexpectedly applied before simulated request death")
	}

	blocked, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, peerID, 0)
	if err != nil || !blocked.Pending {
		t.Fatalf("sibling crossed persisted-plan turn: result=%+v err=%v", blocked, err)
	}
	var peerPlans int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM verification_work_plans p
		 JOIN verification_work w ON w.id=p.work_id WHERE w.task_id=$1`, peerID).Scan(&peerPlans); err != nil {
		t.Fatal(err)
	}
	if peerPlans != 0 {
		t.Fatalf("blocked sibling persisted %d stale no-peer plan(s), want zero", peerPlans)
	}

	if _, err := itPool.Exec(ctx, `UPDATE verification_work SET next_attempt_at=now() WHERE task_id IN ($1,$2)`, rootID, peerID); err != nil {
		t.Fatal(err)
	}
	rootResult, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, rootID, 0)
	if err != nil || rootResult.Pending || rootResult.Outcome != OutcomePass {
		t.Fatalf("recover abandoned plan owner: result=%+v err=%v", rootResult, err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE verification_work SET next_attempt_at=now() WHERE task_id=$1`, peerID); err != nil {
		t.Fatal(err)
	}
	peerResult, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, peerID, 0)
	if err != nil || peerResult.Pending || peerResult.Outcome != OutcomePassWithPenalty {
		t.Fatalf("redundancy did not re-plan against recovered peer: result=%+v err=%v", peerResult, err)
	}
	peerWork, err := itStore.VerificationWorkForAttempt(ctx, peerID, 0)
	if err != nil {
		t.Fatal(err)
	}
	peerPlan, err := itStore.VerificationWorkPlan(ctx, peerWork.ID)
	if err != nil {
		t.Fatal(err)
	}
	var sawMismatch, sawTiebreak bool
	for _, effect := range peerPlan.Decision.Effects {
		sawMismatch = sawMismatch || effect.EventKind == "redundancy_mismatch"
		sawTiebreak = sawTiebreak || (effect.Kind == VerificationEffectInsertTiebreak && effect.PeerWorkerID == tiebreakWorker)
	}
	if !sawMismatch || !sawTiebreak {
		t.Fatalf("recovered sibling plan did not expose mismatch + independent tiebreak: %+v", peerPlan.Decision)
	}
	var tiebreaks int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM tasks
		 WHERE job_id=$1 AND is_redundancy=true AND hedged_from=$2 AND claimed_by=$3`,
		fixture.Dispatch.JobID, peerID, tiebreakWorker).Scan(&tiebreaks); err != nil {
		t.Fatal(err)
	}
	if tiebreaks != 1 {
		t.Fatalf("durable independent tiebreak count=%d, want 1", tiebreaks)
	}
}
