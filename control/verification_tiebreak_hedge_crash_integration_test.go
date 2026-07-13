//go:build integration

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

var (
	verificationTiebreakCrashPeerWorkerID  = uuid.MustParse("10000000-0000-0000-0000-000000000011")
	verificationTiebreakCrashThirdWorkerID = uuid.MustParse("10000000-0000-0000-0000-000000000012")
	verificationTiebreakCrashPeerTaskID    = uuid.MustParse("10000000-0000-0000-0000-000000000013")
	verificationTiebreakCrashHedgeTaskID   = uuid.MustParse("10000000-0000-0000-0000-000000000014")
)

type verificationTiebreakCrashTaskState struct {
	ID             string `json:"id"`
	Status         string `json:"status"`
	RetryCount     int16  `json:"retry_count"`
	InputRef       string `json:"input_ref"`
	ResultKey      string `json:"result_key"`
	ChunkIndex     int    `json:"chunk_index"`
	HedgedFromRoot bool   `json:"hedged_from_root"`
	Redundancy     bool   `json:"redundancy"`
	Honeypot       bool   `json:"honeypot"`
	ClaimedByThird bool   `json:"claimed_by_third"`
	WorkerUnbound  bool   `json:"worker_unbound"`
	Claimed        bool   `json:"claimed"`
	Started        bool   `json:"started"`
	HWClass        string `json:"hw_class"`
	Engine         string `json:"engine"`
	BuildHash      string `json:"build_hash"`
	BuyerCharge    string `json:"buyer_charge"`
	SupplierPayout string `json:"supplier_payout"`
}

type verificationTiebreakHedgeCrashState struct {
	Terminal         verificationCrashState             `json:"terminal"`
	Effects          []VerificationEffect               `json:"effects"`
	ReserveTasks     int                                `json:"reserve_tasks"`
	ReserveConsumed  int                                `json:"reserve_consumed"`
	JobTaskCount     int                                `json:"job_task_count"`
	JobTasksDone     int                                `json:"job_tasks_done"`
	JobStatus        string                             `json:"job_status"`
	TiebreakCount    int                                `json:"tiebreak_count"`
	Tiebreak         verificationTiebreakCrashTaskState `json:"tiebreak"`
	Hedge            verificationTiebreakCrashTaskState `json:"hedge"`
	PeerStatus       string                             `json:"peer_status"`
	PeerOutcome      string                             `json:"peer_outcome"`
	PeerWorkStatus   string                             `json:"peer_work_status"`
	PeerArtifact     VerificationArtifact               `json:"peer_artifact"`
	PeerBodySHA256   string                             `json:"peer_body_sha256"`
	CurrentBuyer     string                             `json:"current_buyer_charge"`
	CurrentSupplier  string                             `json:"current_supplier_payout"`
	CanonicalLedgers []string                           `json:"canonical_ledgers"`
}

func registerVerificationTiebreakCrashWorker(t *testing.T, workerID, supplierID uuid.UUID) {
	t.Helper()
	capability := demoProductionCapability()
	capability.WorkerID = workerID
	capability.SupplierID = supplierID
	capability.AgentVersion = "verification-tiebreak-crash"
	if err := itStore.UpsertWorker(context.Background(), capability); err != nil {
		t.Fatalf("register tiebreak crash worker %s: %v", workerID, err)
	}
}

