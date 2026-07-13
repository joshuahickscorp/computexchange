//go:build integration

package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
)

func seedIntegrationPasskeySession(t *testing.T) (AdminActor, string) {
	t.Helper()
	ctx := context.Background()
	credentialRowID, sessionID := uuid.New(), uuid.New()
	credentialID := []byte("integration-passkey-" + uuid.NewString())
	label := "integration Touch ID"
	if _, err := itPool.Exec(ctx, `
		INSERT INTO admin_credentials (id,credential_id,credential,label,revoked)
		VALUES ($1,$2,'{}'::jsonb,$3,false)`, credentialRowID, credentialID, label); err != nil {
		t.Fatalf("seed passkey credential: %v", err)
	}
	raw := "cx_admin_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := itPool.Exec(ctx, `
		INSERT INTO admin_sessions
		  (id,token_hash,admin_credential_id,expires_at,revoked)
		VALUES ($1,$2,$3,now()+interval '1 hour',false)`,
		sessionID, hashKey(raw), credentialRowID); err != nil {
		t.Fatalf("seed passkey session: %v", err)
	}
	return AdminActor{
		Mode:             AdminAuthPasskeySession,
		PrincipalID:      credentialRowID,
		SessionID:        &sessionID,
		AttributionScope: AdminAttributionCredentialOnly,
		Label:            label,
	}, raw
}

func TestMoneyAuthorityAttributionRetryBindingAndRedactedReview(t *testing.T) {
	reset(t)
	ctx := context.Background()
	passkeyActor, rawSession := seedIntegrationPasskeySession(t)
	fundRef := "authority-fund-" + uuid.NewString()
	treasurySecret := "treasury-secret-" + uuid.NewString()
	fundReason := "bounded operator make-good"

	// A request carrying both credentials must retain authAdmin's passkey-first
	// semantics. The bearer key is present deliberately; it must not win audit
	// attribution over the valid passkey session.
	code, body := req(t, http.MethodPost, "/admin/subsidy-funds",
		map[string]any{
			"fund_ref": fundRef, "external_treasury_ref": treasurySecret,
			"authorized_cents": 200, "reason": fundReason,
		}, hdr{"Cookie", adminSessionCookie + "=" + rawSession}, adminKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("create passkey-attributed fund: %d %s", code, body)
	}

	var (
		fundID, actionID, actorID, sessionID uuid.UUID
		mode, scope, digest                  string
		amount                               int64
	)
	if err := itPool.QueryRow(ctx, `
		SELECT f.id,a.id,a.actor_principal_id,a.actor_session_id,a.actor_mode,
		       a.attribution_scope,a.request_sha256,a.amount_cents
		  FROM platform_subsidy_funds f
		  JOIN admin_actions a ON a.id=f.authorization_action_id
		 WHERE f.fund_ref=$1`, fundRef).Scan(
		&fundID, &actionID, &actorID, &sessionID, &mode, &scope, &digest, &amount); err != nil {
		t.Fatal(err)
	}
	if actorID != passkeyActor.PrincipalID || passkeyActor.SessionID == nil ||
		sessionID != *passkeyActor.SessionID || mode != string(AdminAuthPasskeySession) ||
		scope != string(AdminAttributionCredentialOnly) || len(digest) != 64 || amount != 200 {
		t.Fatalf("fund attribution/binding = actor=%s session=%s mode=%q scope=%q digest=%q cents=%d",
			actorID, sessionID, mode, scope, digest, amount)
	}

	// An exact retry through the shared break-glass credential returns the existing
	// fact but cannot rewrite its original passkey actor or digest.
	code, body = req(t, http.MethodPost, "/admin/subsidy-funds",
		map[string]any{
			"reason": fundReason, "authorized_cents": 200,
			"external_treasury_ref": treasurySecret, "fund_ref": fundRef,
		}, adminKey(), jsonCT())
	if code != http.StatusOK || !strings.Contains(string(body), `"created":false`) {
		t.Fatalf("exact cross-actor retry: %d %s", code, body)
	}
	var retryActorID, retrySessionID uuid.UUID
	var retryDigest string
	if err := itPool.QueryRow(ctx,
		`SELECT actor_principal_id,actor_session_id,request_sha256 FROM admin_actions WHERE id=$1`,
		actionID).Scan(&retryActorID, &retrySessionID, &retryDigest); err != nil {
		t.Fatal(err)
	}
	if retryActorID != actorID || retrySessionID != sessionID || retryDigest != digest {
		t.Fatal("exact retry rewrote the original actor/session/digest")
	}

	// The payout authorization binds the target liability and exact floor cents;
	// the remaining 9,999 microusd is settlement carry, never rounded into cash.
	_, _, entryID := seedDuePayoutLiability(t, 1.239999)
	authorizationRef := "authority-liability-" + uuid.NewString()
	code, body = req(t, http.MethodPost, "/admin/payouts/"+entryID.String()+"/subsidize",
		map[string]any{
			"fund_ref": fundRef, "authorization_ref": authorizationRef,
			"reason": "approve exact supplier liability",
		}, adminKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("authorize payout subsidy: %d %s", code, body)
	}
	breakGlassActor := integrationAdminActor(t)
	var payoutActorID, targetID uuid.UUID
	var payoutMode, payoutScope, payoutDigest string
	var payoutCents int64
	if err := itPool.QueryRow(ctx, `
		SELECT a.actor_principal_id,a.actor_mode,a.attribution_scope,a.target_id,
		       a.amount_cents,a.request_sha256
		  FROM supplier_payout_funding p
		  JOIN admin_actions a ON a.id=p.authorization_action_id
		 WHERE p.ledger_entry_id=$1`, entryID).Scan(
		&payoutActorID, &payoutMode, &payoutScope, &targetID, &payoutCents, &payoutDigest); err != nil {
		t.Fatal(err)
	}
	if payoutActorID != breakGlassActor.PrincipalID || payoutMode != string(AdminAuthBreakGlassAPIKey) ||
		payoutScope != string(AdminAttributionSharedCredentialOnly) || targetID != entryID ||
		payoutCents != 123 || len(payoutDigest) != 64 {
		t.Fatalf("payout authority binding actor=%s mode=%q scope=%q target=%s cents=%d digest=%q",
			payoutActorID, payoutMode, payoutScope, targetID, payoutCents, payoutDigest)
	}

	// Reusing the natural correlation with changed meaning is a conflict and leaves
	// both the funding fact and original audit row byte-for-byte authoritative.
	code, _ = req(t, http.MethodPost, "/admin/payouts/"+entryID.String()+"/subsidize",
		map[string]any{
			"fund_ref": fundRef, "authorization_ref": authorizationRef,
			"reason": "changed retry meaning",
		}, adminKey(), jsonCT())
	if code != http.StatusConflict {
		t.Fatalf("conflicting payout retry status=%d, want 409", code)
	}

	reviewReq, err := http.NewRequest(http.MethodGet, itHTTP.URL+"/admin/actions?limit=200", nil)
	if err != nil {
		t.Fatal(err)
	}
	reviewReq.Header.Set("Authorization", "Bearer "+demoAdminAPIKey)
	reviewResp, err := http.DefaultClient.Do(reviewReq)
	if err != nil {
		t.Fatal(err)
	}
	defer reviewResp.Body.Close()
	reviewBody, _ := io.ReadAll(reviewResp.Body)
	if reviewResp.StatusCode != http.StatusOK || reviewResp.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("review status/cache=%d/%q body=%s", reviewResp.StatusCode,
			reviewResp.Header.Get("Cache-Control"), reviewBody)
	}
	for _, forbidden := range []string{treasurySecret, rawSession, demoAdminAPIKey, hashKey(rawSession), "external_treasury_ref", "detail"} {
		if strings.Contains(string(reviewBody), forbidden) {
			t.Fatalf("review exposed forbidden secret/material %q: %s", forbidden, reviewBody)
		}
	}
	for _, required := range []string{"passkey_session", "break_glass_api_key", "shared_credential_only", authorizationRef} {
		if !strings.Contains(string(reviewBody), required) {
			t.Fatalf("review omitted attribution %q: %s", required, reviewBody)
		}
	}
}

func TestMoneyAuthorityRevocationAndMissingActorFailBeforeMutation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	keyID := uuid.New()
	rawKey := "cx_test_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	if _, err := itPool.Exec(ctx, `
		INSERT INTO api_keys (id,buyer_id,key_hash,is_admin,revoked,name)
		VALUES ($1,$2,$3,true,false,'revocation-race-test')`,
		keyID, demoBuyerUUID, hashKey(rawKey)); err != nil {
		t.Fatal(err)
	}
	actor := AdminActor{
		Mode: AdminAuthBreakGlassAPIKey, PrincipalID: keyID,
		AttributionScope: AdminAttributionSharedCredentialOnly,
	}
	if _, err := itPool.Exec(ctx, `UPDATE api_keys SET revoked=true WHERE id=$1`, keyID); err != nil {
		t.Fatal(err)
	}
	fundRef := "revoked-fund-" + uuid.NewString()
	if created, err := itStore.CreateSubsidyFund(
		ctx, actor, fundRef, "revoked-treasury-"+uuid.NewString(), 10, "must fail revocation recheck",
	); created || !errors.Is(err, errAdminActorUnauthorized) {
		t.Fatalf("revoked actor created=%v err=%v, want unauthorized", created, err)
	}

	passkeyActor, _ := seedIntegrationPasskeySession(t)
	if _, err := itPool.Exec(ctx, `UPDATE admin_credentials SET revoked=true WHERE id=$1`, passkeyActor.PrincipalID); err != nil {
		t.Fatal(err)
	}
	if created, err := itStore.CreateSubsidyFund(
		ctx, passkeyActor, "revoked-passkey-fund-"+uuid.NewString(),
		"revoked-passkey-treasury-"+uuid.NewString(), 10, "must fail credential revocation",
	); created || !errors.Is(err, errAdminActorUnauthorized) {
		t.Fatalf("revoked passkey created=%v err=%v, want unauthorized", created, err)
	}

	if created, err := itStore.CreateSubsidyFund(
		ctx, AdminActor{}, "missing-actor-fund-"+uuid.NewString(),
		"missing-actor-treasury-"+uuid.NewString(), 10, "must fail missing identity",
	); created || !errors.Is(err, errAdminActorUnauthorized) {
		t.Fatalf("missing actor created=%v err=%v, want unauthorized", created, err)
	}
	var rows int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM platform_subsidy_funds WHERE fund_ref LIKE 'revoked-%' OR fund_ref LIKE 'missing-actor-%'`).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 0 {
		t.Fatalf("unauthorized actor left %d subsidy fund rows", rows)
	}
}

