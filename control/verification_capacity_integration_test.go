//go:build integration

package main

import (
	"context"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestVerificationTinyPoolConcurrentProcessorsDoNotDeadlock(t *testing.T) {
	reset(t)
	ctx := context.Background()
	fixture := newVerificationProcessorFixture(t)
	if err := itStorage.PutObject(ctx, fixture.Dispatch.ResultKey, fixture.Result, "application/json"); err != nil {
		t.Fatalf("put tiny-pool verification result: %v", err)
	}
	if _, err := itStore.CommitTask(ctx, fixture.Dispatch.TaskID, demoWorkerUUID, fixture.Commit); err != nil {
		t.Fatalf("enqueue tiny-pool verification work: %v", err)
	}

	cfg, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if err != nil {
		t.Fatalf("parse tiny-pool config: %v", err)
	}
	cfg.MaxConns = 2
	cfg.MinConns = 0
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatalf("open tiny pool: %v", err)
	}
	defer pool.Close()
	store := NewStore(pool)
	if got := cap(store.verificationResources.processSlots); got != 1 {
		t.Fatalf("two-connection pool verification slots=%d, want 1", got)
	}

	processors := []*VerificationProcessor{
		NewVerificationProcessor(store, itStorage, NewVerifier(store).WithStorage(itStorage)),
		NewVerificationProcessor(store, itStorage, NewVerifier(store).WithStorage(itStorage)),
	}
	processCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	start := make(chan struct{})
	results := make(chan struct {
		result VerificationProcessResult
		err    error
	}, len(processors))
	var wg sync.WaitGroup
	for _, processor := range processors {
		processor := processor
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			result, err := processor.ProcessAttempt(processCtx, fixture.Dispatch.TaskID, 0)
			results <- struct {
				result VerificationProcessResult
				err    error
			}{result, err}
		}()
	}
	close(start)
	wg.Wait()
	close(results)
	if processCtx.Err() != nil {
		t.Fatalf("tiny-pool processors deadlocked: %v", processCtx.Err())
	}
	terminal := 0
	for got := range results {
		if got.err != nil {
			t.Fatalf("tiny-pool processor: %v", got.err)
		}
		if !got.result.Pending {
			terminal++
		}
	}
	if terminal == 0 {
		t.Fatal("tiny-pool run produced only pending results; one reserved-headroom processor must converge")
	}

	state := readVerificationProcessorState(t, fixture.Dispatch.TaskID)
	if state.TaskStatus != "complete" || state.WorkStatus != VerificationWorkTerminal || state.VerdictRows != 1 {
		t.Fatalf("tiny-pool verification did not converge exactly once: %+v", state)
	}
}
