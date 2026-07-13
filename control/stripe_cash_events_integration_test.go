//go:build integration

package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"
)

const stripeCashIntegrationSecret = "whsec_test_cash_lifecycle"

func deliverStripeCashEvent(t *testing.T, body string) {
	t.Helper()
	payload := []byte(body)
	code, out := req(t, http.MethodPost, "/v1/stripe/webhook", payload,
		hdr{"Stripe-Signature", stripeTestSig(payload, stripeCashIntegrationSecret)})
	if code != http.StatusOK {
		t.Fatalf("Stripe cash webhook: status=%d body=%s", code, out)
	}
}

func TestBuyerChargeOutcomeUnknownBlocksBlindRetryAndNullPIDisputeStillLinks(t *testing.T) {
	reset(t)
	t.Setenv("STRIPE_WEBHOOK_SECRET", stripeCashIntegrationSecret)
	ctx := context.Background()
	jobID, _, entryID := seedDuePayoutLiability(t, 0.66)
	if _, err := itPool.Exec(ctx, `
		INSERT INTO billing_customers (buyer_id,stripe_customer_id,default_payment_method)
		VALUES ($1,'cus_charge_boundary','pm_charge_boundary')
		ON CONFLICT (buyer_id) DO UPDATE SET
		 stripe_customer_id=EXCLUDED.stripe_customer_id,
		 default_payment_method=EXCLUDED.default_payment_method`, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}

	calls := 0
	withStripeTestServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/payment_intents" {
			t.Fatalf("Stripe path=%s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"id":"pi_charge_boundary","latest_charge":"ch_charge_boundary","status":"succeeded","currency":"usd","amount":66,"amount_received":66}`)
	}))
	operationKey := "job-" + jobID.String()
	charge, err := chargeBuyer(ctx, itStore, demoBuyerUUID, 0.66, operationKey, "job", jobID)
	if err != nil {
		t.Fatalf("first charge request: %v", err)
	}
	if charge.PaymentIntentID != "pi_charge_boundary" || charge.ChargeID != "ch_charge_boundary" {
		t.Fatalf("Stripe success evidence=%+v", charge)
	}
	// Simulate a crash/DB failure after Stripe succeeded but before SetJobCharged.
	// The durable outcome_unknown operation must stop a second external request,
	// even if the worker resumes long after Stripe's idempotency retention window.
	if _, err := chargeBuyer(ctx, itStore, demoBuyerUUID, 0.66, operationKey, "job", jobID); !errors.Is(err, errBuyerChargeOutcomeUnknown) {
		t.Fatalf("second charge error=%v, want outcome_unknown", err)
	}
	if calls != 1 {
		t.Fatalf("outcome_unknown operation made %d Stripe requests, want exactly one", calls)
	}
	var opStatus, jobStatus string
	if err := itPool.QueryRow(ctx, `
		SELECT op.status,j.charge_status
		  FROM buyer_charge_operations op JOIN jobs j ON j.id=op.job_id
		 WHERE op.operation_key=$1`, operationKey,
	).Scan(&opStatus, &jobStatus); err != nil {
		t.Fatal(err)
	}
	if opStatus != "outcome_unknown" || jobStatus != "outcome_unknown" {
		t.Fatalf("ambiguous charge state op=%s job=%s", opStatus, jobStatus)
	}
	// The webhook can beat the local confirmation write and the dispute can omit
	// payment_intent. It is retained by Charge id for the later reconciliation.
	deliverStripeCashEvent(t, `{"id":"evt_null_pi_dispute","type":"charge.dispute.created","created":1700000050,"data":{"object":{"id":"dp_null_pi","charge":"ch_charge_boundary","amount":66,"currency":"usd","status":"needs_response"}}}`)

	// The signature-verified PaymentIntent webhook consumes independent Stripe
	// evidence and performs no new charge request. PI and Charge ids become one
	// canonical cash fact even though the synchronous confirmation write was lost.
	deliverStripeCashEvent(t, fmt.Sprintf(
		`{"id":"evt_pi_succeeded","type":"payment_intent.succeeded","created":1700000060,"data":{"object":{"id":"pi_charge_boundary","latest_charge":"ch_charge_boundary","status":"succeeded","currency":"usd","amount":66,"amount_received":66,"metadata":{"cx_operation_key":%q}}}}`, operationKey))
	var boundCharge, disputePI string
	if err := itPool.QueryRow(ctx, `
		SELECT op.status,c.charge_id,COALESCE(d.payment_intent,'')
		  FROM buyer_charge_operations op
		  JOIN buyer_cash_collections c ON c.payment_intent=op.payment_intent
		  JOIN stripe_dispute_cash_state d ON d.charge_id=c.charge_id
		 WHERE op.operation_key=$1`, operationKey,
	).Scan(&opStatus, &boundCharge, &disputePI); err != nil {
		t.Fatal(err)
	}
	if opStatus != "succeeded" || boundCharge != "ch_charge_boundary" ||
		disputePI != "pi_charge_boundary" || calls != 1 {
		t.Fatalf("reconciled charge op=%s charge=%s dispute_pi=%s Stripe calls=%d",
			opStatus, boundCharge, disputePI, calls)
	}

	// Reconciliation must have back-bound that earlier null-PI dispute before the
	// collection can fund a supplier payout.
	if _, ok, err := itStore.ClaimPayout(ctx, entryID); err != nil || ok {
		t.Fatalf("null-PI dispute failed closed: claimed=%v err=%v", ok, err)
	}
}

