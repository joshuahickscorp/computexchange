//go:build integration

package main

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func createFrozenEconomicTestJob(t *testing.T, initialTasks, reserve int, slaPremium float64) (uuid.UUID, []uuid.UUID, EconomicPlan) {
	t.Helper()
	ctx := context.Background()
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD:   float64(initialTasks),
		InitialTaskCount: initialTasks,
		ExtraTaskReserve: reserve,
		SupplierShare:    supplierShareRate,
		SLAPremiumUSD:    slaPremium,
	}, testEconomicSchedule())
	if !plan.Executable {
		t.Fatalf("test economic plan blocked: %s", plan.BlockReason)
	}
	jobID := uuid.New()
	taskIDs := make([]uuid.UUID, initialTasks)
	tasks := make([]taskRow, initialTasks)
	for i := range tasks {
		taskIDs[i] = uuid.New()
		tasks[i] = taskRow{
			ID: taskIDs[i], JobID: jobID,
			InputRef: "jobs/economic/input.jsonl", ResultKey: "jobs/economic/result-" + taskIDs[i].String() + ".json",
			ChunkIndex: i,
		}
	}
	job := &jobRow{
		ID: jobID, BuyerID: demoBuyerUUID, JobType: "embed", ModelRef: "all-minilm-l6-v2",
		InputRef: "jobs/economic/input.jsonl", OutputRef: "jobs/economic/output.jsonl",
		Tier: "batch", VerificationPolicy: []byte(`{}`), TaskCount: initialTasks,
		EstimatedUSD: plan.InitialBuyerChargeUSD, SLAPremiumUSD: slaPremium,
		EconomicPlan: plan,
	}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("CreateJobWithTasks: %v", err)
	}
	return jobID, taskIDs, plan
}

