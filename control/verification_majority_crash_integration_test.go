//go:build integration

package main

import (
	"context"
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/google/uuid"
)

var (
	majorityCrashFirstTaskID  = uuid.MustParse("20000000-0000-0000-0000-000000000001")
	majorityCrashSecondTaskID = uuid.MustParse("20000000-0000-0000-0000-000000000002")
	majorityCrashWorker2ID    = uuid.MustParse("20000000-0000-0000-0000-0000000000a2")
	majorityCrashWorker3ID    = uuid.MustParse("20000000-0000-0000-0000-0000000000a3")
)

type verificationMajorityCrashFixture struct {
	Upload         verificationCrashFixture
	CommitterLoses bool
}

type verificationMajorityCrashState struct {
	Terminal          verificationCrashState `json:"terminal"`
	FirstTask         string                 `json:"first_task"`
	SecondTask        string                 `json:"second_task"`
	FirstCredit       string                 `json:"first_credit"`
	FirstClawback     string                 `json:"first_clawback"`
	RootMoneyRows     int                    `json:"root_money_rows"`
	BuyerChargeRows   int                    `json:"buyer_charge_rows"`
	TerminalWorkRows  int                    `json:"terminal_work_rows"`
	JobTaskCount      int                    `json:"job_task_count"`
	JobTasksDone      int                    `json:"job_tasks_done"`
	RoleEvents        []string               `json:"role_events"`
	RoleResolutions   []string               `json:"role_resolutions"`
	ArtifactAuthority []string               `json:"artifact_authority"`
}

func registerFixedMajorityCrashWorker(t *testing.T, workerID, supplierID uuid.UUID) {
	t.Helper()
	capability := demoProductionCapability()
	capability.WorkerID = workerID
	capability.SupplierID = supplierID
	capability.AgentVersion = "verification-majority-crash-test"
	if err := itStore.UpsertWorker(context.Background(), capability); err != nil {
		t.Fatalf("register majority crash worker %s: %v", workerID, err)
	}
}

func seedVerificationMajorityCrashFixture(t *testing.T, committerLoses bool) verificationMajorityCrashFixture {
	t.Helper()
	ctx := context.Background()
	ensureExtraDemoSuppliers(t, ctx)
	registerFixedMajorityCrashWorker(t, majorityCrashWorker2ID, demoSupplier2UUID)
	registerFixedMajorityCrashWorker(t, majorityCrashWorker3ID, demoSupplier3UUID)
	upload := seedVerificationCrashFixture(t, false)
	// The authority fixture uses this canonical chunk input. Keep the committing
	// task and its parent on the same immutable chunk identity so peer discovery
	// sees the intended three independent results.
	const inputRef = "jobs/authority/chunk-0.input"
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET input_ref=$2 WHERE id=$1`, verificationCrashJobID, inputRef); err != nil {
		t.Fatalf("align majority job input: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET input_ref=$2 WHERE id=$1`, verificationCrashTaskID, inputRef); err != nil {
		t.Fatalf("align majority committing input: %v", err)
	}

	winningBody := alternateEmbedResultJSON()
	firstBody := embedResultJSON(1)
	rootBody := winningBody
	if committerLoses {
		firstBody = winningBody
		rootBody = embedResultJSON(1)
	}
	if err := itStorage.PutObject(ctx, upload.Commit.ResultKey, rootBody, "application/json"); err != nil {
		t.Fatalf("put majority crash committing artifact: %v", err)
	}
	first := seedTerminalArtifactAuthority(t, verificationCrashJobID, majorityCrashFirstTaskID,
		majorityCrashWorker2ID, demoSupplier2UUID, false, nil, firstBody, 0)
	second := seedTerminalArtifactAuthority(t, verificationCrashJobID, majorityCrashSecondTaskID,
		majorityCrashWorker3ID, demoSupplier3UUID, true, nil, winningBody, 1)
	// Both earlier results are deliberately provisional so the majority decision
	// must promote every winning side and claw back a real credited loser.
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks SET verification_outcome='pass_with_penalty'
		 WHERE id IN ($1,$2)`, first.TaskID, second.TaskID); err != nil {
		t.Fatal(err)
	}
	insertChunkArtifactResolution(t, verificationCrashJobID, first, "provisional")
	if _, err := itPool.Exec(ctx, `
		INSERT INTO ledger_entries
		 (kind,supplier_id,task_id,amount_usd,payout_status,release_at)
		VALUES ('supplier_credit',$1,$2,0.009000,'held',now()+interval '24 hours')`,
		demoSupplier2UUID, first.TaskID); err != nil {
		t.Fatalf("seed real provisional supplier credit: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE tasks SET hedged_from=$2 WHERE id=$1`, verificationCrashTaskID, first.TaskID); err != nil {
		t.Fatalf("bind committing hedge: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET status='verifying',task_count=3,tasks_done=1 WHERE id=$1`,
		verificationCrashJobID); err != nil {
		t.Fatalf("bind majority job counters: %v", err)
	}
	return verificationMajorityCrashFixture{Upload: upload, CommitterLoses: committerLoses}
}

