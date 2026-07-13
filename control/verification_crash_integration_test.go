//go:build integration

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
)

const (
	verificationCrashChildEnv      = "CX_VERIFICATION_CRASH_CHILD"
	verificationCrashBoundaryEnv   = "CX_VERIFICATION_CRASH_BOUNDARY"
	verificationCrashOccurrenceEnv = "CX_VERIFICATION_CRASH_OCCURRENCE"
	verificationCrashPhaseEnv      = "CX_VERIFICATION_CRASH_PHASE"
)

var (
	verificationCrashJobID  = uuid.MustParse("10000000-0000-0000-0000-000000000001")
	verificationCrashTaskID = uuid.MustParse("10000000-0000-0000-0000-000000000002")
)

type verificationCrashFixture struct {
	Commit TaskCommit
	Reject bool
}

type verificationCrashTaskState struct {
	Status         string `json:"status"`
	Outcome        string `json:"outcome"`
	RetryCount     int16  `json:"retry_count"`
	WorkerBound    bool   `json:"worker_bound"`
	ClaimBound     bool   `json:"claim_bound"`
	ExcludedWorker bool   `json:"excluded_worker"`
	ResultKey      string `json:"result_key"`
	ResultSHA256   string `json:"result_sha256"`
}

type verificationCrashState struct {
	Task                verificationCrashTaskState `json:"task"`
	JobStatus           string                     `json:"job_status"`
	JobTasksDone        int                        `json:"job_tasks_done"`
	WorkStatus          string                     `json:"work_status"`
	WorkOutcome         string                     `json:"work_outcome"`
	SnapshotSHA256      string                     `json:"snapshot_sha256"`
	Artifact            VerificationArtifact       `json:"artifact"`
	SamplingPolicy      string                     `json:"sampling_policy"`
	SamplingProbability string                     `json:"sampling_probability"`
	SamplingSelected    bool                       `json:"sampling_selected"`
	PlanOutcome         VerifyOutcome              `json:"plan_outcome"`
	PlanEffects         []string                   `json:"plan_effects"`
	PlanSettlement      []string                   `json:"plan_settlement"`
	DecisionAuthority   bool                       `json:"decision_authority"`
	VerdictOutcome      string                     `json:"verdict_outcome"`
	VerdictAuthority    bool                       `json:"verdict_authority"`
	VerificationEvents  []string                   `json:"verification_events"`
	VerdictResolutions  []string                   `json:"verdict_resolutions"`
	ChunkResolutions    []string                   `json:"chunk_resolutions"`
	Ledger              []string                   `json:"ledger"`
	DurationRows        int                        `json:"duration_rows"`
	DurationMS          int64                      `json:"duration_ms"`
	SupplierReputation  string                     `json:"supplier_reputation"`
	SupplierStatus      string                     `json:"supplier_status"`
	SupplierCompleted   int64                      `json:"supplier_completed"`
	SupplierQuarantined bool                       `json:"supplier_quarantined"`
	ArtifactBodySHA256  string                     `json:"artifact_body_sha256"`
}

