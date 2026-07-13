//go:build integration

package main

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestMigrateReconcilesLegacyVerifyingTaskWithoutInventingAuthority(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/legacy/tasks/0/input.jsonl")
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='verifying',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       result_ref=result_key,result_sha256=repeat('a',64),reported_duration_ms=999,
		       reported_tokens_used=23,reported_hardware_temp_c=61.5
		 WHERE id=$1`, taskID, demoWorkerUUID); err != nil {
		t.Fatalf("seed legacy verifying projection: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='verifying' WHERE id=$1`, jobID); err != nil {
		t.Fatalf("seed legacy parent: %v", err)
	}

	// Migrate is the production startup entry point. Its reconciliation must be
	// enough by itself; no HTTP retry or operator mutation is allowed in this test.
	if err := itStore.Migrate(ctx); err != nil {
		t.Fatalf("startup migration/reconciliation: %v", err)
	}
	var (
		status, jobStatus                   string
		retry                               int
		claimed, worker                     *uuid.UUID
		resultRef, resultSHA, outcome       *string
		duration, tokens                    *int64
		verdicts, ledger, work, events      int
		eventKind, eventPolicy, eventReason string
		eventRecoveredAttempt               int
	)
	if err := itPool.QueryRow(ctx, `
		SELECT status,retry_count,claimed_by,worker_id,result_ref,result_sha256,
		       reported_duration_ms,reported_tokens_used,verification_outcome
		  FROM tasks WHERE id=$1`, taskID).
		Scan(&status, &retry, &claimed, &worker, &resultRef, &resultSHA,
			&duration, &tokens, &outcome); err != nil {
		t.Fatalf("read reconciled task: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT status FROM jobs WHERE id=$1`, jobID).Scan(&jobStatus); err != nil {
		t.Fatalf("read reconciled job: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM task_verdicts WHERE task_id=$1),
		       (SELECT count(*) FROM ledger_entries WHERE task_id=$1),
		       (SELECT count(*) FROM verification_work WHERE task_id=$1),
		       (SELECT count(*) FROM job_events WHERE task_id=$1)`, taskID).
		Scan(&verdicts, &ledger, &work, &events); err != nil {
		t.Fatalf("read reconciliation authority rows: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT event,detail->>'policy',detail->>'reason',(detail->>'recovered_attempt')::int
		  FROM job_events WHERE task_id=$1`, taskID).
		Scan(&eventKind, &eventPolicy, &eventReason, &eventRecoveredAttempt); err != nil {
		t.Fatalf("read reconciliation event: %v", err)
	}
	if status != "retrying" || retry != 1 || claimed != nil || worker != nil ||
		resultRef != nil || resultSHA != nil || duration != nil || tokens != nil || outcome != nil ||
		jobStatus != "running" {
		t.Fatalf("legacy reconciliation state task=%s retry=%d claim=%v worker=%v result=%v/%v duration=%v tokens=%v outcome=%v job=%s",
			status, retry, claimed, worker, resultRef, resultSHA, duration, tokens, outcome, jobStatus)
	}
	if verdicts != 0 || ledger != 0 || work != 0 || events != 1 ||
		eventKind != "verification_recovered" || eventPolicy != "retry_without_settlement_v1" ||
		eventReason != "missing_verification_work" || eventRecoveredAttempt != 0 {
		t.Fatalf("legacy reconciliation authority verdicts=%d ledger=%d work=%d events=%d event=%s/%s/%s/%d",
			verdicts, ledger, work, events, eventKind, eventPolicy, eventReason, eventRecoveredAttempt)
	}
	if n, err := itStore.ReconcileLegacyVerifyingTasks(ctx); err != nil || n != 0 {
		t.Fatalf("idempotent reconciliation = %d, %v; want 0,nil", n, err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM job_events WHERE task_id=$1`, taskID).Scan(&events); err != nil || events != 1 {
		t.Fatalf("idempotent reconciliation duplicated event: events=%d err=%v", events, err)
	}
}

func TestVerificationApplyRejectsAlreadyTerminalParentWithoutEffects(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, entries, _ := seedVerificationApplyTest(t, false, "jobs/terminal-fence/input.jsonl")
	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info,
		TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan terminal-parent case: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='failed' WHERE id=$1`, info.JobID); err != nil {
		t.Fatalf("seed terminal parent: %v", err)
	}
	before := readVerificationApplyTestState(t, info)
	if _, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries); !errors.Is(err, ErrVerificationReplayConflict) {
		t.Fatalf("apply against terminal parent = %v, want replay conflict", err)
	}
	after := readVerificationApplyTestState(t, info)
	if !reflect.DeepEqual(after, before) {
		t.Fatalf("terminal parent leaked apply effects:\nbefore=%+v\nafter =%+v", before, after)
	}
}

func TestConcurrentVerificationApplyAndTerminalFailureConvergeWithoutOrphan(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, entries, _, _, taskIDs := seedVerificationApplyTestWithEconomics(t, false,
		"jobs/fail-race/input.jsonl", 2, 0)
	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info,
		TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan fail race: %v", err)
	}
	failingTask := taskIDs[1]
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET status='running',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, failingTask, demoWorkerUUID); err != nil {
		t.Fatalf("seed concurrently failing task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, info.JobID); err != nil {
		t.Fatalf("seed running parent: %v", err)
	}

	start := make(chan struct{})
	var wg sync.WaitGroup
	var applyResult VerificationApplyResult
	var applyErr, failErr error
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		applyResult, applyErr = itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	}()
	go func() {
		defer wg.Done()
		<-start
		failErr = itStore.FailTaskAndSettleJob(ctx, failingTask, info.JobID)
	}()
	close(start)
	wg.Wait()
	if applyErr != nil || !applyResult.Applied {
		t.Fatalf("verification lost concurrent failure fence: result=%+v err=%v failErr=%v", applyResult, applyErr, failErr)
	}
	if failErr != nil && !errors.Is(failErr, ErrJobVerificationPending) {
		t.Fatalf("terminal failure race returned unexpected error: %v", failErr)
	}
	// If failure reached the parent while verification was pending it rolled back
	// and explicitly asks for retry. Once apply is terminal, that retry may fail the
	// genuinely failed sibling without touching the accepted chunk or its money.
	if errors.Is(failErr, ErrJobVerificationPending) {
		if err := itStore.FailTaskAndSettleJob(ctx, failingTask, info.JobID); err != nil {
			t.Fatalf("terminal failure retry after verification: %v", err)
		}
	}
	var jobStatus, acceptedStatus, failedStatus string
	var verdicts, durations, ledger int
	if err := itPool.QueryRow(ctx, `
		SELECT j.status,a.status,f.status,
		       (SELECT count(*) FROM task_verdicts WHERE task_id=a.id),
		       (SELECT count(*) FROM task_durations WHERE task_id=a.id),
		       (SELECT count(*) FROM ledger_entries WHERE task_id=a.id)
		  FROM jobs j JOIN tasks a ON a.id=$2 JOIN tasks f ON f.id=$3
		 WHERE j.id=$1`, info.JobID, info.TaskID, failingTask).
		Scan(&jobStatus, &acceptedStatus, &failedStatus, &verdicts, &durations, &ledger); err != nil {
		t.Fatalf("read failure race state: %v", err)
	}
	if jobStatus != "failed" || acceptedStatus != "complete" || failedStatus != "failed" ||
		verdicts != 1 || durations != 1 || ledger != 3 {
		t.Fatalf("failure race state job=%s accepted=%s failed=%s verdicts=%d durations=%d ledger=%d",
			jobStatus, acceptedStatus, failedStatus, verdicts, durations, ledger)
	}
}