func TestStripeRefundReplayBlocksNewSupplierPayoutFunding(t *testing.T) {
	reset(t)
	t.Setenv("STRIPE_WEBHOOK_SECRET", stripeCashIntegrationSecret)
	ctx := context.Background()
	jobID, _, entryID := seedDuePayoutLiability(t, 0.75)
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_refund_before_payout", ChargeID: "ch_pi_refund_before_payout", RequestedCents: 75,
		ReceivedCents: 75, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	body := `{"id":"evt_refund_before_payout","type":"charge.refunded","created":1700000100,"data":{"object":{"id":"ch_pi_refund_before_payout","payment_intent":"pi_refund_before_payout","amount":75,"amount_refunded":1,"currency":"usd"}}}`
	deliverStripeCashEvent(t, body)
	deliverStripeCashEvent(t, body) // Stripe retry: exact event is one durable fact.

	claimed, ok, err := itStore.ClaimPayout(ctx, entryID)
	if err != nil {
		t.Fatalf("claim after refund: %v", err)
	}
	if ok || claimed.ID != entryID {
		t.Fatalf("refunded collection funded payout: claimed=%v payout=%+v", ok, claimed)
	}
	var eventRows, fundingRows int
	var ledgerStatus string
	var refunded int64
	if err := itPool.QueryRow(ctx, `
		SELECT
		 (SELECT count(*) FROM stripe_webhook_events WHERE event_id='evt_refund_before_payout'),
		 (SELECT count(*) FROM supplier_payout_funding WHERE ledger_entry_id=$1),
		 (SELECT payout_status FROM ledger_entries WHERE id=$1),
		 (SELECT refunded_cents FROM stripe_charge_cash_state
		   WHERE charge_id='ch_pi_refund_before_payout')`, entryID,
	).Scan(&eventRows, &fundingRows, &ledgerStatus, &refunded); err != nil {
		t.Fatal(err)
	}
	if eventRows != 1 || fundingRows != 0 || ledgerStatus != PayoutAwaitingFunding || refunded != 1 {
		t.Fatalf("refund gate: events=%d funding=%d ledger=%s refunded=%d",
			eventRows, fundingRows, ledgerStatus, refunded)
	}
}

