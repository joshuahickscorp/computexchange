//go:build integration

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

type verificationProcessorFixture struct {
	Dispatch TaskDispatch
	Commit   TaskCommit
	Result   []byte
}

func newVerificationProcessorFixture(t *testing.T) verificationProcessorFixture {
	t.Helper()
	_, taskCount := submitEmbedJob(t, 1, 0, 0, 0)
	if taskCount != 1 {
		t.Fatalf("verification processor fixture: want one task, got %d", taskCount)
	}
	code, body := req(t, http.MethodGet, "/v1/worker/poll", nil, workerTok())
	if code != http.StatusOK {
		t.Fatalf("verification processor fixture poll: %d %s", code, body)
	}
	var dispatch TaskDispatch
	if err := json.Unmarshal(body, &dispatch); err != nil {
		t.Fatalf("verification processor fixture dispatch: %v", err)
	}
	return verificationProcessorFixture{
		Dispatch: dispatch,
		Commit: TaskCommit{
			TaskID: dispatch.TaskID, ResultKey: dispatch.ResultKey,
			DurationMS: 731, TokensUsed: 19,
		},
		Result: embedResultJSON(1),
	}
}

type verificationProcessorState struct {
	TaskStatus      string
	TaskOutcome     string
	JobStatus       string
	JobTasksDone    int
	WorkStatus      string
	TerminalOutcome string
	WorkRows        int
	PlanRows        int
	VerdictRows     int
	DurationRows    int
	LedgerRows      int
	LedgerKinds     int
}

func readVerificationProcessorState(t *testing.T, taskID uuid.UUID) verificationProcessorState {
	t.Helper()
	var got verificationProcessorState
	err := itPool.QueryRow(context.Background(), `
		SELECT t.status,COALESCE(t.verification_outcome,''),j.status,j.tasks_done,
		       COALESCE((SELECT max(status) FROM verification_work WHERE task_id=t.id),''),
		       COALESCE((SELECT max(terminal_outcome) FROM verification_work WHERE task_id=t.id),''),
		       (SELECT count(*) FROM verification_work WHERE task_id=t.id),
		       (SELECT count(*) FROM verification_work_plans p
		          JOIN verification_work w ON w.id=p.work_id WHERE w.task_id=t.id),
		       (SELECT count(*) FROM task_verdicts WHERE task_id=t.id),
		       (SELECT count(*) FROM task_durations WHERE task_id=t.id),
		       (SELECT count(*) FROM ledger_entries WHERE task_id=t.id),
		       (SELECT count(DISTINCT kind) FROM ledger_entries WHERE task_id=t.id)
		  FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=$1`, taskID).
		Scan(&got.TaskStatus, &got.TaskOutcome, &got.JobStatus, &got.JobTasksDone,
			&got.WorkStatus, &got.TerminalOutcome, &got.WorkRows, &got.PlanRows,
			&got.VerdictRows, &got.DurationRows, &got.LedgerRows, &got.LedgerKinds)
	if err != nil {
		t.Fatalf("read verification processor state for %s: %v", taskID, err)
	}
	return got
}

func postVerificationProcessorCommit(commit TaskCommit) (int, []byte, error) {
	body, err := json.Marshal(commit)
	if err != nil {
		return 0, nil, err
	}
	r, err := http.NewRequest(http.MethodPost,
		itHTTP.URL+"/v1/worker/task/"+commit.TaskID.String()+"/commit", bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	r.Header.Set("X-Worker-Token", demoWorkerToken)
	r.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(resp.Body)
	return resp.StatusCode, responseBody, err
}

func TestVerificationPayoutHoldStartsAtDecision(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	fixture := newVerificationProcessorFixture(t)
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatal(err)
	}
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatal(err)
	}
	decisionStarted := time.Now().UTC().Truncate(time.Second)
	result, err := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		ProcessAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil || result.Pending || result.Outcome != OutcomePass {
		t.Fatalf("verification decision = %+v err=%v", result, err)
	}
	var releaseAt time.Time
	if err := itPool.QueryRow(ctx, `
		SELECT release_at FROM ledger_entries
		 WHERE task_id=$1 AND kind='supplier_credit'`, fixture.Dispatch.TaskID).Scan(&releaseAt); err != nil {
		t.Fatal(err)
	}
	if releaseAt.Before(decisionStarted.Add(minimumPayoutHold)) {
		t.Fatalf("supplier hold starts before terminal decision: decision_started=%s release_at=%s floor=%s",
			decisionStarted, releaseAt, minimumPayoutHold)
	}
}

