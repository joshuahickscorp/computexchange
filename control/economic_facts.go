package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// economic_facts.go is the non-circular per-job economics boundary. It only
// projects facts already persisted by the control plane. In particular:
//   - jobs.actual_usd is named quote-derived settlement, never execution cost;
//   - worker token counts are named unverified worker reports;
//   - a Stripe fee is attributed to a job only when the PaymentIntent belongs to
//     that job alone (a direct charge or a one-member batch);
//   - a multi-job batch's real fee total is allocated deterministically from
//     frozen billed_usd weights; fee and margin remain NULL until that real
//     allocation exists, never while only a guessed pro-rata is available.

const (
	economicFactsSchemaVersion = 1

	economicStatePending              = "pending"
	economicStateAwaitingCollection   = "awaiting_collection"
	economicStateAwaitingProcessorFee = "awaiting_processor_fee"
	economicStateUnresolvedBatchFee   = "unresolved_batch_fee"
	economicStateIncomplete           = "incomplete"
	economicStateComplete             = "complete"

	settlementBasisQuoteDerived     = "quote_derived_per_task_buyer_charge_settlement"
	supplierLiabilityBasis          = "job_task_ledger_supplier_credit_plus_clawback"
	refundsBasis                    = "job_linked_ledger_refunds"
	billedBasis                     = "jobs_billed_usd_frozen_collection_amount_net_of_linked_refunds"
	verificationWorkBasis           = "persisted_task_rows_retry_counts_and_task_verdicts"
	workerTokensBasis               = "worker_reported_current_attempt_unverified"
	controlPlaneElapsedBasis        = "server_jobs_created_at_to_results_merged_at"
	processorFeeDirectBasis         = "stripe_balance_transaction_actual_direct_payment_intent"
	processorFeeSoloBatchBasis      = "stripe_balance_transaction_actual_single_member_batch"
	processorFeeBatchTotalBasis     = "stripe_balance_transaction_actual_batch_total_unallocated"
	processorFeeBatchAllocatedBasis = "stripe_balance_transaction_actual_batch_frozen_billed_weight_exact_remainder"

	economicInputSourceSubmitStream    = "submit_stream_exact_raw_bytes_and_nonblank_jsonl_records"
	economicOutputSourceMergedArtifact = "merged_artifact_exact_bytes_and_records"
)

// JobEconomicFact is the persisted/admin wire projection. Pointer fields are
// deliberate: JSON null is an economic fact (unknown or unattributable), and must
// never be silently serialized as zero.
type JobEconomicFact struct {
	JobID               uuid.UUID `json:"job_id"`
	BuyerID             uuid.UUID `json:"buyer_id"`
	JobStatus           string    `json:"job_status"`
	ChargeStatus        string    `json:"charge_status"`
	SchemaVersion       int       `json:"schema_version"`
	ReconciliationState string    `json:"reconciliation_state"`
	MissingDataReasons  []string  `json:"missing_data_reasons"`

	InputRecords      *int64  `json:"input_records"`
	InputBytes        *int64  `json:"input_bytes"`
	InputUnitsSource  *string `json:"input_units_source"`
	OutputRecords     *int64  `json:"output_records"`
	OutputBytes       *int64  `json:"output_bytes"`
	OutputUnitsSource *string `json:"output_units_source"`

	ControlPlaneElapsedMS         *int64  `json:"control_plane_elapsed_ms"`
	ControlPlaneElapsedSource     *string `json:"control_plane_elapsed_source"`
	PrimaryTasksRun               int     `json:"primary_tasks_run"`
	VerificationTasksRun          int     `json:"verification_tasks_run"`
	RetryAttempts                 int     `json:"retry_attempts"`
	VerdictAttempts               int     `json:"verdict_attempts"`
	VerificationTaskServerMS      *int64  `json:"verification_task_server_ms"`
	VerificationTasksWithServerMS int     `json:"verification_tasks_with_server_ms"`
	VerificationWorkSource        string  `json:"verification_work_source"`
	WorkerReportedTokens          *int64  `json:"worker_reported_tokens"`
	WorkerReportedTokensTasks     int     `json:"worker_reported_tokens_tasks"`
	WorkerReportedTokensSource    *string `json:"worker_reported_tokens_source"`

	SettlementUSD             *float64 `json:"settlement_usd"`
	SettlementUSDBasis        string   `json:"settlement_usd_basis"`
	SupplierLiabilityUSD      *float64 `json:"supplier_liability_usd"`
	SupplierLiabilityBasis    string   `json:"supplier_liability_basis"`
	RefundsUSD                *float64 `json:"refunds_usd"`
	RefundsBasis              string   `json:"refunds_basis"`
	BilledUSD                 *float64 `json:"billed_usd"`
	BilledUSDBasis            string   `json:"billed_usd_basis"`
	ProcessorFeePaymentIntent *string  `json:"processor_fee_payment_intent"`
	// For a batch this is the real fee of the whole PI; ProcessorFeeUSD is the
	// separately persisted exact allocation for this job.
	ProcessorFeePaymentIntentTotalUSD *float64 `json:"processor_fee_payment_intent_total_usd"`
	ProcessorFeeUSD                   *float64 `json:"processor_fee_usd"`
	ProcessorFeeBasis                 *string  `json:"processor_fee_basis"`
	ContributionMarginUSD             *float64 `json:"contribution_margin_usd"`

	RecomputedAt time.Time `json:"recomputed_at"`
}

