//go:build integration

package main

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type commitProjectionRaceProbe struct {
	once    sync.Once
	reached chan struct{}
	release chan struct{}
	target  RecoveryBoundary
}

func (p *commitProjectionRaceProbe) Reach(ctx context.Context, boundary RecoveryBoundary) {
	if boundary != p.target {
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

func TestCommitTaskCannotCreateVerificationUnderConcurrentlyFailedParent(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	jobID, taskIDs, _ := createFrozenEconomicTestJob(t, 2, 0, 0)
	committingTask, failingTask := taskIDs[0], taskIDs[1]
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),worker_id=$2,status='running',started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, committingTask, demoWorkerUUID); err != nil {
		t.Fatalf("claim committing task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),worker_id=$2,status='running',started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, failingTask, demoWorkerUUID); err != nil {
		t.Fatalf("insert failing sibling: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}

	probe := &commitProjectionRaceProbe{
		reached: make(chan struct{}), release: make(chan struct{}), target: BoundaryCommitAfterTaskProjection,
	}
	commitDone := make(chan error, 1)
	go func() {
		_, err := itStore.commitTask(ctx, committingTask, demoWorkerUUID, TaskCommit{
			TaskID: committingTask, ResultKey: "jobs/economic/result-" + committingTask.String() + ".json",
			DurationMS: 17, TokensUsed: 3,
		}, probe)
		commitDone <- err
	}()
	select {
	case <-probe.reached:
	case <-ctx.Done():
		t.Fatal("commit did not reach uncommitted task projection")
	}

	// The sibling failure cannot see the uncommitted verifying row. It is allowed
	// to win the parent fence; the blocked upload must then roll its task projection
	// back instead of creating work beneath the failed parent.
	if err := itStore.FailTaskAndSettleJob(ctx, failingTask, jobID); err != nil {
		t.Fatalf("sibling terminal failure: %v", err)
	}
	close(probe.release)
	select {
	case err := <-commitDone:
		if !errors.Is(err, errNotFound) {
			t.Fatalf("commit under terminal parent = %v, want conflict", err)
		}
	case <-ctx.Done():
		t.Fatal("commit did not return after parent failure")
	}

	var jobStatus, commitStatus, failStatus string
	var workRows, verdictRows, ledgerRows int
	if err := itPool.QueryRow(ctx, `
		SELECT j.status,c.status,f.status,
		       (SELECT count(*) FROM verification_work WHERE task_id=c.id),
		       (SELECT count(*) FROM task_verdicts WHERE task_id=c.id),
		       (SELECT count(*) FROM ledger_entries WHERE task_id IN (c.id,f.id))
		  FROM jobs j JOIN tasks c ON c.id=$2 JOIN tasks f ON f.id=$3
		 WHERE j.id=$1`, jobID, committingTask, failingTask).
		Scan(&jobStatus, &commitStatus, &failStatus, &workRows, &verdictRows, &ledgerRows); err != nil {
		t.Fatal(err)
	}
	if jobStatus != "failed" || commitStatus != "running" || failStatus != "failed" ||
		workRows != 0 || verdictRows != 0 || ledgerRows != 0 {
		t.Fatalf("commit/fail convergence job=%s committing=%s failing=%s work=%d verdict=%d ledger=%d",
			jobStatus, commitStatus, failStatus, workRows, verdictRows, ledgerRows)
	}
}

func TestSiblingFailureWaitingOnCommitFenceSeesFreshVerificationWork(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	jobID, taskIDs, _ := createFrozenEconomicTestJob(t, 2, 0, 0)
	committingTask, failingTask := taskIDs[0], taskIDs[1]
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),worker_id=$2,status='running',started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, committingTask, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),worker_id=$2,status='running',started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, failingTask, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}

	probe := &commitProjectionRaceProbe{
		reached: make(chan struct{}), release: make(chan struct{}), target: BoundaryCommitAfterParentFence,
	}
	commitDone := make(chan error, 1)
	go func() {
		_, err := itStore.commitTask(ctx, committingTask, demoWorkerUUID, TaskCommit{
			TaskID: committingTask, ResultKey: "jobs/economic/result-" + committingTask.String() + ".json", DurationMS: 17, TokensUsed: 3,
		}, probe)
		commitDone <- err
	}()
	select {
	case <-probe.reached:
	case <-ctx.Done():
		t.Fatal("commit did not acquire parent fence")
	}
	failStarted := make(chan struct{})
	failDone := make(chan error, 1)
	go func() {
		close(failStarted)
		failDone <- itStore.FailTaskAndSettleJob(ctx, failingTask, jobID)
	}()
	<-failStarted
	// Give the failure transaction a chance to update its distinct task and wait on
	// the parent row. Correctness does not depend on the delay: either way it reads
	// pending state only after the commit releases the fence.
	time.Sleep(50 * time.Millisecond)
	close(probe.release)
	if err := <-commitDone; err != nil {
		t.Fatalf("fenced commit: %v", err)
	}
	if err := <-failDone; !errors.Is(err, ErrJobVerificationPending) {
		t.Fatalf("waiting sibling failure = %v, want unresolved-verification refusal", err)
	}

	var jobStatus, commitStatus, failStatus, workStatus string
	if err := itPool.QueryRow(ctx, `
		SELECT j.status,c.status,f.status,
		       COALESCE((SELECT max(status) FROM verification_work WHERE task_id=c.id),'')
		  FROM jobs j JOIN tasks c ON c.id=$2 JOIN tasks f ON f.id=$3 WHERE j.id=$1`,
		jobID, committingTask, failingTask).
		Scan(&jobStatus, &commitStatus, &failStatus, &workStatus); err != nil {
		t.Fatal(err)
	}
	if jobStatus != "running" || commitStatus != "verifying" || failStatus != "running" || workStatus != VerificationWorkPending {
		t.Fatalf("commit-wins state job=%s committing=%s failing=%s work=%s",
			jobStatus, commitStatus, failStatus, workStatus)
	}
}