func TestEconomicPlanAndTaskAmountsPersistAtomicallyAndStayImmutable(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, tasks, plan := createFrozenEconomicTestJob(t, 2, 2, .25)

	var planJSON []byte
	var scheduleVersion string
	var reserved, consumed int
	if err := itPool.QueryRow(ctx, `
		SELECT p.plan_json,p.schedule_version,r.reserved_tasks,r.consumed_tasks
		  FROM job_economic_plans p JOIN job_economic_reserves r ON r.job_id=p.job_id
		 WHERE p.job_id=$1`, jobID).Scan(&planJSON, &scheduleVersion, &reserved, &consumed); err != nil {
		t.Fatalf("reading persisted plan: %v", err)
	}
	var persisted EconomicPlan
	if err := json.Unmarshal(planJSON, &persisted); err != nil {
		t.Fatal(err)
	}
	if !EconomicPlansEqual(plan, persisted) || scheduleVersion != plan.Schedule.Version || reserved != 2 || consumed != 0 {
		t.Fatalf("persisted plan/reserve drift: plan=%+v version=%q reserve=%d/%d", persisted, scheduleVersion, consumed, reserved)
	}
	for _, taskID := range tasks {
		buyerCharge, supplierPayout, err := itStore.TaskEconomicAmounts(ctx, taskID)
		if err != nil {
			t.Fatal(err)
		}
		if buyerCharge != plan.BuyerChargePerTaskUSD || supplierPayout != plan.SupplierPayoutPerTaskUSD {
			t.Fatalf("task %s amounts=(%.6f,%.6f), plan=(%.6f,%.6f)", taskID, buyerCharge, supplierPayout, plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD)
		}
	}

	if _, err := itPool.Exec(ctx, `UPDATE job_economic_plans SET buyer_charge_per_task_usd=buyer_charge_per_task_usd+.01 WHERE job_id=$1`, jobID); err == nil {
		t.Fatal("immutable job economic plan accepted an update")
	}
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET economic_supplier_payout_usd=economic_supplier_payout_usd+.01 WHERE id=$1`, tasks[0]); err == nil {
		t.Fatal("frozen task payout accepted an update")
	}
}

func TestEconomicReserveConcurrentTiebreakIsBoundedAndIdempotent(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, tasks, plan := createFrozenEconomicTestJob(t, 1, 1, 0)
	primary := tasks[0]
	ensureExtraDemoSuppliers(t, ctx)
	class := demoTiebreakVerificationClass(t, ctx)
	peerWorker := uuid.New()
	insertTiebreakTestWorker(t, ctx, peerWorker, demoSupplier2UUID, class)
	// A third opinion must originate from a real completed anchor execution. Freeze
	// that execution identity before creating the tiebreak; a queued task with no
	// worker is not an admissible source of verification class or independence.
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET status='running',worker_id=$2,claimed_by=$2,claimed_at=now(),started_at=now(),
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, primary, demoWorkerUUID); err != nil {
		t.Fatalf("freeze tiebreak anchor execution: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET status='complete',completed_at=now(),result_ref=result_key
		 WHERE id=$1 AND status='running'`, primary); err != nil {
		t.Fatalf("complete tiebreak anchor execution: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='verifying',tasks_done=1 WHERE id=$1`, jobID); err != nil {
		t.Fatalf("freeze tiebreak anchor parent: %v", err)
	}

	const callers = 12
	ids := make([]uuid.UUID, callers)
	errs := make([]error, callers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			ids[i], errs[i] = itStore.InsertTiebreakTask(ctx, jobID, primary, peerWorker, "jobs/economic/input.jsonl", 0)
		}(i)
	}
	close(start)
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("creator %d: %v", i, err)
		}
		if ids[i] != ids[0] {
			t.Fatalf("creator %d returned %s; first returned %s", i, ids[i], ids[0])
		}
	}

	var taskRows, consumed, jobTaskCount int
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM tasks WHERE job_id=$1 AND is_redundancy=true AND hedged_from IS NOT NULL),
		       (SELECT consumed_tasks FROM job_economic_reserves WHERE job_id=$1),
		       (SELECT task_count FROM jobs WHERE id=$1)`, jobID).Scan(&taskRows, &consumed, &jobTaskCount); err != nil {
		t.Fatal(err)
	}
	if taskRows != 1 || consumed != 1 || jobTaskCount != 2 {
		t.Fatalf("dynamic rows=%d reserve consumed=%d task_count=%d; want 1,1,2", taskRows, consumed, jobTaskCount)
	}
	buyerCharge, supplierPayout, err := itStore.TaskEconomicAmounts(ctx, ids[0])
	if err != nil {
		t.Fatal(err)
	}
	if buyerCharge != plan.BuyerChargePerTaskUSD || supplierPayout != plan.SupplierPayoutPerTaskUSD {
		t.Fatalf("dynamic task did not inherit frozen plan amounts: %.6f/%.6f", buyerCharge, supplierPayout)
	}
	if _, err := itStore.InsertHedgeTask(ctx, jobID, primary, demoWorkerUUID, "jobs/economic/input.jsonl", 0); !errors.Is(err, ErrEconomicReserveExhausted) {
		t.Fatalf("shared reserve admitted a hedge after the only slot was consumed: %v", err)
	}
}

func TestConcurrentPrimaryAndHedgeSettlementHasOneMoneyWinner(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, tasks, plan := createFrozenEconomicTestJob(t, 1, 1, 0)
	primary := tasks[0]
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET status='running',claimed_by=$2,worker_id=$2,started_at=now() WHERE id=$1`,
		primary, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	hedge, err := itStore.InsertHedgeTask(ctx, jobID, primary, demoWorkerUUID, "jobs/economic/input.jsonl", 0)
	if err != nil {
		t.Fatalf("InsertHedgeTask: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE tasks SET status='verifying',worker_id=$2 WHERE id=ANY($1)`, []uuid.UUID{primary, hedge}, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}

	ids := []uuid.UUID{primary, hedge}
	finalErrs := make([]error, 2)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i, taskID := range ids {
		wg.Add(1)
		go func(i int, taskID uuid.UUID) {
			defer wg.Done()
			<-start
			info := &CommitTaskInfo{
				TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID,
				jobType: "embed", SplitSize: 1, DurationMS: 10,
			}
			entries := splitFrozenCharge(demoBuyerUUID, demoSupplierUUID, taskID,
				plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD, 0, timeZero)
			finalErrs[i] = itStore.FinalizeTaskVerification(ctx, info, OutcomePass, entries)
		}(i, taskID)
	}
	close(start)
	wg.Wait()
	for i, err := range finalErrs {
		if err != nil {
			t.Fatalf("finalizer %d: %v", i, err)
		}
	}

	var buyerRows, supplierRows, platformRows int
	var buyerTotal, supplierTotal float64
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE kind='buyer_charge'),
		       count(*) FILTER (WHERE kind='supplier_credit'),
		       count(*) FILTER (WHERE kind='platform_take'),
		       COALESCE(SUM(-amount_usd) FILTER (WHERE kind='buyer_charge'),0)::float8,
		       COALESCE(SUM(amount_usd) FILTER (WHERE kind='supplier_credit'),0)::float8
		  FROM ledger_entries WHERE task_id=ANY($1)`, ids).
		Scan(&buyerRows, &supplierRows, &platformRows, &buyerTotal, &supplierTotal); err != nil {
		t.Fatal(err)
	}
	if buyerRows != 1 || supplierRows != 1 || platformRows != 1 {
		t.Fatalf("primary+hedge wrote %d buyer/%d supplier/%d platform rows; want one money winner", buyerRows, supplierRows, platformRows)
	}
	if math.Abs(buyerTotal-plan.BuyerChargePerTaskUSD) > 0.000001 || math.Abs(supplierTotal-plan.SupplierPayoutPerTaskUSD) > 0.000001 {
		t.Fatalf("winner totals buyer=%.6f supplier=%.6f plan=%.6f/%.6f", buyerTotal, supplierTotal, plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD)
	}
}

func TestSLAPremiumIsOnceOnlyBuyerRevenueAndNeverSupplierPayout(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, tasks, plan := createFrozenEconomicTestJob(t, 1, 0, .40)
	taskID := tasks[0]
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='running' WHERE id=$1`, jobID); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET status='verifying',claimed_by=$2,worker_id=$2 WHERE id=$1`,
		taskID, demoWorkerUUID); err != nil {
		t.Fatal(err)
	}
	info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID, jobType: "embed", SplitSize: 1, DurationMS: 10}
	entries := splitFrozenCharge(demoBuyerUUID, demoSupplierUUID, taskID,
		plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD, 0, timeZero)
	if err := itStore.FinalizeTaskVerification(ctx, info, OutcomePass, entries); err != nil {
		t.Fatal(err)
	}
	if err := itStore.CompleteJobEconomics(ctx, jobID); err != nil {
		t.Fatal(err)
	}
	if err := itStore.CompleteJobEconomics(ctx, jobID); err != nil {
		t.Fatalf("idempotent completion retry: %v", err)
	}

	var premiumRows, supplierRows int
	var actualUSD, supplierUSD float64
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM ledger_entries WHERE kind='buyer_charge' AND task_id IS NULL AND payout_ref=$2),
		       (SELECT count(*) FROM ledger_entries WHERE kind='supplier_credit' AND task_id=$3),
		       actual_usd::float8,
		       (SELECT COALESCE(SUM(amount_usd),0)::float8 FROM ledger_entries WHERE kind='supplier_credit' AND task_id=$3)
		  FROM jobs WHERE id=$1`, jobID, slaPremiumChargeRef(jobID), taskID).
		Scan(&premiumRows, &supplierRows, &actualUSD, &supplierUSD); err != nil {
		t.Fatal(err)
	}
	if premiumRows != 1 || supplierRows != 1 {
		t.Fatalf("premium rows=%d supplier rows=%d; want one each", premiumRows, supplierRows)
	}
	if math.Abs(actualUSD-plan.InitialBuyerChargeUSD) > 0.000001 || math.Abs(supplierUSD-plan.SupplierPayoutPerTaskUSD) > 0.000001 {
		t.Fatalf("actual/supplier=%.6f/%.6f plan=%.6f/%.6f", actualUSD, supplierUSD, plan.InitialBuyerChargeUSD, plan.SupplierPayoutPerTaskUSD)
	}
}

