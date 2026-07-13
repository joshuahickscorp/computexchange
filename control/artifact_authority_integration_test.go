//go:build integration

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

type seededArtifactAuthority struct {
	TaskID   uuid.UUID
	WorkID   uuid.UUID
	Artifact VerificationArtifact
}

// seedTerminalArtifactAuthority is a narrow fixture for buyer-deliverable
// selection tests. It mirrors the durable end state: the task projection and a
// terminal verification_work row point at the exact server-sealed tuple. The
// task is still `verifying` when work is inserted so the production binding
// trigger validates the same attempt/claim state as a real upload, then is
// projected complete before a chunk resolution may reference it.
func seedTerminalArtifactAuthority(
	t *testing.T,
	jobID, taskID, workerID, supplierID uuid.UUID,
	isRedundancy bool,
	hedgedFrom *uuid.UUID,
	body []byte,
	completedOffsetSeconds int,
) seededArtifactAuthority {
	t.Helper()
	ctx := context.Background()
	stagingKey := fmt.Sprintf("jobs/%s/authority/%s/staging.result", jobID, taskID)
	if err := itStorage.PutObject(ctx, stagingKey, body, "application/json"); err != nil {
		t.Fatalf("put authority staging object: %v", err)
	}
	sealed, err := itStorage.SealVerificationArtifact(ctx, taskID, 0, stagingKey)
	if err != nil {
		t.Fatalf("seal authority object: %v", err)
	}
	artifact := VerificationArtifact{Key: sealed.Key, SHA256: sealed.SHA256, Bytes: sealed.Bytes}

	tag, err := itPool.Exec(ctx, `
		INSERT INTO tasks
		 (id,job_id,status,is_honeypot,is_redundancy,retry_count,input_ref,result_key,
		  result_ref,chunk_index,hedged_from,worker_id,claimed_by,claimed_at,
		  reported_duration_ms,reported_tokens_used,
		  execution_worker_id,execution_supplier_id,execution_hw_class,
		  execution_engine,execution_build_hash)
		SELECT $1,$2,'verifying',false,$3,0,$4,$5,$5,0,$6,$7,$7,now(),1,1,
		       $7,$8,w.hw_class,w.engine,w.build_hash
		  FROM workers w WHERE w.id=$7
		ON CONFLICT (id) DO UPDATE SET
		 status='running',
		 is_honeypot=EXCLUDED.is_honeypot,
		 is_redundancy=EXCLUDED.is_redundancy,
		 retry_count=EXCLUDED.retry_count,
		 input_ref=EXCLUDED.input_ref,
		 result_key=EXCLUDED.result_key,
		 result_ref=EXCLUDED.result_ref,
		 chunk_index=EXCLUDED.chunk_index,
		 hedged_from=EXCLUDED.hedged_from,
		 worker_id=EXCLUDED.worker_id,
		 claimed_by=EXCLUDED.claimed_by,
		 claimed_at=EXCLUDED.claimed_at,
		 reported_duration_ms=EXCLUDED.reported_duration_ms,
		 reported_tokens_used=EXCLUDED.reported_tokens_used,
		 execution_worker_id=EXCLUDED.execution_worker_id,
		 execution_supplier_id=EXCLUDED.execution_supplier_id,
		 execution_hw_class=EXCLUDED.execution_hw_class,
		 execution_engine=EXCLUDED.execution_engine,
		 execution_build_hash=EXCLUDED.execution_build_hash
		WHERE tasks.job_id=EXCLUDED.job_id AND tasks.status IN ('queued','retrying')`,
		taskID, jobID, isRedundancy, "jobs/authority/chunk-0.input", stagingKey,
		hedgedFrom, workerID, supplierID)
	if err != nil {
		t.Fatalf("insert authority task: %v", err)
	}
	if tag.RowsAffected() != 1 {
		t.Fatalf("insert/update authority task affected %d rows", tag.RowsAffected())
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='verifying' WHERE id=$1 AND status='running'`, taskID); err != nil {
		t.Fatalf("advance existing authority task to verifying: %v", err)
	}
	var hwClass, engine, build, jobType, modelRef string
	var minMemory float32
	var splitSize int
	if err := itPool.QueryRow(ctx, `
		SELECT COALESCE(w.hw_class,''),COALESCE(w.engine,''),COALESCE(w.build_hash,''),
		       j.job_type,COALESCE(j.model_ref,''),COALESCE(j.min_memory_gb,0),COALESCE(j.split_size,0)
		  FROM workers w CROSS JOIN jobs j WHERE w.id=$1 AND j.id=$2`, workerID, jobID).
		Scan(&hwClass, &engine, &build, &jobType, &modelRef, &minMemory, &splitSize); err != nil {
		t.Fatalf("read authority attempt shape: %v", err)
	}
	info := &CommitTaskInfo{
		TaskID: taskID, JobID: jobID, WorkerID: workerID, SupplierID: supplierID,
		IsRedundancy: isRedundancy, HWClass: hwClass, engine: engine, buildHash: build,
		jobType: jobType, InputRef: "jobs/authority/chunk-0.input", ModelRef: modelRef,
		MinMemoryGB: minMemory, ChunkIndex: 0, SplitSize: splitSize,
		ResultKey: stagingKey, DurationMS: 1, TokensUsed: 1,
	}
	snapshot, err := verificationWorkSnapshotFromCommit(info, TaskCommit{
		TaskID: taskID, ResultKey: stagingKey, DurationMS: 1, TokensUsed: 1,
	})
	if err != nil {
		t.Fatalf("build authority work snapshot: %v", err)
	}
	work, created, err := itStore.CreateVerificationWork(ctx, snapshot)
	if err != nil || !created {
		t.Fatalf("create authority work: created=%v err=%v", created, err)
	}
	leased, err := itStore.ClaimVerificationWorkForAttempt(ctx, taskID, 0, "authority-fixture", time.Minute)
	if err != nil {
		t.Fatalf("claim authority work: %v", err)
	}
	if _, err := itStore.PinVerificationSampling(ctx, leased.Lease, 0, false); err != nil {
		t.Fatalf("pin authority sampling: %v", err)
	}
	if _, err := itStore.PinVerificationArtifact(ctx, leased.Lease, artifact); err != nil {
		t.Fatalf("pin authority artifact: %v", err)
	}
	if _, err := itStore.MarkVerificationWorkTerminal(ctx, leased.Lease, OutcomePass, strings.Repeat("d", 64)); err != nil {
		t.Fatalf("terminalize authority work: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='complete',verification_outcome='pass',verified_at=now(),
		       completed_at=now()+make_interval(secs=>$2)
		 WHERE id=$1`, taskID, completedOffsetSeconds); err != nil {
		t.Fatalf("complete authority task: %v", err)
	}
	return seededArtifactAuthority{TaskID: taskID, WorkID: work.ID, Artifact: artifact}
}