func TestStripeDisputeClassifiesPayoutExposureWithoutInventingReversal(t *testing.T) {
	for _, tc := range []struct {
		name             string
		prepare          func(context.Context, DueHeldEntry) error
		wantStatus       string
		wantCashMoved    bool
		wantUnknown      bool
		definitelyUnsent bool
	}{
		{
			name: "definitely unsent ready stays ready",
			prepare: func(ctx context.Context, payout DueHeldEntry) error {
				_, err := itStore.DeferPayout(ctx, payout.ID, errors.New("provider rejected before send"))
				return err
			},
			wantStatus: PayoutReady, definitelyUnsent: true,
		},
		{
			name:       "actively sending is conservatively surfaced",
			prepare:    func(context.Context, DueHeldEntry) error { return nil },
			wantStatus: PayoutReversalRequired,
		},
		{
			name: "outcome unknown is conservatively surfaced",
			prepare: func(ctx context.Context, payout DueHeldEntry) error {
				_, err := itStore.MarkPayoutOutcomeUnknown(ctx, payout.ID, errors.New("provider response lost"))
				return err
			},
			wantStatus: PayoutReversalRequired, wantUnknown: true,
		},
		{
			name: "cash moved is conservatively surfaced",
			prepare: func(ctx context.Context, payout DueHeldEntry) error {
				_, err := itStore.FinalizePayout(ctx, payout.ID, PayoutResult{
					Ref: "tr_cash_moved_before_dispute", SentCents: payout.RequestedCents,
					Currency: payout.Currency, CashMoved: true,
				})
				return err
			},
			wantStatus: PayoutReversalRequired, wantCashMoved: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			t.Setenv("STRIPE_WEBHOOK_SECRET", stripeCashIntegrationSecret)
			ctx := context.Background()
			jobID, _, entryID := seedDuePayoutLiability(t, 0.50)
			if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
				PaymentIntentID: "pi_dispute_exposure", ChargeID: "ch_pi_dispute_exposure", RequestedCents: 50,
				ReceivedCents: 50, Currency: "usd",
			}); err != nil {
				t.Fatal(err)
			}
			payout, ok, err := itStore.ClaimPayout(ctx, entryID)
			if err != nil || !ok {
				t.Fatalf("initial payout claim: ok=%v err=%v", ok, err)
			}
			if err := tc.prepare(ctx, payout); err != nil {
				t.Fatalf("prepare payout exposure: %v", err)
			}

			deliverStripeCashEvent(t, `{"id":"evt_dispute_exposure","type":"charge.dispute.created","created":1700000200,"data":{"object":{"id":"dp_dispute_exposure","charge":"ch_pi_dispute_exposure","payment_intent":"pi_dispute_exposure","amount":50,"currency":"usd","status":"needs_response"}}}`)

			var ledgerStatus, opStatus, fundingStatus string
			var cashMoved, outcomeUnknown bool
			var compromised int64
			if err := itPool.QueryRow(ctx, `
				SELECT le.payout_status,op.status,op.cash_moved,op.outcome_unknown,
				       fs.status,fs.compromised_cents
				  FROM ledger_entries le
				  JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
				  JOIN supplier_payout_funding_state fs ON fs.funding_id=op.funding_id
				 WHERE le.id=$1`, entryID,
			).Scan(&ledgerStatus, &opStatus, &cashMoved, &outcomeUnknown, &fundingStatus, &compromised); err != nil {
				t.Fatal(err)
			}
			if ledgerStatus != tc.wantStatus || opStatus != tc.wantStatus ||
				cashMoved != tc.wantCashMoved || outcomeUnknown != tc.wantUnknown ||
				fundingStatus != "compromised" || compromised != 50 {
				t.Fatalf("exposure: ledger=%s op=%s cash=%v unknown=%v funding=%s/%d",
					ledgerStatus, opStatus, cashMoved, outcomeUnknown, fundingStatus, compromised)
			}

			if tc.definitelyUnsent {
				if err := itStore.AdminReleasePayoutHold(ctx, entryID, "test compromised retry gate"); err != nil {
					t.Fatalf("re-arm definitely-unsent payout: %v", err)
				}
				_, claimedAgain, err := itStore.ClaimPayout(ctx, entryID)
				if err != nil || claimedAgain {
					t.Fatalf("compromised ready payout reused funding: claimed=%v err=%v", claimedAgain, err)
				}
				if err := itPool.QueryRow(ctx,
					`SELECT payout_status FROM ledger_entries WHERE id=$1`, entryID,
				).Scan(&ledgerStatus); err != nil {
					t.Fatal(err)
				}
				if ledgerStatus != PayoutAwaitingFunding {
					t.Fatalf("compromised ready payout status=%s, want awaiting_funding", ledgerStatus)
				}
			}
		})
	}
}