func runVerificationMajorityCrashToTerminal(t *testing.T, fixture verificationMajorityCrashFixture) verificationMajorityCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Upload.Commit); err != nil {
		t.Fatalf("commit majority crash work: %v", err)
	}
	result, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, verificationCrashTaskID, 0)
	want := OutcomePass
	if fixture.CommitterLoses {
		want = OutcomeLossNoPayout
	}
	if err != nil || result.Pending || result.Outcome != want || !result.Applied.Applied {
		t.Fatalf("majority baseline = %+v err=%v, want %s", result, err, want)
	}
	state := readVerificationMajorityCrashState(t)
	assertVerificationMajorityCrashSemantics(t, fixture, state)
	assertVerificationTerminalReplay(t, fixture.Upload, state.Terminal)
	if replayed := readVerificationMajorityCrashState(t); !reflect.DeepEqual(replayed, state) {
		t.Fatalf("majority terminal replay changed state:\nbefore=%+v\nafter =%+v", state, replayed)
	}
	return state
}

func readVerificationMajorityCrashState(t *testing.T) verificationMajorityCrashState {
	t.Helper()
	ctx := context.Background()
	state := verificationMajorityCrashState{Terminal: readVerificationCrashState(t)}
	if err := itPool.QueryRow(ctx, `
		SELECT status||':'||COALESCE(verification_outcome,'') FROM tasks WHERE id=$1`,
		majorityCrashFirstTaskID).Scan(&state.FirstTask); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT status||':'||COALESCE(verification_outcome,'') FROM tasks WHERE id=$1`,
		majorityCrashSecondTaskID).Scan(&state.SecondTask); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*)::text||':'||COALESCE(sum(amount_usd),0)::text||':'||COALESCE(max(payout_status),'')
		  FROM ledger_entries WHERE task_id=$1 AND kind='supplier_credit'`, majorityCrashFirstTaskID).
		Scan(&state.FirstCredit); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*)::text||':'||COALESCE(sum(amount_usd),0)::text
		  FROM ledger_entries WHERE task_id=$1 AND kind='clawback'`, majorityCrashFirstTaskID).
		Scan(&state.FirstClawback); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT
		 (SELECT count(*) FROM ledger_entries WHERE task_id=$1),
		 (SELECT count(*) FROM ledger_entries le JOIN tasks t ON t.id=le.task_id
		    WHERE t.job_id=$2 AND le.kind='buyer_charge'),
		 (SELECT count(*) FROM verification_work w JOIN tasks t ON t.id=w.task_id
		    WHERE t.job_id=$2 AND w.status='terminal'),
		 task_count,tasks_done
		 FROM jobs WHERE id=$2`, verificationCrashTaskID, verificationCrashJobID).
		Scan(&state.RootMoneyRows, &state.BuyerChargeRows, &state.TerminalWorkRows,
			&state.JobTaskCount, &state.JobTasksDone); err != nil {
		t.Fatal(err)
	}
	state.RoleEvents = readCrashStringsArgs(t, `
		SELECT (CASE task_id WHEN $2 THEN 'root' WHEN $3 THEN 'first' WHEN $4 THEN 'second' ELSE 'other' END)
		       ||':'||kind
		  FROM verification_events WHERE job_id=$1
		 ORDER BY 1`, verificationCrashJobID, verificationCrashTaskID, majorityCrashFirstTaskID, majorityCrashSecondTaskID)
	state.RoleResolutions = readCrashStringsArgs(t, `
		SELECT (CASE task_id WHEN $1 THEN 'root' WHEN $2 THEN 'first' WHEN $3 THEN 'second' ELSE 'other' END)
		       ||':'||kind
		  FROM task_verdict_resolutions
		 WHERE task_id IN ($1,$2,$3)
		 ORDER BY 1`, verificationCrashTaskID, majorityCrashFirstTaskID, majorityCrashSecondTaskID)
	state.ArtifactAuthority = readCrashStringsArgs(t, `
		SELECT basis||':'||(CASE winner_task_id WHEN $2 THEN 'root' WHEN $3 THEN 'first' WHEN $4 THEN 'second' ELSE 'other' END)
		       ||':'||artifact_sha256
		  FROM chunk_artifact_resolutions WHERE job_id=$1 ORDER BY basis`,
		verificationCrashJobID, verificationCrashTaskID, majorityCrashFirstTaskID, majorityCrashSecondTaskID)
	return state
}

