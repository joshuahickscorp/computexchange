package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// collect.go — the charge-collect sweep: the MONEY-TRUTH layer that turns "owed
// in the ledger" into "actually collected", without bleeding ~30¢ of Stripe fixed
// fee on every sub-$5 job. Four duties, one 60s ticker (workers.go):
//
//   1. RETRY attempting batches: a formed batch whose PaymentIntent was not
//      confirmed (failure OR an ambiguous network timeout) is re-charged with the
//      SAME idempotency key ("cxbatch-"+id) and the FROZEN row amount — Stripe
//      replays the first outcome, so a retry after ambiguity can never charge twice.
//   2. FORM new batches: deferred (sub-threshold) jobs are grouped per buyer and
//      batched into ONE PaymentIntent once the buyer's deferred sum reaches
//      CX_CHARGE_MIN_USD or the oldest deferred job turns 24h old. A buyer with no
//      saved card gets those jobs flipped to the existing honest
//      'no_payment_method' state — and flipped BACK to 'deferred' the moment a
//      card appears, so the debt is collected, not stranded.
//   3. TERMINAL COVERAGE: jobs settled by the watchdog/fail paths (cancelled or
//      failed with actual_usd > 0) whose charge was never attempted — the paths
//      that credit suppliers for delivered chunks but billed nobody — are routed
//      through the SAME immediate-or-defer decision as everything else.
//   4. RETRY failed singles: a single-job charge that failed is retried with its
//      ORIGINAL "job-"+jobID idempotency key, backed off 30min × attempts (capped
//      at 6h) so a dead card is not hammered. It is never given up silently — the
//      ledger keeps the amount owed, and every failure is logged.
//
// Plus the stripe_fee backfill: any confirmed charge (job or batch) with a
// PaymentIntent but no stripe_fee ledger row gets its REAL fee fetched and
// recorded (recordStripeFee is idempotent per PI), so a transient fee-fetch
// failure self-heals instead of leaving the fee ledger short.
//
// The whole sweep is gated on stripeKey(): unconfigured billing is one honest log
// line and a clean no-op — nothing is charged, deferred, or faked.

const (
	// chargeCollectInterval is the sweep cadence. 60s: fast enough that an
	// ambiguous batch attempt is resolved within a minute, slow enough that a
	// failing card sees its backoff respected rather than a tight retry loop.
	chargeCollectInterval = 60 * time.Second
	// defaultChargeMinUSD is the batching threshold when CX_CHARGE_MIN_USD is
	// unset: a settled job below this is deferred into a per-buyer batch instead
	// of eating a fixed Stripe fee alone.
	defaultChargeMinUSD = 5.00
	// chargeBatchMaxAge forces a batch for a buyer whose deferred jobs never
	// reach the threshold: once the OLDEST deferred job is this old, the sum is
	// collected anyway — small buyers are billed within a day, not never.
	chargeBatchMaxAge = 24 * time.Hour
	// chargeRetryStep / chargeRetryMax shape the failed-single backoff:
	// next retry = attempts × chargeRetryStep, capped at chargeRetryMax.
	chargeRetryStep = 30 * time.Minute
	chargeRetryMax  = 6 * time.Hour
)

// firmChargeAmountSQL is the shared SQL expression for "the amount this job
// should actually be charged" (Project Detection & Quotation 7->8,
// docs/internal/CREED_AND_PATH_TO_TEN.md, "Ship a firm-quote tier: a real
// commitment, not just an estimate"): actual_usd normally, but capped at
// firm_quote_max_usd for a firm-quote job whose real cost exceeded what it
// committed to — and, since Speed Lane wave 2A, NET of any recorded speed-SLA
// refund (the sla_refund ledger credit keyed 'sla-<job_id>'; see
// settleSLAOutcome below), floored at zero. Used by BOTH charge paths — the
// immediate single-job path (Store.JobChargeInfo, the Go-side twin of this
// same logic) and the batch path (FormChargeBatch) — so neither the firm cap
// nor the SLA remedy can be bypassed by which path happens to collect the job.
const firmChargeAmountSQL = `GREATEST(0, CASE
	WHEN firm_quote AND COALESCE(firm_quote_max_usd,0) > 0
	     AND COALESCE(actual_usd,0) > firm_quote_max_usd
	THEN firm_quote_max_usd
	ELSE COALESCE(actual_usd,0)
END - COALESCE((SELECT SUM(le.amount_usd) FROM ledger_entries le
                WHERE le.kind = 'sla_refund'
                  AND le.payout_ref = 'sla-' || jobs.id::text), 0))`

// chargeMinUSD reads the CX_CHARGE_MIN_USD batching threshold (USD). Unset or
// unparseable → the 5.00 default; a negative value is clamped to 0 (= batch
// nothing, charge every job immediately — an explicit operator choice).
// stripeMinChargeUSD is Stripe's minimum card charge. A batch below it would be
// rejected on every attempt and wedge forever, so formation SKIPS sub-minimum
// sums — the debt stays honestly 'deferred' and accumulates until it clears the
// floor (or forever, for a buyer who owes 10 cents and never returns; the ledger
// keeps it owed either way).
const stripeMinChargeUSD = 0.50