func TestConcurrentVerificationApplyAndCancellationNeverCancelUnsettledAttempt(t *testing.T) {
	reset(t)
	ctx := context.Background()
	info, entries, _ := seedVerificationApplyTest(t, false, "jobs/cancel-race/input.jsonl")
	decision, err := NewVerifier(itStore).PlanTaskResult(ctx, info,
		TaskCommit{TaskID: info.TaskID}, []byte(`{"vectors":[[1,0,0]]}`), nil)
	if err != nil {
		t.Fatalf("plan cancel race: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, info.JobID); err != nil {
		t.Fatalf("seed cancellable parent: %v", err)
	}

	start := make(chan struct{})
	var wg sync.WaitGroup
	var applyResult VerificationApplyResult
	var applyErr, cancelErr error
	var cancelled bool
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		applyResult, applyErr = itStore.ApplyVerificationDecision(ctx, info, decision, entries)
	}()
	go func() {
		defer wg.Done()
		<-start
		cancelled, cancelErr = itStore.CancelStuckJob(ctx, info.JobID)
	}()
	close(start)
	wg.Wait()
	if applyErr != nil || !applyResult.Applied || cancelErr != nil {
		t.Fatalf("apply/cancel race result=%+v applyErr=%v cancelled=%v cancelErr=%v",
			applyResult, applyErr, cancelled, cancelErr)
	}
	if !cancelled {
		var err error
		cancelled, err = itStore.CancelStuckJob(ctx, info.JobID)
		if err != nil || !cancelled {
			t.Fatalf("cancel retry after terminal verification = %v,%v", cancelled, err)
		}
	}
	var taskStatus, jobStatus, workStatus string
	var verdicts, ledger int
	if err := itPool.QueryRow(ctx, `
		SELECT t.status,j.status,
		       COALESCE((SELECT max(status) FROM verification_work WHERE task_id=t.id),''),
		       (SELECT count(*) FROM task_verdicts WHERE task_id=t.id),
		       (SELECT count(*) FROM ledger_entries WHERE task_id=t.id)
		  FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=$1`, info.TaskID).
		Scan(&taskStatus, &jobStatus, &workStatus, &verdicts, &ledger); err != nil {
		t.Fatalf("read cancellation race state: %v", err)
	}
	if taskStatus != "complete" || jobStatus != "cancelled" || workStatus != "" || verdicts != 1 || ledger != 3 {
		t.Fatalf("cancel race state task=%s job=%s work=%s verdicts=%d ledger=%d",
			taskStatus, jobStatus, workStatus, verdicts, ledger)
	}
}