func TestVerificationMissingStagingExhaustsIntoTypedNonPayableRetry(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	fixture := newVerificationProcessorFixture(t)
	// Deliberately do not PUT fixture.Dispatch.ResultKey. The protocol requires
	// PUT-before-commit; recovery gets a bounded grace budget, then must terminate
	// this attempt instead of holding the parent in verifying forever.
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatal(err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	for attempt := 1; attempt <= verificationArtifactUnavailableMaxLeaseAttempts; attempt++ {
		result, err := processor.ProcessAttempt(ctx, fixture.Dispatch.TaskID, 0)
		if attempt < verificationArtifactUnavailableMaxLeaseAttempts {
			if !errors.Is(err, ErrVerificationStagingArtifactMissing) || result.Outcome != "" {
				t.Fatalf("missing staging lease %d = result %+v err=%v", attempt, result, err)
			}
			continue
		}
		if err != nil || result.Pending || result.Outcome != OutcomeFail ||
			!result.Applied.Applied || !result.Applied.Rejected {
			t.Fatalf("exhausted missing staging = result %+v err=%v", result, err)
		}
	}

	work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil || work.Status != VerificationWorkTerminal || work.TerminalOutcome != string(OutcomeFail) ||
		work.Artifact == nil || !isUnavailableVerificationEvidenceKey(work.Artifact.Key) ||
		work.LeaseAttempts != verificationArtifactUnavailableMaxLeaseAttempts {
		t.Fatalf("missing staging work did not terminate on evidence: work=%+v err=%v", work, err)
	}
	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil || plan.Decision.Failure == nil ||
		plan.Decision.Failure.Kind != "artifact_unavailable" ||
		plan.Decision.Failure.Code != "retry_exhausted" || len(plan.Settlement) != 0 {
		t.Fatalf("missing staging durable plan = %+v err=%v", plan, err)
	}
	body, err := itStorage.ReadSealedVerificationArtifact(ctx, *work.Artifact)
	if err != nil {
		t.Fatal(err)
	}
	var evidence struct {
		Version          int    `json:"version"`
		Reason           string `json:"reason"`
		StagingKeySHA256 string `json:"staging_key_sha256"`
		LeaseAttempts    int    `json:"lease_attempts"`
	}
	if err := json.Unmarshal(body, &evidence); err != nil || evidence.Version != 1 ||
		evidence.Reason != "verification_artifact_missing" ||
		evidence.LeaseAttempts != verificationArtifactUnavailableMaxLeaseAttempts ||
		evidence.StagingKeySHA256 == "" {
		t.Fatalf("missing staging evidence = %+v err=%v", evidence, err)
	}
	var taskStatus, taskOutcome, jobStatus string
	var retryCount int
	var excluded *uuid.UUID
	var ledgerRows, durationRows, eventRows int
	if err := itPool.QueryRow(ctx, `
		SELECT t.status,COALESCE(t.verification_outcome,''),t.retry_count,t.excluded_worker,
		       j.status,
		       (SELECT count(*) FROM ledger_entries WHERE task_id=t.id),
		       (SELECT count(*) FROM task_durations WHERE task_id=t.id),
		       (SELECT count(*) FROM verification_events WHERE task_id=t.id AND kind='artifact_unavailable')
		  FROM tasks t JOIN jobs j ON j.id=t.job_id WHERE t.id=$1`, fixture.Dispatch.TaskID).
		Scan(&taskStatus, &taskOutcome, &retryCount, &excluded, &jobStatus,
			&ledgerRows, &durationRows, &eventRows); err != nil {
		t.Fatal(err)
	}
	if taskStatus != "retrying" || taskOutcome != "" || retryCount != 1 || excluded == nil ||
		*excluded != demoWorkerUUID || jobStatus != "running" || ledgerRows != 0 || durationRows != 0 || eventRows != 1 {
		t.Fatalf("missing staging leaked or failed to requeue: task=%s/%s retry=%d excluded=%v job=%s ledger=%d duration=%d events=%d",
			taskStatus, taskOutcome, retryCount, excluded, jobStatus, ledgerRows, durationRows, eventRows)
	}
	exact, err := itStore.ExactTerminalVerificationCommit(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit)
	if err != nil || !exact {
		t.Fatalf("missing staging terminal replay exact=%v err=%v", exact, err)
	}
}

func TestVerificationProcessorHTTPCommitSealsPlansAndReplaysExactly(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("put verification staging result: %v", err)
	}

	code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("HTTP verification commit: want 204, got %d: %s", code, body)
	}

	work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil {
		t.Fatalf("read terminal verification work: %v", err)
	}
	if work.Status != VerificationWorkTerminal || work.TerminalOutcome != string(OutcomePass) ||
		work.TerminalAt == nil || work.Artifact == nil {
		t.Fatalf("terminal verification work = %+v", work)
	}
	if work.Artifact.Key == fixture.Dispatch.ResultKey {
		t.Fatalf("verdict authority still points at worker-writable staging key %q", work.Artifact.Key)
	}
	sealed, err := itStorage.ReadSealedVerificationArtifact(ctx, *work.Artifact)
	if err != nil {
		t.Fatalf("read sealed verification authority: %v", err)
	}
	if !bytes.Equal(sealed, fixture.Result) {
		t.Fatalf("sealed verification bytes differ from committed bytes")
	}

	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil {
		t.Fatalf("read immutable verification plan: %v", err)
	}
	if plan.WorkID != work.ID || plan.SnapshotSHA256 != work.SnapshotSHA256 ||
		plan.Artifact != *work.Artifact || plan.DecisionSHA256 != work.DecisionSHA256 ||
		plan.Decision.Outcome != OutcomePass {
		t.Fatalf("plan/work authority diverged: work=%+v plan=%+v", work, plan)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE verification_work_plans SET sampling_selected=NOT sampling_selected WHERE work_id=$1`, work.ID); err == nil {
		t.Fatal("verification work plan was mutable after terminal decision")
	}

	var (
		verdictWorkID                          uuid.UUID
		verdictOutcome, verdictResultSHA       string
		verdictDecisionSHA, verdictArtifactKey string
		verdictArtifactSHA                     string
		taskResultKey, taskResultSHA           string
	)
	if err := itPool.QueryRow(ctx, `
		SELECT v.verification_work_id,v.outcome,COALESCE(v.result_sha256,''),
		       COALESCE(v.decision_sha256,''),COALESCE(v.artifact_key,''),COALESCE(v.artifact_sha256,''),
		       COALESCE(t.result_ref,''),COALESCE(t.result_sha256,'')
		  FROM task_verdicts v JOIN tasks t ON t.id=v.task_id
		 WHERE v.task_id=$1 AND v.attempt=0`, fixture.Dispatch.TaskID).
		Scan(&verdictWorkID, &verdictOutcome, &verdictResultSHA, &verdictDecisionSHA,
			&verdictArtifactKey, &verdictArtifactSHA, &taskResultKey, &taskResultSHA); err != nil {
		t.Fatalf("read sealed task-verdict binding: %v", err)
	}
	if verdictWorkID != work.ID || verdictOutcome != string(OutcomePass) ||
		verdictResultSHA != work.Artifact.SHA256 || verdictDecisionSHA != plan.DecisionSHA256 ||
		verdictArtifactKey != work.Artifact.Key || verdictArtifactSHA != work.Artifact.SHA256 ||
		taskResultKey != work.Artifact.Key || taskResultSHA != work.Artifact.SHA256 {
		t.Fatalf("sealed task-verdict binding mismatch: work=%+v verdict=%s/%s/%s/%s/%s task=%s/%s",
			work, verdictWorkID, verdictOutcome, verdictResultSHA, verdictDecisionSHA,
			verdictArtifactKey, taskResultKey, taskResultSHA)
	}

	beforeReplay := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if beforeReplay.TaskStatus != "complete" || beforeReplay.WorkStatus != VerificationWorkTerminal ||
		beforeReplay.WorkRows != 1 || beforeReplay.PlanRows != 1 || beforeReplay.VerdictRows != 1 ||
		beforeReplay.DurationRows != 1 || beforeReplay.LedgerRows != 3 || beforeReplay.LedgerKinds != 3 {
		t.Fatalf("terminal state before replay = %+v", beforeReplay)
	}
	code, body = req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("exact terminal HTTP replay: want 204, got %d: %s", code, body)
	}
	if afterReplay := readVerificationProcessorState(t, fixture.Dispatch.TaskID); afterReplay != beforeReplay {
		t.Fatalf("exact HTTP replay changed durable state:\nbefore=%+v\nafter =%+v", beforeReplay, afterReplay)
	}
}

func TestVerificationProcessorDrainRecoversMissingArtifactWithoutSecondCommit(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)

	code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusBadRequest {
		t.Fatalf("missing-artifact commit: want 400 after durable enqueue, got %d: %s", code, body)
	}
	pending := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if pending.TaskStatus != "verifying" || pending.JobStatus != "verifying" ||
		pending.WorkStatus != VerificationWorkPending || pending.WorkRows != 1 ||
		pending.PlanRows != 0 || pending.VerdictRows != 0 || pending.DurationRows != 0 || pending.LedgerRows != 0 {
		t.Fatalf("missing artifact leaked a verdict/effect/money row: %+v", pending)
	}

	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("late upload of staged result: %v", err)
	}
	// The request path deliberately backs off a failed object lookup. Advance only
	// the durable queue clock; no second commit is sent by this test.
	if _, err := itPool.Exec(ctx, `
		UPDATE verification_work SET next_attempt_at=now() WHERE task_id=$1 AND attempt=0`,
		fixture.Dispatch.TaskID); err != nil {
		t.Fatalf("make missing-artifact work due: %v", err)
	}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	if err := processor.Drain(ctx, 10); err != nil {
		t.Fatalf("background verification drain after late upload: %v", err)
	}

	terminal := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if terminal.TaskStatus != "complete" || terminal.TaskOutcome != string(OutcomePass) ||
		terminal.JobTasksDone != 1 || terminal.WorkStatus != VerificationWorkTerminal ||
		terminal.TerminalOutcome != string(OutcomePass) || terminal.WorkRows != 1 || terminal.PlanRows != 1 ||
		terminal.VerdictRows != 1 || terminal.DurationRows != 1 || terminal.LedgerRows != 3 || terminal.LedgerKinds != 3 {
		t.Fatalf("background drain did not converge missing-artifact attempt: %+v", terminal)
	}
}

func TestVerificationProcessorOversizePersistsTerminalRejectionInsteadOfRetryingWork(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	// Freeze the smallest legitimate embed task shape before CommitTask captures
	// the immutable attempt snapshot. The policy still leaves a generous 1 MiB
	// envelope plus 16 KiB for the one 384-dim row.
	if _, err := itPool.Exec(ctx, `UPDATE jobs SET split_size=1 WHERE id=$1`, fixture.Dispatch.JobID); err != nil {
		t.Fatalf("set bounded verification fixture shape: %v", err)
	}
	maxBytes := verificationArtifactMaxBytes("embed", 1, 0)
	oversized := bytes.Repeat([]byte("x"), int(maxBytes+1))
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, oversized, "application/octet-stream"); err != nil {
		t.Fatalf("put oversized verification staging result: %v", err)
	}

	code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("oversized verification commit: want durable 204 rejection, got %d: %s", code, body)
	}

	state := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if state.TaskStatus != "retrying" || state.JobStatus != "running" ||
		state.WorkStatus != VerificationWorkTerminal || state.TerminalOutcome != string(OutcomeFail) ||
		state.WorkRows != 1 || state.PlanRows != 1 || state.VerdictRows != 1 ||
		state.DurationRows != 0 || state.LedgerRows != 0 {
		t.Fatalf("oversize did not converge to one terminal no-money rejection: %+v", state)
	}
	work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil {
		t.Fatalf("read oversized terminal work: %v", err)
	}
	if work.Artifact == nil || !isOversizedVerificationEvidenceKey(work.Artifact.Key) ||
		work.Artifact.Bytes >= 1<<10 || work.Artifact.Bytes > maxBytes {
		t.Fatalf("oversize work did not pin small server evidence: %+v", work.Artifact)
	}
	plan, err := itStore.VerificationWorkPlan(ctx, work.ID)
	if err != nil {
		t.Fatalf("read oversized rejection plan: %v", err)
	}
	if plan.Decision.Outcome != OutcomeFail || plan.Decision.Failure == nil ||
		plan.Decision.Failure.Kind != "artifact_oversize" || plan.Decision.Failure.Code != "too_large" ||
		len(plan.Decision.Effects) != 4 ||
		plan.Decision.Effects[0].Kind != VerificationEffectDockReputation ||
		plan.Decision.Effects[1].Kind != VerificationEffectRecordEvent ||
		plan.Decision.Effects[1].EventKind != "artifact_oversize" ||
		plan.Decision.Effects[2].Kind != VerificationEffectQuarantine ||
		plan.Decision.Effects[3].Kind != VerificationEffectRequeue ||
		plan.Decision.Effects[3].TaskID != fixture.Dispatch.TaskID {
		t.Fatalf("oversized rejection plan = %+v", plan.Decision)
	}
	var supplierStatus string
	var quarantined bool
	var eventCount int
	if err := itPool.QueryRow(ctx, `
		SELECT s.status,s.quarantined_at IS NOT NULL,
		       (SELECT count(*) FROM verification_events
		         WHERE task_id=$2 AND kind='artifact_oversize')
		  FROM suppliers s WHERE s.id=$1`, demoSupplierUUID, fixture.Dispatch.TaskID).
		Scan(&supplierStatus, &quarantined, &eventCount); err != nil {
		t.Fatalf("read oversized supplier outcome: %v", err)
	}
	if supplierStatus != "suspended" || !quarantined || eventCount != 1 {
		t.Fatalf("oversize authority status=%s quarantined=%v events=%d", supplierStatus, quarantined, eventCount)
	}

	// A response-loss replay observes the same terminal verdict; it neither
	// reopens verification_work nor repeats the task retry transition.
	before := state
	code, body = req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("oversized terminal replay: want 204, got %d: %s", code, body)
	}
	if after := readVerificationProcessorState(t, fixture.Dispatch.TaskID); after != before {
		t.Fatalf("oversized terminal replay changed durable state:\nbefore=%+v\nafter =%+v", before, after)
	}
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM verification_events
		 WHERE task_id=$1 AND kind='artifact_oversize'`, fixture.Dispatch.TaskID).Scan(&eventCount); err != nil || eventCount != 1 {
		t.Fatalf("oversized replay duplicated typed event: events=%d err=%v", eventCount, err)
	}
}