// economicFactInputs is the raw, persisted evidence from one database snapshot.
// It is separate from JobEconomicFact so the truth/state rules are pure unit-test
// targets instead of being buried in SQL conditionals.
type economicFactInputs struct {
	JobID, BuyerID uuid.UUID
	JobStatus      string
	ChargeStatus   string

	InputRecords, InputBytes   *int64
	InputSource                string
	OutputRecords, OutputBytes *int64
	OutputSource               string
	CreatedAt                  time.Time
	ResultsMergedAt            *time.Time

	ActualUSD *float64
	BilledUSD *float64
	StripePI  string

	ChargeBatchID     *uuid.UUID
	ChargeBatchStatus string
	ChargeBatchPI     string
	BatchMemberCount  int

	PrimaryTasksRun               int
	VerificationTasksRun          int
	RetryAttempts                 int
	VerdictAttempts               int
	VerificationTaskServerMSSum   *int64
	VerificationTasksWithServerMS int
	WorkerReportedTokens          *int64
	WorkerReportedTokensTasks     int

	SupplierLiabilityUSD     float64
	RefundsUSD               float64
	ProcessorFeePITotalUSD   *float64
	ProcessorFeeAllocatedUSD *float64
}

type batchFeeWeight struct {
	JobID        uuid.UUID
	WeightMicros int64
}

type batchFeeAllocation struct {
	JobID           uuid.UUID
	WeightMicros    int64
	AllocatedMicros int64
}

// allocateBatchFeeMicros deterministically apportions an actual Stripe fee over
// already-stable billed_usd weights. Inputs and outputs are integer micro-dollars
// (the database's NUMERIC(...,6) precision), so no float can create or destroy a
// micro-dollar. Each non-final row receives its exact proportional floor; the
// stable final row receives the remainder, making the sum exact by construction.
func allocateBatchFeeMicros(feeMicros int64, weights []batchFeeWeight) ([]batchFeeAllocation, error) {
	if feeMicros < 0 {
		return nil, fmt.Errorf("negative Stripe fee %d microdollars", feeMicros)
	}
	if len(weights) == 0 {
		return nil, errors.New("charge batch has no jobs to allocate")
	}
	totalWeight := int64(0)
	for _, w := range weights {
		if w.WeightMicros <= 0 {
			return nil, fmt.Errorf("job %s has non-positive frozen billed_usd weight", w.JobID)
		}
		if totalWeight > math.MaxInt64-w.WeightMicros {
			return nil, errors.New("charge batch billed_usd weights overflow int64 microdollars")
		}
		totalWeight += w.WeightMicros
	}

	out := make([]batchFeeAllocation, len(weights))
	allocated := int64(0)
	for i, w := range weights {
		share := int64(0)
		if i == len(weights)-1 {
			share = feeMicros - allocated // exact final-row remainder
		} else if feeMicros > 0 {
			var numerator, quotient big.Int
			numerator.Mul(big.NewInt(feeMicros), big.NewInt(w.WeightMicros))
			quotient.Quo(&numerator, big.NewInt(totalWeight)) // floor, deterministic
			if !quotient.IsInt64() {
				return nil, errors.New("allocated Stripe fee share overflows int64 microdollars")
			}
			share = quotient.Int64()
			allocated += share
		}
		out[i] = batchFeeAllocation{JobID: w.JobID, WeightMicros: w.WeightMicros, AllocatedMicros: share}
	}
	return out, nil
}

func economicStringPtr(s string) *string {
	s = strings.TrimSpace(s)
	if s == "" {
		return nil
	}
	return &s
}

func economicFloatPtr(v float64) *float64 { return &v }
func economicInt64Ptr(v int64) *int64     { return &v }

func terminalEconomicJob(status string) bool {
	return status == "complete" || status == "failed" || status == "cancelled"
}

func roundEconomicUSD(v float64) float64 { return math.Round(v*1_000_000) / 1_000_000 }

