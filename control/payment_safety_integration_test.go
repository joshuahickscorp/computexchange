//go:build integration

package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

type crossedPayoutCall struct {
	supplier uuid.UUID
	cents    int64
	currency string
	key      string
}

// crossedBoundaryPayout stops after the provider-side cash boundary has crossed
// but before the caller receives its confirmation. That makes the otherwise tiny
// release/clawback race deterministic.
type crossedBoundaryPayout struct {
	crossed chan crossedPayoutCall
	finish  chan struct{}
}

type fundingGatePayout struct {
	calls         []crossedPayoutCall
	failRemaining int
}

type payoutFundingClaimResult struct {
	payout  DueHeldEntry
	claimed bool
	err     error
}

type payoutSubsidyAuthorizationResult struct {
	created bool
	err     error
}

// waitForBlockedFundingQueries makes the concurrency tests prove the lock, not
// merely launch two goroutines and hope the scheduler overlaps them. The caller
// holds the shared cash/subsidy row in a third transaction; both contenders must
// be visibly waiting on that exact production SELECT ... FOR UPDATE before the
// blocker is released.
func waitForBlockedFundingQueries(t *testing.T, queryFragment string, want int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		var blocked int
		if err := itPool.QueryRow(ctx, `
			SELECT count(*)
			  FROM pg_stat_activity
			 WHERE datname=current_database()
			   AND pid<>pg_backend_pid()
			   AND state='active'
			   AND wait_event_type='Lock'
			   AND position($1 in query)>0`, queryFragment).Scan(&blocked); err != nil {
			t.Fatalf("inspect concurrent funding locks: %v", err)
		}
		if blocked >= want {
			return
		}
		select {
		case <-ctx.Done():
			t.Fatalf("saw %d/%d funding queries blocked on %q before timeout", blocked, want, queryFragment)
		case <-ticker.C:
		}
	}
}

func (p *fundingGatePayout) Send(_ context.Context, supplier uuid.UUID, cents int64, currency, key string) (PayoutResult, error) {
	p.calls = append(p.calls, crossedPayoutCall{supplier: supplier, cents: cents, currency: currency, key: key})
	if p.failRemaining > 0 {
		p.failRemaining--
		return PayoutResult{}, payoutDefinitelyNotSent(errors.New("injected payout rail failure"))
	}
	return PayoutResult{
		Ref: "tr_funding_" + key, SentCents: cents, Currency: currency, CashMoved: true,
	}, nil
}

func seedDuePayoutLiability(t *testing.T, amountUSD float64) (jobID, taskID, entryID uuid.UUID) {
	t.Helper()
	ctx := context.Background()
	jobID, taskID = uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/funding-gate/tasks/0/input.jsonl")
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='complete',verification_outcome='pass',verified_at=now(),completed_at=now()
		 WHERE id=$1`, taskID); err != nil {
		t.Fatalf("mark funding-gate task verified: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		INSERT INTO ledger_entries
		  (kind,supplier_id,task_id,amount_usd,payout_status,release_at)
		VALUES ('supplier_credit',$1,$2,$3,'held',now()-interval '1 minute')
		RETURNING id`, demoSupplierUUID, taskID, amountUSD).Scan(&entryID); err != nil {
		t.Fatalf("insert due supplier liability: %v", err)
	}
	return jobID, taskID, entryID
}

func assertPayoutStayedUnfunded(t *testing.T, entryID uuid.UUID, rail *fundingGatePayout) {
	t.Helper()
	ctx := context.Background()
	if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
		t.Fatalf("release unfunded payout: %v", err)
	}
	var status string
	if err := itPool.QueryRow(ctx,
		`SELECT payout_status FROM ledger_entries WHERE id=$1`, entryID,
	).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != PayoutAwaitingFunding {
		t.Fatalf("unfunded liability moved to %q, want awaiting_funding", status)
	}
	var funding, operations int
	if err := itPool.QueryRow(ctx, `
		SELECT
		  (SELECT count(*) FROM supplier_payout_funding WHERE ledger_entry_id=$1),
		  (SELECT count(*) FROM supplier_payout_operations WHERE ledger_entry_id=$1)`, entryID,
	).Scan(&funding, &operations); err != nil {
		t.Fatal(err)
	}
	if funding != 0 || operations != 0 || len(rail.calls) != 0 {
		t.Fatalf("unfunded payout crossed claim boundary: funding=%d operations=%d provider_calls=%d",
			funding, operations, len(rail.calls))
	}
}

