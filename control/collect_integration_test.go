//go:build integration

package main

// collect_integration_test.go — the charge-collect money-truth proofs, against
// real Postgres (same harness as integration_test.go). Stripe is NEVER actually
// reachable here: tests either run with the key unset (proving the honest no-op)
// or with a fake key (proving formation/backoff mechanics — every real charge
// attempt fails honestly and the state machine must remain money-safe under
// exactly that failure).

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
)

// seedSettledJob inserts one terminal job with a settled actual_usd and an
// explicit charge_status — the row shape the watchdog/fail/complete settle
// paths leave behind.
func seedSettledJob(t *testing.T, buyerID uuid.UUID, status string, actualUSD float64, chargeStatus string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	if _, err := itPool.Exec(context.Background(),
		`INSERT INTO jobs (id, buyer_id, status, job_type, model_ref, input_ref, tier,
		                   task_count, tasks_done, min_memory_gb, actual_usd, charge_status)
		 VALUES ($1,$2,$3,'embed','all-minilm-l6-v2','jobs/cc/input.jsonl','batch',1,1,2,$4,$5)`,
		id, buyerID, status, actualUSD, chargeStatus); err != nil {
		t.Fatalf("seed settled job: %v", err)
	}
	return id
}

// jobChargeRow reads back the charge-state columns under test.
func jobChargeRow(t *testing.T, jobID uuid.UUID) (chargeStatus string, batchID *uuid.UUID, attempts int, nextAt *time.Time) {
	t.Helper()
	if err := itPool.QueryRow(context.Background(),
		`SELECT charge_status, charge_batch_id, charge_attempts, charge_next_at FROM jobs WHERE id=$1`,
		jobID).Scan(&chargeStatus, &batchID, &attempts, &nextAt); err != nil {
		t.Fatalf("read job charge row: %v", err)
	}
	return
}