func TestVerificationProcessorFencedLeaseRejectsExpiredOwner(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusBadRequest {
		t.Fatalf("seed pending verification work: want 400, got %d: %s", code, body)
	}
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("put staged result: %v", err)
	}
	if _, err := itPool.Exec(ctx, `UPDATE verification_work SET next_attempt_at=now() WHERE task_id=$1`, fixture.Dispatch.TaskID); err != nil {
		t.Fatalf("make verification work due: %v", err)
	}

	oldClaim, err := itStore.ClaimVerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0, "expired-owner", time.Minute)
	if err != nil {
		t.Fatalf("claim old verification lease: %v", err)
	}
	// Move the database clock edge deterministically instead of sleeping. Recovery
	// is then entitled to replace both owner and random token.
	if _, err := itPool.Exec(ctx, `
		UPDATE verification_work SET lease_expires_at=now()-interval '1 second' WHERE id=$1`, oldClaim.Work.ID); err != nil {
		t.Fatalf("expire old verification lease: %v", err)
	}
	newClaim, err := itStore.ClaimVerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0, "recovery-owner", time.Minute)
	if err != nil {
		t.Fatalf("steal expired verification lease: %v", err)
	}
	if oldClaim.Lease.Token == newClaim.Lease.Token || oldClaim.Lease.Owner == newClaim.Lease.Owner {
		t.Fatalf("recovery lease did not replace fence: old=%+v new=%+v", oldClaim.Lease, newClaim.Lease)
	}

	sealed, err := itStorage.SealVerificationArtifact(ctx, fixture.Dispatch.TaskID, 0, fixture.Dispatch.ResultKey)
	if err != nil {
		t.Fatalf("seal staged artifact: %v", err)
	}
	authority := VerificationArtifact{Key: sealed.Key, SHA256: sealed.SHA256, Bytes: sealed.Bytes}
	if _, err := itStore.PinVerificationArtifact(ctx, oldClaim.Lease, authority); !errors.Is(err, ErrVerificationLeaseLost) {
		t.Fatalf("expired lease pinned artifact: got %v, want %v", err, ErrVerificationLeaseLost)
	}

	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	info, _, err := commitInfoFromVerificationWork(newClaim.Work)
	if err != nil {
		t.Fatalf("recover committed task snapshot: %v", err)
	}
	if newClaim.Work.SamplingProbability == nil || newClaim.Work.SamplingSelected == nil {
		probability := processor.verifier.effectiveCheckProb(ctx, info)
		selected := processor.verifier.checkSampled(info.TaskID, probability)
		if _, err := itStore.PinVerificationSampling(ctx, newClaim.Lease, probability, selected); err != nil {
			t.Fatalf("pin sampling on recovery lease: %v", err)
		}
	}
	if _, err := itStore.PinVerificationArtifact(ctx, newClaim.Lease, authority); err != nil {
		t.Fatalf("recovery lease pin artifact: %v", err)
	}
	work, err := itStore.VerificationWorkForAttempt(ctx, fixture.Dispatch.TaskID, 0)
	if err != nil {
		t.Fatalf("reload pinned recovery work: %v", err)
	}
	info, commit, err := commitInfoFromVerificationWork(work)
	if err != nil {
		t.Fatalf("reconstruct pinned recovery attempt: %v", err)
	}
	info.ResultKey, info.ResultSHA256 = authority.Key, authority.SHA256
	commit.ResultKey, commit.ResultSHA256 = authority.Key, authority.SHA256
	sealedBytes, err := itStorage.ReadSealedVerificationArtifact(ctx, authority)
	if err != nil {
		t.Fatalf("read pinned recovery artifact: %v", err)
	}
	plan, err := processor.createPlan(ctx, newClaim.Lease, work, info, commit, sealedBytes)
	if err != nil {
		t.Fatalf("persist recovery plan: %v", err)
	}
	if _, err := itStore.ApplyLeasedVerificationWork(ctx, oldClaim.Lease, plan, info, nil); err == nil ||
		(!errors.Is(err, ErrVerificationLeaseLost) && !errors.Is(err, ErrVerificationWorkConflict)) {
		t.Fatalf("expired lease applied durable plan: %v", err)
	}
	if beforeRecovery := readVerificationProcessorState(t, fixture.Dispatch.TaskID); beforeRecovery.TaskStatus != "verifying" || beforeRecovery.VerdictRows != 0 ||
		beforeRecovery.DurationRows != 0 || beforeRecovery.LedgerRows != 0 {
		t.Fatalf("expired lease leaked terminal effects: %+v", beforeRecovery)
	}

	result, err := processor.processLeased(ctx, LeasedVerificationWork{Work: work, Lease: newClaim.Lease})
	if err != nil {
		t.Fatalf("recovery lease process persisted plan: %v", err)
	}
	if result.Outcome != OutcomePass || !result.Applied.Applied {
		t.Fatalf("recovery lease result = %+v", result)
	}
	afterRecovery := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if afterRecovery.TaskStatus != "complete" || afterRecovery.WorkStatus != VerificationWorkTerminal ||
		afterRecovery.VerdictRows != 1 || afterRecovery.DurationRows != 1 ||
		afterRecovery.LedgerRows != 3 || afterRecovery.LedgerKinds != 3 {
		t.Fatalf("new lease did not converge exactly once: %+v", afterRecovery)
	}
}