func seedVerificationCrashFixture(t *testing.T, reject bool) verificationCrashFixture {
	t.Helper()
	ctx := context.Background()
	// reset deliberately preserves cross-test telemetry. This matrix reuses fixed
	// UUIDs so its subprocess can find the same rows; remove only the prior fixture's
	// duration facts or each subtest would count an earlier synthetic incarnation of
	// that UUID as a duplicate terminal write.
	if _, err := itPool.Exec(ctx, `DELETE FROM task_durations WHERE task_id=$1`, verificationCrashTaskID); err != nil {
		t.Fatalf("clear prior crash-fixture duration telemetry: %v", err)
	}
	inputRef := "jobs/verification-crash/input.jsonl"
	payload := embedResultJSON(1)
	if reject {
		inputRef = demoHoneypotEmbedRef
		payload = []byte(`{"forged":true}`)
	}
	resultKey := "jobs/x/tasks/0/result.json"
	plan := BuildEconomicPlan(EconomicPlanInput{
		BaseComputeUSD: 1, InitialTaskCount: 1, ExtraTaskReserve: 1,
		SupplierShare: supplierShareRate,
	}, testEconomicSchedule())
	if err := ValidateEconomicPlanSnapshot(plan); err != nil {
		t.Fatalf("crash-fixture economic plan: %v", err)
	}
	job := &jobRow{
		ID: verificationCrashJobID, BuyerID: demoBuyerUUID,
		JobType: "embed", ModelRef: "all-minilm-l6-v2",
		InputRef: inputRef, OutputRef: "jobs/verification-crash/output.jsonl",
		Tier: "batch", VerificationPolicy: []byte(`{"payout_hold_secs":0}`),
		TaskCount: 1, EstimatedUSD: plan.InitialBuyerChargeUSD, SplitSize: 1000,
		EconomicPlan: plan,
	}
	tasks := []taskRow{{
		ID: verificationCrashTaskID, JobID: verificationCrashJobID,
		IsHoneypot: reject, InputRef: inputRef, ResultKey: resultKey,
	}}
	if err := itStore.CreateJobWithTasks(ctx, job, tasks); err != nil {
		t.Fatalf("create crash fixture atomically: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE jobs
		   SET verification_policy='{"payout_hold_secs":0}'::jsonb
		 WHERE id=$1`, verificationCrashJobID); err != nil {
		t.Fatalf("stamp crash-fixture verification policy: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks t
		   SET claimed_by=$2,claimed_at=now(),started_at=now(),worker_id=$2,status='running',
		       execution_worker_id=w.id,execution_supplier_id=w.supplier_id,
		       execution_hw_class=w.hw_class,execution_engine=w.engine,
		       execution_build_hash=w.build_hash
		  FROM workers w WHERE t.id=$1 AND w.id=$2`, verificationCrashTaskID, demoWorkerUUID); err != nil {
		t.Fatalf("claim crash-fixture task: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE suppliers
		   SET reputation=0.90,status='active',completed_tasks=0,quarantined_at=NULL
		 WHERE id=$1`, demoSupplierUUID); err != nil {
		t.Fatalf("reset crash-fixture supplier: %v", err)
	}
	if err := itStorage.PutObject(ctx, resultKey, payload, "application/json"); err != nil {
		t.Fatalf("put crash-fixture staging artifact: %v", err)
	}
	return verificationCrashFixture{
		Reject: reject,
		Commit: TaskCommit{
			TaskID: verificationCrashTaskID, ResultKey: resultKey,
			DurationMS: 321, TokensUsed: 17,
		},
	}
}

func runVerificationCrashFixtureToTerminal(t *testing.T, fixture verificationCrashFixture) verificationCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("commit crash-fixture upload: %v", err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("process crash-fixture baseline: %v", err)
	}
	want := OutcomePass
	if fixture.Reject {
		want = OutcomeFail
	}
	if result.Outcome != want || result.Pending {
		t.Fatalf("crash-fixture baseline outcome = %+v, want %s", result, want)
	}
	state := readVerificationCrashState(t)
	assertVerificationTerminalReplay(t, fixture, state)
	return state
}

func readVerificationCrashState(t *testing.T) verificationCrashState {
	t.Helper()
	ctx := context.Background()
	var state verificationCrashState
	var workerID, claimedBy, excludedWorker *uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT status,COALESCE(verification_outcome,''),retry_count,worker_id,claimed_by,excluded_worker,
		       COALESCE(result_ref,''),COALESCE(result_sha256,'')
		  FROM tasks WHERE id=$1`, verificationCrashTaskID).
		Scan(&state.Task.Status, &state.Task.Outcome, &state.Task.RetryCount,
			&workerID, &claimedBy, &excludedWorker, &state.Task.ResultKey, &state.Task.ResultSHA256); err != nil {
		t.Fatalf("read crash terminal task: %v", err)
	}
	state.Task.WorkerBound = workerID != nil && *workerID == demoWorkerUUID
	state.Task.ClaimBound = claimedBy != nil && *claimedBy == demoWorkerUUID
	state.Task.ExcludedWorker = excludedWorker != nil && *excludedWorker == demoWorkerUUID
	if err := itPool.QueryRow(ctx, `SELECT status,tasks_done FROM jobs WHERE id=$1`, verificationCrashJobID).
		Scan(&state.JobStatus, &state.JobTasksDone); err != nil {
		t.Fatalf("read crash terminal job: %v", err)
	}

	work, err := itStore.VerificationWorkForAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("read crash terminal work: %v", err)
	}
	if work.Artifact == nil || work.SamplingProbability == nil || work.SamplingSelected == nil {
		t.Fatalf("terminal crash work lacks authority: %+v", work)
	}
	state.WorkStatus = work.Status
	state.WorkOutcome = work.TerminalOutcome
	state.SnapshotSHA256 = work.SnapshotSHA256
	state.Artifact = *work.Artifact
	state.SamplingPolicy = work.SamplingPolicy
	state.SamplingProbability = strconv.FormatFloat(*work.SamplingProbability, 'g', 17, 64)
	state.SamplingSelected = *work.SamplingSelected

	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil {
		t.Fatalf("read crash terminal plan: %v", err)
	}
	state.PlanOutcome = plan.Decision.Outcome
	for i, effect := range plan.Decision.Effects {
		state.PlanEffects = append(state.PlanEffects, fmt.Sprintf("%d:%s:%s:%s:%s:%s:%s", i,
			effect.Kind, effect.EventKind, effect.ReputationEvent, effect.TaskID,
			effect.SupplierID, effect.PeerWorkerID))
	}
	for _, entry := range plan.Settlement {
		state.PlanSettlement = append(state.PlanSettlement, fmt.Sprintf("%s:%.6f:%s:%t:%t",
			entry.Kind, entry.AmountUSD, entry.PayoutStatus, entry.BuyerID != nil, entry.SupplierID != nil))
	}
	sort.Strings(state.PlanSettlement)
	state.DecisionAuthority = work.DecisionSHA256 != "" && work.DecisionSHA256 == plan.DecisionSHA256 &&
		plan.WorkID == work.ID && plan.SnapshotSHA256 == work.SnapshotSHA256 && plan.Artifact == *work.Artifact

	var verdictDecisionSHA, verdictResultSHA, verdictArtifactKey, verdictArtifactSHA string
	var verdictWorkID *uuid.UUID
	if err := itPool.QueryRow(ctx, `
		SELECT outcome,COALESCE(decision_sha256,''),COALESCE(result_sha256,''),verification_work_id,
		       COALESCE(artifact_key,''),COALESCE(artifact_sha256,'')
		  FROM task_verdicts WHERE task_id=$1 AND attempt=0`, verificationCrashTaskID).
		Scan(&state.VerdictOutcome, &verdictDecisionSHA, &verdictResultSHA, &verdictWorkID,
			&verdictArtifactKey, &verdictArtifactSHA); err != nil {
		t.Fatalf("read crash terminal verdict: %v", err)
	}
	state.VerdictAuthority = verdictWorkID != nil && *verdictWorkID == work.ID &&
		verdictDecisionSHA == work.DecisionSHA256 && verdictResultSHA == work.Artifact.SHA256 &&
		verdictArtifactKey == work.Artifact.Key && verdictArtifactSHA == work.Artifact.SHA256

	state.VerificationEvents = readCrashStrings(t, `
		SELECT kind||':'||COALESCE(attempt::text,'legacy')
		  FROM verification_events WHERE task_id=$1 ORDER BY kind,attempt`, verificationCrashTaskID)
	state.VerdictResolutions = readCrashStrings(t, `
		SELECT kind||':'||(source_task_id IS NOT NULL)::text
		  FROM task_verdict_resolutions WHERE task_id=$1 ORDER BY kind,effect_id`, verificationCrashTaskID)
	state.ChunkResolutions = readCrashStrings(t, `
		SELECT basis||':'||winner_task_id::text||':'||artifact_sha256||':'||artifact_bytes::text
		  FROM chunk_artifact_resolutions WHERE job_id=$1 ORDER BY basis,effect_id`, verificationCrashJobID)
	state.Ledger = readCrashStrings(t, `
		SELECT kind||':'||amount_usd::text||':'||payout_status
		  FROM ledger_entries WHERE task_id=$1 ORDER BY kind`, verificationCrashTaskID)
	if err := itPool.QueryRow(ctx, `
		SELECT count(*),COALESCE(sum(duration_ms),0)
		  FROM task_durations WHERE task_id=$1`, verificationCrashTaskID).
		Scan(&state.DurationRows, &state.DurationMS); err != nil {
		t.Fatalf("read crash terminal duration: %v", err)
	}
	var reputation float64
	if err := itPool.QueryRow(ctx, `
		SELECT reputation::float8,status,completed_tasks,quarantined_at IS NOT NULL
		  FROM suppliers WHERE id=$1`, demoSupplierUUID).
		Scan(&reputation, &state.SupplierStatus, &state.SupplierCompleted, &state.SupplierQuarantined); err != nil {
		t.Fatalf("read crash terminal supplier: %v", err)
	}
	state.SupplierReputation = fmt.Sprintf("%.6f", reputation)
	body, err := itStorage.ReadSealedVerificationArtifact(ctx, *work.Artifact)
	if err != nil {
		t.Fatalf("read crash terminal sealed artifact: %v", err)
	}
	sum := sha256.Sum256(body)
	state.ArtifactBodySHA256 = hex.EncodeToString(sum[:])
	if state.ArtifactBodySHA256 != work.Artifact.SHA256 || int64(len(body)) != work.Artifact.Bytes {
		t.Fatalf("crash terminal artifact bytes diverge from authority")
	}
	return state
}

func readCrashStrings(t *testing.T, query string, arg any) []string {
	t.Helper()
	rows, err := itPool.Query(context.Background(), query, arg)
	if err != nil {
		t.Fatalf("read canonical crash rows: %v", err)
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

func verificationCrashDigest(t *testing.T, state verificationCrashState) string {
	t.Helper()
	canonical, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

func assertVerificationTerminalReplay(t *testing.T, fixture verificationCrashFixture, before verificationCrashState) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	exact, err := itStore.ExactTerminalVerificationCommit(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit)
	if err != nil || !exact {
		t.Fatalf("terminal upload replay exact=%v err=%v", exact, err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil || result.Pending {
		t.Fatalf("terminal work replay result=%+v err=%v", result, err)
	}
	after := readVerificationCrashState(t)
	if !reflect.DeepEqual(after, before) {
		t.Fatalf("terminal response-loss replay changed canonical state:\nbefore=%+v\nafter =%+v", before, after)
	}
}

type processCrashProbe struct {
	target     RecoveryBoundary
	occurrence int
	seen       int
	signal     *os.File
	once       sync.Once
}

func (p *processCrashProbe) Reach(_ context.Context, boundary RecoveryBoundary) {
	if boundary != p.target {
		return
	}
	p.seen++
	if p.seen != p.occurrence {
		return
	}
	p.once.Do(func() {
		_, _ = fmt.Fprintln(p.signal, boundary)
		select {}
	})
}

// TestVerificationCrashChild is invoked only as a subprocess by the matrix
// below. The probe reports that the exact production boundary was reached and
// then blocks until the parent delivers a real SIGKILL.
func TestVerificationCrashChild(t *testing.T) {
	if os.Getenv(verificationCrashChildEnv) != "1" {
		t.Skip("subprocess helper")
	}
	target := RecoveryBoundary(os.Getenv(verificationCrashBoundaryEnv))
	occurrence, err := strconv.Atoi(os.Getenv(verificationCrashOccurrenceEnv))
	if err != nil || occurrence <= 0 {
		t.Fatalf("invalid crash occurrence: %q", os.Getenv(verificationCrashOccurrenceEnv))
	}
	signal := os.NewFile(uintptr(3), "verification-crash-signal")
	if signal == nil {
		t.Fatal("missing crash signal pipe")
	}
	probe := &processCrashProbe{target: target, occurrence: occurrence, signal: signal}
	fixture := verificationCrashFixture{Commit: TaskCommit{
		TaskID: verificationCrashTaskID, ResultKey: "jobs/x/tasks/0/result.json",
		DurationMS: 321, TokensUsed: 17,
	}}
	ctx := context.Background()
	switch os.Getenv(verificationCrashPhaseEnv) {
	case "commit":
		if _, err := itStore.commitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit, probe); err != nil {
			t.Fatalf("crash-child commit returned before boundary: %v", err)
		}
	case "process":
		processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
			WithRecoveryProbe(probe)
		processor.leaseDuration = 500 * time.Millisecond
		processor.leaseRenewal = 100 * time.Millisecond
		if result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0); err != nil {
			t.Fatalf("crash-child process returned before boundary: %v", err)
		} else {
			t.Fatalf("crash-child process missed boundary %s[%d]: %+v", target, occurrence, result)
		}
	default:
		t.Fatalf("invalid crash phase %q", os.Getenv(verificationCrashPhaseEnv))
	}
	t.Fatalf("crash child missed boundary %s[%d]", target, occurrence)
}

type verificationCrashCase struct {
	name       string
	phase      string
	boundary   RecoveryBoundary
	occurrence int
	reject     bool
}

func killVerificationAtBoundary(t *testing.T, tc verificationCrashCase) {
	t.Helper()
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer readPipe.Close()
	var stdout, stderr bytes.Buffer
	cmd := exec.Command(os.Args[0], "-test.run=^TestVerificationCrashChild$", "-test.count=1")
	cmd.Env = append(os.Environ(),
		verificationCrashChildEnv+"=1",
		verificationCrashBoundaryEnv+"="+string(tc.boundary),
		verificationCrashOccurrenceEnv+"="+strconv.Itoa(tc.occurrence),
		verificationCrashPhaseEnv+"="+tc.phase,
	)
	cmd.ExtraFiles = []*os.File{writePipe}
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Start(); err != nil {
		writePipe.Close()
		t.Fatal(err)
	}
	writePipe.Close()
	type signalResult struct {
		line string
		err  error
	}
	signaled := make(chan signalResult, 1)
	go func() {
		line, err := bufio.NewReader(readPipe).ReadString('\n')
		signaled <- signalResult{line: strings.TrimSpace(line), err: err}
	}()
	select {
	case got := <-signaled:
		if got.err != nil || got.line != string(tc.boundary) {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
			t.Fatalf("crash child did not reach %s[%d]: signal=%q err=%v\nstdout=%s\nstderr=%s",
				tc.boundary, tc.occurrence, got.line, got.err, stdout.String(), stderr.String())
		}
	case <-time.After(15 * time.Second):
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		t.Fatalf("timeout waiting for crash boundary %s[%d]\nstdout=%s\nstderr=%s",
			tc.boundary, tc.occurrence, stdout.String(), stderr.String())
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatalf("SIGKILL crash child at %s[%d]: %v", tc.boundary, tc.occurrence, err)
	}
	waitErr := cmd.Wait()
	exitErr, ok := waitErr.(*exec.ExitError)
	if !ok {
		t.Fatalf("crash child was not killed at %s[%d]: %v", tc.boundary, tc.occurrence, waitErr)
	}
	if status, ok := exitErr.Sys().(syscall.WaitStatus); !ok || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatalf("crash child exit was not SIGKILL at %s[%d]: %v", tc.boundary, tc.occurrence, exitErr.Sys())
	}
}

func recoverVerificationAfterCrash(t *testing.T, fixture verificationCrashFixture, phase string) verificationCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if phase == "commit" {
		if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
			t.Fatalf("retry commit after SIGKILL: %v", err)
		}
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	for {
		result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0)
		if err != nil {
			t.Fatalf("recover verification after SIGKILL: %v", err)
		}
		if !result.Pending {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal("verification lease did not recover after crashed owner")
		case <-time.After(50 * time.Millisecond):
		}
	}
	state := readVerificationCrashState(t)
	assertVerificationTerminalReplay(t, fixture, state)
	return state
}

