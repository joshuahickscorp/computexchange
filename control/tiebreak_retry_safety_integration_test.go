//go:build integration

package main

import (
	"context"
	"testing"

	"github.com/google/uuid"
)

var tiebreakSupplier4UUID = uuid.MustParse("00000000-0000-0000-0000-0000000000a4")

func ensureTiebreakTestSupplier(t *testing.T, ctx context.Context, id uuid.UUID, email string) {
	t.Helper()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO suppliers (id,email,reputation,status)
		VALUES ($1,$2,0.90,'active')
		ON CONFLICT (id) DO UPDATE
		SET reputation=0.90,status='active'`, id, email); err != nil {
		t.Fatalf("ensure tiebreak supplier %s: %v", id, err)
	}
}

func insertTiebreakTestWorker(t *testing.T, ctx context.Context, id, supplier uuid.UUID, class tiebreakVerificationClass) {
	t.Helper()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO workers
		 (id,supplier_id,hw_class,engine,build_hash,memory_gb,effective_memory_gb,bw_gbps,
		  last_seen_at,version,supported_jobs,supported_models,min_payout_usd_hr,thermal_ok,throttled)
		VALUES ($1,$2,$3,$4,$5,64,64,400,now(),'tiebreak-retry-test',
		        ARRAY['embed'],ARRAY['all-minilm-l6-v2'],0,true,false)`,
		id, supplier, class.HWClass, class.Engine, class.BuildHash); err != nil {
		t.Fatalf("insert tiebreak worker %s: %v", id, err)
	}
	replaceWorkerAuthorizationsForTest(t, ctx, id,
		[2]string{"embed", "all-minilm-l6-v2"})
}

func demoTiebreakVerificationClass(t *testing.T, ctx context.Context) tiebreakVerificationClass {
	t.Helper()
	var out tiebreakVerificationClass
	if err := itPool.QueryRow(ctx, `
		SELECT COALESCE(hw_class,''),COALESCE(engine,''),COALESCE(build_hash,'')
		  FROM workers WHERE id=$1`, demoWorkerUUID).
		Scan(&out.HWClass, &out.Engine, &out.BuildHash); err != nil {
		t.Fatalf("read demo verification class: %v", err)
	}
	return out
}