func TestVerificationProcessorConcurrentClaimsAndDuplicateCommitConvergeOnce(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("put concurrent verification result: %v", err)
	}
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("durably enqueue concurrent verification attempt: %v", err)
	}

	var beforeRep float32
	var beforeCompleted int64
	if err := itPool.QueryRow(ctx, `SELECT reputation,completed_tasks FROM suppliers WHERE id=$1`, demoSupplierUUID).
		Scan(&beforeRep, &beforeCompleted); err != nil {
		t.Fatalf("read supplier before concurrent verification: %v", err)
	}
	p1 := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	p2 := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage))
	if p1.owner == p2.owner {
		t.Fatalf("independent processors unexpectedly share owner %q", p1.owner)
	}

	type result struct {
		name   string
		status int
		body   []byte
		result VerificationProcessResult
		err    error
	}
	start := make(chan struct{})
	results := make(chan result, 3)
	var wg sync.WaitGroup
	for name, processor := range map[string]*VerificationProcessor{"processor-1": p1, "processor-2": p2} {
		name, processor := name, processor
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			processed, err := processor.ProcessAttempt(ctx, fixture.Dispatch.TaskID, 0)
			results <- result{name: name, result: processed, err: err}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		<-start
		status, body, err := postVerificationProcessorCommit(fixture.Commit)
		results <- result{name: "duplicate-http", status: status, body: body, err: err}
	}()
	close(start)
	wg.Wait()
	close(results)
	for got := range results {
		if got.err != nil {
			t.Fatalf("%s concurrent verification: %v", got.name, got.err)
		}
		if got.name == "duplicate-http" && got.status != http.StatusAccepted && got.status != http.StatusNoContent {
			t.Fatalf("duplicate HTTP commit during active lease: got %d %s", got.status, got.body)
		}
	}

	terminal := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if terminal.TaskStatus != "complete" || terminal.TaskOutcome != string(OutcomePass) ||
		terminal.JobTasksDone != 1 || terminal.WorkStatus != VerificationWorkTerminal ||
		terminal.WorkRows != 1 || terminal.PlanRows != 1 || terminal.VerdictRows != 1 ||
		terminal.DurationRows != 1 || terminal.LedgerRows != 3 || terminal.LedgerKinds != 3 {
		t.Fatalf("concurrent processors did not converge to one terminal set: %+v", terminal)
	}
	var afterRep float32
	var afterCompleted int64
	if err := itPool.QueryRow(ctx, `SELECT reputation,completed_tasks FROM suppliers WHERE id=$1`, demoSupplierUUID).
		Scan(&afterRep, &afterCompleted); err != nil {
		t.Fatalf("read supplier after concurrent verification: %v", err)
	}
	if afterRep != updateReputation(beforeRep, EventTaskSuccess) || afterCompleted != beforeCompleted+1 {
		t.Fatalf("verification effect applied more or less than once: reputation %.6f->%.6f completed %d->%d",
			beforeRep, afterRep, beforeCompleted, afterCompleted)
	}

	code, body := req(t, http.MethodPost,
		"/v1/worker/task/"+fixture.Dispatch.TaskID.String()+"/commit",
		fixture.Commit, workerTok(), jsonCT())
	if code != http.StatusNoContent {
		t.Fatalf("post-convergence exact replay: want 204, got %d: %s", code, body)
	}
	if replay := readVerificationProcessorState(t, fixture.Dispatch.TaskID); replay != terminal {
		t.Fatalf("post-convergence replay changed terminal set:\nterminal=%+v\nreplay  =%+v", terminal, replay)
	}
}
