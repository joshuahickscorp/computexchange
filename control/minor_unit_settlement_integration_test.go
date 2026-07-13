//go:build integration

package main

import (
	"context"
	"errors"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func readMinorUnitSettlement(t *testing.T, entryID uuid.UUID) (policy string, liability, cash, remainder int64) {
	t.Helper()
	if err := itPool.QueryRow(context.Background(), `
		SELECT policy,liability_microusd,cash_cents,remainder_microusd
		  FROM supplier_minor_unit_settlements WHERE ledger_entry_id=$1`, entryID,
	).Scan(&policy, &liability, &cash, &remainder); err != nil {
		t.Fatal(err)
	}
	return policy, liability, cash, remainder
}

func assertMinorUnitSplit(t *testing.T, entryID uuid.UUID, wantLiability, wantCash, wantRemainder int64) {
	t.Helper()
	policy, liability, cash, remainder := readMinorUnitSettlement(t, entryID)
	if policy != supplierSettlementPolicyFloorCentCarryV1 || liability != wantLiability ||
		cash != wantCash || remainder != wantRemainder ||
		liability != cash*microUSDPerCent+remainder {
		t.Fatalf("minor-unit settlement %s = policy=%q liability=%d cash=%d remainder=%d",
			entryID, policy, liability, cash, remainder)
	}
}

func TestMinorUnitSettlementCarriesDustWithoutStarvingPayableLiability(t *testing.T) {
	reset(t)
	ctx := context.Background()

	_, _, zeroEntry := seedDuePayoutLiability(t, 0)
	_, _, belowHalfEntry := seedDuePayoutLiability(t, 0.004999)
	_, _, halfEntry := seedDuePayoutLiability(t, 0.005000)
	payableJob, _, payableEntry := seedDuePayoutLiability(t, 1.239999)
	if err := itStore.SetJobCharged(ctx, payableJob, ChargeResult{
		PaymentIntentID: "pi_minor_unit_payable", ChargeID: "ch_pi_minor_unit_payable", RequestedCents: 124,
		ReceivedCents: 124, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	for i, id := range []uuid.UUID{zeroEntry, belowHalfEntry, halfEntry, payableEntry} {
		if _, err := itPool.Exec(ctx,
			`UPDATE ledger_entries SET release_at=now()-($2::int * interval '1 minute') WHERE id=$1`,
			id, 10-i); err != nil {
			t.Fatal(err)
		}
	}

	// A limit-1 sweep sees each oldest zero/dust row once. Claiming carries it and
	// removes it from the due queue, so the later payable liability advances.
	for _, want := range []uuid.UUID{zeroEntry, belowHalfEntry, halfEntry} {
		due, err := itStore.DuePayouts(ctx, 1)
		if err != nil {
			t.Fatal(err)
		}
		if len(due) != 1 || due[0].ID != want {
			t.Fatalf("due head=%+v, want %s", due, want)
		}
		if _, claimed, err := itStore.ClaimPayout(ctx, want); err != nil || claimed {
			t.Fatalf("carry claim %s: claimed=%v err=%v", want, claimed, err)
		}
		var status string
		if err := itPool.QueryRow(ctx, `SELECT payout_status FROM ledger_entries WHERE id=$1`, want).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status != PayoutCarried {
			t.Fatalf("dust row %s status=%q, want carried", want, status)
		}
	}
	assertMinorUnitSplit(t, zeroEntry, 0, 0, 0)
	assertMinorUnitSplit(t, belowHalfEntry, 4_999, 0, 4_999)
	assertMinorUnitSplit(t, halfEntry, 5_000, 0, 5_000)

	due, err := itStore.DuePayouts(ctx, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(due) != 1 || due[0].ID != payableEntry {
		t.Fatalf("payable row did not advance after dust: %+v", due)
	}
	rail := &fundingGatePayout{}
	if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rail.calls) != 1 || rail.calls[0].cents != 123 || rail.calls[0].currency != "usd" {
		t.Fatalf("provider calls=%+v, want one exact 123-cent call", rail.calls)
	}
	assertMinorUnitSplit(t, payableEntry, 1_239_999, 123, 9_999)

	var status string
	var sent, funded int64
	if err := itPool.QueryRow(ctx, `
		SELECT le.payout_status,op.sent_cents,f.amount_cents
		  FROM ledger_entries le
		  JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
		  JOIN supplier_payout_funding f ON f.ledger_entry_id=le.id
		 WHERE le.id=$1`, payableEntry).Scan(&status, &sent, &funded); err != nil {
		t.Fatal(err)
	}
	if status != PayoutReleased || sent != 123 || funded != 123 {
		t.Fatalf("released payout status=%q sent=%d funded=%d", status, sent, funded)
	}

	earnings, err := itStore.WorkerEarnings(ctx, demoSupplierUUID)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(earnings.BalanceUSD-1.23) > 1e-9 ||
		math.Abs(earnings.CarriedUSD-0.019998) > 1e-9 {
		t.Fatalf("earnings cash/carry mismatch: %+v", earnings)
	}
	summary, err := itStore.AdminSummaryData(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(summary.Money.TransferredUSD-1.23) > 1e-9 ||
		math.Abs(summary.Money.CarriedRemainderUSD-0.019998) > 1e-9 ||
		math.Abs(summary.Money.FlowOwedUSD-0.019998) > 1e-9 {
		t.Fatalf("admin cash/carry mismatch: %+v", summary.Money)
	}

	// Both the settlement row and the ledger amount it binds are immutable.
	if _, err := itPool.Exec(ctx,
		`UPDATE supplier_minor_unit_settlements SET remainder_microusd=0 WHERE ledger_entry_id=$1`, payableEntry); err == nil {
		t.Fatal("append-only minor-unit settlement was mutable")
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE ledger_entries SET amount_usd=1.23 WHERE id=$1`, payableEntry); err == nil {
		t.Fatal("settled ledger liability amount was mutable")
	}
}

type responseLossPayout struct {
	mu       sync.Mutex
	calls    []crossedPayoutCall
	movedKey string
}

func (p *responseLossPayout) Send(_ context.Context, supplier uuid.UUID, cents int64, currency, key string) (PayoutResult, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls = append(p.calls, crossedPayoutCall{supplier: supplier, cents: cents, currency: currency, key: key})
	if p.movedKey == "" {
		p.movedKey = key
		// Deliberately untyped: the boundary defaults every adapter error to
		// unknown unless the adapter proves definitely-not-sent.
		return PayoutResult{}, errors.New("injected response loss after idempotent provider crossing")
	}
	if p.movedKey != key {
		return PayoutResult{}, errors.New("retry changed provider idempotency key")
	}
	return PayoutResult{Ref: "tr_response_loss_" + key, SentCents: cents, Currency: currency, CashMoved: true}, nil
}

func TestMinorUnitSettlementCrashRetryConcurrencyAndRepeatedMicros(t *testing.T) {
	t.Run("stale sending lease resumes exact floor-cent operation", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		jobID, _, entryID := seedDuePayoutLiability(t, 0.019999)
		if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
			PaymentIntentID: "pi_minor_unit_crash", ChargeID: "ch_pi_minor_unit_crash", RequestedCents: 2,
			ReceivedCents: 2, Currency: "usd",
		}); err != nil {
			t.Fatal(err)
		}
		claimed, ok, err := itStore.ClaimPayout(ctx, entryID)
		if err != nil || !ok || claimed.RequestedCents != 1 || claimed.RemainderMicros != 9_999 {
			t.Fatalf("pre-crash claim=%+v ok=%v err=%v", claimed, ok, err)
		}
		// Process death occurs here: no provider call and no DeferPayout. Expire
		// the durable lease; the ordinary worker sweep must re-arm and finish it.
		if _, err := itPool.Exec(ctx,
			`UPDATE supplier_payout_operations SET updated_at=now()-interval '10 minutes' WHERE ledger_entry_id=$1`, entryID); err != nil {
			t.Fatal(err)
		}
		rail := &fundingGatePayout{}
		if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		if len(rail.calls) != 1 || rail.calls[0].cents != 1 || rail.calls[0].key != entryID.String() {
			t.Fatalf("crash recovery calls=%+v", rail.calls)
		}
		assertMinorUnitSplit(t, entryID, 19_999, 1, 9_999)
		var settlements, operations, funding int
		var status string
		if err := itPool.QueryRow(ctx, `
			SELECT
			 (SELECT count(*) FROM supplier_minor_unit_settlements WHERE ledger_entry_id=$1),
			 (SELECT count(*) FROM supplier_payout_operations WHERE ledger_entry_id=$1),
			 (SELECT count(*) FROM supplier_payout_funding WHERE ledger_entry_id=$1),
			 (SELECT payout_status FROM ledger_entries WHERE id=$1)`, entryID,
		).Scan(&settlements, &operations, &funding, &status); err != nil {
			t.Fatal(err)
		}
		if settlements != 1 || operations != 1 || funding != 1 || status != PayoutReleased {
			t.Fatalf("crash recovery duplicated state: settlement=%d op=%d funding=%d status=%q",
				settlements, operations, funding, status)
		}
	})

	t.Run("provider response loss retries one stable instruction", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		jobID, _, entryID := seedDuePayoutLiability(t, 0.019999)
		if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
			PaymentIntentID: "pi_minor_unit_response_loss", ChargeID: "ch_pi_minor_unit_response_loss", RequestedCents: 2,
			ReceivedCents: 2, Currency: "usd",
		}); err != nil {
			t.Fatal(err)
		}
		rail := &responseLossPayout{}
		workers := NewWorkers(itStore, itStorage, rail)
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		var unknownLedger, unknownOperation string
		var unknown bool
		if err := itPool.QueryRow(ctx, `
			SELECT le.payout_status,op.status,op.outcome_unknown
			  FROM ledger_entries le
			  JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
			 WHERE le.id=$1`, entryID,
		).Scan(&unknownLedger, &unknownOperation, &unknown); err != nil {
			t.Fatal(err)
		}
		if unknownLedger != PayoutOutcomeUnknown || unknownOperation != PayoutOutcomeUnknown || !unknown {
			t.Fatalf("response loss became ledger=%q op=%q unknown=%v, want explicit unknown",
				unknownLedger, unknownOperation, unknown)
		}
		if err := itStore.AdminReleasePayoutHold(ctx, entryID, "must not bypass unknown resolution"); !errors.Is(err, errNotHeld) {
			t.Fatalf("admin rearm unknown err=%v, want %v", err, errNotHeld)
		}
		if _, err := itPool.Exec(ctx,
			`UPDATE supplier_payout_operations SET updated_at=now()-interval '10 minutes' WHERE ledger_entry_id=$1`,
			entryID); err != nil {
			t.Fatal(err)
		}
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		if len(rail.calls) != 2 || rail.calls[0].key == "" || rail.calls[0].key != rail.calls[1].key ||
			rail.calls[0].cents != 1 || rail.calls[1].cents != 1 {
			t.Fatalf("response-loss calls=%+v", rail.calls)
		}
		var status string
		if err := itPool.QueryRow(ctx, `SELECT payout_status FROM ledger_entries WHERE id=$1`, entryID).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status != PayoutReleased {
			t.Fatalf("response-loss payout status=%q", status)
		}
	})

	t.Run("concurrent zero carry and repeated micros are lossless", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		_, _, zeroEntry := seedDuePayoutLiability(t, 0)
		results := make(chan error, 2)
		for i := 0; i < 2; i++ {
			go func() {
				_, claimed, err := itStore.ClaimPayout(context.Background(), zeroEntry)
				if err == nil && claimed {
					err = errors.New("zero liability was claimed for provider cash")
				}
				results <- err
			}()
		}
		for i := 0; i < 2; i++ {
			if err := <-results; err != nil {
				t.Fatal(err)
			}
		}
		assertMinorUnitSplit(t, zeroEntry, 0, 0, 0)

		entries := make([]uuid.UUID, 0, 3)
		for _, amount := range []float64{0.003333, 0.003333, 0.003334} {
			_, _, entryID := seedDuePayoutLiability(t, amount)
			entries = append(entries, entryID)
		}
		rail := &fundingGatePayout{}
		if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		var remainderTotal int64
		for i, entryID := range entries {
			want := []int64{3_333, 3_333, 3_334}[i]
			assertMinorUnitSplit(t, entryID, want, 0, want)
			remainderTotal += want
		}
		if remainderTotal != microUSDPerCent || len(rail.calls) != 0 {
			t.Fatalf("repeated micros remainder=%d calls=%+v, want one exact cent carried and no cash", remainderTotal, rail.calls)
		}
		var carried int
		if err := itPool.QueryRow(ctx,
			`SELECT count(*) FROM ledger_entries WHERE id=ANY($1) AND payout_status='carried'`, entries,
		).Scan(&carried); err != nil {
			t.Fatal(err)
		}
		if carried != 3 {
			t.Fatalf("carried rows=%d, want 3", carried)
		}
	})
}

// Keep time imported in this standalone integration file as an assertion that
// the production lease is bounded and non-zero, not an immediate retry loop.
func TestMinorUnitPayoutSendingLeaseIsBounded(t *testing.T) {
	if payoutSendingLease <= 0 || payoutSendingLease > 30*time.Minute {
		t.Fatalf("payout sending lease=%s, want bounded positive recovery window", payoutSendingLease)
	}
	if payoutIdempotencyRetryWindow <= payoutSendingLease || payoutIdempotencyRetryWindow >= 24*time.Hour {
		t.Fatalf("payout idempotency retry window=%s, want > lease and conservatively < 24h",
			payoutIdempotencyRetryWindow)
	}
}
