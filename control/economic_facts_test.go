package main

import (
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestAllocateBatchFeeMicrosUsesStableExactFinalRemainder(t *testing.T) {
	jobs := []batchFeeWeight{
		{JobID: uuid.MustParse("00000000-0000-0000-0000-000000000001"), WeightMicros: 1},
		{JobID: uuid.MustParse("00000000-0000-0000-0000-000000000002"), WeightMicros: 2},
		{JobID: uuid.MustParse("00000000-0000-0000-0000-000000000003"), WeightMicros: 3},
	}
	want := []batchFeeAllocation{
		{JobID: jobs[0].JobID, WeightMicros: 1, AllocatedMicros: 16},
		{JobID: jobs[1].JobID, WeightMicros: 2, AllocatedMicros: 33},
		{JobID: jobs[2].JobID, WeightMicros: 3, AllocatedMicros: 51}, // exact remainder
	}
	first, err := allocateBatchFeeMicros(100, jobs)
	if err != nil {
		t.Fatal(err)
	}
	second, err := allocateBatchFeeMicros(100, jobs)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first, want) || !reflect.DeepEqual(second, want) {
		t.Fatalf("allocation is not deterministic/exact:\nfirst=%+v\nsecond=%+v\nwant=%+v", first, second, want)
	}
	var sum int64
	for _, a := range first {
		sum += a.AllocatedMicros
	}
	if sum != 100 {
		t.Fatalf("allocated sum=%d, want exact fee 100", sum)
	}
}

func TestBuildJobEconomicFactDirectChargeMarginUsesNetBilledOnce(t *testing.T) {
	created := time.Date(2026, 7, 10, 12, 0, 0, 0, time.UTC)
	merged := created.Add(42 * time.Second)
	inputRecords, inputBytes := int64(10), int64(1000)
	outputRecords, outputBytes := int64(10), int64(2200)
	actual, billed, fee := 12.0, 9.0, 0.59
	verificationMS := int64(1700)
	reportedTokens := int64(321)

	f := buildJobEconomicFact(economicFactInputs{
		JobID: uuid.New(), BuyerID: uuid.New(), JobStatus: "complete", ChargeStatus: "charged",
		InputRecords: &inputRecords, InputBytes: &inputBytes, InputSource: "submit_stream_exact",
		OutputRecords: &outputRecords, OutputBytes: &outputBytes, OutputSource: "merged_artifact_exact",
		CreatedAt: created, ResultsMergedAt: &merged,
		ActualUSD: &actual, BilledUSD: &billed, StripePI: "pi_direct",
		PrimaryTasksRun: 2, VerificationTasksRun: 1, RetryAttempts: 1, VerdictAttempts: 3,
		VerificationTaskServerMSSum: &verificationMS, VerificationTasksWithServerMS: 1,
		WorkerReportedTokens: &reportedTokens, WorkerReportedTokensTasks: 2,
		SupplierLiabilityUSD: 6.0, RefundsUSD: 3.0, ProcessorFeePITotalUSD: &fee,
	})

	if f.ReconciliationState != economicStateComplete || len(f.MissingDataReasons) != 0 {
		t.Fatalf("state=%q missing=%v, want complete with no gaps", f.ReconciliationState, f.MissingDataReasons)
	}
	if f.ContributionMarginUSD == nil || *f.ContributionMarginUSD != 2.41 {
		t.Fatalf("margin=%v, want 9 billed - 6 liability - .59 fee = 2.41 (refund already netted from billed)", f.ContributionMarginUSD)
	}
	if f.RefundsUSD == nil || *f.RefundsUSD != 3 {
		t.Fatalf("refunds=%v, want separately surfaced 3", f.RefundsUSD)
	}
	if f.SettlementUSDBasis != settlementBasisQuoteDerived {
		t.Fatalf("settlement basis=%q", f.SettlementUSDBasis)
	}
	if f.WorkerReportedTokensSource == nil || *f.WorkerReportedTokensSource != workerTokensBasis {
		t.Fatalf("worker token source=%v, must explicitly say unverified worker report", f.WorkerReportedTokensSource)
	}
	if f.ProcessorFeeBasis == nil || *f.ProcessorFeeBasis != processorFeeDirectBasis {
		t.Fatalf("processor fee basis=%v", f.ProcessorFeeBasis)
	}
}

