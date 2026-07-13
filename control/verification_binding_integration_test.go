//go:build integration

package main

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestVerificationWorkInsertRequiresExactLiveAttemptBinding(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/work-binding/tasks/0/input.jsonl")
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET status='queued' WHERE id=$1`, taskID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET status='running',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, taskID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='verifying',result_ref=result_key,
		       reported_duration_ms=9,reported_tokens_used=2
		 WHERE id=$1`, taskID); err != nil {
		t.Fatal(err)
	}
	var hwClass, engine, build string
	if err := itPool.QueryRow(ctx, `
		SELECT COALESCE(hw_class,''),COALESCE(engine,''),COALESCE(build_hash,'')
		  FROM workers WHERE id=$1`, demoWorkerUUID).Scan(&hwClass, &engine, &build); err != nil {
		t.Fatal(err)
	}
	info := &CommitTaskInfo{
		TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID,
		HWClass: hwClass, engine: engine, buildHash: build, jobType: "embed",
		InputRef: "jobs/work-binding/tasks/0/input.jsonl", ResultKey: "jobs/x/tasks/0/result.json",
		ChunkIndex: 0, SplitSize: 0, Attempt: 0, DurationMS: 9, TokensUsed: 2,
	}
	snapshot, err := verificationWorkSnapshotFromCommit(info, TaskCommit{
		TaskID: taskID, ResultKey: info.ResultKey, DurationMS: 9, TokensUsed: 2,
	})
	if err != nil {
		t.Fatal(err)
	}
	wrongAttempt := snapshot
	wrongAttempt.Attempt = 1
	if _, _, err := itStore.CreateVerificationWork(ctx, wrongAttempt); err == nil {
		t.Fatal("verification_work accepted a snapshot for a different task attempt")
	}
	var rows int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM verification_work WHERE task_id=$1`, taskID).Scan(&rows); err != nil || rows != 0 {
		t.Fatalf("conflicting work insert leaked rows=%d err=%v", rows, err)
	}
	work, created, err := itStore.CreateVerificationWork(ctx, snapshot)
	if err != nil || !created || work.Status != VerificationWorkPending {
		t.Fatalf("exact live attempt binding created=%v work=%+v err=%v", created, work, err)
	}
}

type decisionFenceProbe struct {
	once    sync.Once
	reached chan struct{}
	release chan struct{}
}

func (p *decisionFenceProbe) Reach(ctx context.Context, boundary RecoveryBoundary) {
	if boundary != BoundaryVerifyAfterDecision {
		return
	}
	p.once.Do(func() {
		close(p.reached)
		select {
		case <-p.release:
		case <-ctx.Done():
		}
	})
}

func TestApplyRechecksTaskProjectionAgainstPinnedArtifact(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	fixture := newVerificationProcessorFixture(t)
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatal(err)
	}
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatal(err)
	}
	probe := &decisionFenceProbe{reached: make(chan struct{}), release: make(chan struct{})}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		WithRecoveryProbe(probe)
	done := make(chan error, 1)
	go func() {
		_, err := processor.ProcessAttempt(ctx, fixture.Dispatch.TaskID, 0)
		done <- err
	}()
	select {
	case <-probe.reached:
	case <-ctx.Done():
		t.Fatal("verification did not persist its decision")
	}
	work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil || work.Artifact == nil {
		t.Fatalf("read pinned work: %+v err=%v", work, err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET result_ref='tampered/result',result_sha256=repeat('f',64)
		 WHERE id=$1`, fixture.Dispatch.TaskID); err != nil {
		t.Fatal(err)
	}
	close(probe.release)
	select {
	case err := <-done:
		if !errors.Is(err, ErrVerificationWorkConflict) {
			t.Fatalf("apply with tampered task projection = %v, want authority conflict", err)
		}
	case <-ctx.Done():
		t.Fatal("tampered apply did not return")
	}
	var status string
	var verdicts, ledger, durations int
	if err := itPool.QueryRow(ctx, `
		SELECT w.status,
		       (SELECT count(*) FROM task_verdicts WHERE task_id=w.task_id),
		       (SELECT count(*) FROM ledger_entries WHERE task_id=w.task_id),
		       (SELECT count(*) FROM task_durations WHERE task_id=w.task_id)
		  FROM verification_work w WHERE w.id=$1`, work.ID).
		Scan(&status, &verdicts, &ledger, &durations); err != nil {
		t.Fatal(err)
	}
	if status != VerificationWorkPending || verdicts != 0 || ledger != 0 || durations != 0 {
		t.Fatalf("tampered projection leaked terminal state: work=%s verdict=%d ledger=%d duration=%d",
			status, verdicts, ledger, durations)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET result_ref=$2,result_sha256=$3 WHERE id=$1`,
		fixture.Dispatch.TaskID, work.Artifact.Key, work.Artifact.SHA256); err != nil {
		t.Fatal(err)
	}
	// The failed owner deliberately released its lease with retry backoff. Make the
	// test's repaired authority immediately eligible instead of accidentally testing
	// the one-second scheduler delay.
	if _, err := itPool.Exec(ctx, `UPDATE verification_work SET next_attempt_at=now() WHERE id=$1`, work.ID); err != nil {
		t.Fatal(err)
	}
	if err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).Drain(ctx, 1); err != nil {
		t.Fatalf("recover after restoring exact projection: %v", err)
	}
	terminal := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if terminal.TaskStatus != "complete" || terminal.WorkStatus != VerificationWorkTerminal || terminal.LedgerRows != 3 {
		t.Fatalf("restored projection did not converge: %+v", terminal)
	}
}
