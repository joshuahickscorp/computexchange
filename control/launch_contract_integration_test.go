//go:build integration

package main

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestUnderfundedPipelineRejectedBeforePersistenceOrJobCreation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	var pipelinesBefore, jobsBefore int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM pipelines`).Scan(&pipelinesBefore); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM jobs`).Scan(&jobsBefore); err != nil {
		t.Fatal(err)
	}
	code, body := req(t, "POST", "/v1/pipelines", map[string]any{
		"name": "underfunded",
		"stages": []map[string]any{{
			"op": "embed", "model": "all-minilm-l6-v2", "from": "input",
		}},
		"input":   "{\"text\":\"bounded\"}\n",
		"max_usd": 0.000000001,
	}, buyerKey(), jsonCT())
	if code != 409 {
		t.Fatalf("underfunded pipeline: status=%d body=%s", code, body)
	}
	var pipelinesAfter, jobsAfter int
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM pipelines`).Scan(&pipelinesAfter); err != nil {
		t.Fatal(err)
	}
	if err := itPool.QueryRow(ctx, `SELECT count(*) FROM jobs`).Scan(&jobsAfter); err != nil {
		t.Fatal(err)
	}
	if pipelinesAfter != pipelinesBefore || jobsAfter != jobsBefore {
		t.Fatalf("underfunded admission had side effects: pipelines %d->%d jobs %d->%d",
			pipelinesBefore, pipelinesAfter, jobsBefore, jobsAfter)
	}
}

func TestReserveIntakeLaunchConcurrentSingleWinner(t *testing.T) {
	reset(t)
	ctx := context.Background()
	intakeID := uuid.New()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO intakes (id,buyer_id,status,pattern,pipeline) VALUES ($1,$2,'detected','tabular-text',$3)`,
		intakeID, demoBuyerUUID, []byte(`{"supported":true,"stages":[]}`),
	); err != nil {
		t.Fatal(err)
	}
	planned, err := json.Marshal(DetectedPipeline{
		Pattern: "tabular-text", Supported: true, Launchable: true,
		Stages: []PipelineStage{{
			Op: "embed", Model: "all-minilm-l6-v2",
			LaunchContract: &LaunchContract{MaxUSD: 2.5},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	const contenders = 32
	var wins atomic.Int32
	errs := make(chan error, contenders)
	var wg sync.WaitGroup
	for i := 0; i < contenders; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			reserved, err := itStore.ReserveIntakeLaunch(ctx, demoBuyerUUID, intakeID, planned)
			if err != nil {
				errs <- err
				return
			}
			if reserved {
				wins.Add(1)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
	if got := wins.Load(); got != 1 {
		t.Fatalf("concurrent intake reservations produced %d winners, want exactly 1", got)
	}
	var status string
	var persisted []byte
	if err := itPool.QueryRow(ctx, `SELECT status,pipeline FROM intakes WHERE id=$1`, intakeID).Scan(&status, &persisted); err != nil {
		t.Fatal(err)
	}
	if status != "launching" {
		t.Fatalf("reserved intake status=%q, want launching", status)
	}
	var got DetectedPipeline
	if err := json.Unmarshal(persisted, &got); err != nil {
		t.Fatal(err)
	}
	if got.Stages[0].LaunchContract == nil || got.Stages[0].LaunchContract.MaxUSD != 2.5 {
		t.Fatalf("persisted launch contract=%+v", got.Stages[0].LaunchContract)
	}
}

func TestBoundedSynchronousReadRejectsOversizedS3Object(t *testing.T) {
	ctx := context.Background()
	key := "tests/bounded-sync/" + uuid.NewString()
	if err := itStorage.PutObject(ctx, key, []byte("12345"), "application/octet-stream"); err != nil {
		t.Fatal(err)
	}
	reader, err := itStorage.GetObjectReader(ctx, key)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := readAndCloseBounded(reader, 4); got != nil || !errors.Is(err, errSynchronousInputTooLarge) {
		t.Fatalf("oversized S3 reader: got=%q err=%v", got, err)
	}
}

func TestWorkflowStageAdvisoryLockSerializesReplicas(t *testing.T) {
	ctx := context.Background()
	workflowID := uuid.New()
	unlockFirst, err := itStore.LockWorkflowStage(ctx, pipelineStageLockNamespace, workflowID, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer unlockFirst()

	acquired := make(chan func(), 1)
	errs := make(chan error, 1)
	go func() {
		unlock, err := itStore.LockWorkflowStage(ctx, pipelineStageLockNamespace, workflowID, 1)
		if err != nil {
			errs <- err
			return
		}
		acquired <- unlock
	}()
	select {
	case unlock := <-acquired:
		unlock()
		t.Fatal("second replica acquired the same workflow stage before the first released it")
	case err := <-errs:
		t.Fatal(err)
	case <-time.After(100 * time.Millisecond):
		// Expected: the second replica is blocked on the session advisory lock.
	}

	unlockFirst()
	select {
	case unlock := <-acquired:
		unlock()
	case err := <-errs:
		t.Fatal(err)
	case <-time.After(5 * time.Second):
		t.Fatal("second replica did not acquire workflow stage after release")
	}
}