func TestStripeDisputeRacingProviderCompletionPreservesCashEvidence(t *testing.T) {
	reset(t)
	t.Setenv("STRIPE_WEBHOOK_SECRET", stripeCashIntegrationSecret)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	jobID, _, entryID := seedDuePayoutLiability(t, 0.80)
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_dispute_race", ChargeID: "ch_pi_dispute_race", RequestedCents: 80,
		ReceivedCents: 80, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}

	rail := &crossedBoundaryPayout{crossed: make(chan crossedPayoutCall, 1), finish: make(chan struct{})}
	done := make(chan error, 1)
	go func() { done <- NewWorkers(itStore, itStorage, rail).releasePayouts(ctx) }()
	select {
	case <-rail.crossed:
	case <-ctx.Done():
		t.Fatalf("provider boundary was not reached: %v", ctx.Err())
	}

	deliverStripeCashEvent(t, `{"id":"evt_dispute_race","type":"charge.dispute.funds_withdrawn","created":1700000300,"data":{"object":{"id":"dp_dispute_race","charge":"ch_pi_dispute_race","payment_intent":"pi_dispute_race","amount":80,"currency":"usd","status":"under_review"}}}`)
	close(rail.finish)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("finalize payout race: %v", err)
		}
	case <-ctx.Done():
		t.Fatalf("payout race did not finish: %v", ctx.Err())
	}

	var ledgerStatus, opStatus, ref, fundingStatus string
	var cashMoved bool
	var sent int64
	if err := itPool.QueryRow(ctx, `
		SELECT le.payout_status,op.status,op.cash_moved,op.sent_cents,
		       COALESCE(op.transfer_ref,''),fs.status
		  FROM ledger_entries le
		  JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
		  JOIN supplier_payout_funding_state fs ON fs.funding_id=op.funding_id
		 WHERE le.id=$1`, entryID,
	).Scan(&ledgerStatus, &opStatus, &cashMoved, &sent, &ref, &fundingStatus); err != nil {
		t.Fatal(err)
	}
	if ledgerStatus != PayoutReversalRequired || opStatus != PayoutReversalRequired ||
		!cashMoved || sent != 80 || ref != "tr_crossed_race" || fundingStatus != "compromised" {
		t.Fatalf("raced cash evidence: ledger=%s op=%s moved=%v sent=%d ref=%q funding=%s",
			ledgerStatus, opStatus, cashMoved, sent, ref, fundingStatus)
	}
}

func TestStripeDisputeOutOfOrderWithdrawalCannotOverrideNewerReinstatement(t *testing.T) {
	reset(t)
	t.Setenv("STRIPE_WEBHOOK_SECRET", stripeCashIntegrationSecret)
	ctx := context.Background()
	jobID, _, entryID := seedDuePayoutLiability(t, 0.40)
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_dispute_order", ChargeID: "ch_pi_dispute_order", RequestedCents: 40,
		ReceivedCents: 40, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	object := `"id":"dp_dispute_order","charge":"ch_pi_dispute_order","payment_intent":"pi_dispute_order","amount":40,"currency":"usd"`
	deliverStripeCashEvent(t, fmt.Sprintf(
		`{"id":"evt_reinstated_newer","type":"charge.dispute.funds_reinstated","created":200,"data":{"object":{%s,"status":"won"}}}`, object))
	deliverStripeCashEvent(t, fmt.Sprintf(
		`{"id":"evt_withdrawn_older","type":"charge.dispute.funds_withdrawn","created":100,"data":{"object":{%s,"status":"under_review"}}}`, object))

	var unavailable bool
	var effectCreated int64
	if err := itPool.QueryRow(ctx, `
		SELECT cash_unavailable,cash_effect_created
		  FROM stripe_dispute_cash_state WHERE dispute_id='dp_dispute_order'`,
	).Scan(&unavailable, &effectCreated); err != nil {
		t.Fatal(err)
	}
	if unavailable || effectCreated != 200 {
		t.Fatalf("older withdrawal overrode restoration: unavailable=%v effect_created=%d", unavailable, effectCreated)
	}
	if _, ok, err := itStore.ClaimPayout(ctx, entryID); err != nil || !ok {
		t.Fatalf("restored collection remained blocked: claimed=%v err=%v", ok, err)
	}
}
