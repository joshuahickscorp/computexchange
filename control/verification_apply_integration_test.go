//go:build integration

package main

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
)

type verificationApplyTestState struct {
	TaskStatus       string
	TaskOutcome      string
	TaskAttempt      int16
	TaskWorker       *uuid.UUID
	ExcludedWorker   *uuid.UUID
	JobStatus        string
	JobTasksDone     int
	SupplierRep      float32
	SupplierStatus   string
	SupplierComplete int64
	Quarantined      bool
	Verdicts         int
	Events           int
	Durations        int
	LedgerRows       int
}

func seedVerificationApplyTest(t *testing.T, honeypot bool, inputRef string) (*CommitTaskInfo, []LedgerEntry, verificationApplyTestState) {
	t.Helper()
	info, entries, before, _, _ := seedVerificationApplyTestWithEconomics(t, honeypot, inputRef, 1, 0)
	return info, entries, before
}

// seedVerificationApplyTestWithEconomics creates the job, immutable economic
// plan, reserve, and every initial task through the production admission path.
// Tests may then project one task into an unusual verifying state without ever
// mutating its frozen economic amounts after INSERT.
func seedVerificationApplyTestWithEconomics(t *testing.T, honeypot bool, inputRef string, initialTasks, extraReserve int) (*CommitTaskInfo, []LedgerEntry, verificationApplyTestState, EconomicPlan, []uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	if initialTasks < 1 {
		t.Fatal("verification apply fixture requires at least one initial task")
	}
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD:   float64(initialTasks),
		InitialTaskCount: initialTasks,
		ExtraTaskReserve: extraReserve,
		SupplierShare:    supplierShareRate,
	}, testEconomicSchedule())
	if err := ValidateEconomicPlanSnapshot(plan); err != nil {
		t.Fatalf("verification apply fixture economic plan: %v", err)
	}
	jobID := uuid.New()
	taskIDs := make([]uuid.UUID, initialTasks)
	tasks := make([]taskRow, initialTasks)
	for i := range tasks {
		taskIDs[i] = uuid.New()
		taskInputRef := fmt.Sprintf("jobs/%s/tasks/%d/input.jsonl", jobID, i)
		if i == 0 {
			taskInputRef = inputRef
		}
		tasks[i] = taskRow{
			ID:         taskIDs[i],
			JobID:      jobID,
			IsHoneypot: i == 0 && honeypot,
			InputRef:   taskInputRef,
			ResultKey:  fmt.Sprintf("jobs/%s/tasks/%d/result.json", jobID, i),
			ChunkIndex: i,
		}
	}
	job := &jobRow{
		ID:                 jobID,
		BuyerID:            demoBuyerUUID,
		JobType:            "embed",
		ModelRef:           "all-minilm-l6-v2",
		InputRef:           inputRef,
		OutputRef:          fmt.Sprintf("jobs/%s/output.jsonl", jobID),
		Tier:               "batch",
		VerificationPolicy: []byte(`{}`),
		TaskCount:          initialTasks,
		EstimatedUSD:       plan.InitialBuyerChargeUSD,
		SplitSize:          1000,
		EconomicPlan:       plan,
	}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("create verification apply fixture: %v", err)
	}
	taskID := taskIDs[0]
	var fixtureSupplier uuid.UUID
	var fixtureHWClass, fixtureEngine, fixtureBuildHash string
	if err := itPool.QueryRow(ctx, `
		SELECT supplier_id,COALESCE(hw_class,''),COALESCE(engine,''),COALESCE(build_hash,'')
		  FROM workers WHERE id=$1`, demoWorkerUUID).
		Scan(&fixtureSupplier, &fixtureHWClass, &fixtureEngine, &fixtureBuildHash); err != nil {
		t.Fatalf("read verification fixture worker authority: %v", err)
	}

	sha := strings.Repeat("a", 64)
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET status='running',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, taskID, demoWorkerUUID); err != nil {
		t.Fatalf("seed running task identity: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='verifying',
		       result_ref=result_key, result_sha256=$2,
		       reported_duration_ms=321, reported_tokens_used=17,
		       verification_outcome=NULL, verified_at=NULL
		 WHERE id=$1`, taskID, sha); err != nil {
		t.Fatalf("seed verifying task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='verifying' WHERE id=$1`, jobID); err != nil {
		t.Fatalf("seed verifying job: %v", err)
	}
	// reset() restores status/reputation but deliberately preserves historical
	// quarantine timestamps. This fixture needs a clean attribution baseline.
	if _, err := itPool.Exec(ctx, `UPDATE suppliers SET quarantined_at=NULL WHERE id=$1`, fixtureSupplier); err != nil {
		t.Fatalf("clear fixture quarantine timestamp: %v", err)
	}

	info := &CommitTaskInfo{
		TaskID:       taskID,
		JobID:        jobID,
		WorkerID:     demoWorkerUUID,
		SupplierID:   fixtureSupplier,
		IsHoneypot:   honeypot,
		HWClass:      fixtureHWClass,
		engine:       fixtureEngine,
		buildHash:    fixtureBuildHash,
		jobType:      "embed",
		InputRef:     inputRef,
		ResultKey:    tasks[0].ResultKey,
		ModelRef:     "all-minilm-l6-v2",
		ChunkIndex:   0,
		SplitSize:    1000,
		Attempt:      0,
		DurationMS:   321,
		TokensUsed:   17,
		ResultSHA256: sha,
	}
	entries := splitFrozenCharge(demoBuyerUUID, fixtureSupplier, taskID,
		plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD, 0, time.Now())
	return info, entries, readVerificationApplyTestState(t, info), plan, taskIDs
}