func TestBuildJobEconomicFactDoesNotGuessMultiJobBatchFee(t *testing.T) {
	created := time.Now().Add(-time.Minute)
	merged := created.Add(30 * time.Second)
	inRecords, inBytes, outRecords, outBytes := int64(1), int64(20), int64(1), int64(40)
	actual, billed, fee := 4.0, 4.0, 0.50
	batchID := uuid.New()
	base := economicFactInputs{
		JobID: uuid.New(), BuyerID: uuid.New(), JobStatus: "complete", ChargeStatus: "charged",
		InputRecords: &inRecords, InputBytes: &inBytes, InputSource: "submit_stream_exact",
		OutputRecords: &outRecords, OutputBytes: &outBytes, OutputSource: "merged_artifact_exact",
		CreatedAt: created, ResultsMergedAt: &merged, ActualUSD: &actual, BilledUSD: &billed,
		ChargeBatchID: &batchID, ChargeBatchStatus: "charged", ChargeBatchPI: "pi_batch",
		BatchMemberCount: 2, SupplierLiabilityUSD: 3, ProcessorFeePITotalUSD: &fee,
	}
	f := buildJobEconomicFact(base)
	if f.ReconciliationState != economicStateUnresolvedBatchFee {
		t.Fatalf("state=%q, want unresolved_batch_fee", f.ReconciliationState)
	}
	if f.ProcessorFeePaymentIntentTotalUSD == nil || *f.ProcessorFeePaymentIntentTotalUSD != fee {
		t.Fatalf("real batch total=%v, want %v", f.ProcessorFeePaymentIntentTotalUSD, fee)
	}
	if f.ProcessorFeeUSD != nil || f.ContributionMarginUSD != nil {
		t.Fatalf("multi-job batch must not guess an allocation: fee=%v margin=%v", f.ProcessorFeeUSD, f.ContributionMarginUSD)
	}
	if !slices.Contains(f.MissingDataReasons, "multi_job_batch_fee_allocation_unresolved") {
		t.Fatalf("missing reasons=%v", f.MissingDataReasons)
	}
	allocated := .17
	base.ProcessorFeeAllocatedUSD = &allocated
	resolved := buildJobEconomicFact(base)
	if resolved.ReconciliationState != economicStateComplete || resolved.ProcessorFeeUSD == nil ||
		*resolved.ProcessorFeeUSD != .17 || resolved.ContributionMarginUSD == nil || *resolved.ContributionMarginUSD != .83 {
		t.Fatalf("persisted weighted batch allocation did not resolve facts: %+v", resolved)
	}
	if resolved.ProcessorFeeBasis == nil || *resolved.ProcessorFeeBasis != processorFeeBatchAllocatedBasis ||
		slices.Contains(resolved.MissingDataReasons, "multi_job_batch_fee_allocation_unresolved") {
		t.Fatalf("resolved batch retained the unresolved label: %+v", resolved)
	}

	// A one-member batch does support exact attribution: the job owns the whole PI.
	base.BatchMemberCount = 1
	base.ProcessorFeeAllocatedUSD = nil
	solo := buildJobEconomicFact(base)
	if solo.ReconciliationState != economicStateComplete {
		t.Fatalf("one-member batch state=%q missing=%v", solo.ReconciliationState, solo.MissingDataReasons)
	}
	if solo.ProcessorFeeUSD == nil || *solo.ProcessorFeeUSD != .5 || solo.ContributionMarginUSD == nil || *solo.ContributionMarginUSD != .5 {
		t.Fatalf("one-member exact allocation fee=%v margin=%v", solo.ProcessorFeeUSD, solo.ContributionMarginUSD)
	}
	if solo.ProcessorFeeBasis == nil || *solo.ProcessorFeeBasis != processorFeeSoloBatchBasis {
		t.Fatalf("one-member basis=%v", solo.ProcessorFeeBasis)
	}
}

func TestBuildJobEconomicFactKeepsUnknownsNull(t *testing.T) {
	created := time.Now().Add(-time.Minute)
	merged := time.Now()
	actual, billed := 1.0, 1.0
	f := buildJobEconomicFact(economicFactInputs{
		JobID: uuid.New(), BuyerID: uuid.New(), JobStatus: "complete", ChargeStatus: "charged",
		CreatedAt: created, ResultsMergedAt: &merged, ActualUSD: &actual, BilledUSD: &billed,
		StripePI: "pi_fee_not_settled",
	})
	if f.ReconciliationState != economicStateAwaitingProcessorFee {
		t.Fatalf("state=%q, want awaiting_processor_fee", f.ReconciliationState)
	}
	if f.InputRecords != nil || f.InputBytes != nil || f.OutputRecords != nil || f.OutputBytes != nil {
		t.Fatalf("unknown exact units must remain null: %+v", f)
	}
	if f.ProcessorFeeUSD != nil || f.ContributionMarginUSD != nil {
		t.Fatalf("missing actual fee must keep fee/margin null: fee=%v margin=%v", f.ProcessorFeeUSD, f.ContributionMarginUSD)
	}
	for _, reason := range []string{
		"exact_input_records_unavailable", "exact_input_bytes_unavailable",
		"exact_output_records_unavailable", "exact_output_bytes_unavailable",
		"stripe_balance_transaction_fee_unavailable",
	} {
		if !slices.Contains(f.MissingDataReasons, reason) {
			t.Fatalf("missing reasons %v lack %q", f.MissingDataReasons, reason)
		}
	}
}