// readCrashStringsArgs is the multi-argument counterpart of the original crash
// helper. pgx receives the slice positionally; keeping this local avoids changing
// the already-proven matrix helper's call surface.
func readCrashStringsArgs(t *testing.T, query string, args ...any) []string {
	t.Helper()
	rows, err := itPool.Query(context.Background(), query, args...)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var item string
		if err := rows.Scan(&item); err != nil {
			t.Fatal(err)
		}
		out = append(out, item)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func assertVerificationMajorityCrashSemantics(t *testing.T, fixture verificationMajorityCrashFixture, state verificationMajorityCrashState) {
	t.Helper()
	wantOutcome := OutcomePass
	if fixture.CommitterLoses {
		wantOutcome = OutcomeLossNoPayout
	}
	if state.Terminal.PlanOutcome != wantOutcome || state.Terminal.WorkOutcome != string(wantOutcome) ||
		state.Terminal.Task.Status != "complete" || state.Terminal.Task.Outcome != string(wantOutcome) ||
		state.Terminal.VerdictOutcome != string(wantOutcome) || !state.Terminal.DecisionAuthority ||
		!state.Terminal.VerdictAuthority {
		t.Fatalf("majority root authority/outcome is wrong: %+v", state.Terminal)
	}
	if state.RootMoneyRows != 0 || len(state.Terminal.Ledger) != 0 ||
		state.BuyerChargeRows != 1 || state.Terminal.DurationRows != 1 ||
		state.JobTaskCount != 3 || state.JobTasksDone != 1 || state.TerminalWorkRows != 3 {
		t.Fatalf("hedge majority double-settled or drifted counters: %+v", state)
	}
	if len(state.Terminal.ChunkResolutions) != 2 || len(state.ArtifactAuthority) != 2 {
		t.Fatalf("majority did not preserve provisional + majority authority: %+v", state)
	}
	if fixture.CommitterLoses {
		if state.FirstTask != "complete:pass" || state.SecondTask != "complete:pass" ||
			state.FirstCredit != "1:0.009000:held" || state.FirstClawback != "0:0" {
			t.Fatalf("committer-loss winner projections/money are wrong: %+v", state)
		}
		return
	}
	if state.FirstTask != "complete:clawed_back" || state.SecondTask != "complete:pass" ||
		state.FirstCredit != "1:0.009000:clawed_back" || state.FirstClawback != "1:-0.009000" {
		t.Fatalf("real loser clawback/promotion is wrong: %+v", state)
	}
	if !containsCrashString(state.RoleResolutions, "first:clawed_back") ||
		!containsCrashString(state.RoleResolutions, "second:promoted_pass") {
		t.Fatalf("majority correction facts are incomplete: %+v", state.RoleResolutions)
	}
}

func containsCrashString(items []string, want string) bool {
	for _, item := range items {
		if item == want {
			return true
		}
	}
	return false
}

func TestVerificationMajorityClawbackAndHedgeSIGKILLRecoveryConvergesExactlyOnce(t *testing.T) {
	reset(t)
	baselineFixture := seedVerificationMajorityCrashFixture(t, false)
	baseline := runVerificationMajorityCrashToTerminal(t, baselineFixture)
	effectCount := len(baseline.Terminal.PlanEffects)
	if effectCount < 5 {
		t.Fatalf("majority baseline did not contain real multi-party effects: %+v", baseline.Terminal.PlanEffects)
	}
	cases := make([]verificationCrashCase, 0, effectCount+9)
	for occurrence := 1; occurrence <= effectCount; occurrence++ {
		cases = append(cases, verificationCrashCase{
			name: fmt.Sprintf("majority-effect-%d", occurrence), phase: "process",
			boundary: BoundaryApplyAfterEffect, occurrence: occurrence,
		})
	}
	cases = append(cases,
		verificationCrashCase{name: "majority-task", phase: "process", boundary: BoundaryAcceptedAfterTask, occurrence: 1},
		verificationCrashCase{name: "majority-verdict", phase: "process", boundary: BoundaryAcceptedAfterVerdict, occurrence: 1},
		verificationCrashCase{name: "majority-supplier-counter", phase: "process", boundary: BoundaryAcceptedAfterSupplierCounter, occurrence: 1},
		verificationCrashCase{name: "majority-duration", phase: "process", boundary: BoundaryAcceptedAfterDuration, occurrence: 1},
		verificationCrashCase{name: "majority-work-terminal", phase: "process", boundary: BoundaryAcceptedAfterWorkTerminal, occurrence: 1},
		verificationCrashCase{name: "majority-artifact-resolution", phase: "process", boundary: BoundaryAcceptedAfterArtifactResolution, occurrence: 1},
		verificationCrashCase{name: "majority-sibling-fence", phase: "process", boundary: BoundaryAcceptedAfterSiblingCancel, occurrence: 1},
		verificationCrashCase{name: "majority-before-db", phase: "process", boundary: BoundaryAcceptedBeforeDBCommit, occurrence: 1},
		verificationCrashCase{name: "majority-after-db", phase: "process", boundary: BoundaryAcceptedAfterDBCommit, occurrence: 1},
	)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture := seedVerificationMajorityCrashFixture(t, false)
			if _, err := itStore.CommitTask(context.Background(), verificationCrashTaskID, demoWorkerUUID, fixture.Upload.Commit); err != nil {
				t.Fatal(err)
			}
			killVerificationAtBoundary(t, tc)
			recoverVerificationAfterCrash(t, fixture.Upload, "process")
			state := readVerificationMajorityCrashState(t)
			assertVerificationMajorityCrashSemantics(t, fixture, state)
			if !reflect.DeepEqual(state, baseline) {
				t.Fatalf("majority SIGKILL at %s[%d] did not converge:\nwant %+v\ngot  %+v",
					tc.boundary, tc.occurrence, baseline, state)
			}
			t.Logf("SIGKILL boundary=%s occurrence=%d majority_clawback_exact=true hedge_second_charge=0",
				tc.boundary, tc.occurrence)
		})
	}
}