func readVerificationApplyTestState(t *testing.T, info *CommitTaskInfo) verificationApplyTestState {
	t.Helper()
	ctx := context.Background()
	var got verificationApplyTestState
	if err := itPool.QueryRow(ctx, `
		SELECT status,COALESCE(verification_outcome,''),COALESCE(retry_count,0),worker_id,excluded_worker
		  FROM tasks WHERE id=$1`, info.TaskID).
		Scan(&got.TaskStatus, &got.TaskOutcome, &got.TaskAttempt, &got.TaskWorker, &got.ExcludedWorker); err != nil {
		t.Fatalf("read task apply state: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT status,tasks_done FROM jobs WHERE id=$1`, info.JobID).
		Scan(&got.JobStatus, &got.JobTasksDone); err != nil {
		t.Fatalf("read job apply state: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT reputation,status,completed_tasks,quarantined_at IS NOT NULL
		  FROM suppliers WHERE id=$1`, info.SupplierID).
		Scan(&got.SupplierRep, &got.SupplierStatus, &got.SupplierComplete, &got.Quarantined); err != nil {
		t.Fatalf("read supplier apply state: %v", err)
	}
	for name, query := range map[string]struct {
		SQL  string
		Dest *int
	}{
		"verdicts":  {`SELECT count(*) FROM task_verdicts WHERE task_id=$1`, &got.Verdicts},
		"events":    {`SELECT count(*) FROM verification_events WHERE task_id=$1`, &got.Events},
		"durations": {`SELECT count(*) FROM task_durations WHERE task_id=$1`, &got.Durations},
		"ledger":    {`SELECT count(*) FROM ledger_entries WHERE task_id=$1`, &got.LedgerRows},
	} {
		if err := itPool.QueryRow(ctx, query.SQL, info.TaskID).Scan(query.Dest); err != nil {
			t.Fatalf("count %s: %v", name, err)
		}
	}
	return got
}

func TestApplyVerificationDecisionAcceptsAndExactReplayIsEffectFree(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, entries, before := seedVerificationApplyTest(t, false, "jobs/x/input.jsonl")

	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info, TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan plain acceptance: %v", err)
	}
	if decision.Outcome != OutcomePass || len(decision.Effects) != 1 ||
		decision.Effects[0].Kind != VerificationEffectDockReputation ||
		decision.Effects[0].ReputationEvent != EventTaskSuccess {
		t.Fatalf("plain decision = %#v, want pass with one task-success reputation effect", decision)
	}
	if afterPlan := readVerificationApplyTestState(t, info); !reflect.DeepEqual(afterPlan, before) {
		t.Fatalf("write-free planner changed durable state:\nbefore=%+v\nafter =%+v", before, afterPlan)
	}

	result, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	if err != nil {
		t.Fatalf("apply plain acceptance: %v", err)
	}
	if !result.Applied || result.Rejected || result.TiebreaksInserted != 0 {
		t.Fatalf("apply result = %+v", result)
	}
	after := readVerificationApplyTestState(t, info)
	if after.TaskStatus != "complete" || after.TaskOutcome != string(OutcomePass) || after.TaskAttempt != 0 ||
		after.TaskWorker == nil || *after.TaskWorker != info.WorkerID {
		t.Fatalf("accepted task projection = %+v", after)
	}
	if after.JobStatus != "verifying" || after.JobTasksDone != before.JobTasksDone+1 {
		t.Fatalf("accepted job state = %+v, before=%+v", after, before)
	}
	if after.SupplierStatus != "active" || after.Quarantined ||
		after.SupplierComplete != before.SupplierComplete+1 ||
		after.SupplierRep != updateReputation(before.SupplierRep, EventTaskSuccess) {
		t.Fatalf("accepted supplier state = %+v, before=%+v", after, before)
	}
	if after.Verdicts != 1 || after.Events != 0 || after.Durations != 1 || after.LedgerRows != 3 {
		t.Fatalf("accepted durable rows = %+v", after)
	}
	var verdict, sha string
	if err := itPool.QueryRow(ctx, `
		SELECT outcome,COALESCE(result_sha256,'') FROM task_verdicts
		 WHERE task_id=$1 AND attempt=$2`, info.TaskID, info.Attempt).Scan(&verdict, &sha); err != nil {
		t.Fatalf("read accepted verdict: %v", err)
	}
	if verdict != string(OutcomePass) || sha != info.ResultSHA256 {
		t.Fatalf("accepted verdict outcome/sha = %q/%q", verdict, sha)
	}
	var duration int64
	var durationWorker uuid.UUID
	if err := itPool.QueryRow(ctx, `SELECT duration_ms,worker_id FROM task_durations WHERE task_id=$1`, info.TaskID).
		Scan(&duration, &durationWorker); err != nil {
		t.Fatalf("read accepted duration: %v", err)
	}
	if duration != int64(info.DurationMS) || durationWorker != info.WorkerID {
		t.Fatalf("accepted duration = %d/%s, want %d/%s", duration, durationWorker, info.DurationMS, info.WorkerID)
	}
	var distinctLedgerKinds int
	if err := itPool.QueryRow(ctx, `SELECT count(DISTINCT kind) FROM ledger_entries WHERE task_id=$1`, info.TaskID).
		Scan(&distinctLedgerKinds); err != nil {
		t.Fatalf("read accepted ledger kinds: %v", err)
	}
	if distinctLedgerKinds != 3 {
		t.Fatalf("accepted ledger has %d distinct kinds, want 3", distinctLedgerKinds)
	}

	replay, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	if err != nil {
		t.Fatalf("replay plain acceptance: %v", err)
	}
	if replay.Applied || replay.Rejected || replay.TiebreaksInserted != 0 {
		t.Fatalf("acceptance replay result = %+v", replay)
	}
	if replayState := readVerificationApplyTestState(t, info); !reflect.DeepEqual(replayState, after) {
		t.Fatalf("acceptance replay duplicated an effect:\nfirst =%+v\nreplay=%+v", after, replayState)
	}
}

func TestApplyVerificationDecisionHoneypotRejectsAtomicallyAndReplaysExactly(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, _, before := seedVerificationApplyTest(t, true, demoHoneypotEmbedRef)

	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info, TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[0,1,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan honeypot rejection: %v", err)
	}
	if decision.Outcome != OutcomeFail {
		t.Fatalf("honeypot outcome = %q, want fail (decision=%#v)", decision.Outcome, decision)
	}
	wantEffects := []VerificationEffectKind{
		VerificationEffectDockReputation,
		VerificationEffectRecordEvent,
		VerificationEffectClawbackCredit,
		VerificationEffectQuarantine,
		VerificationEffectRequeue,
	}
	gotEffects := make([]VerificationEffectKind, len(decision.Effects))
	for i := range decision.Effects {
		gotEffects[i] = decision.Effects[i].Kind
	}
	if !reflect.DeepEqual(gotEffects, wantEffects) {
		t.Fatalf("honeypot effects = %#v, want %#v", gotEffects, wantEffects)
	}
	if afterPlan := readVerificationApplyTestState(t, info); !reflect.DeepEqual(afterPlan, before) {
		t.Fatalf("write-free honeypot plan changed durable state:\nbefore=%+v\nafter =%+v", before, afterPlan)
	}

	result, err := itStore.ApplyVerificationDecision(ctx, info, decision, nil)
	if err != nil {
		t.Fatalf("apply honeypot rejection: %v", err)
	}
	if !result.Applied || !result.Rejected || result.TiebreaksInserted != 0 {
		t.Fatalf("honeypot apply result = %+v", result)
	}
	after := readVerificationApplyTestState(t, info)
	if after.TaskStatus != "retrying" || after.TaskOutcome != "" || after.TaskAttempt != info.Attempt+1 ||
		after.TaskWorker != nil || after.ExcludedWorker == nil || *after.ExcludedWorker != info.WorkerID {
		t.Fatalf("rejected task state = %+v", after)
	}
	if after.JobStatus != "running" || after.JobTasksDone != before.JobTasksDone {
		t.Fatalf("rejected job state = %+v, before=%+v", after, before)
	}
	if after.SupplierStatus != "suspended" || !after.Quarantined ||
		after.SupplierComplete != before.SupplierComplete ||
		after.SupplierRep != updateReputation(before.SupplierRep, EventHoneypotFail) {
		t.Fatalf("rejected supplier state = %+v, before=%+v", after, before)
	}
	if after.Verdicts != 1 || after.Events != 1 || after.Durations != 0 || after.LedgerRows != 0 {
		t.Fatalf("rejected durable rows = %+v", after)
	}
	var verdict, eventKind string
	var eventAttempt int16
	if err := itPool.QueryRow(ctx, `SELECT outcome FROM task_verdicts WHERE task_id=$1 AND attempt=$2`, info.TaskID, info.Attempt).
		Scan(&verdict); err != nil {
		t.Fatalf("read rejected verdict: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT kind,attempt FROM verification_events WHERE task_id=$1`, info.TaskID).
		Scan(&eventKind, &eventAttempt); err != nil {
		t.Fatalf("read rejected event: %v", err)
	}
	if verdict != string(OutcomeFail) || eventKind != "honeypot_fail" || eventAttempt != info.Attempt {
		t.Fatalf("rejected verdict/event = %q/%q attempt %d", verdict, eventKind, eventAttempt)
	}

	replay, err := itStore.ApplyVerificationDecision(ctx, info, decision, nil)
	if err != nil {
		t.Fatalf("replay honeypot rejection: %v", err)
	}
	if replay.Applied || !replay.Rejected || replay.TiebreaksInserted != 0 {
		t.Fatalf("rejection replay result = %+v", replay)
	}
	if replayState := readVerificationApplyTestState(t, info); !reflect.DeepEqual(replayState, after) {
		t.Fatalf("rejection replay duplicated an effect:\nfirst =%+v\nreplay=%+v", after, replayState)
	}
}

func TestApplyVerificationDecisionDatabaseFailureRollsBackThenRetryConverges(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, entries, before := seedVerificationApplyTest(t, false, "jobs/x/input.jsonl")
	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info, TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan rollback case: %v", err)
	}

	// Fail the final platform_take insert inside PostgreSQL. Apply has already
	// changed reputation, completed the task, inserted its verdict, advanced both
	// counters, written duration telemetry, and inserted buyer/supplier money in
	// the still-open transaction; the forced statement failure must erase it all.
	if _, err := itPool.Exec(ctx, fmt.Sprintf(`
		CREATE OR REPLACE FUNCTION fail_verification_platform_take_for_test() RETURNS trigger AS $$
		BEGIN
		  IF NEW.task_id=%s::uuid AND NEW.kind='platform_take' THEN
		    RAISE EXCEPTION 'forced verification settlement failure' USING ERRCODE='23503';
		  END IF;
		  RETURN NEW;
		END;
		$$ LANGUAGE plpgsql;
		DROP TRIGGER IF EXISTS fail_verification_platform_take_for_test ON ledger_entries;
		CREATE TRIGGER fail_verification_platform_take_for_test BEFORE INSERT ON ledger_entries
		FOR EACH ROW EXECUTE FUNCTION fail_verification_platform_take_for_test()`, "'"+info.TaskID.String()+"'")); err != nil {
		t.Fatalf("install forced DB failure: %v", err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(), `DROP TRIGGER IF EXISTS fail_verification_platform_take_for_test ON ledger_entries`)
		_, _ = itPool.Exec(context.Background(), `DROP FUNCTION IF EXISTS fail_verification_platform_take_for_test()`)
	})
	_, err = itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	if err == nil {
		t.Fatal("invalid final ledger FK unexpectedly committed")
	}
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || pgErr.Code != "23503" {
		t.Fatalf("forced failure = %T %v, want PostgreSQL foreign-key violation 23503", err, err)
	}
	if rolledBack := readVerificationApplyTestState(t, info); !reflect.DeepEqual(rolledBack, before) {
		t.Fatalf("failed transaction leaked partial effects:\nbefore  =%+v\nrollback=%+v", before, rolledBack)
	}
	if _, err := itPool.Exec(ctx, `DROP TRIGGER fail_verification_platform_take_for_test ON ledger_entries`); err != nil {
		t.Fatalf("remove forced DB failure: %v", err)
	}

	result, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	if err != nil {
		t.Fatalf("retry after DB rollback: %v", err)
	}
	if !result.Applied || result.Rejected {
		t.Fatalf("retry result = %+v", result)
	}
	after := readVerificationApplyTestState(t, info)
	if after.TaskStatus != "complete" || after.TaskOutcome != string(OutcomePass) ||
		after.JobTasksDone != before.JobTasksDone+1 ||
		after.SupplierComplete != before.SupplierComplete+1 ||
		after.SupplierRep != updateReputation(before.SupplierRep, EventTaskSuccess) ||
		after.Verdicts != 1 || after.Durations != 1 || after.LedgerRows != 3 {
		t.Fatalf("retry did not converge to one accepted state: before=%+v after=%+v", before, after)
	}
	replay, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	if err != nil {
		t.Fatalf("post-convergence replay: %v", err)
	}
	if replay.Applied || replay.Rejected {
		t.Fatalf("post-convergence replay result = %+v", replay)
	}
	if replayState := readVerificationApplyTestState(t, info); !reflect.DeepEqual(replayState, after) {
		t.Fatalf("post-convergence replay duplicated an effect:\nfirst =%+v\nreplay=%+v", after, replayState)
	}
}
