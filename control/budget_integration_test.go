//go:build integration

package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

// createFrozenBudgetTestJob uses the same admission path as production so the
// budget governor reads an immutable per-task buyer charge rather than the legacy
// estimated_usd/task_count approximation. The cap is deliberately halfway between
// one and two task charges: one claim fits, while charged/in-flight + candidate
// does not.
func createFrozenBudgetTestJob(t *testing.T, ctx context.Context) (uuid.UUID, [2]uuid.UUID, EconomicPlan, float64) {
	t.Helper()
	schedule, err := LoadEconomicScheduleFromEnv()
	if err != nil {
		t.Fatalf("LoadEconomicScheduleFromEnv: %v", err)
	}
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD:   1,
		InitialTaskCount: 2,
		SupplierShare:    supplierShareRate,
	}, schedule)
	if !plan.Executable {
		t.Fatalf("budget test economic plan blocked: %s", plan.BlockReason)
	}
	capUSD := roundEconomicUSD(plan.BuyerChargePerTaskUSD * 1.5)
	jobID := uuid.New()
	taskIDs := [2]uuid.UUID{uuid.New(), uuid.New()}
	tasks := make([]taskRow, len(taskIDs))
	for i, taskID := range taskIDs {
		tasks[i] = taskRow{
			ID: taskID, JobID: jobID,
			InputRef: "jobs/budget/t/in.jsonl", ResultKey: "jobs/budget/t/out.json",
			ChunkIndex: i,
		}
	}
	job := &jobRow{
		ID: jobID, BuyerID: demoBuyerUUID, JobType: "embed", ModelRef: "all-minilm-l6-v2",
		InputRef: "jobs/budget/in.jsonl", OutputRef: "jobs/budget/out.jsonl",
		Tier: "batch", VerificationPolicy: []byte(`{}`), TaskCount: len(tasks), MinMemoryGB: 2,
		EstimatedUSD: plan.InitialBuyerChargeUSD, MaxUSD: capUSD, EconomicPlan: plan,
	}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("CreateJobWithTasks: %v", err)
	}
	return jobID, taskIDs, plan, capUSD
}