// TestChargeBatchFormationAndFreeze proves the batching core: sub-threshold
// deferred jobs group per buyer into ONE batch whose amount is FROZEN at
// formation, each job is stamped into the batch exactly once, a second sweep
// forms no duplicate or blind retry, and with no Stripe key the sweep is an
// honest no-op.
func TestChargeBatchFormationAndFreeze(t *testing.T) {
	reset(t)
	ctx := context.Background()
	buyer := uuid.New()

	// Two settled jobs, each below the 5.00 default threshold, summing above it.
	j1 := seedSettledJob(t, buyer, "complete", 2.00, "deferred")
	j2 := seedSettledJob(t, buyer, "complete", 4.00, "deferred")

	// The buyer has a saved card (DB-only fixture — the card check reads the DB,
	// not Stripe).
	if _, err := itPool.Exec(ctx,
		`INSERT INTO billing_customers (buyer_id, stripe_customer_id, default_payment_method)
		 VALUES ($1::uuid,'cus_test_batch_' || $1::uuid::text,'pm_test_batch_' || $1::uuid::text)
		 ON CONFLICT (buyer_id) DO UPDATE
		   SET stripe_customer_id=EXCLUDED.stripe_customer_id,
		       default_payment_method=EXCLUDED.default_payment_method`,
		buyer); err != nil {
		t.Fatalf("seed billing customer: %v", err)
	}

	wk := NewWorkers(itStore, itStorage, stubPayout{})

	// (1) No Stripe key → honest no-op: no batch is formed, nothing changes.
	t.Setenv("STRIPE_SECRET_KEY", "")
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges (no key): %v", err)
	}
	var batches int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM charge_batches`).Scan(&batches); err != nil {
		t.Fatal(err)
	}
	if batches != 0 {
		t.Fatalf("no-key sweep must form no batch, got %d", batches)
	}
	if st, bid, _, _ := jobChargeRow(t, j1); st != "deferred" || bid != nil {
		t.Fatalf("no-key sweep must not touch deferred jobs: status=%q batch=%v", st, bid)
	}

	// (2) With a (fake) key the batch FORMS: frozen sum, both jobs stamped. The
	// durable request boundary is armed before the fake Stripe request. Because
	// the transport failure cannot prove whether Stripe moved cash, the batch and
	// jobs remain outcome_unknown for explicit reconciliation, never blind retry.
	t.Setenv("STRIPE_SECRET_KEY", "sk_test_fake_formation_only")
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges (fake key): %v", err)
	}
	var batchID uuid.UUID
	var amount float64
	var status string
	if err := itPool.QueryRow(ctx,
		`SELECT id, amount_usd::float8, status FROM charge_batches WHERE buyer_id=$1`,
		buyer).Scan(&batchID, &amount, &status); err != nil {
		t.Fatalf("expected exactly one batch for the buyer: %v", err)
	}
	if amount < 5.999 || amount > 6.001 {
		t.Fatalf("batch amount must FREEZE the deferred sum 6.00, got %v", amount)
	}
	if status != "outcome_unknown" {
		t.Fatalf("an ambiguous charge must leave the batch outcome_unknown (reconciled, never blindly retried), got %q", status)
	}
	for _, j := range []uuid.UUID{j1, j2} {
		st, bid, _, _ := jobChargeRow(t, j)
		if st != "outcome_unknown" || bid == nil || *bid != batchID {
			t.Fatalf("job %s must be stamped into outcome-unknown batch %s: status=%q batch=%v", j, batchID, st, bid)
		}
	}

	// (3) A second sweep neither resends the outcome-unknown operation nor forms
	// a duplicate. Resolution requires independently verified Stripe evidence.
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges (second sweep): %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM charge_batches`).Scan(&batches); err != nil {
		t.Fatal(err)
	}
	if batches != 1 {
		t.Fatalf("second sweep must not form a duplicate batch: want 1, got %d", batches)
	}
	var operations int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM buyer_charge_operations WHERE charge_batch_id=$1 AND status='outcome_unknown'`,
		batchID).Scan(&operations); err != nil {
		t.Fatal(err)
	}
	if operations != 1 {
		t.Fatalf("second sweep must retain exactly one outcome-unknown operation, got %d", operations)
	}
	if _, bid, _, _ := jobChargeRow(t, j1); bid == nil || *bid != batchID {
		t.Fatalf("job stamping must be once-only; batch id changed to %v", bid)
	}
}

// TestTerminalJobsEnterCollection proves the confirmed-leak fix: a terminal job
// settled with actual_usd > 0 whose charge was never attempted (the watchdog
// cancel / fail-settle paths) is routed through the SAME immediate-or-defer
// decision — a sub-threshold one becomes 'deferred' (batched later), an
// at-threshold one with no saved card lands in the honest 'no_payment_method'
// state. Nothing stays silently unbilled.
func TestTerminalJobsEnterCollection(t *testing.T) {
	reset(t)
	ctx := context.Background()
	t.Setenv("STRIPE_SECRET_KEY", "sk_test_fake_decision_only")

	// A watchdog-cancelled job: suppliers were credited for delivered chunks,
	// the buyer was billed by nobody. Fresh buyer = no card row exists.
	small := seedSettledJob(t, uuid.New(), "cancelled", 2.00, "not_attempted")
	// A failed job at/above the threshold, buyer also without a card: the
	// immediate path must park it honestly, not charge thin air.
	big := seedSettledJob(t, uuid.New(), "failed", 7.00, "not_attempted")

	wk := NewWorkers(itStore, itStorage, stubPayout{})
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges: %v", err)
	}

	if st, bid, _, _ := jobChargeRow(t, small); st != "deferred" || bid != nil {
		t.Fatalf("sub-threshold cancelled job must become 'deferred' (unbatched yet): status=%q batch=%v", st, bid)
	}
	if st, _, _, _ := jobChargeRow(t, big); st != "no_payment_method" {
		t.Fatalf("at-threshold job with no card must land in 'no_payment_method' (owed, honest), got %q", st)
	}
}

// TestChargeRetryBackoffPushes proves a failed single-job charge is not retried
// before its charge_next_at, and that a due retry (which fails against the fake
// key) advances the attempt counter and pushes charge_next_at into the future
// per the 30min×attempts schedule — owed forever, hammered never.
func TestChargeRetryBackoffPushes(t *testing.T) {
	reset(t)
	ctx := context.Background()
	t.Setenv("STRIPE_SECRET_KEY", "sk_test_fake_backoff_only")

	job := seedSettledJob(t, uuid.New(), "complete", 10.00, "failed")
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET charge_next_at = now() + interval '1 hour' WHERE id=$1`, job); err != nil {
		t.Fatal(err)
	}

	wk := NewWorkers(itStore, itStorage, stubPayout{})

	// Not due yet → untouched.
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges (not due): %v", err)
	}
	if st, _, attempts, _ := jobChargeRow(t, job); st != "failed" || attempts != 0 {
		t.Fatalf("a retry before charge_next_at must not run: status=%q attempts=%d", st, attempts)
	}

	// Arm it and sweep: the retry runs (fails against the fake key), attempts
	// increments, and the next retry is scheduled ~30min out (attempt 1).
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET charge_next_at = now() - interval '1 second' WHERE id=$1`, job); err != nil {
		t.Fatal(err)
	}
	before := time.Now()
	if err := wk.collectCharges(ctx); err != nil {
		t.Fatalf("collectCharges (due): %v", err)
	}
	st, _, attempts, nextAt := jobChargeRow(t, job)
	if st != "failed" || attempts != 1 {
		t.Fatalf("failed due retry must bump attempts to 1 and stay 'failed': status=%q attempts=%d", st, attempts)
	}
	if nextAt == nil || !nextAt.After(before) {
		t.Fatalf("charge_next_at must be pushed into the future, got %v", nextAt)
	}
	if until := nextAt.Sub(before); until < 25*time.Minute || until > 35*time.Minute {
		t.Fatalf("attempt 1 backoff should be ~30min, got %s", until)
	}
}

// TestAdminSummaryMoneyTruth seeds one COLLECTED job and one uncollected
// terminal job with the full ledger constellation (take + held credit on each,
// a released credit, a real stripe_fee row) and asserts every new summary
// field's math — including that the accessible section counts ONLY collected
// jobs and that the stripe section is nil without a key (never fabricated).
func TestAdminSummaryMoneyTruth(t *testing.T) {
	reset(t)
	ctx := context.Background()
	t.Setenv("STRIPE_SECRET_KEY", "") // stripe section must be nil, honestly

	// Collected job with one canonical exact PaymentIntent fact and two tasks.
	charged := seedSettledJob(t, demoBuyerUUID, "complete", 10.00, "not_attempted")
	if err := itStore.SetJobCharged(ctx, charged, ChargeResult{
		PaymentIntentID: "pi_test_mt_1", ChargeID: "ch_pi_test_mt_1", RequestedCents: 1000,
		ReceivedCents: 1000, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	// Uncollected terminal job (settled but deferred).
	uncharged := seedSettledJob(t, demoBuyerUUID, "cancelled", 3.00, "deferred")

	newTask := func(jobID uuid.UUID) uuid.UUID {
		id := uuid.New()
		if _, err := itPool.Exec(ctx,
			`INSERT INTO tasks (id, job_id, status, worker_id, claimed_by, completed_at)
			 VALUES ($1,$2,'complete',$3,$3,now())`, id, jobID, demoWorkerUUID); err != nil {
			t.Fatalf("seed task: %v", err)
		}
		return id
	}
	tC1, tC2, tU := newTask(charged), newTask(charged), newTask(uncharged)

	ledger := []struct {
		q    string
		args []any
	}{
		// On the COLLECTED job: take 0.30, held credit 2.70, and a separate
		// credit already transferred out (released, real ref).
		{`INSERT INTO ledger_entries (kind, task_id, amount_usd, payout_status)
		  VALUES ('platform_take', $1, 0.30, 'released')`, []any{tC1}},
		{`INSERT INTO ledger_entries (kind, supplier_id, task_id, amount_usd, payout_status)
		  VALUES ('supplier_credit', $1, $2, 2.70, 'held')`, []any{demoSupplierUUID, tC1}},
		{`INSERT INTO ledger_entries (kind, supplier_id, task_id, amount_usd, payout_status, payout_ref)
		  VALUES ('supplier_credit', $1, $2, 1.00, 'released', 'tr_test_mt_1')`, []any{demoSupplierUUID, tC2}},
		// On the UNCOLLECTED job: take + held credit that must NOT count as
		// accessible (the buyer's money never arrived).
		{`INSERT INTO ledger_entries (kind, task_id, amount_usd, payout_status)
		  VALUES ('platform_take', $1, 0.09, 'released')`, []any{tU}},
		{`INSERT INTO ledger_entries (kind, supplier_id, task_id, amount_usd, payout_status)
		  VALUES ('supplier_credit', $1, $2, 0.81, 'held')`, []any{demoSupplierUUID, tU}},
		// The REAL Stripe fee of the collected charge (negative, ref = the PI).
		{`INSERT INTO ledger_entries (kind, buyer_id, amount_usd, payout_status, payout_ref)
		  VALUES ('stripe_fee', $1, -0.59, 'released', 'pi_test_mt_1')`, []any{demoBuyerUUID}},
	}
	for _, l := range ledger {
		if _, err := itPool.Exec(ctx, l.q, l.args...); err != nil {
			t.Fatalf("seed ledger: %v", err)
		}
	}
	// A released ledger label is not cash proof. Attach the exact immutable
	// minor-unit split and cash-moved operation for the transferred $1.00 row.
	var releasedEntry uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT id FROM ledger_entries
		 WHERE kind='supplier_credit' AND task_id=$1 AND payout_status='released'`, tC2,
	).Scan(&releasedEntry); err != nil {
		t.Fatal(err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO supplier_minor_unit_settlements
		  (ledger_entry_id,policy,liability_microusd,cash_cents,remainder_microusd,currency)
		VALUES ($1,'floor_cent_carry_v1',1000000,100,0,'usd')`, releasedEntry); err != nil {
		t.Fatalf("seed exact released settlement: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO supplier_payout_operations
		  (ledger_entry_id,supplier_id,requested_cents,sent_cents,currency,status,cash_moved,transfer_ref)
		VALUES ($1,$2,100,100,'usd','released',true,'tr_test_mt_1')`,
		releasedEntry, demoSupplierUUID); err != nil {
		t.Fatalf("seed exact released cash fact: %v", err)
	}

	code, out := req(t, "GET", "/admin/summary", nil, adminKey())
	if code != http.StatusOK {
		t.Fatalf("/admin/summary: want 200, got %d: %s", code, out)
	}
	var sum AdminSummary
	if err := json.Unmarshal(out, &sum); err != nil {
		t.Fatalf("decode summary: %v (%s)", err, out)
	}
	near := func(got, want float64, name string) {
		if got < want-0.001 || got > want+0.001 {
			t.Fatalf("%s: want %v, got %v", name, want, got)
		}
	}

	near(sum.Money.CollectedUSD, 10.00, "money.collected_usd (canonical received cents)")
	near(sum.Money.UncollectedUSD, 3.00, "money.uncollected_usd (settled terminal, not charged)")
	near(sum.Money.StripeFeesUSD, 0.59, "money.stripe_fees_usd (the real fee, positive-normalized)")
	near(sum.Money.PlatformTakeUSD, 0.39, "money.platform_take_usd (0.30 + 0.09)")
	near(sum.Money.TakeNetUSD, 0.39-0.59, "money.take_net_usd (take minus real fees — honestly negative here)")
	near(sum.Money.TransferredUSD, 1.00, "money.transferred_usd (released credit)")
	near(sum.Money.FlowOwedUSD, 2.70+0.81, "money.flow_owed_usd (held on both jobs)")

	if sum.Accessible == nil {
		t.Fatal("accessible section must be present (query did not fail)")
	}
	near(sum.Accessible.TakeCollectedUSD, 0.30, "accessible.take_collected_usd (collected job only — 0.09 excluded)")
	near(sum.Accessible.FeesUSD, 0.59, "accessible.fees_usd")
	near(sum.Accessible.YoursNetUSD, 0.30-0.59, "accessible.yours_net_usd")
	near(sum.Accessible.TheirsPendingTransferUSD, 2.70, "accessible.theirs_pending_transfer_usd (held on the collected job only)")
	if sum.Accessible.SpendableEstimateUSD != nil {
		t.Fatalf("spendable_estimate_usd must be nil without a live Stripe balance, got %v", *sum.Accessible.SpendableEstimateUSD)
	}
	if sum.Stripe != nil {
		t.Fatalf("stripe section must be nil without a key (never fabricated), got %+v", sum.Stripe)
	}
}
