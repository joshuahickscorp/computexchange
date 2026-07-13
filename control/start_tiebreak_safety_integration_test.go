//go:build integration

package main

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
)

type directStartTiebreakFixture struct {
	taskID   uuid.UUID
	workerID uuid.UUID
	token    string
}

func seedDirectStartTiebreakFixture(t *testing.T) directStartTiebreakFixture {
	t.Helper()
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)
	class := demoTiebreakVerificationClass(t, ctx)

	disputant, pinned := uuid.New(), uuid.New()
	insertTiebreakTestWorker(t, ctx, disputant, demoSupplier2UUID, class)
	insertTiebreakTestWorker(t, ctx, pinned, demoSupplier3UUID, class)
	token, err := itStore.CreateWorkerToken(ctx, pinned, demoSupplier3UUID)
	if err != nil {
		t.Fatalf("create pinned worker token: %v", err)
	}

	jobID := uuid.New()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO jobs
		 (id,buyer_id,status,job_type,model_ref,input_ref,tier,task_count,tasks_done,
		  min_memory_gb,offered_rate_usd_hr)
		VALUES ($1,$2,'verifying','embed','all-minilm-l6-v2','jobs/direct-start/input.jsonl',
		        'batch',2,2,2,1)`, jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert direct-start job: %v", err)
	}
	plan := installRawFixtureEconomicPlan(t, ctx, jobID, 2, 1)
	primary, disagreement := uuid.New(), uuid.New()
	for _, row := range []struct {
		id     uuid.UUID
		worker uuid.UUID
		redun  bool
		key    string
	}{
		{primary, demoWorkerUUID, false, "jobs/direct-start/tasks/0/result.json"},
		{disagreement, disputant, true, "jobs/direct-start/redundancy/0/result.json"},
	} {
		if _, err := itPool.Exec(ctx, `
			INSERT INTO tasks
			 (id,job_id,status,is_redundancy,input_ref,result_key,result_ref,chunk_index,
			  worker_id,claimed_by,completed_at,economic_buyer_charge_usd,economic_supplier_payout_usd,
			  execution_worker_id,execution_supplier_id,execution_hw_class,
			  execution_engine,execution_build_hash)
			SELECT $1,$2,'complete',$3,'jobs/direct-start/tasks/0/input.jsonl',$4,$4,0,
			       $5,$5,now(),$6,$7,w.id,w.supplier_id,w.hw_class,w.engine,w.build_hash
			  FROM workers w WHERE w.id=$5`, row.id, jobID, row.redun, row.key, row.worker,
			plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD); err != nil {
			t.Fatalf("insert prior direct-start vote: %v", err)
		}
	}

	tiebreakID, err := itStore.InsertTiebreakTask(ctx, jobID, disagreement, pinned,
		"jobs/direct-start/tasks/0/input.jsonl", 0)
	if err != nil {
		t.Fatalf("insert direct-start tiebreak: %v", err)
	}
	return directStartTiebreakFixture{taskID: tiebreakID, workerID: pinned, token: token}
}

func TestDirectStartTiebreakRevalidatesPinnedPeerAndRecordsHistory(t *testing.T) {
	t.Run("class drift rejected", func(t *testing.T) {
		fixture := seedDirectStartTiebreakFixture(t)
		ctx := context.Background()
		// This is the durable worker projection an intervening re-registration would
		// produce. The task remains pinned to the worker id, but that id no longer
		// belongs to the frozen verification class.
		if _, err := itPool.Exec(ctx, `
			UPDATE workers SET engine='vllm',build_hash='direct-start-drift'
			 WHERE id=$1`, fixture.workerID); err != nil {
			t.Fatalf("drift pinned worker class: %v", err)
		}
		code, body := req(t, http.MethodPost,
			"/v1/worker/task/"+fixture.taskID.String()+"/start", nil,
			hdr{"X-Worker-Token", fixture.token})
		if code != http.StatusConflict {
			t.Fatalf("drifted direct start = %d %s, want 409", code, body)
		}
		var status string
		var workerID *uuid.UUID
		var startedAt *time.Time
		var history int
		if err := itPool.QueryRow(ctx, `
			SELECT status,worker_id,started_at,
			       (SELECT count(*) FROM task_execution_history WHERE task_id=t.id)
			  FROM tasks t WHERE id=$1`, fixture.taskID).
			Scan(&status, &workerID, &startedAt, &history); err != nil {
			t.Fatalf("read rejected direct start: %v", err)
		}
		if status != "queued" || workerID != nil || startedAt != nil || history != 0 {
			t.Fatalf("rejected start mutated task: status=%s worker=%v started=%v history=%d",
				status, workerID, startedAt, history)
		}
	})

	t.Run("exact peer starts once", func(t *testing.T) {
		fixture := seedDirectStartTiebreakFixture(t)
		ctx := context.Background()
		start := func() {
			t.Helper()
			code, body := req(t, http.MethodPost,
				"/v1/worker/task/"+fixture.taskID.String()+"/start", nil,
				hdr{"X-Worker-Token", fixture.token})
			if code != http.StatusNoContent {
				t.Fatalf("valid direct start = %d %s, want 204", code, body)
			}
		}
		start()
		start() // exact idempotent acknowledgement; history must not duplicate.

		var status, parentStatus string
		var workerID uuid.UUID
		var startedAt time.Time
		var history int
		if err := itPool.QueryRow(ctx, `
			SELECT t.status,t.worker_id,t.started_at,j.status,
			       (SELECT count(*) FROM task_execution_history h
			         WHERE h.task_id=t.id AND h.attempt=COALESCE(t.retry_count,0)
			           AND h.worker_id=$2 AND h.supplier_id=$3)
			  FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=$1`,
			fixture.taskID, fixture.workerID, demoSupplier3UUID).
			Scan(&status, &workerID, &startedAt, &parentStatus, &history); err != nil {
			t.Fatalf("read accepted direct start: %v", err)
		}
		if status != "running" || workerID != fixture.workerID || startedAt.IsZero() ||
			parentStatus != "running" || history != 1 {
			t.Fatalf("accepted start state: status=%s worker=%s started=%s parent=%s history=%d",
				status, workerID, startedAt, parentStatus, history)
		}
	})
}