func TestMoneyAuthorityMutationAuditAtomicAndAppendOnly(t *testing.T) {
	reset(t)
	ctx := context.Background()
	actor := integrationAdminActor(t)

	if _, err := itPool.Exec(ctx, `
		CREATE OR REPLACE FUNCTION cx_test_reject_authority_action()
		RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
		  IF NEW.correlation_ref LIKE 'fault-action-%' THEN
		    RAISE EXCEPTION 'injected action failure';
		  END IF;
		  RETURN NEW;
		END $$;
		DROP TRIGGER IF EXISTS cx_test_reject_authority_action ON admin_actions;
		CREATE TRIGGER cx_test_reject_authority_action BEFORE INSERT ON admin_actions
		FOR EACH ROW EXECUTE FUNCTION cx_test_reject_authority_action()`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(), `DROP TRIGGER IF EXISTS cx_test_reject_authority_action ON admin_actions`)
		_, _ = itPool.Exec(context.Background(), `DROP FUNCTION IF EXISTS cx_test_reject_authority_action()`)
	})
	faultActionRef := "fault-action-" + uuid.NewString()
	if created, err := itStore.CreateSubsidyFund(ctx, actor, faultActionRef,
		"fault-action-treasury-"+uuid.NewString(), 10, "injected action rollback"); created || err == nil {
		t.Fatalf("action fault created=%v err=%v, want rollback", created, err)
	}
	var fundRows, actionRows int
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM platform_subsidy_funds WHERE fund_ref=$1),
		       (SELECT count(*) FROM admin_actions WHERE correlation_ref=$1)`, faultActionRef).
		Scan(&fundRows, &actionRows); err != nil {
		t.Fatal(err)
	}
	if fundRows != 0 || actionRows != 0 {
		t.Fatalf("action failure was not atomic: funds=%d actions=%d", fundRows, actionRows)
	}

	if _, err := itPool.Exec(ctx, `
		CREATE OR REPLACE FUNCTION cx_test_reject_authority_fund()
		RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
		  IF NEW.fund_ref LIKE 'fault-fund-%' THEN
		    RAISE EXCEPTION 'injected fund failure';
		  END IF;
		  RETURN NEW;
		END $$;
		DROP TRIGGER IF EXISTS cx_test_reject_authority_fund ON platform_subsidy_funds;
		CREATE TRIGGER cx_test_reject_authority_fund BEFORE INSERT ON platform_subsidy_funds
		FOR EACH ROW EXECUTE FUNCTION cx_test_reject_authority_fund()`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = itPool.Exec(context.Background(), `DROP TRIGGER IF EXISTS cx_test_reject_authority_fund ON platform_subsidy_funds`)
		_, _ = itPool.Exec(context.Background(), `DROP FUNCTION IF EXISTS cx_test_reject_authority_fund()`)
	})
	faultFundRef := "fault-fund-" + uuid.NewString()
	if created, err := itStore.CreateSubsidyFund(ctx, actor, faultFundRef,
		"fault-fund-treasury-"+uuid.NewString(), 10, "injected resource rollback"); created || err == nil {
		t.Fatalf("resource fault created=%v err=%v, want rollback", created, err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM platform_subsidy_funds WHERE fund_ref=$1),
		       (SELECT count(*) FROM admin_actions WHERE correlation_ref=$1)`, faultFundRef).
		Scan(&fundRows, &actionRows); err != nil {
		t.Fatal(err)
	}
	if fundRows != 0 || actionRows != 0 {
		t.Fatalf("resource failure was not atomic: funds=%d actions=%d", fundRows, actionRows)
	}

	validRef := "append-only-fund-" + uuid.NewString()
	if created, err := itStore.CreateSubsidyFund(ctx, actor, validRef,
		"append-only-treasury-"+uuid.NewString(), 25, "immutable authorization"); err != nil || !created {
		t.Fatalf("create append-only fixture: created=%v err=%v", created, err)
	}
	var validFundID, validActionID uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT id,authorization_action_id FROM platform_subsidy_funds WHERE fund_ref=$1`, validRef).
		Scan(&validFundID, &validActionID); err != nil {
		t.Fatal(err)
	}
	for name, tc := range map[string]struct {
		query string
		args  []any
	}{
		"action update": {`UPDATE admin_actions SET reason=reason WHERE id=$1`, []any{validActionID}},
		"action delete": {`DELETE FROM admin_actions WHERE id=$1`, []any{validActionID}},
		"fund mutation": {`UPDATE platform_subsidy_funds SET authorized_cents=authorized_cents+1 WHERE id=$1`, []any{validFundID}},
		"fund delete":   {`DELETE FROM platform_subsidy_funds WHERE id=$1`, []any{validFundID}},
	} {
		if _, err := itPool.Exec(ctx, tc.query, tc.args...); err == nil {
			t.Fatalf("%s unexpectedly succeeded", name)
		}
	}

	// A syntactically valid action paired to a mismatched resource is rejected by
	// the deferred two-way binding at commit, rolling both inserts back.
	mismatchActionID, mismatchFundID := uuid.New(), uuid.New()
	mismatchRef := "mismatch-fund-" + uuid.NewString()
	tx, err := itPool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO admin_actions
		 (id,kind,reason,actor_mode,actor_principal_id,attribution_scope,intent_version,
		  request_sha256,correlation_ref,target_kind,target_id,fund_id,fund_ref,amount_cents,currency)
		VALUES ($1,'subsidy_fund_authorized','mismatch','break_glass_api_key',$2,
		 'shared_credential_only',1,$3,$4,'subsidy_fund',$5,$5,$4,10,'usd')`,
		mismatchActionID, actor.PrincipalID, strings.Repeat("a", 64), mismatchRef, mismatchFundID); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO platform_subsidy_funds
		 (id,authorization_action_id,fund_ref,external_treasury_ref,authorized_cents,currency,reason,status)
		VALUES ($1,$2,$3,$4,11,'usd','mismatch','active')`,
		mismatchFundID, mismatchActionID, mismatchRef, "mismatch-treasury-"+uuid.NewString()); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err == nil {
		t.Fatal("commit accepted mismatched action/resource cents")
	}
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM platform_subsidy_funds WHERE id=$1),
		       (SELECT count(*) FROM admin_actions WHERE id=$2)`, mismatchFundID, mismatchActionID).
		Scan(&fundRows, &actionRows); err != nil {
		t.Fatal(err)
	}
	if fundRows != 0 || actionRows != 0 {
		t.Fatalf("deferred mismatch left rows: funds=%d actions=%d", fundRows, actionRows)
	}

	// Even an otherwise exact direct pair cannot manufacture an authenticated
	// principal UUID. The deferred action trigger resolves the mode-discriminated
	// credential at commit instead of trusting the copied id.
	fakeActionID, fakeFundID, fakePrincipalID := uuid.New(), uuid.New(), uuid.New()
	fakeRef := "fake-principal-fund-" + uuid.NewString()
	tx, err = itPool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO admin_actions
		 (id,kind,reason,actor_mode,actor_principal_id,attribution_scope,intent_version,
		  request_sha256,correlation_ref,target_kind,target_id,fund_id,fund_ref,amount_cents,currency)
		VALUES ($1,'subsidy_fund_authorized','fake principal','break_glass_api_key',$2,
		 'shared_credential_only',1,$3,$4,'subsidy_fund',$5,$5,$4,10,'usd')`,
		fakeActionID, fakePrincipalID, strings.Repeat("b", 64), fakeRef, fakeFundID); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO platform_subsidy_funds
		 (id,authorization_action_id,fund_ref,external_treasury_ref,authorized_cents,currency,reason,status)
		VALUES ($1,$2,$3,$4,10,'usd','fake principal','active')`,
		fakeFundID, fakeActionID, fakeRef, "fake-principal-treasury-"+uuid.NewString()); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(ctx); err == nil {
		t.Fatal("commit accepted a nonexistent break-glass principal id")
	}
}

