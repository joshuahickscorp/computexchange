package main

import (
	"fmt"
	"math"
)

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
	QuoteID       string             `json:"quote_id,omitempty"`
	MaxUSD        float64            `json:"max_usd,omitempty"`
	MinReputation float32            `json:"min_reputation,omitempty"`
	PrivatePool   bool               `json:"private_pool,omitempty"`
	Verification  VerificationPolicy `json:"verification,omitempty"`
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

// allocateAggregateMaxUSD turns one buyer-visible pipeline cap into disjoint
// per-stage caps. Because every job has its own budget governor, copying the full
// aggregate onto every stage would allow an N-stage pipeline to spend N times what
// the buyer approved. The returned caps are all positive and sum to total (within
// floating-point precision), so concurrently running stages still share one hard
// aggregate ceiling. Positive weights preserve the quote's relative stage costs;
// invalid/zero weights fall back to an even allocation rather than creating an
// uncapped (max_usd=0) stage.
func allocateAggregateMaxUSD(total float64, weights []float64) ([]float64, error) {
	if math.IsNaN(total) || math.IsInf(total, 0) || total <= 0 {
		return nil, fmt.Errorf("aggregate max_usd must be a finite positive number")
	}
	if len(weights) == 0 {
		return nil, fmt.Errorf("cannot allocate max_usd across zero stages")
	}

	normalized := append([]float64(nil), weights...)
	weightSum := 0.0
	for _, weight := range normalized {
		if math.IsNaN(weight) || math.IsInf(weight, 0) || weight <= 0 {
			weightSum = 0
			break
		}
		weightSum += weight
	}
	if weightSum <= 0 || math.IsInf(weightSum, 0) {
		weightSum = float64(len(normalized))
		for i := range normalized {
			normalized[i] = 1
		}
	}

	caps := make([]float64, len(normalized))
	allocated := 0.0
	for i := range normalized {
		if i == len(normalized)-1 {
			caps[i] = total - allocated
		} else {
			caps[i] = total * (normalized[i] / weightSum)
			allocated += caps[i]
		}
		if caps[i] <= 0 || math.IsNaN(caps[i]) || math.IsInf(caps[i], 0) {
			return nil, fmt.Errorf("aggregate max_usd is too small to allocate a positive cap to every stage")
		}
	}
	return caps, nil
}

// resolveAggregateMaxUSD treats zero as "use the server quote" and otherwise
// requires the buyer's aggregate cap to cover every stage's reserved quote maximum.
// Rejecting here, before workflow persistence, avoids an orphan/partial pipeline
// whose first stage is created only for a later stage to fail budget admission.
func resolveAggregateMaxUSD(requested, required float64) (float64, error) {
	if math.IsNaN(required) || math.IsInf(required, 0) || required <= 0 {
		return 0, fmt.Errorf("required aggregate stage reserve is not a finite positive amount")
	}
	if math.IsNaN(requested) || math.IsInf(requested, 0) || requested < 0 {
		return 0, fmt.Errorf("aggregate max_usd must be a finite non-negative number")
	}
	if requested == 0 {
		return required, nil
	}
	if requested < required {
		return 0, fmt.Errorf("aggregate max_usd %.6f is below the required stage reserve %.6f", requested, required)
	}
	return requested, nil
}