// AllocateBatchStripeFee persists the exact per-job allocation for a charged
// batch PaymentIntent. Direct-charge PIs are a clean no-op. The charge_batches
// row is locked so concurrent fee-record/recompute callers serialize; replacement
// happens in one transaction and is therefore retry/idempotency safe.
func (s *Store) AllocateBatchStripeFee(ctx context.Context, pi string) (bool, error) {
	if strings.TrimSpace(pi) == "" {
		return false, nil
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)

	var batchID uuid.UUID
	err = tx.QueryRow(ctx, `
		SELECT id FROM charge_batches
		 WHERE stripe_pi=$1 AND status='charged'
		 ORDER BY id LIMIT 1 FOR UPDATE`, pi).Scan(&batchID)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil // a direct job PI has no batch allocation
	}
	if err != nil {
		return false, err
	}

	var feeMicros int64
	err = tx.QueryRow(ctx, `
		SELECT (-amount_usd * 1000000)::bigint
		  FROM ledger_entries
		 WHERE kind='stripe_fee' AND payout_ref=$1`, pi).Scan(&feeMicros)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil // Stripe balance transaction has not been persisted yet
	}
	if err != nil {
		return false, err
	}

	rows, err := tx.Query(ctx, `
		SELECT id, (billed_usd * 1000000)::bigint, charge_status
		  FROM jobs
		 WHERE charge_batch_id=$1
		 ORDER BY created_at, id
		 FOR UPDATE`, batchID)
	if err != nil {
		return false, err
	}
	var weights []batchFeeWeight
	for rows.Next() {
		var (
			jobID        uuid.UUID
			weightMicros *int64
			chargeStatus string
		)
		if err := rows.Scan(&jobID, &weightMicros, &chargeStatus); err != nil {
			rows.Close()
			return false, err
		}
		if chargeStatus != "charged" {
			rows.Close()
			return false, fmt.Errorf("charge batch %s job %s is %q, not charged", batchID, jobID, chargeStatus)
		}
		if weightMicros == nil {
			rows.Close()
			return false, fmt.Errorf("charge batch %s job %s has no frozen billed_usd", batchID, jobID)
		}
		weights = append(weights, batchFeeWeight{JobID: jobID, WeightMicros: *weightMicros})
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return false, err
	}
	allocations, err := allocateBatchFeeMicros(feeMicros, weights)
	if err != nil {
		return false, fmt.Errorf("charge batch %s fee allocation: %w", batchID, err)
	}

	// Membership and stable order are frozen by charge_batch_id; replace the set
	// atomically so a retry can neither duplicate rows nor leave an old remainder.
	if _, err := tx.Exec(ctx, `DELETE FROM charge_batch_fee_allocations WHERE charge_batch_id=$1`, batchID); err != nil {
		return false, err
	}
	for i, a := range allocations {
		if _, err := tx.Exec(ctx, `
			INSERT INTO charge_batch_fee_allocations
			  (charge_batch_id,job_id,stripe_pi,allocation_ordinal,billed_weight_usd,allocated_fee_usd)
			VALUES ($1,$2,$3,$4,$5::numeric/1000000,$6::numeric/1000000)`,
			batchID, a.JobID, pi, i, a.WeightMicros, a.AllocatedMicros); err != nil {
			return false, err
		}
	}
	var allocatedMicros int64
	if err := tx.QueryRow(ctx, `
		SELECT (COALESCE(SUM(allocated_fee_usd),0)*1000000)::bigint
		  FROM charge_batch_fee_allocations WHERE charge_batch_id=$1`, batchID).Scan(&allocatedMicros); err != nil {
		return false, err
	}
	if allocatedMicros != feeMicros {
		return false, fmt.Errorf("charge batch %s allocated %d fee microdollars, Stripe fee is %d", batchID, allocatedMicros, feeMicros)
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

// ensureJobBatchFeeAllocation is the recompute self-heal: a fee row may have
// landed before this schema existed or an allocation write may have transiently
// failed. Recomputing any member deterministically rebuilds the whole batch.
func (s *Store) ensureJobBatchFeeAllocation(ctx context.Context, jobID uuid.UUID) error {
	var pi *string
	err := s.pool.QueryRow(ctx, `
		SELECT cb.stripe_pi
		  FROM jobs j LEFT JOIN charge_batches cb ON cb.id=j.charge_batch_id
		 WHERE j.id=$1`, jobID).Scan(&pi)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil // loadEconomicFactInputs returns the canonical not-found error
	}
	if err != nil || pi == nil || *pi == "" {
		return err
	}
	_, err = s.AllocateBatchStripeFee(ctx, *pi)
	return err
}

// BatchStripeFeesMissingAllocations is the background retry set for the narrow
// failure window where the Stripe fee row committed but its allocation did not.
// It also backfills pre-migration batches. A complete set must have one row per
// member and an exact sum equal to the real PI fee.
func (s *Store) BatchStripeFeesMissingAllocations(ctx context.Context, limit int) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT cb.stripe_pi
		  FROM charge_batches cb
		  JOIN ledger_entries fee ON fee.kind='stripe_fee' AND fee.payout_ref=cb.stripe_pi
		 WHERE cb.status='charged' AND COALESCE(cb.stripe_pi,'') <> ''
		   AND (
		     (SELECT COUNT(*) FROM charge_batch_fee_allocations a WHERE a.charge_batch_id=cb.id)
		       <> (SELECT COUNT(*) FROM jobs j WHERE j.charge_batch_id=cb.id)
		     OR COALESCE((SELECT SUM(a.allocated_fee_usd) FROM charge_batch_fee_allocations a
		                   WHERE a.charge_batch_id=cb.id), -1) <> -fee.amount_usd
		   )
		 ORDER BY cb.charged_at, cb.id
		 LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var pi string
		if err := rows.Scan(&pi); err != nil {
			return nil, err
		}
		out = append(out, pi)
	}
	return out, rows.Err()
}

