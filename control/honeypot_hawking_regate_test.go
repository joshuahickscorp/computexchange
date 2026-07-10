//go:build integration

package main

// honeypot_hawking_regate_test.go — Week-6b HAWKING CROSS-WORKER DETERMINISM
// RE-GATE, control side (docs/DETERMINISM_CLASS.md "Seeding a hawking-class
// byte-exact honeypot"; docs/HAWKING_PORT_PLAN.md Week 6b; the gap CREED
// entries 84/86 named as "(a) the control-side determinism re-gate").
//
// Everything here runs against the REAL Postgres + MinIO stack the shared
// TestMain (integration_test.go) stands up — real rows, real objects, the real
// Verifier — never a mock. What is proven:
//
//	(a) ACTIVATION — a hawking-class worker committing the seeded honeypot's
//	    EXACT known answer passes the byte-exact honeypot check (a real
//	    honeypot_pass receipt, no dock, no quarantine). Before this wave NO
//	    byte-exact honeypot existed at all (seed.go deliberately refused to
//	    seed one without a producing class), so this is the first time
//	    byte-exact money work on the hawking lane has live honeypot coverage.
//	(b) DETECTION — a hawking-class worker committing a WRONG answer on the
//	    same probe is docked, clawed back, quarantined, and the task requeued
//	    (the full fraud path, same as the embed honeypot's).
//	(c) CLASS BOUNDARY — a candle-class worker (and a hawking worker with an
//	    UNKNOWN build hash) is NEVER byte-compared against the hawking-seeded
//	    answer: the probe skips, no honeypot receipt is fabricated, no dock,
//	    no quarantine, no requeue. This is the "a candle-seeded honeypot would
//	    byte-fail a correct hawking result" hazard, inverted and pinned.
//	(d) SEED SAFETY — the real store refuses a class-blind byte-exact honeypot
//	    (validateHoneypotSeed via InsertHoneypot), and seedDemo's hawking seed
//	    is idempotent + round-trips bytes/class/input-object exactly.
//
// The demo identities/constants come from seed.go (TestMain runs seedDemo).

import (
	"bytes"
	"context"
	"errors"
	"testing"

	"github.com/google/uuid"
)

// hawkClass returns the seeded hawking honeypot's answer_class exactly as the
// verifier will compute it for a committing worker of the reference class —
// through classKey, never a hand-assembled string, so a future format change
// in classKey cannot silently split the two.
func hawkClass() string {
	return classKey(demoHoneypotHawkEngine, demoHoneypotHawkBuildHash)
}

// countHoneypotEvents returns how many verification_events of the given kind
// exist for a job — the receipt check for pass/fail/skip paths.
func countVerificationEvents(t *testing.T, ctx context.Context, jobID uuid.UUID, kind string) int {
	t.Helper()
	var n int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM verification_events WHERE job_id=$1 AND kind=$2`, jobID, kind).Scan(&n); err != nil {
		t.Fatalf("count %s events: %v", kind, err)
	}
	return n
}

// TestHawkingHoneypotSeedFidelity proves the seed path itself: the row seedDemo
// wrote carries the REAL harness-captured answer + the producing class, the
// input OBJECT a worker's presigned GET will fetch is the exact harness chunk,
// re-seeding does not duplicate, and the probe is DISPATCHABLE (visible to
// AvailableSeedHoneypots — the activation createJob's injection path reads).
func TestHawkingHoneypotSeedFidelity(t *testing.T) {
	reset(t)
	ctx := context.Background()

	// Idempotency: a second full seedDemo run must not duplicate the probe.
	if err := seedDemo(ctx, itPool, itStorage); err != nil {
		t.Fatalf("re-running seedDemo: %v", err)
	}
	var n int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM honeypots WHERE job_type='batch_infer' AND input_ref=$1`,
		demoHoneypotInferRef).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("want exactly 1 hawking batch_infer honeypot row after re-seed, got %d", n)
	}

	// Round-trip: the verifier's own lookup returns the exact harness bytes +
	// the producing class in classKey format.
	known, class, err := itStore.GetHoneypotAnswer(ctx, "batch_infer", demoHoneypotInferRef)
	if err != nil {
		t.Fatalf("GetHoneypotAnswer: %v", err)
	}
	if !bytes.Equal(known, []byte(demoHoneypotHawkKnownAnswer)) {
		t.Fatalf("known answer does not round-trip the harness-captured document:\n  db:   %s\n  seed: %s",
			known, demoHoneypotHawkKnownAnswer)
	}
	if class != hawkClass() || class == "" {
		t.Fatalf("answer_class must be the producing class %q, got %q", hawkClass(), class)
	}

	// The input OBJECT exists and is byte-for-byte the harness chunk (a real
	// worker's presigned GET must serve exactly the prompts the answer was
	// recorded for — the 404/placeholder failure modes the embed honeypot's
	// history documents).
	obj, err := itStorage.GetObject(ctx, demoHoneypotInferRef)
	if err != nil {
		t.Fatalf("honeypot input object missing (a real worker would 404-retry forever): %v", err)
	}
	if !bytes.Equal(obj, []byte(demoHoneypotHawkInputJSONL)) {
		t.Fatalf("honeypot input object differs from the harness chunk:\n  object: %s\n  seed:   %s",
			obj, demoHoneypotHawkInputJSONL)
	}

	// Dispatchable: the injection path (createJob → AvailableSeedHoneypots)
	// can now draw a batch_infer probe — the coverage that did not exist
	// before this wave.
	// Pass the exact model + a max_tokens at/above the seed's floor so the
	// injection-time param/model guard (AvailableSeedHoneypots) draws the probe.
	hps, err := itStore.AvailableSeedHoneypots(ctx, "batch_infer", demoHoneypotHawkModel, demoHoneypotHawkMinMaxTokens, 10)
	if err != nil {
		t.Fatalf("AvailableSeedHoneypots: %v", err)
	}
	found := false
	for _, hp := range hps {
		if hp.InputRef == demoHoneypotInferRef {
			found = true
			if hp.AnswerClass != hawkClass() {
				t.Fatalf("dispatchable probe must carry the producing class, got %q", hp.AnswerClass)
			}
		}
	}
	if !found {
		t.Fatalf("the hawking honeypot must be dispatchable via AvailableSeedHoneypots; got %+v", hps)
	}
}