func seedVerificationTiebreakHedgeCrashFixture(t *testing.T) verificationCrashFixture {
	t.Helper()
	ctx := context.Background()
	if _, err := itPool.Exec(ctx, `DELETE FROM task_durations WHERE task_id=$1`, verificationCrashTaskID); err != nil {
		t.Fatalf("clear prior tiebreak crash duration: %v", err)
	}
	ensureExtraDemoSuppliers(t, ctx)
	registerVerificationTiebreakCrashWorker(t, verificationTiebreakCrashPeerWorkerID, demoSupplier2UUID)
	registerVerificationTiebreakCrashWorker(t, verificationTiebreakCrashThirdWorkerID, demoSupplier3UUID)

	inputRef := "jobs/authority/chunk-0.input"
	resultKey := "jobs/x/tasks/0/result.json"
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD: 2, InitialTaskCount: 2, ExtraTaskReserve: 2,
		SupplierShare: supplierShareRate,
	}, testEconomicSchedule())
	if err := ValidateEconomicPlanSnapshot(plan); err != nil {
		t.Fatalf("tiebreak crash economic plan: %v", err)
	}
	job := &jobRow{
		ID: verificationCrashJobID, BuyerID: demoBuyerUUID,
		JobType: "embed", ModelRef: "all-minilm-l6-v2",
		InputRef: inputRef, OutputRef: "jobs/verification-crash/output.jsonl",
		Tier: "batch", VerificationPolicy: []byte(`{"payout_hold_secs":0}`),
		TaskCount: 2, EstimatedUSD: plan.InitialBuyerChargeUSD, SplitSize: 1000,
		EconomicPlan: plan,
	}
	tasks := []taskRow{
		{
			ID: verificationCrashTaskID, JobID: verificationCrashJobID,
			InputRef: inputRef, ResultKey: resultKey,
		},
		{
			ID: verificationTiebreakCrashPeerTaskID, JobID: verificationCrashJobID,
			IsRedundancy: true, InputRef: inputRef,
			ResultKey: fmt.Sprintf("jobs/%s/authority/%s/staging.result",
				verificationCrashJobID, verificationTiebreakCrashPeerTaskID),
		},
	}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("create tiebreak crash fixture: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),started_at=now(),worker_id=$2,status='running',
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, verificationCrashTaskID, demoWorkerUUID); err != nil {
		t.Fatalf("claim tiebreak crash committing task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE suppliers
		   SET reputation=0.90,status='active',completed_tasks=0,quarantined_at=NULL
		 WHERE id=$1`, demoSupplierUUID); err != nil {
		t.Fatalf("reset tiebreak crash supplier: %v", err)
	}
	if err := itStorage.PutObject(ctx, resultKey, embedResultJSON(1), "application/json"); err != nil {
		t.Fatalf("put tiebreak crash committing artifact: %v", err)
	}

	peer := seedTerminalArtifactAuthority(t, verificationCrashJobID, verificationTiebreakCrashPeerTaskID,
		verificationTiebreakCrashPeerWorkerID, demoSupplier2UUID, true, nil,
		alternateEmbedResultJSON(), -1)
	if peer.TaskID != verificationTiebreakCrashPeerTaskID {
		t.Fatalf("seeded tiebreak peer task=%s", peer.TaskID)
	}
	if _, err := itPool.Exec(ctx, `
		INSERT INTO tasks
		 (id,job_id,status,is_honeypot,is_redundancy,retry_count,input_ref,result_key,
		  chunk_index,hedged_from,visible_at,economic_buyer_charge_usd,economic_supplier_payout_usd)
		VALUES ($1,$2,'queued',false,false,0,$3,$4,0,$5,now(),$6,$7)`,
		verificationTiebreakCrashHedgeTaskID, verificationCrashJobID, inputRef,
		"jobs/verification-crash/hedge/result.json", verificationCrashTaskID,
		plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD); err != nil {
		t.Fatalf("insert queued crash hedge: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE jobs SET status='running',task_count=3,tasks_done=1 WHERE id=$1`,
		verificationCrashJobID); err != nil {
		t.Fatalf("stamp tiebreak crash parent state: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE job_economic_reserves SET consumed_tasks=1 WHERE job_id=$1`,
		verificationCrashJobID); err != nil {
		t.Fatalf("consume crash hedge reserve: %v", err)
	}
	var hedgeStatus string
	var hedgeRoot uuid.UUID
	var reserved, consumed int
	if err := itPool.QueryRow(ctx, `SELECT status,hedged_from FROM tasks WHERE id=$1`,
		verificationTiebreakCrashHedgeTaskID).Scan(&hedgeStatus, &hedgeRoot); err != nil {
		t.Fatalf("read seeded crash hedge: %v", err)
	}
	if err := itPool.QueryRow(ctx, `SELECT reserved_tasks,consumed_tasks FROM job_economic_reserves WHERE job_id=$1`,
		verificationCrashJobID).Scan(&reserved, &consumed); err != nil {
		t.Fatalf("read seeded crash reserve: %v", err)
	}
	if hedgeStatus != "queued" || hedgeRoot != verificationCrashTaskID || reserved != 2 || consumed != 1 {
		t.Fatalf("invalid pre-crash hedge/reserve: hedge=%s root=%s reserve=%d/%d",
			hedgeStatus, hedgeRoot, consumed, reserved)
	}
	return verificationCrashFixture{Commit: TaskCommit{
		TaskID: verificationCrashTaskID, ResultKey: resultKey,
		DurationMS: 321, TokensUsed: 17,
	}}
}

func readVerificationTiebreakHedgeCrashTask(t *testing.T, taskID uuid.UUID, thirdWorker bool) verificationTiebreakCrashTaskState {
	t.Helper()
	var state verificationTiebreakCrashTaskState
	var hedgedFrom, claimedBy, workerID *uuid.UUID
	if err := itPool.QueryRow(context.Background(), `
		SELECT id::text,status,COALESCE(retry_count,0),COALESCE(input_ref,''),COALESCE(result_key,''),
		       COALESCE(chunk_index,0),hedged_from,is_redundancy,is_honeypot,claimed_by,worker_id,
		       claimed_at IS NOT NULL,started_at IS NOT NULL,
		       COALESCE(verification_hw_class,''),COALESCE(verification_engine,''),
		       COALESCE(verification_build_hash,''),
		       COALESCE(economic_buyer_charge_usd::text,''),
		       COALESCE(economic_supplier_payout_usd::text,'')
		  FROM tasks WHERE id=$1`, taskID).
		Scan(&state.ID, &state.Status, &state.RetryCount, &state.InputRef, &state.ResultKey,
			&state.ChunkIndex, &hedgedFrom, &state.Redundancy, &state.Honeypot, &claimedBy, &workerID,
			&state.Claimed, &state.Started, &state.HWClass, &state.Engine, &state.BuildHash,
			&state.BuyerCharge, &state.SupplierPayout); err != nil {
		t.Fatalf("read tiebreak crash task %s: %v", taskID, err)
	}
	state.HedgedFromRoot = hedgedFrom != nil && *hedgedFrom == verificationCrashTaskID
	state.ClaimedByThird = thirdWorker && claimedBy != nil && *claimedBy == verificationTiebreakCrashThirdWorkerID
	state.WorkerUnbound = workerID == nil
	return state
}

func readVerificationTiebreakHedgeCrashState(t *testing.T) verificationTiebreakHedgeCrashState {
	t.Helper()
	ctx := context.Background()
	state := verificationTiebreakHedgeCrashState{Terminal: readVerificationCrashState(t)}
	if err := itPool.QueryRow(ctx, `
		SELECT r.reserved_tasks,r.consumed_tasks,j.task_count,j.tasks_done,j.status
		  FROM job_economic_reserves r JOIN jobs j ON j.id=r.job_id WHERE r.job_id=$1`,
		verificationCrashJobID).
		Scan(&state.ReserveTasks, &state.ReserveConsumed, &state.JobTaskCount,
			&state.JobTasksDone, &state.JobStatus); err != nil {
		t.Fatalf("read tiebreak crash job/reserve: %v", err)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM tasks
		 WHERE job_id=$1 AND is_redundancy=true AND hedged_from=$2`,
		verificationCrashJobID, verificationCrashTaskID).Scan(&state.TiebreakCount); err != nil {
		t.Fatalf("count crash tiebreaks: %v", err)
	}
	if state.TiebreakCount != 1 {
		t.Fatalf("read crash tiebreak count=%d, want 1", state.TiebreakCount)
	}
	var tiebreakID uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT id FROM tasks
		 WHERE job_id=$1 AND is_redundancy=true AND hedged_from=$2`,
		verificationCrashJobID, verificationCrashTaskID).Scan(&tiebreakID); err != nil {
		t.Fatalf("read crash tiebreak id: %v", err)
	}
	state.Tiebreak = readVerificationTiebreakHedgeCrashTask(t, tiebreakID, true)
	state.Hedge = readVerificationTiebreakHedgeCrashTask(t, verificationTiebreakCrashHedgeTaskID, false)

	work, err := itStore.VerificationWorkForAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("read tiebreak crash work: %v", err)
	}
	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil {
		t.Fatalf("read tiebreak crash plan: %v", err)
	}
	state.Effects = append([]VerificationEffect(nil), plan.Decision.Effects...)
	if err := itPool.QueryRow(ctx, `
		SELECT economic_buyer_charge_usd::text,economic_supplier_payout_usd::text
		  FROM tasks WHERE id=$1`, verificationCrashTaskID).
		Scan(&state.CurrentBuyer, &state.CurrentSupplier); err != nil {
		t.Fatalf("read current frozen economics: %v", err)
	}
	state.CanonicalLedgers = append([]string(nil), state.Terminal.Ledger...)
	sort.Strings(state.CanonicalLedgers)

	peerWork, err := itStore.VerificationWorkForAttempt(ctx, verificationTiebreakCrashPeerTaskID, 0)
	if err != nil {
		t.Fatalf("read sealed crash peer work: %v", err)
	}
	if peerWork.Artifact == nil {
		t.Fatal("sealed crash peer lacks artifact authority")
	}
	state.PeerWorkStatus = peerWork.Status
	state.PeerArtifact = *peerWork.Artifact
	if err := itPool.QueryRow(ctx, `
		SELECT status,COALESCE(verification_outcome,'') FROM tasks WHERE id=$1`,
		verificationTiebreakCrashPeerTaskID).Scan(&state.PeerStatus, &state.PeerOutcome); err != nil {
		t.Fatalf("read sealed crash peer projection: %v", err)
	}
	peerBody, err := itStorage.ReadSealedVerificationArtifact(ctx, state.PeerArtifact)
	if err != nil {
		t.Fatalf("read sealed crash peer bytes: %v", err)
	}
	peerSum := sha256.Sum256(peerBody)
	state.PeerBodySHA256 = hex.EncodeToString(peerSum[:])
	return state
}

