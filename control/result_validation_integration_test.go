//go:build integration

package main

import (
	"context"
	"math"
	"net/http"
	"testing"
)

// TestVerificationProcessorRejectsMalformedArtifactBeforeSettlement exercises
// the real HTTP commit -> durable work -> sealed artifact -> immutable plan ->
// fenced apply path. Neither an empty object nor a shape-valid JSON document with
// the wrong embedding dimension may create money, duration, completion, or a
// buyer-deliverable result.
func TestVerificationProcessorRejectsMalformedArtifactBeforeSettlement(t *testing.T) {
	cases := []struct {
		name   string
		result []byte
		code   string
	}{
		{name: "empty", result: []byte{}, code: resultValidationEmpty},
		{
			name: "wrong-model-dimension",
			result: []byte(`{"job_type":"embed","model":"all-minilm-l6-v2",` +
				`"dim":3,"count":1,"vectors":[[1,0,0]]}`),
			code: resultValidationDimension,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			ctx := context.Background()
			fixture := newVerificationProcessorFixture(t)
			beforeRep := supplierRep(t)
			beforeCompleted := supplierCompletedTasks(t, demoSupplierUUID)
			if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, tc.result, "application/octet-stream"); err != nil {
				t.Fatalf("put invalid staging result: %v", err)
			}

			code, body := req(t, http.MethodPost,
				"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
				fixture.Commit, workerTok(), jsonCT())
			if code != http.StatusNoContent {
				t.Fatalf("invalid artifact commit: want terminal 204, got %d: %s", code, body)
			}

			state := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
			if state.TaskStatus != "retrying" || state.TaskOutcome != "" ||
				state.JobStatus != "running" || state.JobTasksDone != 0 ||
				state.WorkStatus != VerificationWorkTerminal || state.TerminalOutcome != string(OutcomeFail) ||
				state.WorkRows != 1 || state.PlanRows != 1 || state.VerdictRows != 1 ||
				state.DurationRows != 0 || state.LedgerRows != 0 || state.LedgerKinds != 0 {
				t.Fatalf("invalid artifact leaked completion/telemetry/money or failed to converge: %+v", state)
			}
			if got := supplierCompletedTasks(t, demoSupplierUUID); got != beforeCompleted {
				t.Fatalf("supplier completed_tasks changed from %d to %d", beforeCompleted, got)
			}
			if got, want := supplierRep(t), updateReputation(beforeRep, EventResultCorrupt); math.Abs(float64(got-want)) > 1e-6 {
				t.Fatalf("supplier reputation = %v, want result_corrupt update %v", got, want)
			}

			work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
			if err != nil {
				t.Fatalf("read invalid terminal work: %v", err)
			}
			plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
			if err != nil {
				t.Fatalf("read invalid immutable plan: %v", err)
			}
			if plan.Decision.Outcome != OutcomeFail || plan.Decision.Failure == nil ||
				plan.Decision.Failure.Kind != "artifact_invalid" ||
				plan.Decision.Failure.Code != tc.code ||
				len(plan.Settlement) != 0 {
				t.Fatalf("invalid artifact plan = %+v", plan)
			}
			var eventKind, supplierStatus string
			var eventCount, attempt int
			if err := itPool.QueryRow(ctx, `
				SELECT min(kind),count(*),min(attempt)
				  FROM verification_events WHERE task_id=$1`, fixture.Dispatch.TaskID).
				Scan(&eventKind, &eventCount, &attempt); err != nil {
				t.Fatalf("read artifact_invalid event: %v", err)
			}
			if eventKind != "artifact_invalid" || eventCount != 1 || attempt != 0 {
				t.Fatalf("artifact event = %q count=%d attempt=%d", eventKind, eventCount, attempt)
			}
			if err := itPool.QueryRow(ctx, `SELECT status FROM suppliers WHERE id=$1`, demoSupplierUUID).
				Scan(&supplierStatus); err != nil {
				t.Fatal(err)
			}
			if supplierStatus != "suspended" {
				t.Fatalf("malformed artifact supplier status = %q, want suspended", supplierStatus)
			}
		})
	}
}
