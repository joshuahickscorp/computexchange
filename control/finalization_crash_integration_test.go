//go:build integration

package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
)

const (
	finalizationCrashChildEnv    = "CX_FINALIZATION_CRASH_CHILD"
	finalizationCrashBoundaryEnv = "CX_FINALIZATION_CRASH_BOUNDARY"
	finalizationCrashPhaseEnv    = "CX_FINALIZATION_CRASH_PHASE"
	finalizationCrashJobEnv      = "CX_FINALIZATION_CRASH_JOB_ID"
)

type finalizationCrashFixture struct {
	JobID  uuid.UUID
	TaskID uuid.UUID
	Plan   EconomicPlan
}

// finalizationCrashState contains only durable buyer-facing and economic facts.
// Timestamps and generated row UUIDs are deliberately excluded; their presence,
// byte/dollar values, and multiplicity are the canonical contract.
type finalizationCrashState struct {
	JobStatus          string
	TaskOutcome        string
	ResultsMerged      bool
	OutputRecords      int64
	OutputBytes        int64
	OutputSource       string
	ObjectBytes        int64
	ObjectSHA256       string
	ActualUSD          string
	TaskChargeRows     int
	TaskChargeUSD      string
	SLAPremiumRows     int
	SLAPremiumUSD      string
	SupplierCreditRows int
}

