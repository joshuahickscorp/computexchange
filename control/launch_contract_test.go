package main

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"sync"
	"testing"
)

// Item 1: the LaunchContract propagates the buyer's budget/verification/routing onto a
// per-stage jobSubmit, so an indirectly-launched job carries the SAME guarantees as a
// direct submission. applyTo stamps them; launchContractFrom round-trips them.
func TestLaunchContractApplyTo(t *testing.T) {
	c := LaunchContract{QuoteID: "q_1", MaxUSD: 12.5, MinReputation: 0.8, PrivatePool: true}
	stage := jobSubmit{JobType: JobType{Type: "embed"}, Model: ModelRef{Kind: "gguf", Ref: "m"}, Tier: "batch"}
	got := c.applyTo(stage)
	if got.QuoteID != "q_1" || got.MaxUSD != 12.5 || got.MinReputation != 0.8 || !got.PrivatePool {
		t.Fatalf("contract fields must propagate to the stage submission; got %+v", got)
	}
	if got.JobType.Type != "embed" || got.Model.Ref != "m" || got.Tier != "batch" {
		t.Fatal("the stage's own workload fields must be preserved")
	}
	rt := launchContractFrom(got)
	if !reflect.DeepEqual(rt, c) {
		t.Fatalf("launchContractFrom must round-trip the contract; got %+v want %+v", rt, c)
	}
}

func TestAllocateAggregateMaxUSDDisjointCaps(t *testing.T) {
	caps, err := allocateAggregateMaxUSD(10, []float64{1, 3, 6})
	if err != nil {
		t.Fatal(err)
	}
	if len(caps) != 3 || caps[0] != 1 || caps[1] != 3 || caps[2] != 6 {
		t.Fatalf("weighted caps = %v, want [1 3 6]", caps)
	}
	var sum float64
	for _, cap := range caps {
		if cap <= 0 {
			t.Fatalf("stage cap must stay positive, got %v", caps)
		}
		sum += cap
	}
	if math.Abs(sum-10) > 1e-12 {
		t.Fatalf("stage caps sum to %v, want aggregate 10", sum)
	}

	even, err := allocateAggregateMaxUSD(9, []float64{4, 0, 2})
	if err != nil || !reflect.DeepEqual(even, []float64{3, 3, 3}) {
		t.Fatalf("invalid weights must safely fall back to even positive caps: caps=%v err=%v", even, err)
	}
	for _, total := range []float64{0, -1, math.NaN(), math.Inf(1)} {
		if _, err := allocateAggregateMaxUSD(total, []float64{1}); err == nil {
			t.Fatalf("unsafe aggregate %v accepted", total)
		}
	}
}

func TestResolveAggregateMaxUSDRejectsUnderfundedWorkflowBeforePersistence(t *testing.T) {
	if got, err := resolveAggregateMaxUSD(0, 12.5); err != nil || got != 12.5 {
		t.Fatalf("server-derived aggregate=(%v,%v), want (12.5,nil)", got, err)
	}
	if got, err := resolveAggregateMaxUSD(15, 12.5); err != nil || got != 15 {
		t.Fatalf("buyer aggregate=(%v,%v), want (15,nil)", got, err)
	}
	for _, tc := range []struct {
		requested float64
		required  float64
	}{
		{12.49, 12.5},
		{-1, 12.5},
		{1, 0},
		{math.NaN(), 12.5},
	} {
		if _, err := resolveAggregateMaxUSD(tc.requested, tc.required); err == nil {
			t.Fatalf("unsafe aggregate accepted: requested=%v required=%v", tc.requested, tc.required)
		}
	}
}

func TestAllocateAggregateMaxUSDConcurrentDeterminism(t *testing.T) {
	const workers = 64
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			caps, err := allocateAggregateMaxUSD(17.25, []float64{2, 5, 1})
			if err != nil {
				errs <- err
				return
			}
			if math.Abs(caps[0]+caps[1]+caps[2]-17.25) > 1e-12 {
				errs <- fmt.Errorf("concurrent allocation exceeded aggregate: %v", caps)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
}

func TestLaunchContractPersistsAcrossWorkflowStageJSON(t *testing.T) {
	want := LaunchContract{
		QuoteID: "q_one", MaxUSD: 4.25, MinReputation: 0.75, PrivatePool: true,
		Verification: VerificationPolicy{RedundancyFrac: 0.2, HoneypotFrac: 0.1, PayoutHoldSecs: 90},
	}
	for _, tc := range []struct {
		name string
		in   any
		out  any
		get  func(any) *LaunchContract
	}{
		{
			name: "user pipeline",
			in:   pipelineStage{Op: "embed", Model: "all-minilm-l6-v2", From: "previous", LaunchContract: &want},
			out:  &pipelineStage{},
			get:  func(v any) *LaunchContract { return v.(*pipelineStage).LaunchContract },
		},
		{
			name: "detected intake",
			in:   PipelineStage{Op: "embed", Model: "all-minilm-l6-v2", From: "previous", LaunchContract: &want},
			out:  &PipelineStage{},
			get:  func(v any) *LaunchContract { return v.(*PipelineStage).LaunchContract },
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			blob, err := json.Marshal(tc.in)
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(blob, tc.out); err != nil {
				t.Fatal(err)
			}
			if got := tc.get(tc.out); got == nil || !reflect.DeepEqual(*got, want) {
				t.Fatalf("launch contract did not survive JSON persistence: got=%+v want=%+v", got, want)
			}
		})
	}
}