func economicQuoteSubmitBody(quoteID string) map[string]any {
	body := map[string]any{
		"job_type": map[string]any{"type": "embed"},
		"model":    map[string]any{"kind": "hf", "ref": "all-minilm-l6-v2"},
		"params":   map[string]any{"split_size": 1000},
		"verification": map[string]any{
			"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true,
		},
		"tier":  "batch",
		"input": "{\"id\":\"economic-parity\",\"text\":\"hello\"}\n",
	}
	if quoteID != "" {
		body["quote_id"] = quoteID
	}
	return body
}

func TestQuoteSubmitEconomicParityAndTamperRejection(t *testing.T) {
	reset(t)
	ctx := context.Background()
	code, out := req(t, "POST", "/v1/quote", economicQuoteSubmitBody(""), buyerKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("quote: %d %s", code, out)
	}
	var quote Quote
	if err := json.Unmarshal(out, &quote); err != nil {
		t.Fatal(err)
	}
	if err := ValidateEconomicPlanSnapshot(quote.Economics); err != nil {
		t.Fatalf("quote omitted executable reproducible economics: %v", err)
	}
	if quote.Cost.ExpectedUSD != quote.Economics.InitialBuyerChargeUSD || quote.Cost.MaxUSD != quote.Economics.ReservedBuyerChargeUSD {
		t.Fatalf("quote cost band and economics disagree: cost=%+v economics=%+v", quote.Cost, quote.Economics)
	}
	code, out = req(t, "POST", "/v1/jobs", economicQuoteSubmitBody(quote.QuoteID), buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("bound submit: %d %s", code, out)
	}
	var submit JobSubmitResponse
	if err := json.Unmarshal(out, &submit); err != nil {
		t.Fatal(err)
	}
	var persistedJSON []byte
	if err := itPool.QueryRow(ctx, `SELECT plan_json FROM job_economic_plans WHERE job_id=$1`, submit.JobID).Scan(&persistedJSON); err != nil {
		t.Fatal(err)
	}
	var persisted EconomicPlan
	if err := json.Unmarshal(persistedJSON, &persisted); err != nil {
		t.Fatal(err)
	}
	if !EconomicPlansEqual(quote.Economics, persisted) {
		t.Fatalf("quote/submit plans differ:\nquote=%+v\nsubmit=%+v", quote.Economics, persisted)
	}

	code, out = req(t, "POST", "/v1/quote", economicQuoteSubmitBody(""), buyerKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("tamper quote: %d %s", code, out)
	}
	var tampered Quote
	if err := json.Unmarshal(out, &tampered); err != nil {
		t.Fatal(err)
	}
	qid, err := quoteIDToUUID(tampered.QuoteID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE quotes
		   SET economic_plan=jsonb_set(economic_plan,'{supplier_payout_per_task_usd}',
		       to_jsonb(((economic_plan->>'supplier_payout_per_task_usd')::numeric + .01)))
		 WHERE id=$1`, qid); err != nil {
		t.Fatal(err)
	}
	code, out = req(t, "POST", "/v1/jobs", economicQuoteSubmitBody(tampered.QuoteID), buyerKey(), jsonCT())
	if code != http.StatusConflict {
		t.Fatalf("tampered quote submit: want 409, got %d %s", code, out)
	}
}

func TestQuoteFailsClosedWhenEconomicScheduleIsMissing(t *testing.T) {
	reset(t)
	t.Setenv(economicScheduleVersionEnv, "")
	code, out := req(t, "POST", "/v1/quote", economicQuoteSubmitBody(""), buyerKey(), jsonCT())
	if code != http.StatusServiceUnavailable {
		t.Fatalf("missing schedule: want 503, got %d %s", code, out)
	}
}

var timeZero = time.Unix(0, 0)