// Budget Governor (Plane C §12 / Plane D §14 D8): a job with a tiny max_usd whose
// next task's PROJECTED charge (already-charged + one frozen task charge) would breach
// the cap must NOT have that task dispatched. The cap PREVENTS dispatch — the task
// stays queued; a subsequent SweepBudgetStops (Control Plane Hot Path 7->8: this
// used to run inline inside ClaimTask's own transaction, now its own ticker — see
// Workers.sweepBudgetStops) flips budget_state to paused_for_budget and fires a
// budget_stopped event once. No refund, no over-charge: the money math only GATES,
// it never moves money. Then raising the cap lets the same worker claim the same
// task, proving it was the budget gate (not any other hard filter) that held it back.
func TestBudgetCapPausesDispatch(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `TRUNCATE job_events`)
		itPool.Exec(ctx, `DELETE FROM ledger_entries WHERE buyer_id=$1`, demoBuyerUUID)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	reset(t) // demo worker live + eligible (apple_silicon_max, supports embed)

	jobID, taskIDs, plan, capUSD := createFrozenBudgetTestJob(t, ctx)
	doneTask, queuedTask := taskIDs[0], taskIDs[1]
	// One frozen task charge fits under capUSD; two do not. Mark task1 complete and
	// charged so projecting task2 crosses that exact immutable-plan boundary.
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET status='running', tasks_done=1 WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET status='complete', worker_id=$2, completed_at=now() WHERE id=$1`,
		doneTask, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	// The real buyer_charge debit the projection reads (same ledger shape
	// failJobAndSettleOnce settles from). This is what makes the next dispatch breach the cap.
	if _, err := itPool.Exec(ctx,
		`INSERT INTO ledger_entries (kind, buyer_id, task_id, amount_usd, payout_status)
		 VALUES ('buyer_charge',$1,$2,$3,'released')`,
		demoBuyerUUID, doneTask, -plan.BuyerChargePerTaskUSD); err != nil {
		t.Fatal(err)
	}

	wauth := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}

	// 1) The cap PREVENTS dispatch: the claim returns nothing.
	c, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask: %v", err)
	}
	if c != nil {
		t.Fatalf("budget cap should block dispatch, but task %s was claimed (over-cap dispatch)", c.TaskID)
	}

	// 2) The queued task is untouched (still queued, unclaimed) — not failed, not refunded.
	var tstatus string
	var claimedBy *uuid.UUID
	itPool.QueryRow(ctx, `SELECT status, claimed_by FROM tasks WHERE id=$1`, queuedTask).Scan(&tstatus, &claimedBy)
	if tstatus != "queued" || claimedBy != nil {
		t.Fatalf("queued task must stay queued+unclaimed under budget pause, got status=%s claimed_by=%v", tstatus, claimedBy)
	}

	// 3) budget_state flips to paused_for_budget.
	//
	// PATCH (Control Plane Hot Path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md
	// "Move markBudgetStoppedJobs off the claim path onto its own ticker"): this
	// state transition is no longer run synchronously inside ClaimTask's own
	// transaction — it is now Store.SweepBudgetStops, driven by its own ticker
	// (Workers.sweepBudgetStops, control/workers.go) in production. The dispatch
	// guarantee asserted in step 1/2 above is completely unaffected (it comes
	// from ClaimTaskSQL's own synchronous hard-filter predicate); only the
	// VISIBLE budget_state flip + one-time event now happen on the sweep's own
	// cadence, so the test drives that sweep directly rather than relying on
	// ClaimTask to have run it inline.
	if _, err := itStore.SweepBudgetStops(ctx); err != nil {
		t.Fatalf("SweepBudgetStops: %v", err)
	}
	var bstate string
	itPool.QueryRow(ctx, `SELECT budget_state FROM jobs WHERE id=$1`, jobID).Scan(&bstate)
	if bstate != "paused_for_budget" {
		t.Fatalf("budget_state = %q, want paused_for_budget", bstate)
	}

	// 4) Exactly one budget_stopped event (poll + sweep again — it must NOT re-emit).
	if _, err := itStore.ClaimTask(ctx, wauth); err != nil {
		t.Fatalf("second ClaimTask: %v", err)
	}
	if _, err := itStore.SweepBudgetStops(ctx); err != nil {
		t.Fatalf("second SweepBudgetStops: %v", err)
	}
	var nStopped int
	itPool.QueryRow(ctx, `SELECT count(*) FROM job_events WHERE job_id=$1 AND event='budget_stopped'`, jobID).Scan(&nStopped)
	if nStopped != 1 {
		t.Fatalf("expected exactly one budget_stopped event, got %d (re-emitted on repeat poll?)", nStopped)
	}

	// 5) No money moved: the cap GATES, it never refunds. There must be zero refund rows.
	var nRefund int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE kind='refund' AND buyer_id=$1`, demoBuyerUUID).Scan(&nRefund)
	if nRefund != 0 {
		t.Fatalf("budget pause must not refund (cap prevents dispatch, never moves money), got %d refund rows", nRefund)
	}

	// 6) Raising the cap above the projection lets the SAME worker claim the SAME task,
	// proving the budget gate (not another filter) was what held it back.
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET max_usd=$2 WHERE id=$1`,
		jobID, roundEconomicUSD(capUSD+2*plan.BuyerChargePerTaskUSD)); err != nil {
		t.Fatal(err)
	}
	c2, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask after raising cap: %v", err)
	}
	if c2 == nil || c2.TaskID != queuedTask {
		t.Fatalf("raising the cap should release the queued task, but it was not claimed (got %v)", c2)
	}
}

// The cap must hold under CONCURRENCY: an in-flight (claimed, running, not-yet-
// committed) task counts toward projected exposure, so a second task is NOT
// dispatched when the first running task already commits the cap. Without counting
// in-flight work, both would claim before either charged and overshoot the cap.
func TestBudgetCapCountsInflightTasks(t *testing.T) {
	ctx := context.Background()
	t.Cleanup(func() {
		itPool.Exec(ctx, `DELETE FROM ledger_entries WHERE buyer_id=$1`, demoBuyerUUID)
		itPool.Exec(ctx, `TRUNCATE tasks, jobs CASCADE`)
	})
	reset(t)

	createFrozenBudgetTestJob(t, ctx)
	wauth := WorkerAuth{WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID}

	// First claim succeeds (nothing charged/running yet, 1 candidate within cap).
	c1, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask 1: %v", err)
	}
	if c1 == nil {
		t.Fatal("first task should be claimable under the cap")
	}
	// It is now 'running' (claimed, uncommitted). The SECOND claim must be refused:
	// the in-flight task's frozen charge + the candidate's frozen charge breaches the cap.
	c2, err := itStore.ClaimTask(ctx, wauth)
	if err != nil {
		t.Fatalf("ClaimTask 2: %v", err)
	}
	if c2 != nil {
		t.Fatalf("budget cap overshoot: a 2nd task (%s) was dispatched while an in-flight task already commits the cap", c2.TaskID)
	}
}