func TestPayoutFundingGateAdversarialCases(t *testing.T) {
	t.Run("unfunded buyer liability never reaches provider", func(t *testing.T) {
		reset(t)
		_, _, entryID := seedDuePayoutLiability(t, 0.75)
		assertPayoutStayedUnfunded(t, entryID, &fundingGatePayout{})
	})

	t.Run("partially funded liability never reaches provider", func(t *testing.T) {
		reset(t)
		jobID, _, entryID := seedDuePayoutLiability(t, 0.75)
		if err := itStore.SetJobCharged(context.Background(), jobID, ChargeResult{
			PaymentIntentID: "pi_funding_partial", ChargeID: "ch_pi_funding_partial", RequestedCents: 50,
			ReceivedCents: 50, Currency: "usd",
		}); err != nil {
			t.Fatalf("record partial exact collection: %v", err)
		}
		assertPayoutStayedUnfunded(t, entryID, &fundingGatePayout{})
	})

	t.Run("exact buyer cash funds and releases only its liability", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		jobID, _, entryID := seedDuePayoutLiability(t, 0.75)
		charge := ChargeResult{
			PaymentIntentID: "pi_funding_exact", ChargeID: "ch_pi_funding_exact", RequestedCents: 75,
			ReceivedCents: 75, Currency: "usd",
		}
		if err := itStore.SetJobCharged(ctx, jobID, charge); err != nil {
			t.Fatalf("record exact collection: %v", err)
		}
		rail := &fundingGatePayout{}
		if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
			t.Fatalf("release exactly funded payout: %v", err)
		}
		if len(rail.calls) != 1 || rail.calls[0].cents != 75 || rail.calls[0].currency != "usd" {
			t.Fatalf("provider calls = %+v, want one exact $0.75 call", rail.calls)
		}
		var status, source, pi string
		var reserved int64
		if err := itPool.QueryRow(ctx, `
			SELECT le.payout_status,f.source_kind,COALESCE(f.collection_payment_intent,''),f.amount_cents
			  FROM ledger_entries le
			  JOIN supplier_payout_funding f ON f.ledger_entry_id=le.id
			 WHERE le.id=$1`, entryID,
		).Scan(&status, &source, &pi, &reserved); err != nil {
			t.Fatal(err)
		}
		if status != PayoutReleased || source != payoutFundingBuyerCollection ||
			pi != charge.PaymentIntentID || reserved != 75 {
			t.Fatalf("funded payout = status=%q source=%q pi=%q cents=%d", status, source, pi, reserved)
		}
	})

	t.Run("response failure retry reuses cash reservation and payout key", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		jobID, _, entryID := seedDuePayoutLiability(t, 0.33)
		charge := ChargeResult{
			PaymentIntentID: "pi_funding_retry", ChargeID: "ch_pi_funding_retry", RequestedCents: 33,
			ReceivedCents: 33, Currency: "usd",
		}
		if err := itStore.SetJobCharged(ctx, jobID, charge); err != nil {
			t.Fatal(err)
		}
		// The buyer-cash write itself is idempotent too: same PI/source/fact yields
		// one canonical row, not a second funding pool.
		if err := itStore.SetJobCharged(ctx, jobID, charge); err != nil {
			t.Fatalf("replay identical charge confirmation: %v", err)
		}
		otherJob, otherTask := uuid.New(), uuid.New()
		mustJobTask(t, otherJob, otherTask, false, false, "jobs/funding-duplicate/tasks/0/input.jsonl")
		if err := itStore.SetJobCharged(ctx, otherJob, charge); err == nil {
			t.Fatal("one PaymentIntent was rebound to a second job cash pool")
		}
		// The primary key is global across source kinds, not merely unique among
		// standalone jobs. A batch must not turn the same external receipt into a
		// second pool either, and its state must remain unconfirmed on rejection.
		batchID := uuid.New()
		if _, err := itPool.Exec(ctx, `
			INSERT INTO charge_batches (id,buyer_id,amount_usd,status)
			VALUES ($1,$2,0.33,'attempting')`, batchID, demoBuyerUUID); err != nil {
			t.Fatal(err)
		}
		if err := itStore.MarkChargeBatchCharged(ctx, batchID, charge); err == nil {
			t.Fatal("one PaymentIntent was rebound from a job to a batch cash pool")
		}
		var batchStatus string
		if err := itPool.QueryRow(ctx,
			`SELECT status FROM charge_batches WHERE id=$1`, batchID).Scan(&batchStatus); err != nil {
			t.Fatal(err)
		}
		if batchStatus != "attempting" {
			t.Fatalf("rejected cross-source PaymentIntent changed batch status to %q", batchStatus)
		}
		rail := &fundingGatePayout{failRemaining: 1}
		workers := NewWorkers(itStore, itStorage, rail)
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatalf("first payout attempt: %v", err)
		}
		if err := itStore.AdminReleasePayoutHold(ctx, entryID, "retry after inspected rail failure"); err != nil {
			t.Fatalf("rearm payout retry: %v", err)
		}
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatalf("second payout attempt: %v", err)
		}
		if len(rail.calls) != 2 || rail.calls[0].key == "" || rail.calls[0].key != rail.calls[1].key {
			t.Fatalf("retry calls did not reuse one stable payout key: %+v", rail.calls)
		}
		var cashRows, fundingRows, opRows int
		var status string
		if err := itPool.QueryRow(ctx, `
			SELECT
			 (SELECT count(*) FROM buyer_cash_collections WHERE payment_intent=$1),
			 (SELECT count(*) FROM supplier_payout_funding WHERE ledger_entry_id=$2),
			 (SELECT count(*) FROM supplier_payout_operations WHERE ledger_entry_id=$2),
			 (SELECT payout_status FROM ledger_entries WHERE id=$2)`, charge.PaymentIntentID, entryID,
		).Scan(&cashRows, &fundingRows, &opRows, &status); err != nil {
			t.Fatal(err)
		}
		if cashRows != 1 || fundingRows != 1 || opRows != 1 || status != PayoutReleased {
			t.Fatalf("retry duplicated money state: cash=%d funding=%d op=%d status=%q",
				cashRows, fundingRows, opRows, status)
		}
	})

	t.Run("finite durable subsidy pool funds otherwise unfunded liability", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		fundRef := "subsidy-fund-" + uuid.NewString()
		treasuryRef := "treasury-assertion-" + uuid.NewString()
		authorizationRef := "subsidy-liability-" + uuid.NewString()
		actor := integrationAdminActor(t)
		created, err := itStore.CreateSubsidyFund(
			ctx, actor, fundRef, treasuryRef, 42, "operator-declared make-good treasury reserve")
		if err != nil || !created {
			t.Fatalf("create capped subsidy pool: created=%v err=%v", created, err)
		}
		created, err = itStore.CreateSubsidyFund(
			ctx, actor, fundRef, treasuryRef, 42, "operator-declared make-good treasury reserve")
		if err != nil || created {
			t.Fatalf("identical subsidy-pool retry: created=%v err=%v", created, err)
		}
		_, _, entryID := seedDuePayoutLiability(t, 0.42)
		created, err = itStore.AuthorizePayoutSubsidy(
			ctx, actor, entryID, fundRef, authorizationRef, "operator-approved make-good reserve")
		if err != nil || !created {
			t.Fatalf("authorize subsidy: created=%v err=%v", created, err)
		}
		created, err = itStore.AuthorizePayoutSubsidy(
			ctx, actor, entryID, fundRef, authorizationRef, "operator-approved make-good reserve")
		if err != nil || created {
			t.Fatalf("identical subsidy retry: created=%v err=%v", created, err)
		}
		if created, err = itStore.AuthorizePayoutSubsidy(
			ctx, actor, entryID, fundRef, authorizationRef, "conflicting replacement audit reason",
		); created || !errors.Is(err, errPayoutFundingAlreadyBound) {
			t.Fatalf("conflicting subsidy-reason retry: created=%v err=%v, want immutable-binding conflict",
				created, err)
		}
		// Capacity is fully reserved. A second liability cannot turn the same pool
		// into unlimited platform money, even under a distinct authorization ref.
		_, _, overflowEntry := seedDuePayoutLiability(t, 0.01)
		if _, err := itStore.AuthorizePayoutSubsidy(
			ctx, actor, overflowEntry, fundRef, "subsidy-liability-"+uuid.NewString(), "must fail capacity check",
		); !errors.Is(err, errSubsidyFundUnavailable) {
			t.Fatalf("over-cap subsidy authorization err=%v, want %v", err, errSubsidyFundUnavailable)
		}
		rail := &fundingGatePayout{}
		if err := NewWorkers(itStore, itStorage, rail).releasePayouts(ctx); err != nil {
			t.Fatalf("release subsidized payout: %v", err)
		}
		var source, gotFundRef, ref, reason, status string
		var capacity, reserved int64
		var actions int
		if err := itPool.QueryRow(ctx, `
			SELECT f.source_kind,fund.fund_ref,COALESCE(f.subsidy_authorization_ref,''),
			       COALESCE(f.subsidy_reason,''),le.payout_status,
			       fund.authorized_cents,
			       (SELECT COALESCE(sum(amount_cents),0)::bigint
			          FROM supplier_payout_funding WHERE subsidy_fund_id=fund.id),
			       (SELECT count(*) FROM admin_actions
			         WHERE kind='payout_subsidy_authorized' AND ledger_entry_id=$1)
			  FROM supplier_payout_funding f
			  JOIN platform_subsidy_funds fund ON fund.id=f.subsidy_fund_id
			  JOIN ledger_entries le ON le.id=f.ledger_entry_id
			 WHERE f.ledger_entry_id=$1`, entryID,
		).Scan(&source, &gotFundRef, &ref, &reason, &status, &capacity, &reserved, &actions); err != nil {
			t.Fatal(err)
		}
		if source != payoutFundingPlatformSubsidy || gotFundRef != fundRef || ref != authorizationRef ||
			reason != "operator-approved make-good reserve" || status != PayoutReleased || capacity != 42 || reserved != 42 ||
			actions != 1 || len(rail.calls) != 1 {
			t.Fatalf("subsidy path = source=%q fund=%q ref=%q reason=%q status=%q capacity=%d reserved=%d actions=%d calls=%d",
				source, gotFundRef, ref, reason, status, capacity, reserved, actions, len(rail.calls))
		}
	})
}

