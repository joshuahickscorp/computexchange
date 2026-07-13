//go:build integration

package main

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestEconomicFactsPersistExactStreamingInputAndMergedOutputUnits(t *testing.T) {
	reset(t)
	ctx := context.Background()
	input := "{\"id\":\"1\",\"text\":\"a\"}\r\n \n{\"id\":\"2\",\"text\":\"bb\"}"
	body := map[string]any{
		"job_type":    map[string]any{"type": "embed"},
		"model":       map[string]any{"kind": "hf", "ref": "all-minilm-l6-v2"},
		"params":      map[string]any{"split_size": 1000},
		"constraints": map[string]any{"min_memory_gb": 2},
		"verification": map[string]any{
			"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true,
		},
		"tier":  "batch",
		"input": input,
	}
	code, raw := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("submit: %d %s", code, raw)
	}
	var submit JobSubmitResponse
	if err := json.Unmarshal(raw, &submit); err != nil {
		t.Fatalf("decode submit: %v", err)
	}
	var inputRecords, inputBytes int64
	var inputSource string
	if err := itPool.QueryRow(ctx, `
		SELECT economic_input_records,economic_input_bytes,economic_input_source
		  FROM jobs WHERE id=$1`, submit.JobID).Scan(&inputRecords, &inputBytes, &inputSource); err != nil {
		t.Fatalf("read input units: %v", err)
	}
	if inputRecords != 2 || inputBytes != int64(len(input)) || inputSource != economicInputSourceSubmitStream {
		t.Fatalf("input facts records=%d bytes=%d source=%q, want 2/%d/%q",
			inputRecords, inputBytes, inputSource, len(input), economicInputSourceSubmitStream)
	}

	var taskID uuid.UUID
	var resultKey string
	if err := itPool.QueryRow(ctx, `
		SELECT id,result_key FROM tasks
		 WHERE job_id=$1 AND is_honeypot=false AND is_redundancy=false`, submit.JobID).Scan(&taskID, &resultKey); err != nil {
		t.Fatalf("read primary: %v", err)
	}
	if err := itStorage.PutObject(ctx, resultKey, embedResultJSON(2), "application/json"); err != nil {
		t.Fatalf("put result: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET status='complete',result_ref=$2,started_at=now()-interval '1 second',verified_at=now()
		 WHERE id=$1`, taskID, resultKey); err != nil {
		t.Fatalf("complete task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET status='complete',tasks_done=1 WHERE id=$1`, submit.JobID); err != nil {
		t.Fatalf("complete job: %v", err)
	}
	before, err := itStore.RecomputeJobEconomicFact(ctx, submit.JobID)
	if err != nil {
		t.Fatalf("pre-merge recompute: %v", err)
	}
	if !slices.Contains(before.MissingDataReasons, "exact_output_records_unavailable") ||
		!slices.Contains(before.MissingDataReasons, "exact_output_bytes_unavailable") {
		t.Fatalf("pre-merge facts did not surface genuinely missing output units: %v", before.MissingDataReasons)
	}

	written, err := mergeJobResults(ctx, itStore, itStorage, submit.JobID)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	var outputRecords, outputBytes int64
	var outputSource, outputRef string
	if err := itPool.QueryRow(ctx, `
		SELECT economic_output_records,economic_output_bytes,economic_output_source,output_ref
		  FROM jobs WHERE id=$1`, submit.JobID).Scan(&outputRecords, &outputBytes, &outputSource, &outputRef); err != nil {
		t.Fatalf("read output units: %v", err)
	}
	mergedBytes, err := itStorage.GetObject(ctx, outputRef)
	if err != nil {
		t.Fatalf("read merged artifact: %v", err)
	}
	if outputRecords != 2 || outputBytes != int64(len(mergedBytes)) || written != len(mergedBytes) ||
		outputSource != economicOutputSourceMergedArtifact {
		t.Fatalf("output facts records=%d bytes=%d written=%d actual=%d source=%q",
			outputRecords, outputBytes, written, len(mergedBytes), outputSource)
	}
	after, err := itStore.RecomputeJobEconomicFact(ctx, submit.JobID)
	if err != nil {
		t.Fatalf("post-merge recompute: %v", err)
	}
	for _, removed := range []string{
		"exact_input_records_unavailable", "exact_input_bytes_unavailable",
		"exact_output_records_unavailable", "exact_output_bytes_unavailable",
		"control_plane_elapsed_unavailable",
	} {
		if slices.Contains(after.MissingDataReasons, removed) {
			t.Fatalf("post-merge facts retained %q despite authoritative data: %v", removed, after.MissingDataReasons)
		}
	}
}