func chargeMinUSD() float64 {
	s := strings.TrimSpace(os.Getenv("CX_CHARGE_MIN_USD"))
	if s == "" {
		return defaultChargeMinUSD
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return defaultChargeMinUSD
	}
	if v < 0 {
		return 0
	}
	return v
}

// shouldDeferCharge is the pure immediate-vs-defer decision: a settled actual
// below the threshold is deferred into a per-buyer batch; at or above it is
// charged immediately. Exactly one comparison — kept as a function so the
// finalize path and the sweep can never drift apart, and so it is unit-testable.
func shouldDeferCharge(actualUSD, thresholdUSD float64) bool {
	return actualUSD < thresholdUSD
}

// chargeRetryBackoff is the pure failed-single backoff: attempts × 30min, capped
// at 6h. attempts is the post-increment count (>= 1); anything lower is treated
// as 1 so the backoff is never zero.
func chargeRetryBackoff(attempts int) time.Duration {
	if attempts < 1 {
		attempts = 1
	}
	d := time.Duration(attempts) * chargeRetryStep
	if d > chargeRetryMax {
		d = chargeRetryMax
	}
	return d
}

// --- store access (collect-sweep queries, kept next to the loop that owns them,
// --- the same file-locality pattern as summary.go's AdminSummaryData) ---

// ChargeBatch is one charge_batches row the sweep acts on.
type ChargeBatch struct {
	ID        uuid.UUID
	BuyerID   uuid.UUID
	AmountUSD float64
}

