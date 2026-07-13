package main

import (
	"runtime"
	"sync"
	"testing"
)

func TestVerificationProcessSlotsReserveDatabaseHeadroom(t *testing.T) {
	for _, tc := range []struct {
		maxConns int32
		want     int
		reserve  int32
	}{
		{1, 0, 0},
		{2, 1, 1},
		{3, 1, 2},
		{4, 2, 2},
		{20, 18, 2},
	} {
		budget := newVerificationResourceBudget(tc.maxConns, 100)
		if got := cap(budget.processSlots); got != tc.want || budget.reservedDB != tc.reserve {
			t.Fatalf("maxConns=%d slots/reserved=%d/%d, want %d/%d",
				tc.maxConns, got, budget.reservedDB, tc.want, tc.reserve)
		}
	}
}

func TestWeightedVerificationBytesNeverExceedCeilingUnderConcurrentLargeArtifacts(t *testing.T) {
	const (
		ceiling = int64(100)
		weight  = int64(40)
		readers = 64
	)
	budget := newWeightedByteBudget(ceiling)
	start := make(chan struct{})
	release := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if !budget.tryAcquire(weight) {
				return
			}
			<-release
			budget.release(weight)
		}()
	}
	close(start)

	// Wait until every goroutine has either acquired or observed the full budget.
	// They cannot finish while holding a reservation because release is still shut.
	for {
		inUse, peak, capBytes := budget.snapshot()
		if inUse == 2*weight {
			if peak > capBytes || peak != 2*weight {
				t.Fatalf("weighted peak=%d ceiling=%d want=%d", peak, capBytes, 2*weight)
			}
			break
		}
		runtime.Gosched()
	}
	close(release)
	wg.Wait()
	inUse, peak, capBytes := budget.snapshot()
	if inUse != 0 || peak > capBytes {
		t.Fatalf("weighted budget final inUse/peak/ceiling=%d/%d/%d", inUse, peak, capBytes)
	}
}

func TestVerificationMemoryTrackerReleasesMarksAndOperation(t *testing.T) {
	budget := newWeightedByteBudget(100)
	ctx, release := withVerificationMemoryTracker(t.Context(), budget)
	if err := reserveVerificationMemory(ctx, 60); err != nil {
		t.Fatal(err)
	}
	mark := verificationMemoryMark(ctx)
	if err := reserveVerificationMemory(ctx, 40); err != nil {
		t.Fatal(err)
	}
	if err := reserveVerificationMemory(ctx, 1); err != ErrVerificationResourceBusy {
		t.Fatalf("over-cap reservation=%v, want %v", err, ErrVerificationResourceBusy)
	}
	releaseVerificationMemoryToMark(ctx, mark)
	if inUse, _, _ := budget.snapshot(); inUse != 60 {
		t.Fatalf("after mark release inUse=%d, want 60", inUse)
	}
	release()
	if inUse, _, _ := budget.snapshot(); inUse != 0 {
		t.Fatalf("after operation release inUse=%d, want 0", inUse)
	}
}