// buildJobEconomicFact applies the honesty and reconciliation state machine to a
// persisted snapshot. Refunds are NOT subtracted again from margin: billed_usd is
// the frozen amount actually collected and is already net of the linked refund.
func buildJobEconomicFact(in economicFactInputs) JobEconomicFact {
	f := JobEconomicFact{
		JobID:                             in.JobID,
		BuyerID:                           in.BuyerID,
		JobStatus:                         in.JobStatus,
		ChargeStatus:                      in.ChargeStatus,
		SchemaVersion:                     economicFactsSchemaVersion,
		MissingDataReasons:                []string{},
		InputRecords:                      in.InputRecords,
		InputBytes:                        in.InputBytes,
		InputUnitsSource:                  economicStringPtr(in.InputSource),
		OutputRecords:                     in.OutputRecords,
		OutputBytes:                       in.OutputBytes,
		OutputUnitsSource:                 economicStringPtr(in.OutputSource),
		PrimaryTasksRun:                   in.PrimaryTasksRun,
		VerificationTasksRun:              in.VerificationTasksRun,
		RetryAttempts:                     in.RetryAttempts,
		VerdictAttempts:                   in.VerdictAttempts,
		VerificationTasksWithServerMS:     in.VerificationTasksWithServerMS,
		VerificationWorkSource:            verificationWorkBasis,
		WorkerReportedTokens:              in.WorkerReportedTokens,
		WorkerReportedTokensTasks:         in.WorkerReportedTokensTasks,
		SettlementUSDBasis:                settlementBasisQuoteDerived,
		SupplierLiabilityUSD:              economicFloatPtr(roundEconomicUSD(in.SupplierLiabilityUSD)),
		SupplierLiabilityBasis:            supplierLiabilityBasis,
		RefundsUSD:                        economicFloatPtr(roundEconomicUSD(in.RefundsUSD)),
		RefundsBasis:                      refundsBasis,
		BilledUSD:                         in.BilledUSD,
		BilledUSDBasis:                    billedBasis,
		ProcessorFeePaymentIntentTotalUSD: in.ProcessorFeePITotalUSD,
	}
	if in.WorkerReportedTokensTasks > 0 {
		f.WorkerReportedTokensSource = economicStringPtr(workerTokensBasis)
	}

	requiredDataIncomplete := false
	missing := func(reason string) {
		f.MissingDataReasons = append(f.MissingDataReasons, reason)
		requiredDataIncomplete = true
	}
	if in.InputRecords == nil {
		missing("exact_input_records_unavailable")
	}
	if in.InputBytes == nil {
		missing("exact_input_bytes_unavailable")
	}
	if (in.InputRecords != nil || in.InputBytes != nil) && f.InputUnitsSource == nil {
		missing("input_units_source_unavailable")
	}

	terminal := terminalEconomicJob(in.JobStatus)
	if terminal {
		f.SettlementUSD = in.ActualUSD
		if in.ActualUSD == nil {
			missing("quote_derived_settlement_unavailable")
		}
		if in.OutputRecords == nil {
			missing("exact_output_records_unavailable")
		}
		if in.OutputBytes == nil {
			missing("exact_output_bytes_unavailable")
		}
		if (in.OutputRecords != nil || in.OutputBytes != nil) && f.OutputUnitsSource == nil {
			missing("output_units_source_unavailable")
		}
		if in.ResultsMergedAt == nil || in.ResultsMergedAt.Before(in.CreatedAt) {
			missing("control_plane_elapsed_unavailable")
		} else {
			ms := in.ResultsMergedAt.Sub(in.CreatedAt).Milliseconds()
			f.ControlPlaneElapsedMS = &ms
			f.ControlPlaneElapsedSource = economicStringPtr(controlPlaneElapsedBasis)
		}
	}

	if in.VerificationTasksRun == 0 {
		f.VerificationTaskServerMS = economicInt64Ptr(0)
	} else if in.VerificationTasksWithServerMS == in.VerificationTasksRun && in.VerificationTaskServerMSSum != nil {
		f.VerificationTaskServerMS = in.VerificationTaskServerMSSum
	} else {
		missing("verification_server_elapsed_incomplete")
	}

	// Resolve the PaymentIntent that actually collected this job. A batch PI wins
	// whenever charge_batch_id is present; mixing in jobs.stripe_pi would attach a
	// different rail to a batched job.
	pi := in.StripePI
	if in.ChargeBatchID != nil {
		pi = in.ChargeBatchPI
		if in.ChargeBatchStatus == "" {
			missing("charge_batch_record_unavailable")
		}
	}
	f.ProcessorFeePaymentIntent = economicStringPtr(pi)

	switch {
	case !terminal:
		f.ReconciliationState = economicStatePending
	case in.ChargeStatus != "charged":
		f.ReconciliationState = economicStateAwaitingCollection
		f.MissingDataReasons = append(f.MissingDataReasons, "buyer_charge_not_collected:"+in.ChargeStatus)
	case in.BilledUSD == nil:
		f.ReconciliationState = economicStateIncomplete
		missing("billed_usd_unavailable_for_charged_job")
	case pi == "":
		f.ReconciliationState = economicStateIncomplete
		missing("payment_intent_unavailable_for_charged_job")
	case in.ProcessorFeePITotalUSD == nil:
		f.ReconciliationState = economicStateAwaitingProcessorFee
		f.MissingDataReasons = append(f.MissingDataReasons, "stripe_balance_transaction_fee_unavailable")
	case in.ChargeBatchID != nil && in.ProcessorFeeAllocatedUSD != nil:
		fee := roundEconomicUSD(*in.ProcessorFeeAllocatedUSD)
		f.ProcessorFeeUSD = &fee
		f.ProcessorFeeBasis = economicStringPtr(processorFeeBatchAllocatedBasis)
		margin := roundEconomicUSD(*in.BilledUSD - in.SupplierLiabilityUSD - fee)
		f.ContributionMarginUSD = &margin
		if requiredDataIncomplete {
			f.ReconciliationState = economicStateIncomplete
		} else {
			f.ReconciliationState = economicStateComplete
		}
	case in.ChargeBatchID != nil && in.BatchMemberCount > 1:
		f.ReconciliationState = economicStateUnresolvedBatchFee
		f.ProcessorFeeBasis = economicStringPtr(processorFeeBatchTotalBasis)
		f.MissingDataReasons = append(f.MissingDataReasons, "multi_job_batch_fee_allocation_unresolved")
	case in.ChargeBatchID != nil && in.BatchMemberCount != 1:
		f.ReconciliationState = economicStateIncomplete
		f.ProcessorFeeBasis = economicStringPtr(processorFeeBatchTotalBasis)
		missing("charge_batch_membership_unavailable")
	default:
		fee := roundEconomicUSD(*in.ProcessorFeePITotalUSD)
		f.ProcessorFeeUSD = &fee
		if in.ChargeBatchID == nil {
			f.ProcessorFeeBasis = economicStringPtr(processorFeeDirectBasis)
		} else {
			f.ProcessorFeeBasis = economicStringPtr(processorFeeSoloBatchBasis)
		}
		margin := roundEconomicUSD(*in.BilledUSD - in.SupplierLiabilityUSD - fee)
		f.ContributionMarginUSD = &margin
		if requiredDataIncomplete {
			f.ReconciliationState = economicStateIncomplete
		} else {
			f.ReconciliationState = economicStateComplete
		}
	}
	return f
}

