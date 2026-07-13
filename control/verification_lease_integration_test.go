//go:build integration

package main

import (
	"context"
	"sync"
	"testing"
	"time"
)

type blockingVerificationProbe struct {
	once    sync.Once
	reached chan struct{}
	release chan struct{}
}

func (p *blockingVerificationProbe) Reach(ctx context.Context, boundary RecoveryBoundary) {
	if boundary != BoundaryVerifyAfterStagingRead {
		return
	}
	p.once.Do(func() {
		close(p.reached)
		select {
		case <-p.release:
		case <-ctx.Done():
		}
	})
}

func TestVerificationDrainClaimsJustInTimeAndRenewsActiveLease(t *testing.T) {
	reset(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	enqueue := func() verificationProcessorFixture {
		fixture := newVerificationProcessorFixture(t)
		if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
			t.Fatalf("put verification staging result: %v", err)
		}
		if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
			t.Fatalf("durably enqueue verification work: %v", err)
		}
		return fixture
	}
	first := enqueue()
	second := enqueue()

	probe := &blockingVerificationProbe{reached: make(chan struct{}), release: make(chan struct{})}
	processor := NewVerificationProcessor(itStore, itStorage, NewVerifier(itStore).WithStorage(itStorage)).
		WithRecoveryProbe(probe)
	processor.leaseDuration = 600 * time.Millisecond
	processor.leaseRenewal = 100 * time.Millisecond

	done := make(chan error, 1)
	go func() { done <- processor.Drain(ctx, 2) }()
	select {
	case <-probe.reached:
	case <-ctx.Done():
		t.Fatal("processor did not reach the blocked staging-read boundary")
	}

	// Wait beyond the original lease. The active row must still be live because its
	// heartbeat renewed it, while the second row must remain pending because Drain
	// claims work immediately before processing rather than leasing a batch up front.
	time.Sleep(3 * processor.leaseDuration)
	var leased, pending, live int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE status='leased'),
		       count(*) FILTER (WHERE status='pending'),
		       count(*) FILTER (WHERE status='leased' AND lease_expires_at>now())
		  FROM verification_work
		 WHERE task_id IN ($1,$2)`, first.Dispatch.TaskID, second.Dispatch.TaskID).
		Scan(&leased, &pending, &live); err != nil {
		t.Fatalf("inspect verification leases: %v", err)
	}
	if leased != 1 || pending != 1 || live != 1 {
		t.Fatalf("just-in-time renewed leases = leased:%d pending:%d live:%d, want 1/1/1", leased, pending, live)
	}

	close(probe.release)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("drain after renewed lease: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("drain did not finish after releasing the probe")
	}
	var terminal int
	if err := itPool.QueryRow(ctx, `
		SELECT count(*) FROM verification_work
		 WHERE task_id IN ($1,$2) AND status='terminal'`, first.Dispatch.TaskID, second.Dispatch.TaskID).
		Scan(&terminal); err != nil {
		t.Fatal(err)
	}
	if terminal != 2 {
		t.Fatalf("terminal verification rows = %d, want 2", terminal)
	}
}