func TestPayoutFundingBatchPoolCannotBeOverallocated(t *testing.T) {
	reset(t)
	ctx := context.Background()
	batchID := uuid.New()
	if _, err := itPool.Exec(ctx, `
		INSERT INTO charge_batches (id,buyer_id,amount_usd,status)
		VALUES ($1,$2,1.00,'attempting')`, batchID, demoBuyerUUID); err != nil {
		t.Fatal(err)
	}
	entries := make([]uuid.UUID, 0, 2)
	for i := 0; i < 2; i++ {
		jobID, _, entryID := seedDuePayoutLiability(t, 0.60)
		if _, err := itPool.Exec(ctx,
			`UPDATE jobs SET charge_batch_id=$2,charge_status='deferred' WHERE id=$1`, jobID, batchID); err != nil {
			t.Fatal(err)
		}
		entries = append(entries, entryID)
	}
	if err := itStore.MarkChargeBatchCharged(ctx, batchID, ChargeResult{
		PaymentIntentID: "pi_funding_batch_pool", ChargeID: "ch_pi_funding_batch_pool", RequestedCents: 100,
		ReceivedCents: 100, Currency: "usd",
	}); err != nil {
		t.Fatalf("record exact batch collection: %v", err)
	}

	// Hold the production batch mutex, launch both claims, and prove both backend
	// sessions are waiting on that exact row lock before releasing them together.
	// One may then reserve 60 cents; the other must observe that committed
	// reservation and remain held because only 40 cents are left.
	blocker, err := itPool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer blocker.Rollback(ctx)
	if _, err := blocker.Exec(ctx,
		`SELECT id FROM charge_batches WHERE id=$1 FOR UPDATE`, batchID); err != nil {
		t.Fatalf("lock shared charge batch: %v", err)
	}
	start := make(chan struct{})
	results := make(chan payoutFundingClaimResult, len(entries))
	var ready sync.WaitGroup
	ready.Add(len(entries))
	for _, entryID := range entries {
		entryID := entryID
		go func() {
			ready.Done()
			<-start
			payout, claimed, err := itStore.ClaimPayout(context.Background(), entryID)
			results <- payoutFundingClaimResult{payout: payout, claimed: claimed, err: err}
		}()
	}
	ready.Wait()
	close(start)
	waitForBlockedFundingQueries(t, "FROM charge_batches WHERE id=$1 FOR UPDATE", len(entries))
	if err := blocker.Commit(ctx); err != nil {
		t.Fatalf("release shared charge-batch lock: %v", err)
	}

	var winner DueHeldEntry
	claimed, unfunded := 0, 0
	for range entries {
		result := <-results
		if result.err != nil {
			t.Fatalf("concurrent batch-funded claim: %v", result.err)
		}
		if result.claimed {
			claimed++
			winner = result.payout
		} else {
			unfunded++
		}
	}
	if claimed != 1 || unfunded != 1 {
		t.Fatalf("concurrent batch claims: claimed=%d unfunded=%d, want 1/1", claimed, unfunded)
	}

	rail := &fundingGatePayout{}
	payoutResult, err := rail.Send(ctx, winner.SupplierID, winner.RequestedCents, winner.Currency, winner.ID.String())
	if err != nil {
		t.Fatalf("send winning batch-funded payout: %v", err)
	}
	if state, err := itStore.FinalizePayout(ctx, winner.ID, payoutResult); err != nil {
		t.Fatalf("finalize winning batch-funded payout: %v", err)
	} else if state != PayoutReleased {
		t.Fatalf("winning batch-funded payout state=%q, want %q", state, PayoutReleased)
	}
	var released, awaitingFunding, reservations int
	var reservedCents int64
	if err := itPool.QueryRow(ctx, `
		SELECT
		 count(*) FILTER (WHERE payout_status='released'),
		 count(*) FILTER (WHERE payout_status='awaiting_funding')
		FROM ledger_entries WHERE id=ANY($1)`, entries,
	).Scan(&released, &awaitingFunding); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*),COALESCE(sum(amount_cents),0)::bigint
		  FROM supplier_payout_funding
		 WHERE collection_payment_intent='pi_funding_batch_pool'`,
	).Scan(&reservations, &reservedCents); err != nil {
		t.Fatal(err)
	}
	if released != 1 || awaitingFunding != 1 || reservations != 1 || reservedCents != 60 || len(rail.calls) != 1 {
		t.Fatalf("batch pool over/under-allocation: released=%d awaiting=%d reservations=%d cents=%d calls=%d",
			released, awaitingFunding, reservations, reservedCents, len(rail.calls))
	}
}

func TestPayoutFundingSubsidyPoolConcurrentAuthorizationsCannotOverallocate(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fundRef := "subsidy-race-fund-" + uuid.NewString()
	actor := integrationAdminActor(t)
	created, err := itStore.CreateSubsidyFund(
		ctx, actor, fundRef, "subsidy-race-treasury-"+uuid.NewString(), 100,
		"finite treasury reserve for concurrent authorization proof")
	if err != nil || !created {
		t.Fatalf("create concurrent subsidy pool: created=%v err=%v", created, err)
	}

	entries := make([]uuid.UUID, 0, 2)
	for i := 0; i < 2; i++ {
		_, _, entryID := seedDuePayoutLiability(t, 0.60)
		entries = append(entries, entryID)
	}

	// As with the buyer batch test, force both production transactions to reach
	// the same fund-row lock before either can calculate remaining capacity.
	blocker, err := itPool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer blocker.Rollback(ctx)
	if _, err := blocker.Exec(ctx,
		`SELECT id FROM platform_subsidy_funds WHERE fund_ref=$1 FOR UPDATE`, fundRef); err != nil {
		t.Fatalf("lock shared subsidy fund: %v", err)
	}
	start := make(chan struct{})
	results := make(chan payoutSubsidyAuthorizationResult, len(entries))
	var ready sync.WaitGroup
	ready.Add(len(entries))
	for i, entryID := range entries {
		i, entryID := i, entryID
		go func() {
			ready.Done()
			<-start
			created, err := itStore.AuthorizePayoutSubsidy(
				context.Background(), actor, entryID, fundRef,
				fmt.Sprintf("subsidy-race-authorization-%d-%s", i, uuid.NewString()),
				"concurrent finite-capacity authorization")
			results <- payoutSubsidyAuthorizationResult{created: created, err: err}
		}()
	}
	ready.Wait()
	close(start)
	waitForBlockedFundingQueries(t, "FROM platform_subsidy_funds", len(entries))
	if err := blocker.Commit(ctx); err != nil {
		t.Fatalf("release shared subsidy-fund lock: %v", err)
	}

	authorized, rejected := 0, 0
	for range entries {
		result := <-results
		switch {
		case result.err == nil && result.created:
			authorized++
		case errors.Is(result.err, errSubsidyFundUnavailable):
			rejected++
		default:
			t.Fatalf("unexpected concurrent subsidy result: created=%v err=%v", result.created, result.err)
		}
	}
	if authorized != 1 || rejected != 1 {
		t.Fatalf("concurrent subsidy authorizations: authorized=%d rejected=%d, want 1/1", authorized, rejected)
	}

	var reservations, actions int
	var reservedCents int64
	if err := itPool.QueryRow(ctx, `
		SELECT count(*),COALESCE(sum(funding.amount_cents),0)::bigint,
		       (SELECT count(*) FROM admin_actions
		         WHERE kind='payout_subsidy_authorized'
		           AND fund_ref=$1)
		  FROM supplier_payout_funding funding
		  JOIN platform_subsidy_funds fund ON fund.id=funding.subsidy_fund_id
		 WHERE fund.fund_ref=$1`, fundRef,
	).Scan(&reservations, &reservedCents, &actions); err != nil {
		t.Fatal(err)
	}
	if reservations != 1 || reservedCents != 60 || actions != 1 {
		t.Fatalf("concurrent subsidy pool over/under-allocation: reservations=%d cents=%d actions=%d",
			reservations, reservedCents, actions)
	}
}

func (p *crossedBoundaryPayout) Send(ctx context.Context, supplier uuid.UUID, cents int64, currency, key string) (PayoutResult, error) {
	select {
	case p.crossed <- crossedPayoutCall{supplier: supplier, cents: cents, currency: currency, key: key}:
	case <-ctx.Done():
		return PayoutResult{}, ctx.Err()
	}
	select {
	case <-p.finish:
		return PayoutResult{Ref: "tr_crossed_race", SentCents: cents, Currency: currency, CashMoved: true}, nil
	case <-ctx.Done():
		return PayoutResult{}, ctx.Err()
	}
}

func TestPayoutCrossedBoundaryClawbackCannotBecomeReleasedOrRecovered(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	jobID := uuid.New()
	taskID := uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/payout-race/tasks/0/input.jsonl")
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='complete',verification_outcome='pass',verified_at=now(),completed_at=now()
		 WHERE id=$1`, taskID); err != nil {
		t.Fatalf("mark payout task verified: %v", err)
	}
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_payout_crossed_race", ChargeID: "ch_pi_payout_crossed_race", RequestedCents: 123,
		ReceivedCents: 123, Currency: "usd",
	}); err != nil {
		t.Fatalf("record exact buyer cash funding: %v", err)
	}
	past := time.Now().Add(-time.Minute)
	if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
		Kind: KindSupplierCredit, SupplierID: &demoSupplierUUID, TaskID: &taskID,
		AmountUSD: 1.23, PayoutStatus: PayoutHeld, ReleaseAt: &past,
	}}); err != nil {
		t.Fatalf("insert held supplier credit: %v", err)
	}

	rail := &crossedBoundaryPayout{
		crossed: make(chan crossedPayoutCall, 1),
		finish:  make(chan struct{}),
	}
	workerDone := make(chan error, 1)
	go func() { workerDone <- NewWorkers(itStore, itStorage, rail).releasePayouts(ctx) }()

	var call crossedPayoutCall
	select {
	case call = <-rail.crossed:
		// ClaimPayout has committed `sending`; the fake rail now represents cash
		// already moved, while its confirmation is withheld from the worker.
	case <-ctx.Done():
		t.Fatalf("payout never crossed provider boundary: %v", ctx.Err())
	}
	if call.supplier != demoSupplierUUID || call.cents != 123 || call.currency != "usd" || call.key == "" {
		t.Fatalf("provider call = %+v", call)
	}
	var before string
	if err := itPool.QueryRow(ctx,
		`SELECT payout_status FROM ledger_entries WHERE kind='supplier_credit' AND task_id=$1`, taskID,
	).Scan(&before); err != nil {
		t.Fatalf("read in-flight credit: %v", err)
	}
	if before != PayoutSending {
		t.Fatalf("provider was called without durable sending state: got %q", before)
	}

	// Fraud is confirmed while the transfer confirmation is in flight. This must
	// record liability reversal but cannot invent an external cash reversal.
	if err := itStore.ClawbackTaskCredit(ctx, demoSupplierUUID, taskID); err != nil {
		t.Fatalf("claw back in-flight credit: %v", err)
	}
	close(rail.finish)
	select {
	case err := <-workerDone:
		if err != nil {
			t.Fatalf("finalize crossed transfer: %v", err)
		}
	case <-ctx.Done():
		t.Fatalf("release worker did not finish: %v", ctx.Err())
	}

	var ledgerStatus, ledgerRef string
	if err := itPool.QueryRow(ctx, `
		SELECT payout_status,COALESCE(payout_ref,'')
		  FROM ledger_entries
		 WHERE kind='supplier_credit' AND task_id=$1`, taskID,
	).Scan(&ledgerStatus, &ledgerRef); err != nil {
		t.Fatalf("read final supplier credit: %v", err)
	}
	if ledgerStatus != PayoutReversalRequired || ledgerRef != "tr_crossed_race" {
		t.Fatalf("final credit = status %q ref %q, want reversal_required with real transfer ref", ledgerStatus, ledgerRef)
	}
	if ledgerStatus == PayoutReleased || ledgerStatus == PayoutClawedBack {
		t.Fatalf("crossed cash was mislabeled as %q", ledgerStatus)
	}

	var (
		opStatus          string
		requested, sent   int64
		currency, railRef string
		cashMoved         bool
	)
	if err := itPool.QueryRow(ctx, `
		SELECT status,requested_cents,COALESCE(sent_cents,0),currency,
		       cash_moved,COALESCE(transfer_ref,'')
		  FROM supplier_payout_operations op
		  JOIN ledger_entries le ON le.id=op.ledger_entry_id
		 WHERE le.task_id=$1 AND le.kind='supplier_credit'`, taskID,
	).Scan(&opStatus, &requested, &sent, &currency, &cashMoved, &railRef); err != nil {
		t.Fatalf("read durable payout operation: %v", err)
	}
	if opStatus != PayoutReversalRequired || requested != 123 || sent != 123 ||
		currency != "usd" || !cashMoved || railRef != "tr_crossed_race" {
		t.Fatalf("durable operation = status=%q requested=%d sent=%d currency=%q cash=%v ref=%q",
			opStatus, requested, sent, currency, cashMoved, railRef)
	}

	var clawbacks int
	var supplierNet float64
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE kind='clawback'),COALESCE(sum(amount_usd),0)::float8
		  FROM ledger_entries
		 WHERE task_id=$1 AND supplier_id=$2 AND kind IN ('supplier_credit','clawback')`,
		taskID, demoSupplierUUID).Scan(&clawbacks, &supplierNet); err != nil {
		t.Fatalf("read liability entries: %v", err)
	}
	if clawbacks != 1 || supplierNet != 0 {
		t.Fatalf("liability reversal = clawbacks %d net %.6f", clawbacks, supplierNet)
	}
	if !cashMoved {
		t.Fatal("liability netting erased the crossed-cash fact")
	}
}

func TestUnfundedPayoutsCannotStarveFundedLiability(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// More than one bounded sweep page of old unfunded liabilities used to recur
	// forever and hide every later payable row. Each failed funding admission now
	// advances once to awaiting_funding, so the queue makes bounded progress.
	unfundedEntries := make([]uuid.UUID, 0, sweepBatch+1)
	for i := 0; i < sweepBatch+1; i++ {
		_, _, entryID := seedDuePayoutLiability(t, 0.01)
		unfundedEntries = append(unfundedEntries, entryID)
	}
	fundedJob, _, fundedEntry := seedDuePayoutLiability(t, 0.25)
	if err := itStore.SetJobCharged(ctx, fundedJob, ChargeResult{
		PaymentIntentID: "pi_unfunded_starvation_younger", ChargeID: "ch_pi_unfunded_starvation_younger", RequestedCents: 25,
		ReceivedCents: 25, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}

	rail := &fundingGatePayout{}
	workers := NewWorkers(itStore, itStorage, rail)
	if err := workers.releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rail.calls) != 0 {
		t.Fatalf("first bounded page crossed provider for unfunded debt: %+v", rail.calls)
	}
	if err := workers.releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rail.calls) != 1 || rail.calls[0].key != fundedEntry.String() || rail.calls[0].cents != 25 {
		t.Fatalf("later funded liability remained starved: %+v", rail.calls)
	}
	if err := workers.releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	if len(rail.calls) != 1 {
		t.Fatalf("funded liability replayed after release: %+v", rail.calls)
	}

	var awaiting, unfundedReservations, unfundedOperations int
	if err := itPool.QueryRow(ctx, `
		SELECT
		 (SELECT count(*) FROM ledger_entries
		   WHERE id=ANY($1) AND payout_status='awaiting_funding'),
		 (SELECT count(*) FROM supplier_payout_funding WHERE ledger_entry_id=ANY($1)),
		 (SELECT count(*) FROM supplier_payout_operations WHERE ledger_entry_id=ANY($1))`,
		unfundedEntries,
	).Scan(&awaiting, &unfundedReservations, &unfundedOperations); err != nil {
		t.Fatal(err)
	}
	if awaiting != len(unfundedEntries) || unfundedReservations != 0 || unfundedOperations != 0 {
		t.Fatalf("unfunded queue state awaiting=%d/%d funding=%d operations=%d",
			awaiting, len(unfundedEntries), unfundedReservations, unfundedOperations)
	}
}

func TestAwaitingFundingRearmsOnlyOnExactCashOrSubsidy(t *testing.T) {
	t.Run("canonical buyer cash re-arms exact liability", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		jobID, _, entryID := seedDuePayoutLiability(t, 0.31)
		rail := &fundingGatePayout{}
		workers := NewWorkers(itStore, itStorage, rail)
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		var status string
		if err := itPool.QueryRow(ctx,
			`SELECT payout_status FROM ledger_entries WHERE id=$1`, entryID).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status != PayoutAwaitingFunding || len(rail.calls) != 0 {
			t.Fatalf("unfunded state=%q calls=%+v", status, rail.calls)
		}
		if err := itStore.AdminReleasePayoutHold(ctx, entryID, "must not manufacture cash"); !errors.Is(err, errNotHeld) {
			t.Fatalf("admin rearm awaiting funding err=%v, want %v", err, errNotHeld)
		}
		if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
			PaymentIntentID: "pi_awaiting_rearm", ChargeID: "ch_pi_awaiting_rearm", RequestedCents: 31,
			ReceivedCents: 31, Currency: "usd",
		}); err != nil {
			t.Fatal(err)
		}
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		if len(rail.calls) != 1 || rail.calls[0].key != entryID.String() {
			t.Fatalf("exact collection did not re-arm liability: %+v", rail.calls)
		}
	})

	t.Run("explicit finite subsidy re-arms exact liability", func(t *testing.T) {
		reset(t)
		ctx := context.Background()
		_, _, entryID := seedDuePayoutLiability(t, 0.32)
		rail := &fundingGatePayout{}
		workers := NewWorkers(itStore, itStorage, rail)
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		fundRef := "awaiting-fund-" + uuid.NewString()
		actor := integrationAdminActor(t)
		if created, err := itStore.CreateSubsidyFund(ctx, actor, fundRef,
			"awaiting-treasury-"+uuid.NewString(), 32, "explicit awaiting-funding recovery"); err != nil || !created {
			t.Fatalf("create subsidy created=%v err=%v", created, err)
		}
		if created, err := itStore.AuthorizePayoutSubsidy(ctx, actor, entryID, fundRef,
			"awaiting-auth-"+uuid.NewString(), "explicit awaiting-funding recovery"); err != nil || !created {
			t.Fatalf("authorize subsidy created=%v err=%v", created, err)
		}
		if err := workers.releasePayouts(ctx); err != nil {
			t.Fatal(err)
		}
		if len(rail.calls) != 1 || rail.calls[0].key != entryID.String() {
			t.Fatalf("subsidy did not re-arm liability: %+v", rail.calls)
		}
	})
}

func TestAmbiguousPayoutOutcomeCannotBecomeSafeReadyOrClawedBack(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID, entryID := seedDuePayoutLiability(t, 0.27)
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_ambiguous_payout", ChargeID: "ch_pi_ambiguous_payout", RequestedCents: 27,
		ReceivedCents: 27, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	rail := &responseLossPayout{}
	workers := NewWorkers(itStore, itStorage, rail)
	if err := workers.releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	var ledgerStatus, opStatus string
	var unknown, cashMoved bool
	if err := itPool.QueryRow(ctx, `
		SELECT le.payout_status,op.status,op.outcome_unknown,op.cash_moved
		  FROM ledger_entries le JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
		 WHERE le.id=$1`, entryID,
	).Scan(&ledgerStatus, &opStatus, &unknown, &cashMoved); err != nil {
		t.Fatal(err)
	}
	if ledgerStatus != PayoutOutcomeUnknown || opStatus != PayoutOutcomeUnknown || !unknown || cashMoved {
		t.Fatalf("ambiguous call = ledger=%q op=%q unknown=%v cash=%v",
			ledgerStatus, opStatus, unknown, cashMoved)
	}
	if err := itStore.AdminReleasePayoutHold(ctx, entryID, "unsafe unknown bypass"); !errors.Is(err, errNotHeld) {
		t.Fatalf("admin rearm ambiguous payout err=%v, want %v", err, errNotHeld)
	}

	if err := itStore.ClawbackTaskCredit(ctx, demoSupplierUUID, taskID); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT le.payout_status,op.status,op.outcome_unknown
		  FROM ledger_entries le JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
		 WHERE le.id=$1`, entryID,
	).Scan(&ledgerStatus, &opStatus, &unknown); err != nil {
		t.Fatal(err)
	}
	if ledgerStatus != PayoutReversalRequired || opStatus != PayoutReversalRequired || !unknown {
		t.Fatalf("unknown clawback = ledger=%q op=%q unknown=%v, want reversal_required",
			ledgerStatus, opStatus, unknown)
	}

	// Resolution replays only the same key. The fake provider returns the one
	// transfer created before response loss; cash is recorded, while the clawback
	// keeps the row reversal_required until a real reversal exists.
	if _, err := itPool.Exec(ctx,
		`UPDATE supplier_payout_operations SET updated_at=now()-interval '10 minutes' WHERE ledger_entry_id=$1`,
		entryID); err != nil {
		t.Fatal(err)
	}
	if err := workers.releasePayouts(ctx); err != nil {
		t.Fatal(err)
	}
	var ref string
	if err := itPool.QueryRow(ctx, `
		SELECT le.payout_status,op.status,op.outcome_unknown,op.cash_moved,COALESCE(op.transfer_ref,'')
		  FROM ledger_entries le JOIN supplier_payout_operations op ON op.ledger_entry_id=le.id
		 WHERE le.id=$1`, entryID,
	).Scan(&ledgerStatus, &opStatus, &unknown, &cashMoved, &ref); err != nil {
		t.Fatal(err)
	}
	if ledgerStatus != PayoutReversalRequired || opStatus != PayoutReversalRequired ||
		unknown || !cashMoved || ref == "" || len(rail.calls) != 2 || rail.calls[0].key != rail.calls[1].key {
		t.Fatalf("resolved unknown = ledger=%q op=%q unknown=%v cash=%v ref=%q calls=%+v",
			ledgerStatus, opStatus, unknown, cashMoved, ref, rail.calls)
	}
	// Canonical cash, its exact reservation, and payout-operation identity remain
	// immutable even to a direct SQL writer; only the explicit state fields move.
	if _, err := itPool.Exec(ctx,
		`UPDATE buyer_cash_collections SET received_cents=26 WHERE payment_intent='pi_ambiguous_payout'`); err == nil {
		t.Fatal("canonical buyer cash fact was mutable")
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE supplier_payout_funding SET amount_cents=26 WHERE ledger_entry_id=$1`, entryID); err == nil {
		t.Fatal("supplier payout funding reservation was mutable")
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE supplier_payout_operations SET requested_cents=26 WHERE ledger_entry_id=$1`, entryID); err == nil {
		t.Fatal("supplier payout operation identity was mutable")
	}
	if _, err := itPool.Exec(ctx,
		`DELETE FROM supplier_payout_operations WHERE ledger_entry_id=$1`, entryID); err == nil {
		t.Fatal("supplier payout operation was deletable")
	}
}