func TestStartedTiebreakRetryCannotBeClaimedByAnyPriorWorkerOrSupplier(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)
	ensureTiebreakTestSupplier(t, ctx, tiebreakSupplier4UUID, "demo-supplier-4@computexchange.test")
	class := demoTiebreakVerificationClass(t, ctx)

	disputant := uuid.New()
	pinned := uuid.New()
	samePrimarySupplier := uuid.New()
	sameDisputantSupplier := uuid.New()
	sameFailedPeerSupplier := uuid.New()
	independentFourth := uuid.New()
	insertTiebreakTestWorker(t, ctx, disputant, demoSupplier2UUID, class)
	insertTiebreakTestWorker(t, ctx, pinned, demoSupplier3UUID, class)
	insertTiebreakTestWorker(t, ctx, samePrimarySupplier, demoSupplierUUID, class)
	insertTiebreakTestWorker(t, ctx, sameDisputantSupplier, demoSupplier2UUID, class)
	insertTiebreakTestWorker(t, ctx, sameFailedPeerSupplier, demoSupplier3UUID, class)
	insertTiebreakTestWorker(t, ctx, independentFourth, tiebreakSupplier4UUID, class)

	jobID := uuid.New()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO jobs
		 (id,buyer_id,status,job_type,model_ref,input_ref,tier,task_count,tasks_done,
		  min_memory_gb,offered_rate_usd_hr)
		VALUES ($1,$2,'verifying','embed','all-minilm-l6-v2','jobs/retry-safe/input.jsonl',
		        'batch',2,2,2,1)`, jobID, demoBuyerUUID); err != nil {
		t.Fatalf("insert tiebreak retry job: %v", err)
	}
	plan := installRawFixtureEconomicPlan(t, ctx, jobID, 2, 1)
	primary, disagreement := uuid.New(), uuid.New()
	for _, row := range []struct {
		id     uuid.UUID
		worker uuid.UUID
		redun  bool
		key    string
	}{
		{primary, demoWorkerUUID, false, "jobs/retry-safe/tasks/0/result.json"},
		{disagreement, disputant, true, "jobs/retry-safe/redundancy/0/result.json"},
	} {
		if _, err := itPool.Exec(ctx, `
			INSERT INTO tasks
			 (id,job_id,status,is_redundancy,input_ref,result_key,result_ref,chunk_index,
			  worker_id,claimed_by,completed_at,economic_buyer_charge_usd,economic_supplier_payout_usd,
			  execution_worker_id,execution_supplier_id,execution_hw_class,
			  execution_engine,execution_build_hash)
			SELECT $1,$2,'complete',$3,'jobs/retry-safe/tasks/0/input.jsonl',$4,$4,0,
			       $5,$5,now(),$6,$7,w.id,w.supplier_id,w.hw_class,w.engine,w.build_hash
			  FROM workers w WHERE w.id=$5`, row.id, jobID, row.redun, row.key, row.worker,
			plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD); err != nil {
			t.Fatalf("insert prior tiebreak vote: %v", err)
		}
	}

	tiebreakID, err := itStore.InsertTiebreakTask(ctx, jobID, disagreement, pinned,
		"jobs/retry-safe/tasks/0/input.jsonl", 0)
	if err != nil {
		t.Fatalf("insert pinned tiebreak: %v", err)
	}
	claim, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: pinned, SupplierID: demoSupplier3UUID})
	if err != nil || claim == nil || claim.TaskID != tiebreakID {
		t.Fatalf("initial independent peer could not start tiebreak: claim=%+v err=%v", claim, err)
	}
	var history int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM task_execution_history
		 WHERE task_id=$1 AND worker_id=$2 AND supplier_id=$3`,
		tiebreakID, pinned, demoSupplier3UUID).Scan(&history); err != nil || history != 1 {
		t.Fatalf("initial tiebreak execution history=%d err=%v", history, err)
	}

	outcome, err := itStore.FailTask(ctx, tiebreakID, pinned, FailureReport{
		Class: "worker_shutdown", Message: "test retry after the third opinion started",
	})
	if err != nil || outcome != FailRequeued {
		t.Fatalf("typed tiebreak failure outcome=%s err=%v", outcome, err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET visible_at=now() WHERE id=$1`, tiebreakID); err != nil {
		t.Fatalf("make typed retry visible: %v", err)
	}

	for _, unsafe := range []struct {
		name     string
		worker   uuid.UUID
		supplier uuid.UUID
	}{
		{"primary disputant", demoWorkerUUID, demoSupplierUUID},
		{"primary supplier sibling", samePrimarySupplier, demoSupplierUUID},
		{"redundancy disputant", disputant, demoSupplier2UUID},
		{"redundancy supplier sibling", sameDisputantSupplier, demoSupplier2UUID},
		{"failed pinned peer", pinned, demoSupplier3UUID},
		{"failed peer supplier sibling", sameFailedPeerSupplier, demoSupplier3UUID},
	} {
		got, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: unsafe.worker, SupplierID: unsafe.supplier})
		if err != nil || got != nil {
			t.Fatalf("%s claimed retried third vote: claim=%+v err=%v", unsafe.name, got, err)
		}
	}
	claim, err = itStore.ClaimTask(ctx, WorkerAuth{WorkerID: independentFourth, SupplierID: tiebreakSupplier4UUID})
	if err != nil || claim == nil || claim.TaskID != tiebreakID {
		t.Fatalf("eligible fourth supplier could not claim retried tiebreak: claim=%+v err=%v", claim, err)
	}
}

func TestPlannedTiebreakPeerTamperCreatesRecoverableUnpinnedTask(t *testing.T) {
	for _, tc := range []struct {
		name   string
		tamper func(t *testing.T, ctx context.Context, worker uuid.UUID)
	}{
		{
			name: "class",
			tamper: func(t *testing.T, ctx context.Context, worker uuid.UUID) {
				t.Helper()
				if _, err := itPool.Exec(ctx, `UPDATE workers SET hw_class='cpu' WHERE id=$1`, worker); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "supplier",
			tamper: func(t *testing.T, ctx context.Context, worker uuid.UUID) {
				t.Helper()
				if _, err := itPool.Exec(ctx, `UPDATE workers SET supplier_id=$2 WHERE id=$1`, worker, demoSupplierUUID); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "capability",
			tamper: func(t *testing.T, ctx context.Context, worker uuid.UUID) {
				t.Helper()
				if _, err := itPool.Exec(ctx, `DELETE FROM worker_authorized_capabilities WHERE worker_id=$1`, worker); err != nil {
					t.Fatal(err)
				}
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			ctx := context.Background()
			ensureExtraDemoSuppliers(t, ctx)
			ensureTiebreakTestSupplier(t, ctx, tiebreakSupplier4UUID, "demo-supplier-4@computexchange.test")

			info, entries, _, _, _ := seedVerificationApplyTestWithEconomics(t, false,
				"jobs/plan-tamper/tasks/0/input.jsonl", 1, 1)
			frozen := demoTiebreakVerificationClass(t, ctx)
			info.HWClass, info.engine, info.buildHash = frozen.HWClass, frozen.Engine, frozen.BuildHash
			if _, err := itPool.Exec(ctx, `
				UPDATE jobs SET model_ref=$2,min_memory_gb=2,offered_rate_usd_hr=1
				 WHERE id=$1`, info.JobID, info.ModelRef); err != nil {
				t.Fatalf("set planned tiebreak job gates: %v", err)
			}
			class := tiebreakVerificationClass{HWClass: info.HWClass, Engine: info.engine, BuildHash: info.buildHash}
			plannedPeer, fallback := uuid.New(), uuid.New()
			insertTiebreakTestWorker(t, ctx, plannedPeer, demoSupplier3UUID, class)
			insertTiebreakTestWorker(t, ctx, fallback, tiebreakSupplier4UUID, class)

			effect := VerificationEffect{
				Kind: VerificationEffectInsertTiebreak, JobID: info.JobID,
				PrimaryTaskID: info.TaskID, PeerWorkerID: plannedPeer,
				InputRef: info.InputRef, ChunkIndex: info.ChunkIndex,
			}
			effect.ID = verificationEffectPayloadID(info.TaskID, info.Attempt, 0, effect)
			effect.TaskID = effect.ID
			decision := VerificationDecision{Outcome: OutcomePass, Effects: []VerificationEffect{effect}}

			tc.tamper(t, ctx, plannedPeer)
			result, err := itStore.ApplyVerificationDecision(ctx, info, decision, entries)
			if err != nil || !result.Applied || result.TiebreaksInserted != 1 {
				t.Fatalf("apply after %s tamper: result=%+v err=%v", tc.name, result, err)
			}
			var claimedBy *uuid.UUID
			var frozenHW, frozenEngine, frozenBuild string
			if err := itPool.QueryRow(ctx, `
				SELECT claimed_by,COALESCE(verification_hw_class,''),
				       COALESCE(verification_engine,''),COALESCE(verification_build_hash,'')
				  FROM tasks WHERE id=$1`, effect.TaskID).
				Scan(&claimedBy, &frozenHW, &frozenEngine, &frozenBuild); err != nil {
				t.Fatalf("read recoverable tiebreak: %v", err)
			}
			if claimedBy != nil || frozenHW != info.HWClass || frozenEngine != info.engine || frozenBuild != info.buildHash {
				t.Fatalf("tampered peer was pinned or class drifted: pin=%v class=(%q,%q,%q)",
					claimedBy, frozenHW, frozenEngine, frozenBuild)
			}

			var plannedSupplier uuid.UUID
			if err := itPool.QueryRow(ctx, `SELECT supplier_id FROM workers WHERE id=$1`, plannedPeer).Scan(&plannedSupplier); err != nil {
				t.Fatal(err)
			}
			unsafeClaim, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: plannedPeer, SupplierID: plannedSupplier})
			if err != nil || unsafeClaim != nil {
				t.Fatalf("tampered planned peer claimed task: claim=%+v err=%v", unsafeClaim, err)
			}
			claim, err := itStore.ClaimTask(ctx, WorkerAuth{WorkerID: fallback, SupplierID: tiebreakSupplier4UUID})
			if err != nil || claim == nil || claim.TaskID != effect.TaskID {
				t.Fatalf("safe fallback did not claim recoverable task: claim=%+v err=%v", claim, err)
			}
		})
	}
}