func seedFinalizationCrashFixture(t *testing.T) finalizationCrashFixture {
	t.Helper()
	ctx := context.Background()
	jobID, tasks, plan := createFrozenEconomicTestJob(t, 1, 0, .40)
	taskID := tasks[0]
	outputRef := fmt.Sprintf("jobs/%s/finalization-crash/output.jsonl", jobID)
	var resultKey string
	if err := itPool.QueryRow(ctx, `SELECT result_key FROM tasks WHERE id=$1`, taskID).Scan(&resultKey); err != nil {
		t.Fatalf("read finalization crash result key: %v", err)
	}
	result := embedResultJSON(2)
	if err := itStorage.PutObject(ctx, resultKey, result, "application/json"); err != nil {
		t.Fatalf("put finalization crash task result: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE jobs
		   SET status='running',output_ref=$2,split_size=2
		 WHERE id=$1`, jobID, outputRef); err != nil {
		t.Fatalf("prepare finalization crash job: %v", err)
	}
	if _, err := itPool.Exec(ctx, `
		UPDATE tasks
		   SET status='verifying',worker_id=$2,claimed_by=$2,claimed_at=now(),
		       started_at=now(),result_ref=result_key
		 WHERE id=$1`, taskID, demoWorkerUUID); err != nil {
		t.Fatalf("prepare finalization crash task: %v", err)
	}
	entries := splitFrozenCharge(
		demoBuyerUUID, demoSupplierUUID, taskID,
		plan.BuyerChargePerTaskUSD, plan.SupplierPayoutPerTaskUSD,
		0, time.Unix(0, 0).UTC(),
	)
	info := &CommitTaskInfo{
		TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID, SupplierID: demoSupplierUUID,
		jobType: "embed", ModelRef: "all-minilm-l6-v2", SplitSize: 2, DurationMS: 10,
	}
	if err := itStore.FinalizeTaskVerification(ctx, info, OutcomePass, entries); err != nil {
		t.Fatalf("accept finalization crash task: %v", err)
	}
	return finalizationCrashFixture{JobID: jobID, TaskID: taskID, Plan: plan}
}

func runFinalizationToTerminal(t *testing.T, fixture finalizationCrashFixture) finalizationCrashState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := mergeJobResults(ctx, itStore, itStorage, fixture.JobID); err != nil {
		t.Fatalf("merge finalization crash fixture: %v", err)
	}
	if err := itStore.CompleteJobEconomics(ctx, fixture.JobID); err != nil {
		t.Fatalf("complete finalization crash fixture: %v", err)
	}
	state := readFinalizationCrashState(t, fixture)
	assertFinalizationCrashState(t, fixture, state)
	return state
}

func readFinalizationCrashState(t *testing.T, fixture finalizationCrashFixture) finalizationCrashState {
	t.Helper()
	ctx := context.Background()
	var (
		state                          finalizationCrashState
		actual, taskCharge, slaPremium float64
		outputRef                      string
	)
	if err := itPool.QueryRow(ctx, `
		SELECT j.status,
		       COALESCE(t.verification_outcome,''),
		       j.results_merged_at IS NOT NULL,
		       COALESCE(j.economic_output_records,0),
		       COALESCE(j.economic_output_bytes,0),
		       COALESCE(j.economic_output_source,''),
		       COALESCE(j.output_ref,''),
		       j.actual_usd::float8,
		       (SELECT count(*) FROM ledger_entries le WHERE le.task_id=t.id AND le.kind='buyer_charge'),
		       (SELECT COALESCE(sum(-le.amount_usd),0)::float8 FROM ledger_entries le WHERE le.task_id=t.id AND le.kind='buyer_charge'),
		       (SELECT count(*) FROM ledger_entries le WHERE le.kind='buyer_charge' AND le.task_id IS NULL AND le.payout_ref=$3),
		       (SELECT COALESCE(sum(-le.amount_usd),0)::float8 FROM ledger_entries le WHERE le.kind='buyer_charge' AND le.task_id IS NULL AND le.payout_ref=$3),
		       (SELECT count(*) FROM ledger_entries le WHERE le.task_id=t.id AND le.kind='supplier_credit')
		  FROM jobs j JOIN tasks t ON t.job_id=j.id
		 WHERE j.id=$1 AND t.id=$2`, fixture.JobID, fixture.TaskID, slaPremiumChargeRef(fixture.JobID)).
		Scan(
			&state.JobStatus, &state.TaskOutcome, &state.ResultsMerged,
			&state.OutputRecords, &state.OutputBytes, &state.OutputSource, &outputRef,
			&actual, &state.TaskChargeRows, &taskCharge,
			&state.SLAPremiumRows, &slaPremium, &state.SupplierCreditRows,
		); err != nil {
		t.Fatalf("read finalization crash state: %v", err)
	}
	state.ActualUSD = fmt.Sprintf("%.6f", actual)
	state.TaskChargeUSD = fmt.Sprintf("%.6f", taskCharge)
	state.SLAPremiumUSD = fmt.Sprintf("%.6f", slaPremium)
	body, err := itStorage.GetObject(ctx, outputRef)
	if err != nil {
		t.Fatalf("read canonical merged output: %v", err)
	}
	sum := sha256.Sum256(body)
	state.ObjectBytes = int64(len(body))
	state.ObjectSHA256 = hex.EncodeToString(sum[:])
	return state
}

func assertFinalizationCrashState(t *testing.T, fixture finalizationCrashFixture, state finalizationCrashState) {
	t.Helper()
	if state.JobStatus != "complete" || state.TaskOutcome != string(OutcomePass) {
		t.Fatalf("terminal lifecycle = job %q task %q", state.JobStatus, state.TaskOutcome)
	}
	if !state.ResultsMerged || state.OutputRecords != 2 || state.OutputBytes <= 0 ||
		state.OutputBytes != state.ObjectBytes || state.OutputSource != economicOutputSourceMergedArtifact {
		t.Fatalf("terminal output authority is incomplete: %+v", state)
	}
	if state.ActualUSD != fmt.Sprintf("%.6f", fixture.Plan.InitialBuyerChargeUSD) ||
		state.TaskChargeUSD != fmt.Sprintf("%.6f", fixture.Plan.BuyerChargePerTaskUSD) ||
		state.SLAPremiumUSD != fmt.Sprintf("%.6f", fixture.Plan.Input.SLAPremiumUSD) {
		t.Fatalf("terminal economic totals disagree with frozen plan: state=%+v plan=%+v", state, fixture.Plan)
	}
	if state.TaskChargeRows != 1 || state.SLAPremiumRows != 1 || state.SupplierCreditRows != 1 {
		t.Fatalf("terminal settlement is not exactly once: %+v", state)
	}
}

type finalizationProcessCrashProbe struct {
	target RecoveryBoundary
	signal *os.File
	once   sync.Once
}

func (p *finalizationProcessCrashProbe) Reach(_ context.Context, boundary RecoveryBoundary) {
	if boundary != p.target {
		return
	}
	p.once.Do(func() {
		_, _ = fmt.Fprintln(p.signal, boundary)
		select {}
	})
}

// TestFinalizationCrashChild is invoked only by the parent matrix. It executes
// the real merge/completion implementation, reports one exact boundary over FD
// 3, and remains blocked until the parent delivers SIGKILL.
func TestFinalizationCrashChild(t *testing.T) {
	if os.Getenv(finalizationCrashChildEnv) != "1" {
		t.Skip("subprocess helper")
	}
	jobID, err := uuid.Parse(os.Getenv(finalizationCrashJobEnv))
	if err != nil {
		t.Fatalf("invalid finalization crash job: %v", err)
	}
	signal := os.NewFile(uintptr(3), "finalization-crash-signal")
	if signal == nil {
		t.Fatal("missing finalization crash signal pipe")
	}
	boundary := RecoveryBoundary(os.Getenv(finalizationCrashBoundaryEnv))
	probe := &finalizationProcessCrashProbe{target: boundary, signal: signal}
	ctx := context.Background()
	switch os.Getenv(finalizationCrashPhaseEnv) {
	case "merge":
		if _, err := mergeJobResultsWithProbe(ctx, itStore, itStorage, jobID, probe); err != nil {
			t.Fatalf("merge crash child returned before boundary %s: %v", boundary, err)
		}
	case "complete":
		if err := itStore.completeJobEconomics(ctx, jobID, probe); err != nil {
			t.Fatalf("completion crash child returned before boundary %s: %v", boundary, err)
		}
	default:
		t.Fatalf("invalid finalization crash phase %q", os.Getenv(finalizationCrashPhaseEnv))
	}
	t.Fatalf("finalization crash child missed boundary %s", boundary)
}

type finalizationCrashCase struct {
	name     string
	phase    string
	boundary RecoveryBoundary
}

func killFinalizationAtBoundary(t *testing.T, fixture finalizationCrashFixture, tc finalizationCrashCase) {
	t.Helper()
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer readPipe.Close()
	var stdout, stderr bytes.Buffer
	cmd := exec.Command(os.Args[0], "-test.run=^TestFinalizationCrashChild$", "-test.count=1")
	cmd.Env = append(os.Environ(),
		finalizationCrashChildEnv+"=1",
		finalizationCrashBoundaryEnv+"="+string(tc.boundary),
		finalizationCrashPhaseEnv+"="+tc.phase,
		finalizationCrashJobEnv+"="+fixture.JobID.String(),
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
			t.Fatalf("finalization child did not reach %s: signal=%q err=%v\nstdout=%s\nstderr=%s",
				tc.boundary, got.line, got.err, stdout.String(), stderr.String())
		}
	case <-time.After(15 * time.Second):
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		t.Fatalf("timeout waiting for finalization boundary %s\nstdout=%s\nstderr=%s",
			tc.boundary, stdout.String(), stderr.String())
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatalf("SIGKILL finalization child at %s: %v", tc.boundary, err)
	}
	waitErr := cmd.Wait()
	exitErr, ok := waitErr.(*exec.ExitError)
	if !ok {
		t.Fatalf("finalization child was not killed at %s: %v", tc.boundary, waitErr)
	}
	status, ok := exitErr.Sys().(syscall.WaitStatus)
	if !ok || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatalf("finalization child exit was not SIGKILL at %s: %v", tc.boundary, exitErr.Sys())
	}
}

func TestFinalizationSIGKILLRecoveryConvergesToCanonicalArtifactAndEconomics(t *testing.T) {
	reset(t)
	baselineFixture := seedFinalizationCrashFixture(t)
	baseline := runFinalizationToTerminal(t, baselineFixture)

	cases := []finalizationCrashCase{
		{name: "merge-before-put", phase: "merge", boundary: BoundaryMergeBeforePut},
		{name: "merge-after-put", phase: "merge", boundary: BoundaryMergeAfterPut},
		{name: "merge-after-readback", phase: "merge", boundary: BoundaryMergeAfterVerify},
		{name: "merge-before-watermark", phase: "merge", boundary: BoundaryMergeBeforePublish},
		{name: "merge-after-watermark", phase: "merge", boundary: BoundaryMergeAfterPublish},
		{name: "complete-after-job", phase: "complete", boundary: BoundaryCompleteAfterJobProjection},
		{name: "complete-after-sla-premium", phase: "complete", boundary: BoundaryCompleteAfterSLAPremium},
		{name: "complete-after-actual-usd", phase: "complete", boundary: BoundaryCompleteAfterActualUSD},
		{name: "complete-before-db", phase: "complete", boundary: BoundaryCompleteBeforeDBCommit},
		{name: "complete-after-db", phase: "complete", boundary: BoundaryCompleteAfterDBCommit},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			fixture := seedFinalizationCrashFixture(t)
			if tc.phase == "complete" {
				if _, err := mergeJobResults(context.Background(), itStore, itStorage, fixture.JobID); err != nil {
					t.Fatalf("prepare completion crash: %v", err)
				}
			}
			killFinalizationAtBoundary(t, fixture, tc)

			// Recovery deliberately invokes the ordinary nil-probe production path
			// from its start. Object PUT and SQL effects may already have committed;
			// fixed-key writes, unique SLA authority, and ledger recomputation must
			// converge without a phase-specific repair routine.
			state := runFinalizationToTerminal(t, fixture)
			if !reflect.DeepEqual(state, baseline) {
				t.Fatalf("SIGKILL at %s did not converge:\nwant %+v\ngot  %+v", tc.boundary, baseline, state)
			}

			// Replay the full normal path once more to model response loss after
			// recovery. It may rewrite identical object bytes, but no buyer charge,
			// SLA premium, output unit, or actual_usd fact may multiply or drift.
			replayed := runFinalizationToTerminal(t, fixture)
			if !reflect.DeepEqual(replayed, state) {
				t.Fatalf("terminal finalization replay changed canonical state at %s:\nbefore %+v\nafter  %+v",
					tc.boundary, state, replayed)
			}
			t.Logf("SIGKILL boundary=%s canonical_output_sha256=%s bytes=%d actual_usd=%s sla_rows=%s",
				tc.boundary, state.ObjectSHA256, state.ObjectBytes, state.ActualUSD,
				strconv.Itoa(state.SLAPremiumRows))
		})
	}
}