// loadEconomicFactInputs reads one coherent database snapshot. Every aggregate is
// tied to the job by task_id or an explicit job/PaymentIntent reference; no email,
// buyer-wide proportional allocation, or quote estimate is used.
func (s *Store) loadEconomicFactInputs(ctx context.Context, jobID uuid.UUID) (economicFactInputs, error) {
	var in economicFactInputs
	err := s.pool.QueryRow(ctx, `
		SELECT j.id, j.buyer_id, j.status, COALESCE(j.charge_status,'not_attempted'),
		       j.economic_input_records, j.economic_input_bytes, COALESCE(j.economic_input_source,''),
		       j.economic_output_records, j.economic_output_bytes, COALESCE(j.economic_output_source,''),
		       j.created_at, j.results_merged_at, j.actual_usd::float8, j.billed_usd::float8,
		       COALESCE(j.stripe_pi,''), j.charge_batch_id,
		       COALESCE(cb.status,''), COALESCE(cb.stripe_pi,''),
		       CASE WHEN j.charge_batch_id IS NULL THEN 0 ELSE
		         (SELECT COUNT(*)::int FROM jobs bj WHERE bj.charge_batch_id = j.charge_batch_id)
		       END,
		       COALESCE(ts.primary_run,0), COALESCE(ts.verification_run,0),
		       COALESCE(ts.retry_attempts,0), COALESCE(tv.verdict_attempts,0),
		       ts.verification_server_ms, COALESCE(ts.verification_with_ms,0),
		       ts.reported_tokens, COALESCE(ts.reported_token_tasks,0),
		       COALESCE(money.supplier_liability_usd,0)::float8,
		       COALESCE(refunds.refunds_usd,0)::float8,
		       fee.pi_fee_usd, bfa.allocated_fee_usd::float8
		  FROM jobs j
		  LEFT JOIN charge_batches cb ON cb.id = j.charge_batch_id
		  LEFT JOIN charge_batch_fee_allocations bfa
		    ON bfa.charge_batch_id=j.charge_batch_id AND bfa.job_id=j.id AND bfa.stripe_pi=cb.stripe_pi
		  LEFT JOIN LATERAL (
		    SELECT
		      (COUNT(*) FILTER (WHERE
		        (t.started_at IS NOT NULL OR t.verified_at IS NOT NULL OR t.retry_count > 0
		         OR t.status IN ('running','verifying','complete','failed'))
		        AND NOT COALESCE(t.is_honeypot,false) AND NOT COALESCE(t.is_redundancy,false)))::int AS primary_run,
		      (COUNT(*) FILTER (WHERE
		        (t.started_at IS NOT NULL OR t.verified_at IS NOT NULL OR t.retry_count > 0
		         OR t.status IN ('running','verifying','complete','failed'))
		        AND (COALESCE(t.is_honeypot,false) OR COALESCE(t.is_redundancy,false))))::int AS verification_run,
		      COALESCE(SUM(t.retry_count),0)::int AS retry_attempts,
		      CASE WHEN COUNT(*) FILTER (WHERE t.reported_tokens_used IS NOT NULL) = 0 THEN NULL
		           ELSE (SUM(t.reported_tokens_used) FILTER (WHERE t.reported_tokens_used IS NOT NULL))::bigint END AS reported_tokens,
		      (COUNT(*) FILTER (WHERE t.reported_tokens_used IS NOT NULL))::int AS reported_token_tasks,
		      CASE WHEN COUNT(*) FILTER (WHERE
		             (COALESCE(t.is_honeypot,false) OR COALESCE(t.is_redundancy,false))
		             AND t.started_at IS NOT NULL AND t.verified_at IS NOT NULL) = 0 THEN NULL
		           ELSE (SUM(EXTRACT(EPOCH FROM (t.verified_at-t.started_at))*1000)
		             FILTER (WHERE (COALESCE(t.is_honeypot,false) OR COALESCE(t.is_redundancy,false))
		                     AND t.started_at IS NOT NULL AND t.verified_at IS NOT NULL))::bigint END AS verification_server_ms,
		      (COUNT(*) FILTER (WHERE
		        (COALESCE(t.is_honeypot,false) OR COALESCE(t.is_redundancy,false))
		        AND t.started_at IS NOT NULL AND t.verified_at IS NOT NULL))::int AS verification_with_ms
		      FROM tasks t WHERE t.job_id = j.id
		  ) ts ON true
		  LEFT JOIN LATERAL (
		    SELECT COUNT(*)::int AS verdict_attempts FROM task_verdicts v WHERE v.job_id = j.id
		  ) tv ON true
		  LEFT JOIN LATERAL (
		    SELECT SUM(CASE WHEN le.kind IN ('supplier_credit','clawback') THEN le.amount_usd ELSE 0 END) AS supplier_liability_usd
		      FROM ledger_entries le
		     WHERE le.task_id IN (SELECT t.id FROM tasks t WHERE t.job_id = j.id)
		       AND le.kind IN ('supplier_credit','clawback')
		  ) money ON true
		  LEFT JOIN LATERAL (
		    SELECT SUM(le.amount_usd) AS refunds_usd
		      FROM ledger_entries le
		     WHERE (le.kind = 'sla_refund' AND le.payout_ref = 'sla-' || j.id::text)
		        OR (le.kind IN ('refund','sla_refund')
		            AND le.task_id IN (SELECT t.id FROM tasks t WHERE t.job_id = j.id))
		  ) refunds ON true
		  LEFT JOIN LATERAL (
		    SELECT SUM(-le.amount_usd)::float8 AS pi_fee_usd
		      FROM ledger_entries le
		     WHERE le.kind = 'stripe_fee'
		       AND le.payout_ref = CASE WHEN j.charge_batch_id IS NULL THEN NULLIF(j.stripe_pi,'') ELSE NULLIF(cb.stripe_pi,'') END
		  ) fee ON true
		 WHERE j.id = $1`, jobID).Scan(
		&in.JobID, &in.BuyerID, &in.JobStatus, &in.ChargeStatus,
		&in.InputRecords, &in.InputBytes, &in.InputSource,
		&in.OutputRecords, &in.OutputBytes, &in.OutputSource,
		&in.CreatedAt, &in.ResultsMergedAt, &in.ActualUSD, &in.BilledUSD,
		&in.StripePI, &in.ChargeBatchID, &in.ChargeBatchStatus, &in.ChargeBatchPI,
		&in.BatchMemberCount, &in.PrimaryTasksRun, &in.VerificationTasksRun,
		&in.RetryAttempts, &in.VerdictAttempts, &in.VerificationTaskServerMSSum,
		&in.VerificationTasksWithServerMS, &in.WorkerReportedTokens,
		&in.WorkerReportedTokensTasks, &in.SupplierLiabilityUSD, &in.RefundsUSD,
		&in.ProcessorFeePITotalUSD, &in.ProcessorFeeAllocatedUSD,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return in, errNotFound
	}
	return in, err
}