func TestVerificationSIGKILLRecoveryConvergesToCanonicalTerminalState(t *testing.T) {
	reset(t)
	acceptedBaseline := runVerificationCrashFixtureToTerminal(t, seedVerificationCrashFixture(t, false))
	acceptedDigest := verificationCrashDigest(t, acceptedBaseline)
	reset(t)
	rejectedBaseline := runVerificationCrashFixtureToTerminal(t, seedVerificationCrashFixture(t, true))
	rejectedDigest := verificationCrashDigest(t, rejectedBaseline)

	acceptCases := []verificationCrashCase{
		{name: "commit-task-projected", phase: "commit", boundary: BoundaryCommitAfterTaskProjection, occurrence: 1},
		{name: "commit-parent-fenced", phase: "commit", boundary: BoundaryCommitAfterParentFence, occurrence: 1},
		{name: "commit-job-projected", phase: "commit", boundary: BoundaryCommitAfterJobProjection, occurrence: 1},
		{name: "commit-work-inserted", phase: "commit", boundary: BoundaryCommitAfterWorkInsert, occurrence: 1},
		{name: "commit-before-db", phase: "commit", boundary: BoundaryCommitBeforeDBCommit, occurrence: 1},
		{name: "commit-after-db", phase: "commit", boundary: BoundaryCommitAfterDBCommit, occurrence: 1},
		{name: "work-claimed", phase: "process", boundary: BoundaryVerifyWorkClaimed, occurrence: 1},
		{name: "sampling-pinned", phase: "process", boundary: BoundaryVerifyAfterSamplingPin, occurrence: 1},
		{name: "staging-read", phase: "process", boundary: BoundaryVerifyAfterStagingRead, occurrence: 1},
		{name: "sealed-put", phase: "process", boundary: BoundaryVerifyAfterSealedPut, occurrence: 1},
		{name: "sealed-readback", phase: "process", boundary: BoundaryVerifyAfterSealedReadback, occurrence: 1},
		{name: "artifact-pinned", phase: "process", boundary: BoundaryVerifyAfterArtifactPin, occurrence: 1},
		{name: "decision-persisted", phase: "process", boundary: BoundaryVerifyAfterDecision, occurrence: 1},
		{name: "accept-effect-1", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 1},
		{name: "accept-task", phase: "process", boundary: BoundaryAcceptedAfterTask, occurrence: 1},
		{name: "accept-verdict", phase: "process", boundary: BoundaryAcceptedAfterVerdict, occurrence: 1},
		{name: "accept-job-counter", phase: "process", boundary: BoundaryAcceptedAfterJobCounter, occurrence: 1},
		{name: "accept-supplier-counter", phase: "process", boundary: BoundaryAcceptedAfterSupplierCounter, occurrence: 1},
		{name: "accept-counters", phase: "process", boundary: BoundaryAcceptedAfterCounters, occurrence: 1},
		{name: "accept-duration", phase: "process", boundary: BoundaryAcceptedAfterDuration, occurrence: 1},
		{name: "accept-work-terminal", phase: "process", boundary: BoundaryAcceptedAfterWorkTerminal, occurrence: 1},
		{name: "accept-ledger-1", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 1},
		{name: "accept-ledger-2", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 2},
		{name: "accept-ledger-3", phase: "process", boundary: BoundaryAcceptedAfterLedger, occurrence: 3},
		{name: "accept-artifact-resolution", phase: "process", boundary: BoundaryAcceptedAfterArtifactResolution, occurrence: 1},
		{name: "accept-sibling-cancel", phase: "process", boundary: BoundaryAcceptedAfterSiblingCancel, occurrence: 1},
		{name: "accept-before-db", phase: "process", boundary: BoundaryAcceptedBeforeDBCommit, occurrence: 1},
		{name: "accept-after-db", phase: "process", boundary: BoundaryAcceptedAfterDBCommit, occurrence: 1},
	}
	rejectCases := []verificationCrashCase{
		{name: "reject-effect-1", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 1, reject: true},
		{name: "reject-effect-2", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 2, reject: true},
		{name: "reject-effect-3", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 3, reject: true},
		{name: "reject-effect-4", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 4, reject: true},
		{name: "reject-effect-5", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 5, reject: true},
		{name: "reject-verdict", phase: "process", boundary: BoundaryRejectedAfterVerdict, occurrence: 1, reject: true},
		{name: "reject-requeue", phase: "process", boundary: BoundaryRejectedAfterRequeue, occurrence: 1, reject: true},
		{name: "reject-parent-running", phase: "process", boundary: BoundaryRejectedAfterParentRunning, occurrence: 1, reject: true},
		{name: "reject-work-terminal", phase: "process", boundary: BoundaryRejectedAfterWorkTerminal, occurrence: 1, reject: true},
		{name: "reject-before-db", phase: "process", boundary: BoundaryRejectedBeforeDBCommit, occurrence: 1, reject: true},
		{name: "reject-after-db", phase: "process", boundary: BoundaryRejectedAfterDBCommit, occurrence: 1, reject: true},
	}
	for _, tc := range append(acceptCases, rejectCases...) {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture := seedVerificationCrashFixture(t, tc.reject)
			if tc.phase == "process" {
				if _, err := itStore.CommitTask(context.Background(), verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
					t.Fatalf("prepare process crash: %v", err)
				}
			}
			killVerificationAtBoundary(t, tc)
			state := recoverVerificationAfterCrash(t, fixture, tc.phase)
			wantState, wantDigest := acceptedBaseline, acceptedDigest
			if tc.reject {
				wantState, wantDigest = rejectedBaseline, rejectedDigest
			}
			gotDigest := verificationCrashDigest(t, state)
			if !reflect.DeepEqual(state, wantState) || gotDigest != wantDigest {
				t.Fatalf("SIGKILL at %s[%d] did not converge:\nwant %s %+v\ngot  %s %+v",
					tc.boundary, tc.occurrence, wantDigest, wantState, gotDigest, state)
			}
			t.Logf("SIGKILL boundary=%s occurrence=%d canonical_terminal_sha256=%s",
				tc.boundary, tc.occurrence, gotDigest)
		})
	}
}