func TestMoneyAuthorityConcurrentExactFundHasOneOriginalActor(t *testing.T) {
	reset(t)
	ctx := context.Background()
	actor := integrationAdminActor(t)
	fundRef := "concurrent-exact-fund-" + uuid.NewString()
	treasuryRef := "concurrent-exact-treasury-" + uuid.NewString()
	type result struct {
		created bool
		err     error
	}
	results := make(chan result, 2)
	start := make(chan struct{})
	var ready sync.WaitGroup
	ready.Add(2)
	for i := 0; i < 2; i++ {
		go func() {
			ready.Done()
			<-start
			created, err := itStore.CreateSubsidyFund(
				context.Background(), actor, fundRef, treasuryRef, 50, "concurrent exact intent")
			results <- result{created: created, err: err}
		}()
	}
	ready.Wait()
	close(start)
	createdCount, retryCount := 0, 0
	for i := 0; i < 2; i++ {
		r := <-results
		if r.err != nil {
			t.Fatalf("concurrent exact result: %v", r.err)
		}
		if r.created {
			createdCount++
		} else {
			retryCount++
		}
	}
	if createdCount != 1 || retryCount != 1 {
		t.Fatalf("concurrent exact create/retry=%d/%d, want 1/1", createdCount, retryCount)
	}
	var funds, actions int
	if err := itPool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM platform_subsidy_funds WHERE fund_ref=$1),
		       (SELECT count(*) FROM admin_actions
		         WHERE kind='subsidy_fund_authorized' AND correlation_ref=$1)`, fundRef).
		Scan(&funds, &actions); err != nil {
		t.Fatal(err)
	}
	if funds != 1 || actions != 1 {
		t.Fatalf("concurrent exact rows funds=%d actions=%d, want 1/1", funds, actions)
	}
}

func TestMoneyAuthorityCanonicalDigestChangesOnlyWithSemanticIntent(t *testing.T) {
	fundID := uuid.New()
	base := moneyAuthorityIntent{
		Kind: "subsidy_fund_authorized", TargetKind: "subsidy_fund",
		TargetID: fundID, FundID: fundID, FundRef: "fund-one",
		ExternalTreasuryRef: "treasury-one", AmountCents: 25, Currency: "USD",
		Reason: "  documented reason  ", CorrelationRef: "fund-one",
	}
	one, err := moneyAuthorityRequestSHA256(base)
	if err != nil {
		t.Fatal(err)
	}
	normalized := base
	normalized.Currency = "usd"
	normalized.Reason = "documented reason"
	two, err := moneyAuthorityRequestSHA256(normalized)
	if err != nil {
		t.Fatal(err)
	}
	if one != two || len(one) != 64 {
		t.Fatalf("normalization digest one=%q two=%q", one, two)
	}
	changed := normalized
	changed.AmountCents++
	three, err := moneyAuthorityRequestSHA256(changed)
	if err != nil {
		t.Fatal(err)
	}
	if three == one {
		t.Fatal("changed cents did not change canonical request digest")
	}
	if _, err := moneyAuthorityRequestSHA256(moneyAuthorityIntent{}); err == nil {
		t.Fatal("empty money authority intent was accepted")
	}

}
