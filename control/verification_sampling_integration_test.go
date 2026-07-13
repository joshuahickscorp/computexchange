//go:build integration

package main

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"github.com/google/uuid"
)

func TestVerificationSamplingRunsOnlyAfterDurableCommit(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, true, false, demoHoneypotEmbedRef)
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET status='queued' WHERE id=$1`, taskID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),started_at=now(),worker_id=$2,status='running',
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, taskID, demoWorkerUUID); err != nil {
		t.Fatalf("claim sampling-order fixture: %v", err)
	}
	const resultKey = "jobs/x/tasks/0/result.json"
	if err := itStorage.PutObject(ctx, resultKey, []byte(`{"vectors":[[0,1,0]]}`), "application/json"); err != nil {
		t.Fatalf("put sampling-order result: %v", err)
	}

	var observed bool
	var observedStatus, observedResultRef string
	var observeErr error
	itServer.verifier.WithSamplingDecisionObserver(func(observedTask uuid.UUID) {
		if observedTask != taskID {
			observeErr = fmt.Errorf("sampler observed task %s, want %s", observedTask, taskID)
			return
		}
		observeErr = itPool.QueryRow(context.Background(), `
			SELECT status,COALESCE(result_ref,'') FROM tasks WHERE id=$1`, taskID,
		).Scan(&observedStatus, &observedResultRef)
		observed = true
	})
	t.Cleanup(func() { itServer.verifier.WithSamplingDecisionObserver(nil) })

	commit := TaskCommit{TaskID: taskID, ResultKey: resultKey, DurationMS: 10}
	code, body := req(t, http.MethodPost, "/v1/worker/task/"+taskID.String()+"/commit",
		commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("commit sampling-order fixture: want 204, got %d: %s", code, body)
	}
	if observeErr != nil {
		t.Fatalf("sampling observer: %v", observeErr)
	}
	if !observed {
		t.Fatal("verification sampling decision was never observed")
	}
	if observedStatus != "verifying" || observedResultRef != resultKey {
		t.Fatalf("sampling ran before durable result commit: status=%q result_ref=%q", observedStatus, observedResultRef)
	}
}
