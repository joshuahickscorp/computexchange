package main

import (
	"reflect"
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