// TestHawkingHoneypotPassSameClass — (a): the EXACT known answer from a worker
// of the EXACT producing class passes with a real honeypot_pass receipt.
func TestHawkingHoneypotPassSameClass(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	// Real job + task rows: the honeypot_pass RECEIPT this test asserts is a
	// verification_events row FK-bound to the job.
	mustJobTask(t, jobID, taskID, true /*honeypot*/, false, demoHoneypotInferRef)
	repBefore := supplierRep(t)

	info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
		SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotInferRef,
		jobType: "batch_infer", engine: demoHoneypotHawkEngine, buildHash: demoHoneypotHawkBuildHash}
	out, err := itServer.verifier.verifyTaskResult(ctx, info,
		TaskCommit{TaskID: taskID}, []byte(demoHoneypotHawkKnownAnswer), nil)
	if err != nil || out != OutcomePass {
		t.Fatalf("hawking-class exact answer: out=%v err=%v (want pass)", out, err)
	}
	if n := countVerificationEvents(t, ctx, jobID, "honeypot_pass"); n != 1 {
		t.Fatalf("want 1 honeypot_pass receipt (the byte compare really ran), got %d", n)
	}
	if n := countVerificationEvents(t, ctx, jobID, "honeypot_fail"); n != 0 {
		t.Fatalf("want 0 honeypot_fail receipts, got %d", n)
	}
	if rep := supplierRep(t); rep < repBefore {
		t.Fatalf("an honest honeypot pass must never dock: rep %v -> %v", repBefore, rep)
	}
	var status string
	if err := itPool.QueryRow(ctx, `SELECT status FROM suppliers WHERE id=$1`, demoSupplierUUID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	if status != "active" {
		t.Fatalf("an honest honeypot pass must never quarantine: supplier status %q", status)
	}
}

// TestHawkingHoneypotFraudSameClass — (b): a WRONG answer from the producing
// class takes the full fraud path (dock + clawback + quarantine + requeue).
// The wrong bytes are a plausible, well-formed BatchInferResult with one
// flipped completion — not garbage — so this pins that the compare is
// byte-exact, not schema-shaped.
func TestHawkingHoneypotFraudSameClass(t *testing.T) {
	reset(t)
	ctx := context.Background()
	jobID, taskID := uuid.New(), uuid.New()
	mustJobTask(t, jobID, taskID, true /*honeypot*/, false, demoHoneypotInferRef)
	if err := itStore.InsertLedgerEntries(ctx, []LedgerEntry{{
		Kind: KindSupplierCredit, SupplierID: &demoSupplierUUID, TaskID: &taskID,
		AmountUSD: 0.005, PayoutStatus: PayoutHeld,
	}}); err != nil {
		t.Fatal(err)
	}

	wrong := []byte(`{"job_type":"batch_infer","model":"llama-3.2-1b-instruct-q4","completions":[{"index":0,"text":"The capital of France is Lyon.","tokens":7}]}`)
	info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
		SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotInferRef,
		jobType: "batch_infer", engine: demoHoneypotHawkEngine, buildHash: demoHoneypotHawkBuildHash}
	out, err := itServer.verifier.verifyTaskResult(ctx, info, TaskCommit{TaskID: taskID}, wrong, nil)
	if err != nil || out != OutcomeFail {
		t.Fatalf("hawking-class wrong answer: out=%v err=%v (want fail)", out, err)
	}
	if n := countVerificationEvents(t, ctx, jobID, "honeypot_fail"); n != 1 {
		t.Fatalf("want 1 honeypot_fail receipt, got %d", n)
	}
	var clawbacks int
	itPool.QueryRow(ctx, `SELECT count(*) FROM ledger_entries WHERE task_id=$1 AND kind='clawback'`, taskID).Scan(&clawbacks)
	if clawbacks != 1 {
		t.Fatalf("want 1 clawback, got %d", clawbacks)
	}
	var taskStatus string
	itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&taskStatus)
	if taskStatus != "retrying" {
		t.Fatalf("fraud task should requeue (retrying), got %q", taskStatus)
	}
	if rep := supplierRep(t); rep > 0.80 {
		t.Fatalf("honeypot fraud should dock hard (~0.75), got %v", rep)
	}
	var status string
	itPool.QueryRow(ctx, `SELECT status FROM suppliers WHERE id=$1`, demoSupplierUUID).Scan(&status)
	if status != "suspended" {
		t.Fatalf("honeypot fraud must quarantine, supplier status %q", status)
	}
}

