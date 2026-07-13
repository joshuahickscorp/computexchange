//go:build integration

package main

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
)

// TestCommitRequiresArtifactBeforeAtomicSettlement proves the stop-ship invariant:
// upload intent alone leaves the task/job in verifying with no counters, accepted
// telemetry, verdict, or money. Once the same worker supplies the artifact and
// retries, all accepted state appears together exactly once.
func TestCommitRequiresArtifactBeforeAtomicSettlement(t *testing.T) {
	reset(t)
	ctx := context.Background()
	if _, err := itPool.Exec(ctx, `TRUNCATE task_durations`); err != nil {
		t.Fatalf("truncate durations: %v", err)
	}
	if code, body := req(t, "POST", "/v1/worker/register",
		WorkerCapability{HWClass: "apple_silicon_max", MemoryGB: 64,
			SupportedJobs: []string{"embed"}, SupportedModels: []string{"all-minilm-l6-v2"}},
		workerTok(), jsonCT()); code != http.StatusOK {
		t.Fatalf("register: %d %s", code, body)
	}
	jobID, _ := submitEmbedJob(t, 1, 0, 0, 0)
	code, body := req(t, "GET", "/v1/worker/poll", nil, workerTok())
	if code != http.StatusOK {
		t.Fatalf("poll: %d %s", code, body)
	}
	var dispatch TaskDispatch
	if err := json.Unmarshal(body, &dispatch); err != nil {
		t.Fatalf("decode dispatch: %v", err)
	}
	commit := TaskCommit{
		TaskID: dispatch.TaskID, ResultKey: dispatch.ResultKey,
		DurationMS: 123, TokensUsed: 7,
	}
	code, body = req(t, "POST", "/v1/worker/task/"+dispatch.TaskID.String()+"/commit", commit, workerTok(), jsonCT())
	if code != http.StatusBadRequest {
		t.Fatalf("missing artifact commit: want 400, got %d: %s", code, body)
	}

	assertState := func(wantTask, wantJob string, wantDone, wantVerdicts, wantLedger, wantDurations int) {
		t.Helper()
		var taskStatus, jobStatus string
		var tasksDone int
		if err := itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, dispatch.TaskID).Scan(&taskStatus); err != nil {
			t.Fatalf("read task status: %v", err)
		}
		if err := itPool.QueryRow(ctx, `SELECT status,tasks_done FROM jobs WHERE id=$1`, jobID).Scan(&jobStatus, &tasksDone); err != nil {
			t.Fatalf("read job status: %v", err)
		}
		var verdicts, ledger, durations int
		if err := itPool.QueryRow(ctx, `SELECT count(*) FROM task_verdicts WHERE task_id=$1`, dispatch.TaskID).Scan(&verdicts); err != nil {
			t.Fatalf("count verdicts: %v", err)
		}
		if err := itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE task_id=$1`, dispatch.TaskID).Scan(&ledger); err != nil {
			t.Fatalf("count ledger: %v", err)
		}
		if err := itPool.QueryRow(ctx, `SELECT count(*) FROM task_durations WHERE job_id=$1`, jobID).Scan(&durations); err != nil {
			t.Fatalf("count durations: %v", err)
		}
		if taskStatus != wantTask || jobStatus != wantJob || tasksDone != wantDone ||
			verdicts != wantVerdicts || ledger != wantLedger || durations != wantDurations {
			t.Fatalf("state task=%s job=%s done=%d verdicts=%d ledger=%d durations=%d; want %s/%s/%d/%d/%d/%d",
				taskStatus, jobStatus, tasksDone, verdicts, ledger, durations,
				wantTask, wantJob, wantDone, wantVerdicts, wantLedger, wantDurations)
		}
	}
	assertState("verifying", "verifying", 0, 0, 0, 0)

	if err := itStorage.PutObject(ctx, dispatch.ResultKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatalf("put artifact: %v", err)
	}
	code, body = req(t, "POST", "/v1/worker/task/"+dispatch.TaskID.String()+"/commit", commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("retry after artifact: want 204, got %d: %s", code, body)
	}
	assertState("complete", "complete", 1, 1, 3, 1)

	var current, durable string
	if err := itPool.QueryRow(ctx,
		`SELECT t.verification_outcome, v.outcome
		   FROM tasks t JOIN task_verdicts v ON v.task_id=t.id
		  WHERE t.id=$1`, dispatch.TaskID,
	).Scan(&current, &durable); err != nil {
		t.Fatalf("read accepted verdict: %v", err)
	}
	if current != string(OutcomePass) || durable != string(OutcomePass) {
		t.Fatalf("verdict projection/history = %q/%q, want pass/pass", current, durable)
	}

	// A post-success duplicate cannot create a second verdict, duration, or money.
	if code, _ := req(t, "POST", "/v1/worker/task/"+dispatch.TaskID.String()+"/commit", commit, workerTok(), jsonCT()); code != http.StatusNoContent {
		t.Fatalf("duplicate accepted commit replay: want 204, got %d", code)
	}
	assertState("complete", "complete", 1, 1, 3, 1)
}

func TestPenaltyVerdictCannotReleaseSupplierMoney(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/x/input.jsonl")
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='complete', completed_at=now(),
		 verification_outcome='pass_with_penalty', verified_at=now() WHERE id=$1`, taskID); err != nil {
		t.Fatalf("seed penalty verdict: %v", err)
	}
	release := time.Now().Add(-time.Minute)
	if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
		Kind: KindSupplierCredit, SupplierID: &demoSupplierUUID, TaskID: &taskID,
		AmountUSD: 1, PayoutStatus: PayoutHeld, ReleaseAt: &release,
	}}); err != nil {
		t.Fatalf("seed held credit: %v", err)
	}
	due, err := itStore.DuePayouts(ctx, 10)
	if err != nil {
		t.Fatalf("read due penalty payout: %v", err)
	}
	for _, entry := range due {
		if entry.AmountUSD == 1 && entry.SupplierID == demoSupplierUUID {
			t.Fatalf("pass_with_penalty credit became payable: %+v", entry)
		}
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET verification_outcome='pass', verified_at=now() WHERE id=$1`, taskID); err != nil {
		t.Fatalf("resolve verdict: %v", err)
	}
	due, err = itStore.DuePayouts(ctx, 10)
	if err != nil || len(due) != 1 {
		t.Fatalf("resolved pass should release exactly one credit: due=%+v err=%v", due, err)
	}
}