// RecomputeJobEconomicFact rebuilds and upserts exactly one current projection.
// The job_id primary key makes retries idempotent; every mutable field is replaced
// from the latest persisted evidence, so a fee backfill or clawback self-heals the
// next report without additive/double-count behavior.
func (s *Store) RecomputeJobEconomicFact(ctx context.Context, jobID uuid.UUID) (JobEconomicFact, error) {
	if err := s.ensureJobBatchFeeAllocation(ctx, jobID); err != nil {
		return JobEconomicFact{}, err
	}
	in, err := s.loadEconomicFactInputs(ctx, jobID)
	if err != nil {
		return JobEconomicFact{}, err
	}
	f := buildJobEconomicFact(in)
	missingJSON, err := json.Marshal(f.MissingDataReasons)
	if err != nil {
		return JobEconomicFact{}, err
	}
	err = s.pool.QueryRow(ctx, `
		INSERT INTO job_economic_facts (
		  job_id,buyer_id,job_status,charge_status,schema_version,reconciliation_state,missing_data_reasons,
		  input_records,input_bytes,input_units_source,output_records,output_bytes,output_units_source,
		  control_plane_elapsed_ms,control_plane_elapsed_source,
		  primary_tasks_run,verification_tasks_run,retry_attempts,verdict_attempts,
		  verification_task_server_ms,verification_tasks_with_server_ms,verification_work_source,
		  worker_reported_tokens,worker_reported_tokens_tasks,worker_reported_tokens_source,
		  settlement_usd,settlement_usd_basis,supplier_liability_usd,supplier_liability_basis,
		  refunds_usd,refunds_basis,billed_usd,billed_usd_basis,
		  processor_fee_payment_intent,processor_fee_payment_intent_total_usd,
		  processor_fee_usd,processor_fee_basis,contribution_margin_usd,recomputed_at
		) VALUES (
		  @job_id,@buyer_id,@job_status,@charge_status,@schema_version,@state,@missing,
		  @input_records,@input_bytes,@input_source,@output_records,@output_bytes,@output_source,
		  @control_ms,@control_source,
		  @primary_run,@verification_run,@retries,@verdicts,
		  @verification_ms,@verification_ms_tasks,@verification_source,
		  @worker_tokens,@worker_token_tasks,@worker_tokens_source,
		  @settlement,@settlement_basis,@supplier_liability,@supplier_basis,
		  @refunds,@refunds_basis,@billed,@billed_basis,
		  @pi,@pi_fee_total,@processor_fee,@processor_basis,@margin,now()
		)
		ON CONFLICT (job_id) DO UPDATE SET
		  buyer_id=EXCLUDED.buyer_id, job_status=EXCLUDED.job_status, charge_status=EXCLUDED.charge_status,
		  schema_version=EXCLUDED.schema_version, reconciliation_state=EXCLUDED.reconciliation_state,
		  missing_data_reasons=EXCLUDED.missing_data_reasons,
		  input_records=EXCLUDED.input_records, input_bytes=EXCLUDED.input_bytes, input_units_source=EXCLUDED.input_units_source,
		  output_records=EXCLUDED.output_records, output_bytes=EXCLUDED.output_bytes, output_units_source=EXCLUDED.output_units_source,
		  control_plane_elapsed_ms=EXCLUDED.control_plane_elapsed_ms,
		  control_plane_elapsed_source=EXCLUDED.control_plane_elapsed_source,
		  primary_tasks_run=EXCLUDED.primary_tasks_run, verification_tasks_run=EXCLUDED.verification_tasks_run,
		  retry_attempts=EXCLUDED.retry_attempts, verdict_attempts=EXCLUDED.verdict_attempts,
		  verification_task_server_ms=EXCLUDED.verification_task_server_ms,
		  verification_tasks_with_server_ms=EXCLUDED.verification_tasks_with_server_ms,
		  verification_work_source=EXCLUDED.verification_work_source,
		  worker_reported_tokens=EXCLUDED.worker_reported_tokens,
		  worker_reported_tokens_tasks=EXCLUDED.worker_reported_tokens_tasks,
		  worker_reported_tokens_source=EXCLUDED.worker_reported_tokens_source,
		  settlement_usd=EXCLUDED.settlement_usd, settlement_usd_basis=EXCLUDED.settlement_usd_basis,
		  supplier_liability_usd=EXCLUDED.supplier_liability_usd,
		  supplier_liability_basis=EXCLUDED.supplier_liability_basis,
		  refunds_usd=EXCLUDED.refunds_usd, refunds_basis=EXCLUDED.refunds_basis,
		  billed_usd=EXCLUDED.billed_usd, billed_usd_basis=EXCLUDED.billed_usd_basis,
		  processor_fee_payment_intent=EXCLUDED.processor_fee_payment_intent,
		  processor_fee_payment_intent_total_usd=EXCLUDED.processor_fee_payment_intent_total_usd,
		  processor_fee_usd=EXCLUDED.processor_fee_usd, processor_fee_basis=EXCLUDED.processor_fee_basis,
		  contribution_margin_usd=EXCLUDED.contribution_margin_usd, recomputed_at=now()
		RETURNING recomputed_at`, pgx.NamedArgs{
		"job_id": f.JobID, "buyer_id": f.BuyerID, "job_status": f.JobStatus,
		"charge_status": f.ChargeStatus, "schema_version": f.SchemaVersion,
		"state": f.ReconciliationState, "missing": missingJSON,
		"input_records": f.InputRecords, "input_bytes": f.InputBytes, "input_source": f.InputUnitsSource,
		"output_records": f.OutputRecords, "output_bytes": f.OutputBytes, "output_source": f.OutputUnitsSource,
		"control_ms": f.ControlPlaneElapsedMS, "control_source": f.ControlPlaneElapsedSource,
		"primary_run": f.PrimaryTasksRun, "verification_run": f.VerificationTasksRun,
		"retries": f.RetryAttempts, "verdicts": f.VerdictAttempts,
		"verification_ms":       f.VerificationTaskServerMS,
		"verification_ms_tasks": f.VerificationTasksWithServerMS,
		"verification_source":   f.VerificationWorkSource,
		"worker_tokens":         f.WorkerReportedTokens, "worker_token_tasks": f.WorkerReportedTokensTasks,
		"worker_tokens_source": f.WorkerReportedTokensSource,
		"settlement":           f.SettlementUSD, "settlement_basis": f.SettlementUSDBasis,
		"supplier_liability": f.SupplierLiabilityUSD, "supplier_basis": f.SupplierLiabilityBasis,
		"refunds": f.RefundsUSD, "refunds_basis": f.RefundsBasis,
		"billed": f.BilledUSD, "billed_basis": f.BilledUSDBasis,
		"pi": f.ProcessorFeePaymentIntent, "pi_fee_total": f.ProcessorFeePaymentIntentTotalUSD,
		"processor_fee": f.ProcessorFeeUSD, "processor_basis": f.ProcessorFeeBasis,
		"margin": f.ContributionMarginUSD,
	}).Scan(&f.RecomputedAt)
	return f, err
}