func TestPinnedTiebreakRecoveryReselectsIndependentSameClassPeer(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)

	var engine, build string
	if err := itPool.QueryRow(ctx, `SELECT COALESCE(engine,''),COALESCE(build_hash,'') FROM workers WHERE id=$1`, demoWorkerUUID).
		Scan(&engine, &build); err != nil {
		t.Fatalf("read anchor verification class: %v", err)
	}
	insertPeer := func(id, supplier uuid.UUID, live bool) {
		t.Helper()
		lastSeen := time.Now()
		if !live {
			lastSeen = time.Now().Add(-time.Hour)
		}
		if _, err := itPool.Exec(ctx, `
			INSERT INTO workers
			 (id,supplier_id,hw_class,engine,build_hash,memory_gb,effective_memory_gb,bw_gbps,
			  last_seen_at,version,supported_jobs,supported_models,min_payout_usd_hr,thermal_ok)
			VALUES ($1,$2,'apple_silicon_max',$3,$4,64,64,400,$5,'lifecycle-test',
			        ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true)`,
			id, supplier, engine, build, lastSeen); err != nil {
			t.Fatalf("insert lifecycle peer %s: %v", id, err)
		}
		replaceWorkerAuthorizationsForTest(t, ctx, id,
			[2]string{"embed", "all-minilm-l6-v2"})
	}
	disputant := uuid.New()
	deadPinned := uuid.New()
	ineligible := uuid.New()
	replacement := uuid.New()
	insertPeer(disputant, demoSupplier2UUID, true)
	insertPeer(deadPinned, demoSupplier3UUID, false)
	insertPeer(ineligible, demoSupplier3UUID, true)
	insertPeer(replacement, demoSupplier3UUID, true)
	if _, err := itPool.Exec(ctx, `UPDATE workers SET min_payout_usd_hr=1000 WHERE id=$1`, ineligible); err != nil {
		t.Fatalf("make top-ranked peer payout-ineligible: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO benchmark_results (worker_id,job_type,tps,thermal_ok)
		VALUES ($1,'embed',1000,true)`, ineligible); err != nil {
		t.Fatalf("rank payout-ineligible peer first: %v", err)
	}

	jobID := uuid.New()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO jobs
		 (id,buyer_id,status,job_type,model_ref,input_ref,tier,task_count,tasks_done,
		  min_memory_gb,offered_rate_usd_hr)
		VALUES ($1,$2,'verifying','embed','all-minilm-l6-v2','jobs/pin/input.jsonl','batch',2,2,2,1)`,
		jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert pinned recovery job: %v", err)
	}
	plan := installRawFixtureEconomicPlan(t, ctx, jobID, 2, 1)
	primary, disagreement := uuid.New(), uuid.New()
	for _, row := range []struct {
		id     uuid.UUID
		worker uuid.UUID
		redun  bool
		key    string
	}{
		{primary, demoWorkerUUID, false, "jobs/pin/tasks/0/result.json"},
		{disagreement, disputant, true, "jobs/pin/redundancy/0/result.json"},
	} {
		if _, err := itPool.Exec(ctx, `
			INSERT INTO tasks
			 (id,job_id,status,is_redundancy,input_ref,result_key,result_ref,chunk_index,
			  worker_id,claimed_by,completed_at,economic_buyer_charge_usd,economic_supplier_payout_usd,
			  execution_worker_id,execution_supplier_id,execution_hw_class,
			  execution_engine,execution_build_hash)
			SELECT $1,$2,'complete',$3,'jobs/pin/tasks/0/input.jsonl',$4,$4,0,
			       $5,$5,now(),$6,$7,w.id,w.supplier_id,w.hw_class,w.engine,w.build_hash
			  FROM workers w WHERE w.id=$5`, row.id, jobID, row.redun, row.key, row.worker,
			plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD); err != nil {
			t.Fatalf("insert prior chunk execution: %v", err)
		}
	}
	tiebreakID, err := itStore.InsertTiebreakTask(ctx, jobID, disagreement, deadPinned,
		"jobs/pin/tasks/0/input.jsonl", 0)
	if err != nil {
		t.Fatalf("insert pinned tiebreak: %v", err)
	}
	var insertedJobStatus string
	var taskCount int
	if err := itPool.QueryRow(ctx, `SELECT status,task_count FROM jobs WHERE id=$1`, jobID).
		Scan(&insertedJobStatus, &taskCount); err != nil {
		t.Fatalf("read tiebreak parent: %v", err)
	}
	if insertedJobStatus != "running" || taskCount != 3 {
		t.Fatalf("tiebreak insertion left parent unrunnable: status=%s task_count=%d", insertedJobStatus, taskCount)
	}
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET claimed_at=now()-interval '10 minutes' WHERE id=$1`, tiebreakID); err != nil {
		t.Fatalf("age pinned tiebreak: %v", err)
	}
	stale, err := itStore.StalePinnedTiebreaks(ctx, pinnedTiebreakTimeout, 10)
	if err != nil || len(stale) != 1 {
		t.Fatalf("read stale pinned fixture: rows=%+v err=%v", stale, err)
	}
	anchor, also, alsoSuppliers, err := itStore.PinnedTiebreakExclusions(ctx, stale[0])
	if err != nil {
		t.Fatalf("read pinned exclusions: %v", err)
	}
	top, err := itStore.SelectRedundancyPeerExcluding(ctx, stale[0].JobType, stale[0].ModelRef,
		stale[0].MinMemoryGB, anchor, also, alsoSuppliers)
	if err != nil || top != ineligible {
		t.Fatalf("fixture did not rank unclaimable peer first: top=%s want=%s err=%v", top, ineligible, err)
	}

	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.recoverPinnedTiebreaks(ctx); err != nil {
		t.Fatalf("recover pinned tiebreak: %v", err)
	}
	var pinned uuid.UUID
	var claimedAt time.Time
	var retry int
	if err := itPool.QueryRow(ctx, `SELECT claimed_by,claimed_at,retry_count FROM tasks WHERE id=$1`, tiebreakID).
		Scan(&pinned, &claimedAt, &retry); err != nil {
		t.Fatalf("read recovered tiebreak: %v", err)
	}
	if pinned != replacement || retry != 0 || time.Since(claimedAt) > time.Minute {
		t.Fatalf("recovered tiebreak pin=%s retry=%d claimed_at=%s; want live replacement %s without retry burn",
			pinned, retry, claimedAt, replacement)
	}
	var recoveryEvents int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM job_events WHERE task_id=$1 AND event='tiebreak_reassigned'`, tiebreakID).
		Scan(&recoveryEvents); err != nil || recoveryEvents != 1 {
		t.Fatalf("pinned recovery events=%d err=%v", recoveryEvents, err)
	}
	if err := wk.recoverPinnedTiebreaks(ctx); err != nil {
		t.Fatalf("idempotent pinned recovery: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM job_events WHERE task_id=$1 AND event='tiebreak_reassigned'`, tiebreakID).
		Scan(&recoveryEvents); err != nil || recoveryEvents != 1 {
		t.Fatalf("idempotent pinned recovery duplicated event: events=%d err=%v", recoveryEvents, err)
	}

	oldClaim, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: deadPinned, SupplierID: demoSupplier3UUID})
	if err != nil || oldClaim != nil {
		t.Fatalf("superseded worker claimed recovered tiebreak: claim=%+v err=%v", oldClaim, err)
	}
	newClaim, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: replacement, SupplierID: demoSupplier3UUID})
	if err != nil || newClaim == nil || newClaim.TaskID != tiebreakID {
		t.Fatalf("replacement could not claim recovered tiebreak: claim=%+v err=%v", newClaim, err)
	}
	var sameClass, distinctFromVotes bool
	if err := itPool.QueryRow(ctx, `
		SELECT (nw.hw_class,nw.engine,nw.build_hash) IS NOT DISTINCT FROM
		       (aw.hw_class,aw.engine,aw.build_hash),
		       nw.supplier_id<>ALL(ARRAY[$3::uuid,$4::uuid])
		  FROM workers nw JOIN workers aw ON aw.id=$2 WHERE nw.id=$1`,
		replacement, disputant, demoSupplierUUID, demoSupplier2UUID).
		Scan(&sameClass, &distinctFromVotes); err != nil {
		t.Fatalf("read recovered peer independence: %v", err)
	}
	if !sameClass || !distinctFromVotes {
		t.Fatalf("recovered peer weakened verification class/independence: sameClass=%v distinct=%v",
			sameClass, distinctFromVotes)
	}
}