func verificationTiebreakHedgeCrashDigest(t *testing.T, state verificationTiebreakHedgeCrashState) string {
	t.Helper()
	canonical, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

func assertVerificationTiebreakHedgeCrashSemantics(t *testing.T, state verificationTiebreakHedgeCrashState) {
	t.Helper()
	terminal := state.Terminal
	if terminal.Task.Status != "complete" || terminal.Task.Outcome != string(OutcomePassWithPenalty) ||
		terminal.Task.RetryCount != 0 || !terminal.Task.WorkerBound || !terminal.Task.ClaimBound || terminal.Task.ExcludedWorker {
		t.Fatalf("mismatch committer projection is wrong: %+v", terminal.Task)
	}
	if terminal.WorkStatus != VerificationWorkTerminal || terminal.WorkOutcome != string(OutcomePassWithPenalty) ||
		terminal.PlanOutcome != OutcomePassWithPenalty || !terminal.DecisionAuthority ||
		terminal.VerdictOutcome != string(OutcomePassWithPenalty) || !terminal.VerdictAuthority {
		t.Fatalf("mismatch work/decision/verdict authority is wrong: %+v", terminal)
	}
	if len(terminal.PlanEffects) != 2 || len(state.Effects) != 2 || len(terminal.PlanSettlement) != 3 ||
		len(terminal.Ledger) != 3 || terminal.DurationRows != 1 || terminal.DurationMS != 321 ||
		len(terminal.VerificationEvents) != 1 || terminal.VerificationEvents[0] != "redundancy_mismatch:0" ||
		len(terminal.ChunkResolutions) != 1 || !strings.HasPrefix(terminal.ChunkResolutions[0], "provisional:") {
		t.Fatalf("mismatch durable effects/settlement are wrong: %+v", terminal)
	}
	if terminal.SupplierStatus != "active" || terminal.SupplierQuarantined ||
		terminal.SupplierCompleted != 1 || terminal.SupplierReputation != "0.900000" {
		t.Fatalf("mismatch committer supplier projection is wrong: %+v", terminal)
	}
	first, second := state.Effects[0], state.Effects[1]
	if first.Kind != VerificationEffectRecordEvent || first.EventKind != "redundancy_mismatch" ||
		first.JobID != verificationCrashJobID || first.TaskID != verificationCrashTaskID || first.SupplierID != demoSupplierUUID {
		t.Fatalf("first mismatch effect is wrong: %+v", first)
	}
	if second.Kind != VerificationEffectInsertTiebreak || second.ID == uuid.Nil || second.TaskID != second.ID ||
		second.JobID != verificationCrashJobID || second.PrimaryTaskID != verificationCrashTaskID ||
		second.PeerWorkerID != verificationTiebreakCrashThirdWorkerID || second.InputRef != "jobs/authority/chunk-0.input" ||
		second.ChunkIndex != 0 || state.Tiebreak.ID != second.TaskID.String() {
		t.Fatalf("deterministic tiebreak effect is wrong: effect=%+v task=%+v", second, state.Tiebreak)
	}
	if state.ReserveTasks != 2 || state.ReserveConsumed != 2 || state.JobTaskCount != 4 ||
		state.JobTasksDone != 2 || state.JobStatus != "running" || state.TiebreakCount != 1 {
		t.Fatalf("tiebreak reserve/job state is wrong: %+v", state)
	}
	if state.Tiebreak.Status != "queued" || state.Tiebreak.RetryCount != 0 || !state.Tiebreak.Redundancy ||
		state.Tiebreak.Honeypot || !state.Tiebreak.HedgedFromRoot || !state.Tiebreak.ClaimedByThird ||
		!state.Tiebreak.Claimed || state.Tiebreak.Started || !state.Tiebreak.WorkerUnbound ||
		state.Tiebreak.InputRef != second.InputRef || state.Tiebreak.ChunkIndex != 0 ||
		state.Tiebreak.ResultKey != fmt.Sprintf("jobs/%s/tiebreak/%s/result.json", verificationCrashJobID, second.TaskID) {
		t.Fatalf("inserted deterministic tiebreak row is wrong: %+v", state.Tiebreak)
	}
	work, err := itStore.VerificationWorkForAttempt(context.Background(), verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("reload committer work for frozen class: %v", err)
	}
	info, _, err := commitInfoFromVerificationWork(work)
	if err != nil {
		t.Fatalf("decode committer frozen class: %v", err)
	}
	if state.Tiebreak.HWClass != info.HWClass || state.Tiebreak.Engine != info.engine ||
		state.Tiebreak.BuildHash != info.buildHash || state.Tiebreak.BuyerCharge != state.CurrentBuyer ||
		state.Tiebreak.SupplierPayout != state.CurrentSupplier || state.Tiebreak.BuyerCharge == "" ||
		state.Tiebreak.SupplierPayout == "" {
		t.Fatalf("tiebreak class/economics drifted: task=%+v current=%s/%s class=%s/%s/%s",
			state.Tiebreak, state.CurrentBuyer, state.CurrentSupplier, info.HWClass, info.engine, info.buildHash)
	}
	if state.Hedge.ID != verificationTiebreakCrashHedgeTaskID.String() || state.Hedge.Status != "failed" ||
		state.Hedge.Redundancy || state.Hedge.Honeypot || !state.Hedge.HedgedFromRoot || state.Hedge.Claimed ||
		state.Hedge.BuyerCharge != state.CurrentBuyer || state.Hedge.SupplierPayout != state.CurrentSupplier {
		t.Fatalf("real queued hedge was not cancelled exactly once: %+v", state.Hedge)
	}
	if state.PeerStatus != "complete" || state.PeerOutcome != string(OutcomePass) ||
		state.PeerWorkStatus != VerificationWorkTerminal || state.PeerArtifact.SHA256 == "" ||
		state.PeerBodySHA256 != state.PeerArtifact.SHA256 {
		t.Fatalf("completed sealed disagreement peer is wrong: %+v", state)
	}
	currentBody, err := itStorage.ReadSealedVerificationArtifact(context.Background(), terminal.Artifact)
	if err != nil {
		t.Fatalf("read current sealed mismatch bytes: %v", err)
	}
	peerBody, err := itStorage.ReadSealedVerificationArtifact(context.Background(), state.PeerArtifact)
	if err != nil {
		t.Fatalf("read peer sealed mismatch bytes: %v", err)
	}
	if resultsAgree("embed", currentBody, peerBody) {
		t.Fatal("seeded sealed peer does not actually disagree with committer")
	}
	kinds := make([]string, 0, len(state.CanonicalLedgers))
	for _, row := range state.CanonicalLedgers {
		kind, _, _ := strings.Cut(row, ":")
		kinds = append(kinds, kind)
	}
	sort.Strings(kinds)
	if !reflect.DeepEqual(kinds, []string{KindBuyerCharge, KindPlatformTake, KindSupplierCredit}) {
		t.Fatalf("mismatch settlement ledger kinds=%v rows=%v", kinds, state.CanonicalLedgers)
	}
}

func runVerificationTiebreakHedgeCrashFixtureToTerminal(t *testing.T, fixture verificationCrashFixture) verificationTiebreakHedgeCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("commit tiebreak hedge crash fixture: %v", err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("process tiebreak hedge crash baseline: %v", err)
	}
	if result.Pending || result.Outcome != OutcomePassWithPenalty || !result.Applied.Applied ||
		result.Applied.Rejected || result.Applied.TiebreaksInserted != 1 {
		t.Fatalf("tiebreak hedge crash baseline result=%+v", result)
	}
	before := readVerificationTiebreakHedgeCrashState(t)
	assertVerificationTiebreakHedgeCrashSemantics(t, before)
	assertVerificationTerminalReplay(t, fixture, before.Terminal)
	after := readVerificationTiebreakHedgeCrashState(t)
	if !reflect.DeepEqual(after, before) {
		t.Fatalf("tiebreak/hedge terminal replay changed canonical state:\nbefore=%+v\nafter =%+v", before, after)
	}
	return before
}