func TestVerificationCommittingMajorityLoserSIGKILLRemainsNonPayable(t *testing.T) {
	reset(t)
	baselineFixture := seedVerificationMajorityCrashFixture(t, true)
	baseline := runVerificationMajorityCrashToTerminal(t, baselineFixture)
	cases := []verificationCrashCase{
		{name: "loss-after-task", phase: "process", boundary: BoundaryAcceptedAfterTask, occurrence: 1},
		{name: "loss-after-work-terminal", phase: "process", boundary: BoundaryAcceptedAfterWorkTerminal, occurrence: 1},
		{name: "loss-after-artifact-resolution", phase: "process", boundary: BoundaryAcceptedAfterArtifactResolution, occurrence: 1},
		{name: "loss-after-db", phase: "process", boundary: BoundaryAcceptedAfterDBCommit, occurrence: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture := seedVerificationMajorityCrashFixture(t, true)
			if _, err := itStore.CommitTask(context.Background(), verificationCrashTaskID, demoWorkerUUID, fixture.Upload.Commit); err != nil {
				t.Fatal(err)
			}
			killVerificationAtBoundary(t, tc)
			recoverVerificationAfterCrash(t, fixture.Upload, "process")
			state := readVerificationMajorityCrashState(t)
			assertVerificationMajorityCrashSemantics(t, fixture, state)
			if !reflect.DeepEqual(state, baseline) {
				t.Fatalf("loss_no_payout SIGKILL at %s did not converge:\nwant %+v\ngot  %+v",
					tc.boundary, baseline, state)
			}
			t.Logf("SIGKILL boundary=%s outcome=loss_no_payout settlement_rows=0", tc.boundary)
		})
	}
}