func TestAdminMoneyTruthRequiresCashMovedOperation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, false, false, "jobs/admin-money-truth/tasks/0/input.jsonl")
	if err := itStore.SetJobCharged(ctx, jobID, ChargeResult{
		PaymentIntentID: "pi_admin_money_truth", ChargeID: "ch_pi_admin_money_truth", RequestedCents: 124,
		ReceivedCents: 124, Currency: "usd",
	}); err != nil {
		t.Fatal(err)
	}
	var entryID uuid.UUID
	if err := itPool.QueryRow(ctx, `
		INSERT INTO ledger_entries
		  (kind,supplier_id,task_id,amount_usd,payout_status,payout_ref)
		VALUES ('supplier_credit',$1,$2,1.239999,'released','legacy-unproved-ref')
		RETURNING id`, demoSupplierUUID, taskID,
	).Scan(&entryID); err != nil {
		t.Fatal(err)
	}

	summary, err := itStore.AdminSummaryData(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(summary.Money.CollectedUSD-1.24) > 1e-9 ||
		math.Abs(summary.Money.TransferredUSD) > 1e-9 ||
		math.Abs(summary.Money.FlowOwedUSD-1.239999) > 1e-9 ||
		summary.Accessible == nil || math.Abs(summary.Accessible.TheirsPendingTransferUSD-1.239999) > 1e-9 {
		t.Fatalf("unproved released row distorted admin money truth: %+v", summary)
	}
	rollups, err := itStore.ListPayoutsAdmin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, row := range rollups {
		if row.SupplierID == demoSupplierUUID && row.PayoutStatus == PayoutReleased {
			found = true
			if row.ReleasedWithoutCashCount != 1 || row.CashSentUSD != 0 {
				t.Fatalf("released anomaly rollup=%+v", row)
			}
		}
	}
	if !found {
		t.Fatal("released-without-cash anomaly missing from admin rollup")
	}
}