func TestVerificationTiebreakInsertionAndHedgeCancellationSIGKILLConvergeExactlyOnce(t *testing.T) {
	reset(t)
	baselineFixture := seedVerificationTiebreakHedgeCrashFixture(t)
	baseline := runVerificationTiebreakHedgeCrashFixtureToTerminal(t, baselineFixture)
	baselineDigest := verificationTiebreakHedgeCrashDigest(t, baseline)

	cases := []verificationCrashCase{
		{name: "mismatch-decision", phase: "process", boundary: BoundaryVerifyAfterDecision, occurrence: 1},
		{name: "mismatch-event-effect", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 1},
		{name: "tiebreak-insert-effect", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 2},
		{name: "accepted-task", phase: "process", boundary: BoundaryAcceptedAfterTask, occurrence: 1},
		{name: "accepted-verdict", phase: "process", boundary: BoundaryAcceptedAfterVerdict, occurrence: 1},
		{name: "accepted-job-counter", phase: "process", boundary: BoundaryAcceptedAfterJobCounter, occurrence: 1},
		{name: "accepted-supplier-counter", phase: "process", boundary: BoundaryAcceptedAfterSupplierCounter, occurrence: 1},
		{name: "accepted-duration", phase: "process", boundary: BoundaryAcceptedAfterDuration, occurrence: 1},
		{name: "accepted-work-terminal", phase: "process", boundary: BoundaryAcceptedAfterWorkTerminal, occurrence: 1},
		{name: "accepted-ledger-1", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 1},
		{name: "accepted-ledger-2", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 2},
		{name: "accepted-ledger-3", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 3},
		{name: "accepted-artifact-resolution", phase: "process", boundary: BoundaryAcceptedAfterArtifactResolution, occurrence: 1},
		{name: "real-hedge-sibling-cancel", phase: "process", boundary: BoundaryAcceptedAfterSiblingCancel, occurrence: 1},
		{name: "accepted-before-db", phase: "process", boundary: BoundaryAcceptedBeforeDBCommit, occurrence: 1},
		{name: "accepted-after-db", phase: "process", boundary: BoundaryAcceptedAfterDBCommit, occurrence: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture := seedVerificationTiebreakHedgeCrashFixture(t)
			if _, err := itStore.CommitTask(context.Background(), verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
				t.Fatalf("prepare tiebreak hedge process crash: %v", err)
			}
			killVerificationAtBoundary(t, tc)
			recoverVerificationAfterCrash(t, fixture, tc.phase)
			state := readVerificationTiebreakHedgeCrashState(t)
			assertVerificationTiebreakHedgeCrashSemantics(t, state)
			gotDigest := verificationTiebreakHedgeCrashDigest(t, state)
			if !reflect.DeepEqual(state, baseline) || gotDigest != baselineDigest {
				t.Fatalf("tiebreak/hedge SIGKILL at %s[%d] did not converge:\nwant %s %+v\ngot  %s %+v",
					tc.boundary, tc.occurrence, baselineDigest, baseline, gotDigest, state)
			}
			t.Logf("tiebreak/hedge SIGKILL boundary=%s occurrence=%d canonical_terminal_sha256=%s",
				tc.boundary, tc.occurrence, gotDigest)
		})
	}
}
