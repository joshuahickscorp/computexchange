//go:build integration

package main

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
)

func seedPlanTurnCrashSibling(t *testing.T, peerWorker uuid.UUID) (uuid.UUID, TaskCommit) {
	t.Helper()
	ctx := context.Background()
	peerID := uuid.New()
	peerKey := fmt.Sprintf("jobs/%s/redundancy/%s/result.json", verificationCrashJobID, peerID)
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
		peerID, peerKey, peerWorker, verificationCrashTaskID); err != nil {
		t.Fatalf("insert plan-turn crash sibling: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET task_count=2 WHERE id=$1`, verificationCrashJobID); err != nil {
		t.Fatal(err)
	}
	if err := itStorage.PutObject(ctx, peerKey, alternateEmbedResultJSON(), "application/json"); err != nil {
		t.Fatal(err)
	}
	return peerID, TaskCommit{TaskID: peerID, ResultKey: peerKey, DurationMS: 654, TokensUsed: 23}
}

// TestPersistedChunkPlanSIGKILLBlocksSiblingUntilOwnerRecovery proves the
// counterexample with a real process death. The killed primary has already
// persisted a no-peer pass. A disagreeing redundancy attempt may be leased, but
// it cannot persist or apply its own stale plan until recovery terminalizes the
// owner; it then compares the sealed primary and dispatches an independent third
// opinion.
func TestPersistedChunkPlanSIGKILLBlocksSiblingUntilOwnerRecovery(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	ensureExtraDemoSuppliers(t, ctx)
	peerWorker := registerIndependentEmbedWorker(t, demoSupplier2UUID)
	tiebreakWorker := registerIndependentEmbedWorker(t, demoSupplier3UUID)
	fixture := seedVerificationCrashFixture(t, false)
	peerID, peerCommit := seedPlanTurnCrashSibling(t, peerWorker)
	if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("commit primary crash work: %v", err)
	}
	if _, err := itStore.CommitTask(ctx, peerID, peerWorker, peerCommit); err != nil {
		t.Fatalf("commit sibling crash work: %v", err)
	}

	crash := verificationCrashCase{
		name: "persisted-plan-owner", phase: "process",
		boundary: BoundaryVerifyAfterDecision, occurrence: 1,
	}
	killVerificationAtBoundary(t, crash)

	blocked, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, peerID, 0)
	if err != nil || !blocked.Pending {
		t.Fatalf("sibling crossed killed persisted-plan owner: result=%+v err=%v", blocked, err)
	}
	var peerPlans int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM verification_work_plans p
		 JOIN verification_work w ON w.id=p.work_id WHERE w.task_id=$1`, peerID).Scan(&peerPlans); err != nil {
		t.Fatal(err)
	}
	if peerPlans != 0 {
		t.Fatalf("blocked sibling persisted %d stale plan(s), want zero", peerPlans)
	}

	ownerState := recoverVerificationAfterCrash(t, fixture, "process")
	if ownerState.PlanOutcome != OutcomePass || ownerState.WorkStatus != VerificationWorkTerminal {
		t.Fatalf("killed plan owner did not recover exactly once: %+v", ownerState)
	}
	if _, err := itPool.Exec(ctx, `UPDATE verification_work SET next_attempt_at=now() WHERE task_id=$1`, peerID); err != nil {
		t.Fatal(err)
	}
	peerResult, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, peerID, 0)
	if err != nil || peerResult.Pending || peerResult.Outcome != OutcomePassWithPenalty {
		t.Fatalf("post-recovery sibling did not compare: result=%+v err=%v", peerResult, err)
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
		t.Fatalf("post-SIGKILL plan omitted mismatch/tiebreak: %+v", peerPlan.Decision)
	}
	var tiebreaks int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM tasks
		 WHERE job_id=$1 AND is_redundancy=true AND hedged_from=$2 AND claimed_by=$3`,
		verificationCrashJobID, peerID, tiebreakWorker).Scan(&tiebreaks); err != nil {
		t.Fatal(err)
	}
	if tiebreaks != 1 {
		t.Fatalf("post-SIGKILL independent tiebreak count=%d, want 1", tiebreaks)
	}
	t.Logf("SIGKILL boundary=%s persisted owner recovered before sibling plan; independent_tiebreak=%s",
		BoundaryVerifyAfterDecision, tiebreakWorker)
}