// GetJobEconomicFact reads the last persisted projection without recomputing it.
func (s *Store) GetJobEconomicFact(ctx context.Context, jobID uuid.UUID) (JobEconomicFact, error) {
	var f JobEconomicFact
	var missing []byte
	err := s.pool.QueryRow(ctx, `
		SELECT job_id,buyer_id,job_status,charge_status,schema_version,reconciliation_state,missing_data_reasons,
		       input_records,input_bytes,input_units_source,output_records,output_bytes,output_units_source,
		       control_plane_elapsed_ms,control_plane_elapsed_source,
		       primary_tasks_run,verification_tasks_run,retry_attempts,verdict_attempts,
		       verification_task_server_ms,verification_tasks_with_server_ms,verification_work_source,
		       worker_reported_tokens,worker_reported_tokens_tasks,worker_reported_tokens_source,
		       settlement_usd::float8,settlement_usd_basis,supplier_liability_usd::float8,supplier_liability_basis,
		       refunds_usd::float8,refunds_basis,billed_usd::float8,billed_usd_basis,
		       processor_fee_payment_intent,processor_fee_payment_intent_total_usd::float8,
		       processor_fee_usd::float8,processor_fee_basis,contribution_margin_usd::float8,recomputed_at
		  FROM job_economic_facts WHERE job_id=$1`, jobID).Scan(
		&f.JobID, &f.BuyerID, &f.JobStatus, &f.ChargeStatus, &f.SchemaVersion,
		&f.ReconciliationState, &missing, &f.InputRecords, &f.InputBytes, &f.InputUnitsSource,
		&f.OutputRecords, &f.OutputBytes, &f.OutputUnitsSource, &f.ControlPlaneElapsedMS,
		&f.ControlPlaneElapsedSource, &f.PrimaryTasksRun, &f.VerificationTasksRun,
		&f.RetryAttempts, &f.VerdictAttempts, &f.VerificationTaskServerMS,
		&f.VerificationTasksWithServerMS, &f.VerificationWorkSource,
		&f.WorkerReportedTokens, &f.WorkerReportedTokensTasks, &f.WorkerReportedTokensSource,
		&f.SettlementUSD, &f.SettlementUSDBasis, &f.SupplierLiabilityUSD,
		&f.SupplierLiabilityBasis, &f.RefundsUSD, &f.RefundsBasis, &f.BilledUSD,
		&f.BilledUSDBasis, &f.ProcessorFeePaymentIntent,
		&f.ProcessorFeePaymentIntentTotalUSD, &f.ProcessorFeeUSD, &f.ProcessorFeeBasis,
		&f.ContributionMarginUSD, &f.RecomputedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return f, errNotFound
	}
	if err != nil {
		return f, err
	}
	if err := json.Unmarshal(missing, &f.MissingDataReasons); err != nil {
		return f, err
	}
	return f, nil
}

// RecomputeRecentJobEconomicFacts is the admin report path. It recomputes the
// newest jobs before returning them, so the report immediately sees a fee
// backfill, refund, or clawback rather than serving a stale materialization.
func (s *Store) RecomputeRecentJobEconomicFacts(ctx context.Context, limit int) ([]JobEconomicFact, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	rows, err := s.pool.Query(ctx, `SELECT id FROM jobs ORDER BY created_at DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	var ids []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	out := make([]JobEconomicFact, 0, len(ids))
	for _, id := range ids {
		f, err := s.RecomputeJobEconomicFact(ctx, id)
		if err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, nil
}

// handleAdminEconomicFacts serves the per-job economics report. Platform margin
// stays admin-only; buyer receipts should expose charges/refunds, not CX margin.
func (s *Server) handleAdminEconomicFacts(w http.ResponseWriter, r *http.Request) {
	limit := 200
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil || v < 1 || v > 200 {
			writeErr(w, http.StatusBadRequest, "limit must be an integer from 1 to 200")
			return
		}
		limit = v
	}
	facts, err := s.store.RecomputeRecentJobEconomicFacts(r.Context(), limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"facts": facts,
		"count": len(facts),
	})
}