func insertChunkArtifactResolution(t *testing.T, jobID uuid.UUID, winner seededArtifactAuthority, basis string) {
	t.Helper()
	if basis == "provisional" {
		if _, err := itPool.Exec(context.Background(), `
			INSERT INTO ledger_entries
			 (kind,buyer_id,task_id,amount_usd,payout_status)
			VALUES ('buyer_charge',$1,$2,-0.01,'released')`, demoBuyerUUID, winner.TaskID); err != nil {
			t.Fatalf("insert provisional economic-winner fact: %v", err)
		}
	}
	if _, err := itPool.Exec(context.Background(), `
		INSERT INTO chunk_artifact_resolutions
		 (effect_id,job_id,chunk_index,winner_task_id,verification_work_id,
		  artifact_key,artifact_sha256,artifact_bytes,basis)
		VALUES ($1,$2,0,$3,$4,$5,$6,$7,$8)`,
		uuid.New(), jobID, winner.TaskID, winner.WorkID,
		winner.Artifact.Key, winner.Artifact.SHA256, winner.Artifact.Bytes, basis); err != nil {
		t.Fatalf("insert %s chunk artifact resolution: %v", basis, err)
	}
}

func TestBuyerMergeUsesMajoritySealedArtifactAndFailsClosedOnMutation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)

	workerB, workerTiebreak := uuid.New(), uuid.New()
	for _, w := range []struct {
		id       uuid.UUID
		supplier uuid.UUID
	}{{workerB, demoSupplier2UUID}, {workerTiebreak, demoSupplier3UUID}} {
		if _, err := itPool.Exec(ctx, `
			INSERT INTO workers
			 (id,supplier_id,hw_class,memory_gb,last_seen_at,version,engine,build_hash)
			VALUES ($1,$2,'apple_silicon_max',64,now(),'authority-test','candle','authority-build')`,
			w.id, w.supplier); err != nil {
			t.Fatalf("insert authority worker: %v", err)
		}
	}

	jobID := uuid.New()
	outputKey := fmt.Sprintf("jobs/%s/final.jsonl", jobID)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO jobs
		 (id,buyer_id,status,job_type,model_ref,input_ref,output_ref,tier,task_count,tasks_done)
		VALUES ($1,$2,'running','batch_infer','llama-3.2-1b-instruct-q4',$3,$4,'batch',3,3)`,
		jobID, demoBuyerUUID, "jobs/authority/input.jsonl", outputKey); err != nil {
		t.Fatal(err)
	}

	primaryID, redundancyID, tiebreakID := uuid.New(), uuid.New(), uuid.New()
	primaryBody := []byte(`{"completions":[{"text":"A"}]}`)
	majorityBody := []byte(`{"completions":[{"text":"B"}]}`)
	primary := seedTerminalArtifactAuthority(t, jobID, primaryID, demoWorkerUUID, demoSupplierUUID,
		false, nil, primaryBody, 0)
	redundancy := seedTerminalArtifactAuthority(t, jobID, redundancyID, workerB, demoSupplier2UUID,
		true, nil, majorityBody, 1)
	tiebreak := seedTerminalArtifactAuthority(t, jobID, tiebreakID, workerTiebreak, demoSupplier3UUID,
		true, &primaryID, majorityBody, 2)

	insertChunkArtifactResolution(t, jobID, primary, "provisional")
	insertChunkArtifactResolution(t, jobID, redundancy, "majority")
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='complete' WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}

	inputs, err := itStore.JobMergeInputs(ctx, jobID)
	if err != nil {
		t.Fatalf("load authority-aware merge inputs: %v", err)
	}
	if len(inputs.Results) != 1 || inputs.Results[0].Artifact == nil ||
		*inputs.Results[0].Artifact != redundancy.Artifact ||
		inputs.Results[0].ResultRef != redundancy.Artifact.Key {
		t.Fatalf("merge did not select B majority authority: %+v", inputs.Results)
	}
	keys, err := itStore.JobResultKeys(ctx, jobID)
	if err != nil {
		t.Fatalf("load buyer result keys: %v", err)
	}
	if len(keys) != 1 || keys[0] != redundancy.Artifact.Key {
		t.Fatalf("buyer result keys = %v, want majority key %q", keys, redundancy.Artifact.Key)
	}

	if _, err := mergeJobResults(ctx, itStore, itStorage, jobID); err != nil {
		t.Fatalf("merge majority artifact: %v", err)
	}
	merged, err := itStorage.GetObject(ctx, outputKey)
	if err != nil {
		t.Fatalf("read merged buyer artifact: %v", err)
	}
	if !bytes.Contains(merged, []byte(`"B"`)) || bytes.Contains(merged, []byte(`"A"`)) {
		t.Fatalf("buyer merge did not contain B majority only: %s", merged)
	}

	// The tiebreak's independently sealed B bytes prove the actual vote shape was
	// A/B/B, even though the deterministic majority resolution selected the
	// earlier B-side redundancy task as the buyer artifact.
	tiebreakBytes, err := itStorage.ReadSealedVerificationArtifact(ctx, tiebreak.Artifact)
	if err != nil || !bytes.Equal(tiebreakBytes, majorityBody) {
		t.Fatalf("tiebreak B authority = %q, err=%v", tiebreakBytes, err)
	}

	// A later worker re-registration/ownership edit is a mutable projection. Vote
	// attribution must stay on the supplier + engine/build frozen into the accepted
	// attempt, not silently reinterpret historical work using today's worker row.
	if _, err := itPool.Exec(ctx, `
		UPDATE workers SET supplier_id=$2,engine='mutated-engine',build_hash='mutated-build'
		 WHERE id=$1`, workerB, demoSupplierUUID); err != nil {
		t.Fatalf("mutate current worker projection: %v", err)
	}
	chunk, err := itStore.ChunkResults(ctx, jobID, 0)
	if err != nil {
		t.Fatalf("read frozen vote attribution: %v", err)
	}
	foundFrozen := false
	for _, result := range chunk {
		if result.TaskID == redundancy.TaskID {
			foundFrozen = result.SupplierID == demoSupplier2UUID &&
				result.Engine == "candle" && result.BuildHash == "authority-build"
		}
	}
	if !foundFrozen {
		t.Fatalf("terminal vote attribution followed mutable worker row: %+v", chunk)
	}
	peerArtifact, peerSupplier, peerEngine, peerBuild, err := itStore.PeerSealedResult(ctx, primary.TaskID)
	if err != nil || peerArtifact != redundancy.Artifact || peerSupplier != demoSupplier2UUID ||
		peerEngine != "candle" || peerBuild != "authority-build" {
		t.Fatalf("peer authority drifted with worker row: artifact=%+v supplier=%s class=%s/%s err=%v",
			peerArtifact, peerSupplier, peerEngine, peerBuild, err)
	}

	// Sealed keys are authority, not an assumption that the object store is
	// immutable. Overwriting one must make both the merge and N-way gather fail
	// closed on the pinned byte count/hash, never accept the changed bytes.
	if err := itStorage.PutObject(ctx, redundancy.Artifact.Key, []byte(`{"completions":[{"text":"MUTATED"}]}`), "application/json"); err != nil {
		t.Fatalf("mutate sealed object for adversarial proof: %v", err)
	}
	if _, err := mergeJobResults(ctx, itStore, itStorage, jobID); !errors.Is(err, ErrVerificationArtifactChanged) {
		t.Fatalf("merge accepted mutated sealed authority: %v", err)
	}
	verifier := NewVerifier(itStore).WithStorage(itStorage)
	_, err = verifier.gatherChunkResults(ctx, &CommitTaskInfo{
		TaskID: primary.TaskID, JobID: jobID, WorkerID: demoWorkerUUID,
		SupplierID: demoSupplierUUID, ChunkIndex: 0, jobType: "batch_infer",
	}, primaryBody)
	if !errors.Is(err, ErrVerificationArtifactChanged) {
		t.Fatalf("N-way gather accepted mutated sealed authority: %v", err)
	}
}

func TestBothVerifyingHedgeSiblingsConvergeOnOneProvisionalWinner(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)

	hedgeID := uuid.New()
	hedgeKey := fmt.Sprintf("jobs/%s/hedge/%s/result.json", fixture.Dispatch.JobID, hedgeID)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO tasks
		 (id,job_id,status,is_honeypot,is_redundancy,retry_count,input_ref,result_key,
		  chunk_index,hedged_from,worker_id,claimed_by,claimed_at,started_at,visible_at,
		  economic_buyer_charge_usd,economic_supplier_payout_usd,
		  execution_worker_id,execution_supplier_id,execution_hw_class,
		  execution_engine,execution_build_hash)
		SELECT $1,job_id,'running',false,false,0,input_ref,$2,COALESCE(chunk_index,0),id,
		       $3,$3,now(),now(),now(),economic_buyer_charge_usd,economic_supplier_payout_usd,
		       execution_worker_id,execution_supplier_id,execution_hw_class,
		       execution_engine,execution_build_hash
		  FROM tasks WHERE id=$4`, hedgeID, hedgeKey, demoWorkerUUID, fixture.Dispatch.TaskID); err != nil {
		t.Fatalf("insert already-running hedge sibling: %v", err)
	}
	for _, key := range []string{fixture.Dispatch.ResultKey, hedgeKey} {
		if err := itStorage.PutObject(ctx, key, fixture.Result, "application/json"); err != nil {
			t.Fatalf("put hedge sibling staging result %q: %v", key, err)
		}
	}
	rootCommit := fixture.Commit
	hedgeCommit := fixture.Commit
	hedgeCommit.TaskID, hedgeCommit.ResultKey = hedgeID, hedgeKey
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, rootCommit); err != nil {
		t.Fatalf("enqueue root verification work: %v", err)
	}
	if _, err := itStore.CommitTask(ctx, hedgeID, demoWorkerUUID, hedgeCommit); err != nil {
		t.Fatalf("enqueue hedge verification work: %v", err)
	}

	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	for _, taskID := range []uuid.UUID{fixture.Dispatch.TaskID, hedgeID} {
		result, err := processor.ProcessAttempt(ctx, taskID, 0)
		if err != nil {
			t.Fatalf("process already-verifying hedge sibling %s: %v", taskID, err)
		}
		if !result.Applied.Applied || result.Outcome != OutcomePass {
			t.Fatalf("hedge sibling %s result = %+v", taskID, result)
		}
	}

	var completeTasks, terminalWork, provisionalRows, buyerCharges, tasksDone int
	if err := itPool.QueryRow(ctx, `
		SELECT
		 (SELECT count(*) FROM tasks WHERE id IN ($1,$2) AND status='complete'),
		 (SELECT count(*) FROM verification_work WHERE task_id IN ($1,$2) AND status='terminal'),
		 (SELECT count(*) FROM chunk_artifact_resolutions WHERE job_id=$3 AND chunk_index=0 AND basis='provisional'),
		 (SELECT count(*) FROM ledger_entries WHERE task_id IN ($1,$2) AND kind='buyer_charge'),
		 (SELECT tasks_done FROM jobs WHERE id=$3)`,
		fixture.Dispatch.TaskID, hedgeID, fixture.Dispatch.JobID).
		Scan(&completeTasks, &terminalWork, &provisionalRows, &buyerCharges, &tasksDone); err != nil {
		t.Fatalf("read converged hedge state: %v", err)
	}
	if completeTasks != 2 || terminalWork != 2 || provisionalRows != 1 || buyerCharges != 1 || tasksDone != 1 {
		t.Fatalf("hedge convergence complete=%d terminal_work=%d provisional=%d buyer_charges=%d tasks_done=%d",
			completeTasks, terminalWork, provisionalRows, buyerCharges, tasksDone)
	}
}