func TestEconomicFactsRecomputePersistsExactMoneySemanticsIdempotently(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, primaryID, verificationID := uuid.New(), uuid.New(), uuid.New()
	created := time.Now().UTC().Add(-time.Minute).Truncate(time.Millisecond)
	merged := created.Add(42 * time.Second)

	if _, err := itPool.Exec(ctx, `
		INSERT INTO jobs (
		  id,buyer_id,status,job_type,input_ref,output_ref,actual_usd,billed_usd,
		  charge_status,stripe_pi,created_at,results_merged_at,task_count,tasks_done,
		  economic_input_records,economic_input_bytes,economic_input_source,
		  economic_output_records,economic_output_bytes,economic_output_source
		) VALUES ($1,$2,'complete','batch_infer','input','output',12,9,
		          'charged','pi_econ_direct',$3,$4,2,2,10,1000,'submit_stream_exact',
		          10,2200,'merged_artifact_exact')`, jobID, demoBuyerUUID, created, merged); err != nil {
		t.Fatalf("insert job: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO tasks (
		  id,job_id,worker_id,status,is_honeypot,is_redundancy,retry_count,
		  started_at,verified_at,reported_tokens_used
		) VALUES
		  ($1,$3,$4,'complete',false,false,1,$5,$6,100),
		  ($2,$3,$4,'complete',false,true,0,$7,$8,50)`,
		primaryID, verificationID, jobID, demoWorkerUUID,
		created.Add(2*time.Second), created.Add(7*time.Second),
		created.Add(10*time.Second), created.Add(12*time.Second)); err != nil {
		t.Fatalf("insert tasks: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO task_verdicts (task_id,attempt,job_id,supplier_id,outcome) VALUES
		  ($1,1,$3,$4,'pass'), ($2,0,$3,$4,'pass')`,
		primaryID, verificationID, jobID, demoSupplierUUID); err != nil {
		t.Fatalf("insert verdicts: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO ledger_entries (kind,supplier_id,buyer_id,task_id,amount_usd,payout_status,payout_ref) VALUES
		  ('supplier_credit',$1,NULL,$2,6,'held',NULL),
		  ('supplier_credit',$1,NULL,$3,2,'held',NULL),
		  ('clawback',$1,NULL,$3,-2,'clawed_back',NULL),
		  ('sla_refund',NULL,$4,NULL,3,'released',$5),
		  ('stripe_fee',NULL,$4,NULL,-0.59,'released','pi_econ_direct')`,
		demoSupplierUUID, primaryID, verificationID, demoBuyerUUID, slaRefundRef(jobID)); err != nil {
		t.Fatalf("insert ledger: %v", err)
	}

	first, err := itStore.RecomputeJobEconomicFact(ctx, jobID)
	if err != nil {
		t.Fatalf("first recompute: %v", err)
	}
	second, err := itStore.RecomputeJobEconomicFact(ctx, jobID)
	if err != nil {
		t.Fatalf("idempotent recompute: %v", err)
	}
	if first.ReconciliationState != economicStateComplete || second.ReconciliationState != economicStateComplete {
		t.Fatalf("states first=%q second=%q missing=%v", first.ReconciliationState, second.ReconciliationState, second.MissingDataReasons)
	}
	var rows int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM job_economic_facts WHERE job_id=$1`, jobID).Scan(&rows); err != nil || rows != 1 {
		t.Fatalf("idempotent projection row count=%d err=%v, want 1", rows, err)
	}
	persisted, err := itStore.GetJobEconomicFact(ctx, jobID)
	if err != nil {
		t.Fatalf("read persisted fact: %v", err)
	}
	assertEconomicFloat := func(name string, got *float64, want float64) {
		t.Helper()
		if got == nil || math.Abs(*got-want) > 1e-9 {
			t.Fatalf("%s=%v, want %.6f", name, got, want)
		}
	}
	assertEconomicFloat("supplier liability", persisted.SupplierLiabilityUSD, 6)
	assertEconomicFloat("refunds", persisted.RefundsUSD, 3)
	assertEconomicFloat("billed", persisted.BilledUSD, 9)
	assertEconomicFloat("processor fee", persisted.ProcessorFeeUSD, .59)
	assertEconomicFloat("margin", persisted.ContributionMarginUSD, 2.41)
	if persisted.SettlementUSDBasis != settlementBasisQuoteDerived {
		t.Fatalf("settlement basis=%q", persisted.SettlementUSDBasis)
	}
	if persisted.WorkerReportedTokens == nil || *persisted.WorkerReportedTokens != 150 ||
		persisted.WorkerReportedTokensSource == nil || *persisted.WorkerReportedTokensSource != workerTokensBasis {
		t.Fatalf("worker token fact/source not honestly labelled: %+v", persisted)
	}

	// The admin surface recomputes before reading and is protected by the normal
	// admin gate. This also exercises the report JSON's null-preserving pointers.
	if code, body := req(t, "GET", "/admin/economics/jobs?limit=1", nil, buyerKey()); code != http.StatusForbidden {
		t.Fatalf("buyer access: got %d %s, want 403", code, body)
	}
	code, body := req(t, "GET", "/admin/economics/jobs?limit=1", nil, adminKey())
	if code != http.StatusOK {
		t.Fatalf("admin economics: %d %s", code, body)
	}
	var report struct {
		Facts []JobEconomicFact `json:"facts"`
		Count int               `json:"count"`
	}
	if err := json.Unmarshal(body, &report); err != nil || report.Count != 1 || len(report.Facts) != 1 || report.Facts[0].JobID != jobID {
		t.Fatalf("admin report decode=%v report=%+v body=%s", err, report, body)
	}
}

func TestEconomicFactsAllocatesMultiJobBatchFeeWithExactRemainderIdempotently(t *testing.T) {
	reset(t)
	ctx := context.Background()
	batchID := uuid.New()
	jobs := []struct {
		id     uuid.UUID
		billed float64
	}{
		{uuid.MustParse("00000000-0000-0000-0000-000000000101"), 1},
		{uuid.MustParse("00000000-0000-0000-0000-000000000102"), 2},
		{uuid.MustParse("00000000-0000-0000-0000-000000000103"), 3},
	}
	created := time.Now().UTC().Add(-time.Minute)
	merged := created.Add(20 * time.Second)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO charge_batches (id,buyer_id,amount_usd,status,stripe_pi,charged_at)
		VALUES ($1,$2,6,'charged','pi_econ_batch',now())`, batchID, demoBuyerUUID); err != nil {
		t.Fatalf("insert batch: %v", err)
	}
	for i, job := range jobs {
		if _, err := itPool.Exec(ctx, `
			INSERT INTO jobs (
			  id,buyer_id,status,job_type,input_ref,actual_usd,billed_usd,charge_status,charge_batch_id,
			  created_at,results_merged_at,economic_input_records,economic_input_bytes,economic_input_source,
			  economic_output_records,economic_output_bytes,economic_output_source
			) VALUES ($1,$2,'complete','embed','input',$3,$3,'charged',$4,$5,$6,
			          1,20,$7,1,40,$8)`,
			job.id, demoBuyerUUID, job.billed, batchID, created.Add(time.Duration(i)*time.Second), merged,
			economicInputSourceSubmitStream, economicOutputSourceMergedArtifact); err != nil {
			t.Fatalf("insert batch member %s: %v", job.id, err)
		}
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO ledger_entries (kind,buyer_id,amount_usd,payout_status,payout_ref)
		VALUES ('stripe_fee',$1,-0.01,'released','pi_econ_batch')`, demoBuyerUUID); err != nil {
		t.Fatalf("insert batch fee: %v", err)
	}
	f, err := itStore.RecomputeJobEconomicFact(ctx, jobs[0].id)
	if err != nil {
		t.Fatalf("recompute: %v", err)
	}
	if f.ReconciliationState != economicStateComplete ||
		f.ProcessorFeePaymentIntentTotalUSD == nil || *f.ProcessorFeePaymentIntentTotalUSD != .01 ||
		f.ProcessorFeeUSD == nil || *f.ProcessorFeeUSD != .001666 || f.ContributionMarginUSD == nil || *f.ContributionMarginUSD != .998334 {
		t.Fatalf("first batch fact not exactly allocated: %+v", f)
	}
	if slices.Contains(f.MissingDataReasons, "multi_job_batch_fee_allocation_unresolved") {
		t.Fatalf("resolved allocation retained missing reason: %v", f.MissingDataReasons)
	}

	readAllocations := func() ([]float64, float64, int) {
		t.Helper()
		rows, err := itPool.Query(ctx, `
			SELECT allocated_fee_usd::float8 FROM charge_batch_fee_allocations
			 WHERE charge_batch_id=$1 ORDER BY allocation_ordinal`, batchID)
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		var amounts []float64
		for rows.Next() {
			var amount float64
			if err := rows.Scan(&amount); err != nil {
				t.Fatal(err)
			}
			amounts = append(amounts, amount)
		}
		var sum float64
		var count int
		if err := itPool.QueryRow(ctx, `
			SELECT COALESCE(SUM(allocated_fee_usd),0)::float8,COUNT(*)
			  FROM charge_batch_fee_allocations WHERE charge_batch_id=$1`, batchID).Scan(&sum, &count); err != nil {
			t.Fatal(err)
		}
		return amounts, sum, count
	}
	first, sum, count := readAllocations()
	want := []float64{.001666, .003333, .005001}
	if !slices.Equal(first, want) || sum != .01 || count != 3 {
		t.Fatalf("allocations=%v sum=%v count=%d, want %v/.01/3", first, sum, count, want)
	}
	if allocated, err := itStore.AllocateBatchStripeFee(ctx, "pi_econ_batch"); err != nil || !allocated {
		t.Fatalf("idempotent allocation: allocated=%v err=%v", allocated, err)
	}
	second, sum, count := readAllocations()
	if !slices.Equal(second, want) || sum != .01 || count != 3 {
		t.Fatalf("reallocation drifted/duplicated: %v sum=%v count=%d", second, sum, count)
	}
}
