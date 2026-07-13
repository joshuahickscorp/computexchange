//go:build integration

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

type verificationOversizeEvidenceState struct {
	Version              int    `json:"version"`
	Reason               string `json:"reason"`
	StagingKeySHA256     string `json:"staging_key_sha256"`
	ObservedBytesAtLeast int64  `json:"observed_bytes_at_least"`
	MaxBytes             int64  `json:"max_bytes"`
}

type verificationOversizeCrashState struct {
	Terminal verificationCrashState            `json:"terminal"`
	Failure  VerificationFailure               `json:"failure"`
	Evidence verificationOversizeEvidenceState `json:"evidence"`
}

func seedVerificationOversizeCrashFixture(t *testing.T) (verificationCrashFixture, int64) {
	t.Helper()
	ctx := context.Background()
	fixture := seedVerificationCrashFixture(t, false)
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET split_size=1 WHERE id=$1`, verificationCrashJobID); err != nil {
		t.Fatalf("set oversize crash fixture split: %v", err)
	}
	maxBytes := verificationArtifactMaxBytes("embed", 1, 0)
	oversized := bytes.Repeat([]byte("x"), int(maxBytes+1))
	if err := itStorage.PutObject(ctx, fixture.Commit.ResultKey, oversized, "application/octet-stream"); err != nil {
		t.Fatalf("put oversize crash staging artifact: %v", err)
	}
	return fixture, maxBytes
}

func readVerificationOversizeCrashState(t *testing.T) verificationOversizeCrashState {
	t.Helper()
	state := verificationOversizeCrashState{Terminal: readVerificationCrashState(t)}
	work, err := itStore.VerificationWorkForAttempt(context.Background(), verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("read oversize crash work: %v", err)
	}
	if work.Artifact == nil {
		t.Fatal("oversize crash work has no pinned evidence")
	}
	plan, err := itStore.VerificationWorkPlan(context.Background(), work.ID)
	if err != nil {
		t.Fatalf("read oversize crash plan: %v", err)
	}
	if plan.Decision.Failure == nil {
		t.Fatal("oversize crash plan has no typed failure")
	}
	state.Failure = *plan.Decision.Failure
	body, err := itStorage.ReadSealedVerificationArtifact(context.Background(), *work.Artifact)
	if err != nil {
		t.Fatalf("read oversize crash evidence: %v", err)
	}
	if err := json.Unmarshal(body, &state.Evidence); err != nil {
		t.Fatalf("decode oversize crash evidence: %v", err)
	}
	return state
}

func verificationOversizeCrashDigest(t *testing.T, state verificationOversizeCrashState) string {
	t.Helper()
	canonical, err := json.Marshal(state)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

func assertVerificationOversizeCrashSemantics(t *testing.T, fixture verificationCrashFixture, maxBytes int64, state verificationOversizeCrashState) {
	t.Helper()
	terminal := state.Terminal
	if terminal.Task.Status != "retrying" || terminal.Task.Outcome != "" || terminal.Task.RetryCount != 1 ||
		terminal.Task.WorkerBound || terminal.Task.ClaimBound || !terminal.Task.ExcludedWorker {
		t.Fatalf("oversize task did not become exactly one fenced retry: %+v", terminal.Task)
	}
	if terminal.JobStatus != "running" || terminal.JobTasksDone != 0 ||
		terminal.WorkStatus != VerificationWorkTerminal || terminal.WorkOutcome != string(OutcomeFail) {
		t.Fatalf("oversize job/work terminal state is wrong: %+v", terminal)
	}
	if terminal.PlanOutcome != OutcomeFail || len(terminal.PlanEffects) != 4 || len(terminal.PlanSettlement) != 0 ||
		!terminal.DecisionAuthority || terminal.VerdictOutcome != string(OutcomeFail) || !terminal.VerdictAuthority {
		t.Fatalf("oversize decision/verdict authority is wrong: %+v", terminal)
	}
	if len(terminal.VerificationEvents) != 1 || terminal.VerificationEvents[0] != "artifact_oversize:0" ||
		len(terminal.VerdictResolutions) != 0 || len(terminal.ChunkResolutions) != 0 || len(terminal.Ledger) != 0 ||
		terminal.DurationRows != 0 || terminal.DurationMS != 0 {
		t.Fatalf("oversize rejection duplicated or leaked durable effects/money: %+v", terminal)
	}
	if terminal.SupplierStatus != "suspended" || !terminal.SupplierQuarantined || terminal.SupplierCompleted != 0 ||
		terminal.SupplierReputation != "0.700000" {
		t.Fatalf("oversize supplier consequence is wrong: %+v", terminal)
	}
	if !isOversizedVerificationEvidenceKey(terminal.Artifact.Key) || terminal.Artifact.Bytes <= 0 ||
		terminal.Artifact.Bytes >= 1<<10 || terminal.Artifact.Bytes > maxBytes ||
		terminal.ArtifactBodySHA256 != terminal.Artifact.SHA256 {
		t.Fatalf("oversize server evidence authority is wrong: %+v", terminal.Artifact)
	}
	if state.Failure.Kind != "artifact_oversize" || state.Failure.Code != "too_large" || state.Failure.JobType != "embed" {
		t.Fatalf("oversize typed failure is wrong: %+v", state.Failure)
	}
	stagingKeySum := sha256.Sum256([]byte(fixture.Commit.ResultKey))
	if state.Evidence.Version != 1 || state.Evidence.Reason != "verification_artifact_too_large" ||
		state.Evidence.StagingKeySHA256 != hex.EncodeToString(stagingKeySum[:]) ||
		state.Evidence.ObservedBytesAtLeast != maxBytes+1 || state.Evidence.MaxBytes != maxBytes {
		t.Fatalf("oversize evidence payload is wrong: %+v", state.Evidence)
	}

	work, err := itStore.VerificationWorkForAttempt(context.Background(), verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("reload oversize work for effects: %v", err)
	}
	plan, err := itStore.VerificationWorkPlan(context.Background(), work.ID)
	if err != nil {
		t.Fatalf("reload oversize plan for effects: %v", err)
	}
	effects := plan.Decision.Effects
	if len(effects) != 4 ||
		effects[0].Kind != VerificationEffectDockReputation || effects[0].ReputationEvent != EventArtifactOversize ||
		effects[1].Kind != VerificationEffectRecordEvent || effects[1].EventKind != "artifact_oversize" ||
		effects[2].Kind != VerificationEffectQuarantine || effects[2].SupplierID != demoSupplierUUID ||
		effects[3].Kind != VerificationEffectRequeue || effects[3].TaskID != verificationCrashTaskID {
		t.Fatalf("oversize effect sequence is wrong: %+v", effects)
	}
}

func runVerificationOversizeCrashFixtureToTerminal(t *testing.T, fixture verificationCrashFixture, maxBytes int64) verificationOversizeCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := itStore.CommitTask(ctx, verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("commit oversize crash fixture: %v", err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	result, err := processor.ProcessAttempt(ctx, verificationCrashTaskID, 0)
	if err != nil {
		t.Fatalf("process oversize crash baseline: %v", err)
	}
	if result.Pending || result.Outcome != OutcomeFail || !result.Applied.Applied || !result.Applied.Rejected {
		t.Fatalf("oversize crash baseline result = %+v", result)
	}
	before := readVerificationOversizeCrashState(t)
	assertVerificationOversizeCrashSemantics(t, fixture, maxBytes, before)
	assertVerificationTerminalReplay(t, fixture, before.Terminal)
	after := readVerificationOversizeCrashState(t)
	if !reflect.DeepEqual(after, before) {
		t.Fatalf("oversize terminal replay changed canonical state:\nbefore=%+v\nafter =%+v", before, after)
	}
	return before
}

func TestVerificationOversizeEvidenceSIGKILLRecoveryConvergesExactlyOnce(t *testing.T) {
	reset(t)
	baselineFixture, maxBytes := seedVerificationOversizeCrashFixture(t)
	baseline := runVerificationOversizeCrashFixtureToTerminal(t, baselineFixture, maxBytes)
	baselineDigest := verificationOversizeCrashDigest(t, baseline)

	cases := []verificationCrashCase{
		{name: "oversize-evidence-put", phase: "process", boundary: BoundaryVerifyAfterSealedPut, occurrence: 1},
		{name: "oversize-evidence-readback", phase: "process", boundary: BoundaryVerifyAfterSealedReadback, occurrence: 1},
		{name: "oversize-artifact-pinned", phase: "process", boundary: BoundaryVerifyAfterArtifactPin, occurrence: 1},
		{name: "oversize-decision-persisted", phase: "process", boundary: BoundaryVerifyAfterDecision, occurrence: 1},
		{name: "oversize-effect-1", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 1},
		{name: "oversize-effect-2", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 2},
		{name: "oversize-effect-3", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 3},
		{name: "oversize-effect-4", phase: "process", boundary: BoundaryApplyAfterEffect, occurrence: 4},
		{name: "oversize-verdict", phase: "process", boundary: BoundaryRejectedAfterVerdict, occurrence: 1},
		{name: "oversize-requeue", phase: "process", boundary: BoundaryRejectedAfterRequeue, occurrence: 1},
		{name: "oversize-parent-running", phase: "process", boundary: BoundaryRejectedAfterParentRunning, occurrence: 1},
		{name: "oversize-work-terminal", phase: "process", boundary: BoundaryRejectedAfterWorkTerminal, occurrence: 1},
		{name: "oversize-before-db", phase: "process", boundary: BoundaryRejectedBeforeDBCommit, occurrence: 1},
		{name: "oversize-after-db", phase: "process", boundary: BoundaryRejectedAfterDBCommit, occurrence: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture, caseMaxBytes := seedVerificationOversizeCrashFixture(t)
			if caseMaxBytes != maxBytes {
				t.Fatalf("oversize cap changed within matrix: baseline=%d case=%d", maxBytes, caseMaxBytes)
			}
			if _, err := itStore.CommitTask(context.Background(), verificationCrashTaskID, demoWorkerUUID, fixture.Commit); err != nil {
				t.Fatalf("prepare oversize process crash: %v", err)
			}
			killVerificationAtBoundary(t, tc)
			recoverVerificationAfterCrash(t, fixture, tc.phase)
			state := readVerificationOversizeCrashState(t)
			assertVerificationOversizeCrashSemantics(t, fixture, maxBytes, state)
			gotDigest := verificationOversizeCrashDigest(t, state)
			if !reflect.DeepEqual(state, baseline) || gotDigest != baselineDigest {
				t.Fatalf("oversize SIGKILL at %s[%d] did not converge:\nwant %s %+v\ngot  %s %+v",
					tc.boundary, tc.occurrence, baselineDigest, baseline, gotDigest, state)
			}
			t.Logf("oversize SIGKILL boundary=%s occurrence=%d canonical_terminal_sha256=%s",
				tc.boundary, tc.occurrence, gotDigest)
		})
	}
}
