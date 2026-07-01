package main

// launch_contract.go — the LaunchContract (backlog P0 items 1-5).
//
// Every job-submit path (direct /v1/jobs, intake launch, pipeline stage, OpenAI Batch)
// funnels through Server.createJob with a jobSubmit. A DIRECT submission carries the
// buyer's budget cap, verification policy, reputation floor, private-pool routing, and
// quote binding; the INDIRECT paths historically built their jobSubmit inline and
// DROPPED all of those, so a launched/pipelined/batch job silently lost the buyer's
// spend cap and routing guarantees. LaunchContract is the shared carrier that every
// indirect path stamps onto each stage's jobSubmit, so the guarantees propagate
// uniformly. Pure + unit-tested so the propagation cannot silently regress.

// LaunchContract is the shared budget / verification / routing contract that every
// job-submit path must propagate. The stage supplies the workload (JobType/Model/Tier/
// Input); the contract supplies everything that governs spend and trust.
type LaunchContract struct {
	QuoteID       string
	MaxUSD        float64
	MinReputation float32
	PrivatePool   bool
	Verification  VerificationPolicy
}

// applyTo stamps the contract's fields onto a per-stage jobSubmit and returns it, so a
// caller writes `contract.applyTo(jobSubmit{JobType: ..., Model: ..., Input: ...})` and
// the budget/verification/routing are guaranteed present. Pure.
func (c LaunchContract) applyTo(sub jobSubmit) jobSubmit {
	sub.QuoteID = c.QuoteID
	sub.MaxUSD = c.MaxUSD
	sub.MinReputation = c.MinReputation
	sub.PrivatePool = c.PrivatePool
	sub.Verification = c.Verification
	return sub
}

// launchContractFrom extracts the contract carried by a direct jobSubmit, so a path that
// already holds a full submission can forward the SAME contract to chained/downstream
// stages (stage 1 inherits stage 0's budget + verification).
func launchContractFrom(sub jobSubmit) LaunchContract {
	return LaunchContract{
		QuoteID:       sub.QuoteID,
		MaxUSD:        sub.MaxUSD,
		MinReputation: sub.MinReputation,
		PrivatePool:   sub.PrivatePool,
		Verification:  sub.Verification,
	}
}