// AttemptingChargeBatches lists batches not yet confirmed charged, oldest first.
func (s *Store) AttemptingChargeBatches(ctx context.Context, limit int) ([]ChargeBatch, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, buyer_id, amount_usd::float8 FROM charge_batches
		 WHERE status = 'attempting' AND (next_at IS NULL OR next_at < now())
		 ORDER BY created_at ASC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ChargeBatch
	for rows.Next() {
		var b ChargeBatch
		if err := rows.Scan(&b.ID, &b.BuyerID, &b.AmountUSD); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

// MarkChargeBatchCharged confirms a batch's PaymentIntent in one transaction:
// the batch row flips to 'charged' (stripe_pi + charged_at) and every member job
// flips to charge_status='charged'. Guarded on status='attempting' so a replayed
// confirmation is a no-op.
func (s *Store) MarkChargeBatchCharged(ctx context.Context, batchID uuid.UUID, pi string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx,
		`UPDATE charge_batches SET status = 'charged', stripe_pi = $2, charged_at = now()
		 WHERE id = $1 AND status = 'attempting'`, batchID, pi); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE jobs SET charge_status = 'charged' WHERE charge_batch_id = $1`, batchID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// ReflipNoCardJobs makes stranded 'no_payment_method' jobs re-eligible for
// collection the moment their buyer saves a card: flipped back to 'deferred' so
// the next batch-formation pass picks them up. Only unbatched jobs with a real
// settled amount qualify. Returns how many were flipped (0 is the common case).
func (s *Store) ReflipNoCardJobs(ctx context.Context) (int64, error) {
	tag, err := s.pool.Exec(ctx,
		`UPDATE jobs SET charge_status = 'deferred', deferred_at = now()
		 WHERE charge_status = 'no_payment_method'
		   AND charge_batch_id IS NULL
		   AND COALESCE(actual_usd, 0) > 0
		   AND buyer_id IN (SELECT buyer_id FROM billing_customers
		                    WHERE COALESCE(default_payment_method,'') <> '')`)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// BuyersDueForBatch lists buyers whose unbatched deferred jobs are ready to
// collect: the deferred sum reached the threshold, OR the oldest deferred job is
// older than maxAge (a small buyer is billed within a day, not never).
func (s *Store) BuyersDueForBatch(ctx context.Context, thresholdUSD float64, maxAge time.Duration, limit int) ([]uuid.UUID, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT buyer_id FROM jobs
		 WHERE charge_status = 'deferred' AND charge_batch_id IS NULL
		   AND COALESCE(actual_usd, 0) > 0
		 GROUP BY buyer_id
		 HAVING (SUM(actual_usd) >= $1
		         OR MIN(COALESCE(deferred_at, created_at)) < now() - make_interval(secs => $2))
		   AND SUM(actual_usd) >= $4
		 ORDER BY MIN(COALESCE(deferred_at, created_at)) ASC LIMIT $3`,
		thresholdUSD, maxAge.Seconds(), limit, stripeMinChargeUSD)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// MarkBuyerDeferredNoCard flips a buyer's unbatched deferred jobs to the honest
// 'no_payment_method' state (they were due for collection but there is no saved
// card to charge). ReflipNoCardJobs undoes this the moment a card appears, so
// the state is a parking spot, not a write-off. Returns the affected job ids so
// the caller can surface the debt on each job's timeline.
func (s *Store) MarkBuyerDeferredNoCard(ctx context.Context, buyerID uuid.UUID) ([]uuid.UUID, error) {
	rows, err := s.pool.Query(ctx,
		`UPDATE jobs SET charge_status = 'no_payment_method'
		 WHERE buyer_id = $1 AND charge_status = 'deferred' AND charge_batch_id IS NULL
		 RETURNING id`, buyerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// FormChargeBatch freezes one buyer's unbatched deferred jobs into a new batch,
// in ONE transaction: the member rows are locked (FOR UPDATE), the batch row is
// inserted with the FROZEN sum of exactly those rows, and each is stamped with
// the batch id under the same WHERE (charge_status='deferred' AND
// charge_batch_id IS NULL) — so a concurrent sweep or finalize can neither
// double-batch a job nor smuggle an uncounted job into the frozen amount.
// formed=false (no error) when the buyer had nothing to batch.
func (s *Store) FormChargeBatch(ctx context.Context, buyerID uuid.UUID) (batch ChargeBatch, formed bool, err error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return batch, false, err
	}
	defer tx.Rollback(ctx)

	// Membership is capped per batch: an unbounded sum can overflow the NUMERIC
	// column (months of parked debt re-armed at once) and a giant member set makes
	// the freeze transaction long. Leftovers simply form the next batch next tick.
	//
	// firmChargeAmountSQL (Project Detection & Quotation 7->8): a batched job can
	// be firm-quoted too, and batching must never let it bypass the cap
	// JobChargeInfo already enforces on the immediate-charge path — a firm-quote
	// job that happens to settle under the batching threshold would otherwise be
	// charged its full uncapped actual_usd once it lands in a batch with siblings.
	rows, err := tx.Query(ctx,
		`SELECT id, `+firmChargeAmountSQL+`::float8 FROM jobs
		 WHERE buyer_id = $1 AND charge_status = 'deferred' AND charge_batch_id IS NULL
		   AND COALESCE(actual_usd, 0) > 0
		 ORDER BY created_at ASC
		 LIMIT 500
		 FOR UPDATE`, buyerID)
	if err != nil {
		return batch, false, err
	}
	var ids []string
	var sum float64
	for rows.Next() {
		var id uuid.UUID
		var usd float64
		if err := rows.Scan(&id, &usd); err != nil {
			rows.Close()
			return batch, false, err
		}
		ids = append(ids, id.String())
		sum += usd
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return batch, false, err
	}
	if len(ids) == 0 || sum <= 0 {
		return batch, false, nil // raced away or nothing chargeable — clean no-op
	}
	if sum < stripeMinChargeUSD {
		// Below Stripe's minimum charge: forming this batch would wedge it on a
		// per-tick rejection forever. Leave the jobs deferred to keep accumulating.
		return batch, false, nil
	}

	if err := tx.QueryRow(ctx,
		`INSERT INTO charge_batches (buyer_id, amount_usd) VALUES ($1, $2) RETURNING id`,
		buyerID, sum).Scan(&batch.ID); err != nil {
		return batch, false, err
	}
	// billed_usd is stamped HERE (not just charge_attempt_usd, which this batch
	// path never sets) so a firm-quote job's invoice can show the real capped
	// amount it was actually batched at, not its uncapped actual_usd.
	if _, err := tx.Exec(ctx,
		`UPDATE jobs SET charge_batch_id = $1, billed_usd = `+firmChargeAmountSQL+`
		 WHERE id = ANY($2::uuid[]) AND charge_status = 'deferred' AND charge_batch_id IS NULL`,
		batch.ID, ids); err != nil {
		return batch, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return batch, false, err
	}
	batch.BuyerID, batch.AmountUSD = buyerID, sum
	return batch, true, nil
}

// TerminalUnattemptedJobs finds the confirmed leak: terminal jobs with a real
// settled amount whose charge was never attempted (the watchdog-cancel and
// fail-settle paths credit suppliers for delivered chunks but call no charge
// site). Routed through the same immediate-or-defer decision as everything else.
func (s *Store) TerminalUnattemptedJobs(ctx context.Context, limit int) ([]uuid.UUID, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id FROM jobs
		 WHERE status IN ('complete','failed','cancelled')
		   AND COALESCE(actual_usd, 0) > 0
		   AND charge_status = 'not_attempted'
		 ORDER BY created_at ASC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// FailedChargesDue lists failed single-job charges whose retry backoff has
// elapsed (or was never set — a pre-backoff row).
func (s *Store) FailedChargesDue(ctx context.Context, limit int) ([]uuid.UUID, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id FROM jobs
		 WHERE charge_status = 'failed'
		   AND (charge_next_at IS NULL OR charge_next_at < now())
		 ORDER BY created_at ASC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// IncrementChargeAttempts bumps a job's failed-charge attempt counter and
// returns the post-increment count (the input to chargeRetryBackoff).
func (s *Store) IncrementChargeAttempts(ctx context.Context, jobID uuid.UUID) (int, error) {
	var attempts int
	err := s.pool.QueryRow(ctx,
		`UPDATE jobs SET charge_attempts = charge_attempts + 1 WHERE id = $1
		 RETURNING charge_attempts`, jobID).Scan(&attempts)
	return attempts, err
}

// SetChargeNextAt schedules a failed single's next retry.
func (s *Store) SetChargeNextAt(ctx context.Context, jobID uuid.UUID, at time.Time) error {
	_, err := s.pool.Exec(ctx, `UPDATE jobs SET charge_next_at = $2 WHERE id = $1`, jobID, at)
	return err
}

// SetJobCharged records a confirmed single-job charge: charge_status='charged'
// plus the PaymentIntent id the stripe_fee backfill scan keys on.
// JobChargeStatus reads a job's current charge_status — the double-charge guard's
// input (a re-finalize may only decide a 'not_attempted' job).
func (s *Store) JobChargeStatus(ctx context.Context, jobID uuid.UUID) (string, error) {
	var st string
	err := s.pool.QueryRow(ctx, `SELECT charge_status FROM jobs WHERE id = $1`, jobID).Scan(&st)
	return st, err
}

// MarkJobDeferred parks a sub-threshold settled job for batching — GUARDED on
// 'not_attempted' so a re-finalize can never clobber a decided state, and stamping
// deferred_at so the 24h batching age counts from DEFERRAL, not job creation (a
// long-queued job still gets its full accumulation window).
func (s *Store) MarkJobDeferred(ctx context.Context, jobID uuid.UUID) (bool, error) {
	ct, err := s.pool.Exec(ctx,
		`UPDATE jobs SET charge_status = 'deferred', deferred_at = now()
		 WHERE id = $1 AND charge_status = 'not_attempted'`, jobID)
	if err != nil {
		return false, err
	}
	return ct.RowsAffected() > 0, nil
}

// FreezeChargeAmount records the amount a single-job charge attempt is made for,
// once (first writer wins): every retry must replay the SAME (idempotency key,
// amount) pair, so the figure is pinned before the first attempt and never drifts
// with a later re-settle. Also stamps billed_usd — the real amount the buyer is
// actually being charged (already capped by JobChargeInfo for a firm-quote job
// whose actual cost exceeded its committed maximum) — so the invoice can show the
// cap took effect, not just the ledger's own uncapped actual_usd.
func (s *Store) FreezeChargeAmount(ctx context.Context, jobID uuid.UUID, usd float64) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE jobs SET charge_attempt_usd = $2, billed_usd = COALESCE(billed_usd, $2)
		 WHERE id = $1 AND charge_attempt_usd IS NULL`, jobID, usd)
	return err
}

// JobFrozenChargeInfo returns the buyer and the FROZEN attempt amount for a
// single-job charge retry (falling back to actual_usd only when no attempt was
// ever frozen — a pre-migration row).
func (s *Store) JobFrozenChargeInfo(ctx context.Context, jobID uuid.UUID) (uuid.UUID, float64, error) {
	var buyerID uuid.UUID
	var usd float64
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id, COALESCE(charge_attempt_usd, actual_usd, 0)::float8
		 FROM jobs WHERE id = $1`, jobID).Scan(&buyerID, &usd)
	return buyerID, usd, err
}

// BumpChargeBatchRetry advances a failed batch's attempt counter and schedules the
// next try (same 30min x attempts <= 6h schedule as failed singles), so a dead card
// is not hammered once a minute forever. Returns the new attempt count.
func (s *Store) BumpChargeBatchRetry(ctx context.Context, batchID uuid.UUID, backoff func(int) time.Duration) (int, error) {
	var attempts int
	if err := s.pool.QueryRow(ctx,
		`UPDATE charge_batches SET attempts = attempts + 1 WHERE id = $1 RETURNING attempts`,
		batchID).Scan(&attempts); err != nil {
		return 0, err
	}
	_, err := s.pool.Exec(ctx,
		`UPDATE charge_batches SET next_at = now() + make_interval(secs => $2) WHERE id = $1`,
		batchID, backoff(attempts).Seconds())
	return attempts, err
}

func (s *Store) SetJobCharged(ctx context.Context, jobID uuid.UUID, pi string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE jobs SET charge_status = 'charged', stripe_pi = $2 WHERE id = $1`, jobID, pi)
	return err
}

// InsertStripeFee writes the one negative 'stripe_fee' ledger row for a
// PaymentIntent, if absent. payout_ref = the PI id is the idempotency key
// (INSERT-if-absent + the ledger_stripe_fee_ref_uniq partial unique index), so a
// replay — the finalize path and the backfill sweep both call this — never
// double-counts a fee.
func (s *Store) InsertStripeFee(ctx context.Context, buyerID uuid.UUID, pi string, feeUSD float64) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO ledger_entries (kind, buyer_id, amount_usd, payout_status, payout_ref)
		 SELECT 'stripe_fee', $1, $2, 'released', $3
		 WHERE NOT EXISTS (SELECT 1 FROM ledger_entries WHERE kind = 'stripe_fee' AND payout_ref = $3)`,
		buyerID, -feeUSD, pi)
	return err
}

// UnfeedCharge is one confirmed charge (single job or batch) whose PaymentIntent
// has no stripe_fee ledger row yet — the backfill scan's work item.
type UnfeedCharge struct {
	BuyerID uuid.UUID
	PI      string
}

// ChargesMissingFeeRows scans confirmed charges — jobs and batches — that carry
// a PaymentIntent id but no matching stripe_fee ledger row. These are charges
// whose fee fetch failed (or whose balance_transaction had not settled yet);
// the sweep retries them until the REAL fee is recorded. Charges from before
// the stripe_pi column existed have no PI stored and are honestly out of reach
// (their fee is unknown, never estimated).
func (s *Store) ChargesMissingFeeRows(ctx context.Context, limit int) ([]UnfeedCharge, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT buyer_id, stripe_pi FROM jobs
		 WHERE charge_status = 'charged' AND COALESCE(stripe_pi,'') <> ''
		   AND NOT EXISTS (SELECT 1 FROM ledger_entries
		                   WHERE kind = 'stripe_fee' AND payout_ref = jobs.stripe_pi)
		 UNION ALL
		 SELECT buyer_id, stripe_pi FROM charge_batches
		 WHERE status = 'charged' AND COALESCE(stripe_pi,'') <> ''
		   AND NOT EXISTS (SELECT 1 FROM ledger_entries
		                   WHERE kind = 'stripe_fee' AND payout_ref = charge_batches.stripe_pi)
		 LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []UnfeedCharge
	for rows.Next() {
		var u UnfeedCharge
		if err := rows.Scan(&u.BuyerID, &u.PI); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// --- speed-SLA enforcement (Speed Lane wave 2A,
// --- docs/speed-lane-reports/SLA_QUOTE_WAVE2A.md) -------------------------------
//
// A job that bound a speed-SLA (jobs.sla_guarantee_secs, stamped at submit from
// the quote's offer) is judged ONCE, at completion, on the buyer-visible span:
// created_at (submit) → results_merged_at (the merged artifact exists). Met →
// sla_met=true and nothing else. Missed → sla_met=false + exactly one
// sla_refund ledger credit for the premium (capped at what the job is actually
// chargeable for — the remedy nets a bill down, it never mints free money) + a
// buyer-visible sla_missed timeline event. The refund is then netted off the
// collection by firmChargeAmountSQL / JobChargeInfo — the EXISTING collect
// rails, no new payment path.
//
// Idempotent by construction, because THREE sites can observe the same miss
// (the commit-path finalize, this file's sweep pass, and a re-run of either):
// the jobs row is locked FOR UPDATE while deciding, sla_met is stamped once
// (later calls see it non-NULL and no-op), and the refund insert is
// INSERT-if-absent by payout_ref ('sla-<job_id>') backed by the
// ledger_sla_refund_ref_uniq partial unique index — the same replay-proof
// pattern as the stripe_fee recorder.
//
// Deliberately NOT wired into deadline_secs/the stuck-run watchdog: the
// deadline drives a rescue→KILL ladder, and a missed SLA must COMPLETE and
// REFUND — killing a late job would destroy the buyer's results to punish
// lateness. A job that never completes (failed/cancelled) is likewise not
// judged here: its remedy is the existing partial-settle path (pay only for
// completed tasks), and its sla_met honestly stays NULL — named boundary in
// the wave report.

// KindSLARefund is the ledger kind of the speed-SLA miss remedy: a POSITIVE
// buyer amount (credit — the schema's sign convention), payout_ref =
// slaRefundRef(job) as the idempotency key.
const KindSLARefund = "sla_refund"

// slaRefundRef is the sla_refund ledger row's payout_ref for a job — the
// idempotency key the partial unique index enforces (one refund per job, ever).
func slaRefundRef(jobID uuid.UUID) string { return "sla-" + jobID.String() }

// slaRefundAmount is the pure remedy figure: the full premium, capped at what
// the job is actually chargeable for (a refund larger than the bill would be
// minted money, not a remedy). Unit-tested directly.
func slaRefundAmount(premiumUSD, chargeableUSD float64) float64 {
	if premiumUSD <= 0 || chargeableUSD <= 0 {
		return 0
	}
	if premiumUSD > chargeableUSD {
		return chargeableUSD
	}
	return premiumUSD
}

// slaSpanMissed is the pure miss test: the buyer-visible span (submit →
// results merged) strictly exceeded the guarantee. Landing exactly ON the
// guarantee is a MET (the promise is "within"). Unit-tested directly.
func slaSpanMissed(createdAt, mergedAt time.Time, guaranteeSecs int) bool {
	return mergedAt.Sub(createdAt) > time.Duration(guaranteeSecs)*time.Second
}

// SLASettleResult reports what SettleJobSLA actually did, so the caller emits
// the buyer-visible event exactly once (only the call that DECIDED emits).
type SLASettleResult struct {
	Decided    bool    // this call stamped sla_met (false: no SLA / already decided / not finalized yet)
	Met        bool    // the outcome stamped by this call
	RefundUSD  float64 // the sla_refund credit recorded by this call (0 on met / nothing chargeable)
	OverBySecs int     // how far past the guarantee the span landed (miss only; for the event text)
}

// SettleJobSLA decides one job's speed-SLA outcome, exactly once, inside a
// single transaction. The jobs row is locked FOR UPDATE so the competing
// settle sites serialize; the refund insert is INSERT-if-absent by payout_ref
// (plus the partial unique index) so even a settle racing an already-committed
// sibling can never double-credit.
func (s *Store) SettleJobSLA(ctx context.Context, jobID uuid.UUID) (SLASettleResult, error) {
	var res SLASettleResult
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return res, err
	}
	defer tx.Rollback(ctx)

	var (
		buyerID    uuid.UUID
		guarantee  int
		premium    float64
		met        *bool
		status     string
		createdAt  time.Time
		mergedAt   *time.Time
		chargeable float64 // firm-capped actual BEFORE refund netting (no refund exists yet)
	)
	err = tx.QueryRow(ctx,
		`SELECT buyer_id, COALESCE(sla_guarantee_secs,0), COALESCE(sla_premium_usd,0)::float8,
		        sla_met, status, created_at, results_merged_at,
		        (CASE WHEN firm_quote AND COALESCE(firm_quote_max_usd,0) > 0
		                   AND COALESCE(actual_usd,0) > firm_quote_max_usd
		              THEN firm_quote_max_usd
		              ELSE COALESCE(actual_usd,0) END)::float8
		   FROM jobs WHERE id = $1
		    FOR UPDATE`,
		jobID,
	).Scan(&buyerID, &guarantee, &premium, &met, &status, &createdAt, &mergedAt, &chargeable)
	if err != nil {
		return res, err
	}
	// Only a completed, merged, SLA-carrying, not-yet-decided job is judged —
	// anything else is a clean no-op (the sweep retries next tick when the job
	// simply has not finished merging yet).
	if guarantee <= 0 || met != nil || status != "complete" || mergedAt == nil {
		return res, nil
	}

	if !slaSpanMissed(createdAt, *mergedAt, guarantee) {
		if _, err := tx.Exec(ctx,
			`UPDATE jobs SET sla_met = true WHERE id = $1 AND sla_met IS NULL`, jobID); err != nil {
			return res, err
		}
		if err := tx.Commit(ctx); err != nil {
			return res, err
		}
		return SLASettleResult{Decided: true, Met: true}, nil
	}

	// MISS: record the once-only refund credit (the premium, capped at the
	// chargeable amount) and stamp the outcome, atomically.
	refund := slaRefundAmount(premium, chargeable)
	if refund > 0 {
		if _, err := tx.Exec(ctx,
			`INSERT INTO ledger_entries (kind, buyer_id, amount_usd, payout_status, payout_ref)
			 SELECT $1, $2, $3, 'released', $4
			 WHERE NOT EXISTS (SELECT 1 FROM ledger_entries WHERE kind = $1 AND payout_ref = $4)`,
			KindSLARefund, buyerID, refund, slaRefundRef(jobID)); err != nil {
			return res, err
		}
	}
	if _, err := tx.Exec(ctx,
		`UPDATE jobs SET sla_met = false WHERE id = $1 AND sla_met IS NULL`, jobID); err != nil {
		return res, err
	}
	if err := tx.Commit(ctx); err != nil {
		return res, err
	}
	over := int(mergedAt.Sub(createdAt)/time.Second) - guarantee
	return SLASettleResult{Decided: true, Met: false, RefundUSD: refund, OverBySecs: over}, nil
}

// SLAUndecidedCompleteJobs lists completed, merged jobs whose bound speed-SLA
// outcome has not been decided yet — the sweep's work items. This is the
// backstop for jobs finalized by the background completion sweep
// (workers.go sweepAndDeliver), which does not run the commit-path finalize.
func (s *Store) SLAUndecidedCompleteJobs(ctx context.Context, limit int) ([]uuid.UUID, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id FROM jobs
		 WHERE COALESCE(sla_guarantee_secs,0) > 0
		   AND sla_met IS NULL
		   AND status = 'complete'
		   AND results_merged_at IS NOT NULL
		 ORDER BY created_at ASC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// settleSLAOutcome is the shared settle entry point: decide the outcome (once)
// and, if THIS call decided a miss, surface it on the buyer's timeline. Called
// from the commit-path finalize (api.go finalizeJobIfDone, BEFORE the charge
// decision so the refund nets the very first collection) and from the collect
// sweep below (the backstop for sweep-finalized jobs).
func settleSLAOutcome(ctx context.Context, store *Store, jobID uuid.UUID) {
	res, err := store.SettleJobSLA(ctx, jobID)
	if err != nil {
		log.Printf("sla: settling outcome for job %s: %v (retried by the collect sweep)", jobID, err)
		return
	}
	if !res.Decided {
		return
	}
	if res.Met {
		log.Printf("sla: job %s met its speed-SLA (guarantee held)", jobID)
		return
	}
	// Miss: the event is emitted only by the call that decided (Decided=true),
	// so a re-run/sweep replay never duplicates it.
	_ = store.InsertJobEvent(ctx, jobID, nil, "sla_missed",
		fmt.Sprintf("Speed SLA missed by %ds · the $%.6f premium was refunded automatically (netted off your charge)", res.OverBySecs, res.RefundUSD), nil)
	// cx_sla_misses_total (Speed Lane wave 2A): bumped HERE, in the same
	// decided-a-miss branch that stamps the sla_missed event — reachable only when
	// SettleJobSLA returned Decided && !Met, i.e. THIS call is the one that durably
	// stamped sla_met=false and recorded the refund. A re-settle of an
	// already-missed job returns Decided=false (sla_met is non-NULL, the FOR UPDATE
	// query no-ops) and bails at the `!res.Decided` guard above without reaching
	// here, so with three competing settle sites the miss is still counted exactly
	// ONCE per job — the same replay-proof semantics that gate the event and the
	// ledger row. This retires the "a dedicated metrics counter would live in
	// metrics.go" follow-up the prior version of this function noted.
	metrics.slaMisses.Add(1)
	log.Printf("sla: job %s MISSED its speed-SLA by %ds — refunded $%.6f (once, ledger sla_refund)", jobID, res.OverBySecs, res.RefundUSD)
}

// settleSLAOutcomes is the sweep pass: judge every completed-but-undecided
// SLA job. Runs inside the charge-collect tick BEFORE any charging duty, and
// deliberately BEFORE the Stripe gate — the SLA outcome and its ledger truth
// are independent of whether billing is configured.
func (wk *Workers) settleSLAOutcomes(ctx context.Context) error {
	ids, err := wk.store.SLAUndecidedCompleteJobs(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, id := range ids {
		settleSLAOutcome(ctx, wk.store, id)
	}
	return nil
}

// --- the sweep ---

// collectCharges is the charge-collect tick. Gated on stripeKey(): unconfigured
// billing is one honest log line and a clean no-op. Each duty's failure is
// logged and never aborts the others — one bad buyer must not stall collection
// for the rest — but a query failure surfaces as the tick's error so the
// liveness guard sees a genuinely wedged sweep.
func (wk *Workers) collectCharges(ctx context.Context) error {
	// Speed-SLA outcomes FIRST, and before the Stripe gate (wave 2A): the
	// outcome + refund are ledger truth independent of billing configuration,
	// and running them ahead of every charging duty below guarantees a
	// sweep-finalized job's refund exists BEFORE its terminal-coverage charge is
	// decided in the same tick — the refund nets the very first collection.
	if err := wk.settleSLAOutcomes(ctx); err != nil {
		return err
	}

	if stripeKey() == "" {
		log.Print("workers: charge-collect: skipped — billing not configured (STRIPE_SECRET_KEY unset), nothing charged or faked")
		return nil
	}

	// 1. Retry attempting batches: same idempotency key + frozen amount, so an
	// ambiguous prior attempt is replayed by Stripe, never charged twice.
	batches, err := wk.store.AttemptingChargeBatches(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, b := range batches {
		wk.chargeBatch(ctx, b)
	}

	// 2a. Re-arm stranded no-card jobs whose buyer now has a card.
	if n, rerr := wk.store.ReflipNoCardJobs(ctx); rerr != nil {
		return rerr
	} else if n > 0 {
		log.Printf("workers: charge-collect: %d no_payment_method job(s) re-eligible (buyer saved a card) — back to deferred", n)
	}

	// 2b. Form new batches per due buyer.
	threshold := chargeMinUSD()
	buyers, err := wk.store.BuyersDueForBatch(ctx, threshold, chargeBatchMaxAge, sweepBatch)
	if err != nil {
		return err
	}
	for _, buyerID := range buyers {
		cust, pm, gerr := wk.store.GetBillingCustomer(ctx, buyerID)
		if gerr != nil || cust == "" || pm == "" {
			// Due for collection but no saved card: park the jobs in the existing
			// honest state (re-armed automatically once a card appears).
			ids, merr := wk.store.MarkBuyerDeferredNoCard(ctx, buyerID)
			if merr != nil {
				log.Printf("workers: charge-collect: marking buyer %s deferred jobs no_payment_method: %v", buyerID, merr)
				continue
			}
			for _, id := range ids {
				_ = wk.store.InsertJobEvent(ctx, id, nil, "charge_failed",
					"Job billed with your other recent jobs but no saved payment method · amount is owed and will be charged once a card is on file", nil)
			}
			log.Printf("workers: charge-collect: buyer %s due for a batch but has no saved card — %d job(s) parked no_payment_method (owed)", buyerID, len(ids))
			continue
		}
		batch, formed, ferr := wk.store.FormChargeBatch(ctx, buyerID)
		if ferr != nil {
			log.Printf("workers: charge-collect: forming batch for buyer %s: %v", buyerID, ferr)
			continue
		}
		if !formed {
			continue // raced away between the due-scan and the lock — clean no-op
		}
		log.Printf("workers: charge-collect: formed batch %s for buyer %s ($%.6f frozen)", batch.ID, buyerID, batch.AmountUSD)
		wk.chargeBatch(ctx, batch)
	}

	// 3. Terminal coverage: watchdog/fail-settled jobs that bill nobody today.
	terminal, err := wk.store.TerminalUnattemptedJobs(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, id := range terminal {
		chargeOrDeferJob(ctx, wk.store, id)
	}

	// 4. Retry failed singles whose backoff elapsed, with the ORIGINAL key.
	due, err := wk.store.FailedChargesDue(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, id := range due {
		wk.retryFailedSingle(ctx, id)
	}

	// 5. Fee backfill: confirmed charges with a PI but no stripe_fee row.
	unfeed, err := wk.store.ChargesMissingFeeRows(ctx, sweepBatch)
	if err != nil {
		return err
	}
	for _, u := range unfeed {
		if ferr := recordStripeFee(ctx, wk.store, u.BuyerID, u.PI); ferr != nil {
			log.Printf("workers: charge-collect: fee backfill for pi %s: %v (retried next tick)", u.PI, ferr)
		}
	}
	return nil
}

// chargeBatch attempts one batch's PaymentIntent: the FROZEN row amount under
// the stable "cxbatch-"+id idempotency key. Success confirms the batch + flips
// its member jobs to charged and records the real Stripe fee; failure is logged
// and the batch stays 'attempting' for the next tick (the same key makes that
// retry safe even if THIS attempt was ambiguous).
func (wk *Workers) chargeBatch(ctx context.Context, b ChargeBatch) {
	pi, err := chargeBuyer(ctx, wk.store, b.BuyerID, b.AmountUSD, "cxbatch-"+b.ID.String())
	if err != nil {
		// Backoff mirrors the failed-singles schedule (30min x attempts, <= 6h): a
		// hard-declined card must not be hammered once a minute forever — that is
		// card-network excessive-reattempt territory. The frozen amount + stable
		// idempotency key make every retry a replay, never a second charge.
		attempts, aerr := wk.store.BumpChargeBatchRetry(ctx, b.ID, chargeRetryBackoff)
		if aerr != nil {
			log.Printf("workers: charge-collect: bumping retry for batch %s: %v", b.ID, aerr)
		}
		log.Printf("workers: charge-collect: batch %s ($%.6f, buyer %s) charge failed (attempt %d, backed off, still owed): %v",
			b.ID, b.AmountUSD, b.BuyerID, attempts, err)
		return
	}
	if merr := wk.store.MarkChargeBatchCharged(ctx, b.ID, pi); merr != nil {
		// The charge went through but the confirmation write failed: the next tick
		// retries with the same idempotency key (a Stripe replay, not a re-charge)
		// and confirms then. Money-safe, just late.
		log.Printf("workers: charge-collect: batch %s charged (pi %s) but confirmation write failed: %v (reconfirmed next tick)", b.ID, pi, merr)
		return
	}
	log.Printf("workers: charge-collect: batch %s charged ($%.6f, buyer %s, pi %s)", b.ID, b.AmountUSD, b.BuyerID, pi)
	if ferr := recordStripeFee(ctx, wk.store, b.BuyerID, pi); ferr != nil {
		log.Printf("workers: charge-collect: stripe fee for batch %s (pi %s) not recorded yet: %v (backfilled next tick)", b.ID, pi, ferr)
	}
}

// retryFailedSingle retries one failed single-job charge with its ORIGINAL
// "job-"+jobID idempotency key (an ambiguous prior attempt is replayed, never
// doubled). On failure the attempt counter and the 30min×attempts (≤6h) backoff
// are advanced; the job is never written off — the ledger keeps it owed and the
// failure is logged every time.
func (wk *Workers) retryFailedSingle(ctx context.Context, jobID uuid.UUID) {
	// Retry the FROZEN attempt amount (charge_attempt_usd), never the current
	// actual_usd: the original attempt recorded its params under the "job-"+id
	// idempotency key, and replaying that key with a drifted amount is a permanent
	// idempotency_error loop — then a double charge once the key expires.
	buyerID, usd, err := wk.store.JobFrozenChargeInfo(ctx, jobID)
	if err != nil || usd <= 0 {
		return
	}
	pi, err := chargeBuyer(ctx, wk.store, buyerID, usd, "job-"+jobID.String())
	if err != nil {
		attempts, aerr := wk.store.IncrementChargeAttempts(ctx, jobID)
		if aerr != nil {
			log.Printf("workers: charge-collect: bumping charge attempts for job %s: %v", jobID, aerr)
			return
		}
		next := time.Now().Add(chargeRetryBackoff(attempts))
		if serr := wk.store.SetChargeNextAt(ctx, jobID, next); serr != nil {
			log.Printf("workers: charge-collect: scheduling next retry for job %s: %v", jobID, serr)
		}
		log.Printf("workers: charge-collect: retry %d for job %s ($%.6f) failed, next at %s (still owed): %v",
			attempts, jobID, usd, next.UTC().Format(time.RFC3339), err)
		return
	}
	if serr := wk.store.SetJobCharged(ctx, jobID, pi); serr != nil {
		log.Printf("workers: charge-collect: marking job %s charged (pi %s): %v", jobID, pi, serr)
		return
	}
	log.Printf("workers: charge-collect: job %s charged on retry ($%.6f, pi %s)", jobID, usd, pi)
	if ferr := recordStripeFee(ctx, wk.store, buyerID, pi); ferr != nil {
		log.Printf("workers: charge-collect: stripe fee for job %s (pi %s) not recorded yet: %v (backfilled next tick)", jobID, pi, ferr)
	}
}