// TestHawkingHoneypotCrossClassSkips — (c): a worker OUTSIDE the producing
// class is never byte-compared against the hawking answer, even when its
// bytes disagree. Two boundary cases:
//   - a candle-class worker (different engine, different build): the exact
//     "cross-engine honeypot would quarantine an honest worker" hazard;
//   - a hawking worker with an UNKNOWN build hash (""): an older binary that
//     never advertised one — unknown is its own class, never provably the
//     same kernels (docs/DETERMINISM_CLASS.md "Unknown build = its own class").
//
// In both cases the probe must SKIP: no honeypot receipt of either kind (a
// skipped check is never reported as a pass — BLACKHOLE), no dock, no
// quarantine, no requeue; the task falls through to the ordinary success path.
func TestHawkingHoneypotCrossClassSkips(t *testing.T) {
	cases := []struct {
		name      string
		engine    string
		buildHash string
	}{
		{"candle_class_worker", "candle", "29788fb25f948522"},
		{"hawking_unknown_build", demoHoneypotHawkEngine, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reset(t)
			ctx := context.Background()
			jobID, taskID := uuid.New(), uuid.New()
			mustJobTask(t, jobID, taskID, true /*honeypot*/, false, demoHoneypotInferRef)
			repBefore := supplierRep(t)

			// Bytes that DISAGREE with the hawking-seeded answer — a candle
			// worker's legitimately different greedy decode stands in here.
			disagreeing := []byte(`{"job_type":"batch_infer","model":"llama-3.2-1b-instruct-q4","completions":[{"index":0,"text":"Paris","tokens":2}]}`)
			info := &CommitTaskInfo{TaskID: taskID, JobID: jobID, WorkerID: demoWorkerUUID,
				SupplierID: demoSupplierUUID, IsHoneypot: true, InputRef: demoHoneypotInferRef,
				jobType: "batch_infer", engine: tc.engine, buildHash: tc.buildHash}
			out, err := itServer.verifier.verifyTaskResult(ctx, info, TaskCommit{TaskID: taskID}, disagreeing, nil)
			if err != nil || out != OutcomePass {
				t.Fatalf("cross-class probe must fall through to the normal path: out=%v err=%v", out, err)
			}
			// Receipts: NO honeypot event of either kind — never a fabricated
			// pass, never a wrongful fail.
			if n := countVerificationEvents(t, ctx, jobID, "honeypot_pass"); n != 0 {
				t.Fatalf("cross-class skip must not fabricate a honeypot_pass, got %d", n)
			}
			if n := countVerificationEvents(t, ctx, jobID, "honeypot_fail"); n != 0 {
				t.Fatalf("cross-class byte difference must never be a honeypot_fail, got %d", n)
			}
			// No dock, no quarantine, no requeue.
			if rep := supplierRep(t); rep < repBefore {
				t.Fatalf("cross-class skip must not dock: rep %v -> %v", repBefore, rep)
			}
			var status string
			itPool.QueryRow(ctx, `SELECT status FROM suppliers WHERE id=$1`, demoSupplierUUID).Scan(&status)
			if status != "active" {
				t.Fatalf("cross-class skip must not quarantine, supplier status %q", status)
			}
			var taskStatus string
			itPool.QueryRow(ctx, `SELECT status FROM tasks WHERE id=$1`, taskID).Scan(&taskStatus)
			if taskStatus == "retrying" {
				t.Fatal("cross-class skip must not requeue the task")
			}
		})
	}
}

// TestInsertHoneypotRefusesClassBlindByteExact — (d): the REAL store path
// (InsertHoneypot → validateHoneypotSeed) refuses a class-blind byte-exact
// seed and writes nothing. The pure-function behavior is pinned at unit level
// (honeypot_class_test.go); this pins it against the live Postgres write path.
func TestInsertHoneypotRefusesClassBlindByteExact(t *testing.T) {
	reset(t)
	ctx := context.Background()
	ref := "honeypots/batch_infer/blank-class-refusal-test/input.jsonl"
	err := itStore.InsertHoneypot(ctx, "batch_infer", ref, []byte(`{"x":1}`), "")
	if !errors.Is(err, errHoneypotBlankClass) {
		t.Fatalf("class-blind byte-exact seed must be refused, got %v", err)
	}
	var n int
	itPool.QueryRow(ctx, `SELECT count(*) FROM honeypots WHERE input_ref=$1`, ref).Scan(&n)
	if n != 0 {
		t.Fatalf("refused seed must write nothing, got %d rows", n)
	}
}
