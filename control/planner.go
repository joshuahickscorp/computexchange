package main

// planner.go — the speed-optimal data-parallel fan-out planner (Speed Lane wave
// 1B, docs/research/SPEED_LANE_GOAL_PROMPT.md target item 2 — "THE MOAT").
//
// The marketplace's structural edge is that an embarrassingly-parallel batch job
// split across a heterogeneous fleet can beat one big GPU on WALL-CLOCK — but
// only if the split is speed-optimal. Before this file, the split was not:
// chunks were sized once at submit from a STATIC per-job-type throughput map
// (api.go jobTypeThroughput × targetTaskSecs), the ETA used a blunt
// ceil(queue/workers)×perTaskSecs aggregate with no heterogeneity or cold-load
// term, and nothing sized the fan-out WIDTH to the job's shape at all.
//
// This file is the PURE math: no database, no clock, no randomness — every
// function here is deterministic in its inputs and unit-tested in
// planner_test.go. The DB wiring (turning worker_tps_cache / benchmark_results
// load_ms / worker_model_state rows into a []PlannerWorker) lives in
// benchmark.go (FleetRateSnapshot) + api.go (adaptiveSplitSizeLive,
// plannerETASecs); the endgame-racing sweep the plan's tail depends on lives in
// workers.go (raceEndgameTails).
//
// The model. A worker w that is assigned k items of a job completes at
//
//	completion(w,k) = startCost(w) + k / rate(w)
//	startCost(w)    = plannerChunkOverheadSecs + (0 if warm else coldLoadSecs(w))
//
// — the cold-load penalty is the REAL, measured cost this codebase already
// persists but never read at planning time (benchmark_results.load_ms; a cold
// GGUF fetch is minutes, a warm pool hit is ~0). The planner picks the
// assignment minimizing max_w completion(w,k_w) subject to Σk_w = items: the
// classic divisible-load makespan problem with per-worker start costs. The
// optimum has a water-filling shape — there is a critical finish time T* such
// that every worker with startCost < T* runs exactly until T* and every worker
// with startCost ≥ T* is left OUT of the plan entirely. That exclusion IS
// adaptive-N: a small job on a fleet with one warm fast node yields width 1
// (fanning to a cold node would RAISE wall-clock past its cold load), while a
// huge job amortizes any number of cold loads and goes wide. T* is found by
// monotone bisection (capacity(T) = Σ_w max(0, (T-startCost(w))·rate(w)) is
// continuous and nondecreasing in T), then made integral by largest-remainder
// rounding — deterministic throughout, ties broken by worker id.
//
// Honesty labels: every number a Plan carries is MODELED from measured inputs
// (rates from real benchmarks, cold loads from real load_ms where present). A
// Plan never claims a measured speedup; the L2 integration proof measures the
// real control plane and the L3 multi-node run is the owner's (see
// docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md).

import (
	"os"
	"sort"
	"sync/atomic"

	"github.com/google/uuid"
)

// fanoutPlannerEnabled gates every wave-1B planner-backed path: live-fleet
// chunk sizing (adaptiveSplitSizeLive), the planner ETA (plannerETASecs), and
// endgame racing (raceEndgameTails). Default ON; the environment variable
// CX_DISABLE_FANOUT_PLANNER=1 (any non-empty value) reverts every path to the
// exact pre-wave behavior — the same switch the L2 integration proof uses to
// measure planner-vs-baseline on one identical harness, and an operator
// escape hatch if the planner ever misbehaves in production.
var fanoutPlannerEnabled atomic.Bool

func init() {
	fanoutPlannerEnabled.Store(os.Getenv("CX_DISABLE_FANOUT_PLANNER") == "")
}

const (
	// plannerChunkOverheadSecs is the MODELED fixed per-worker dispatch cost of
	// participating in a job at all: one claim round-trip, the chunk input GET,
	// the result PUT, and the commit round-trip. It is what stops the planner
	// fanning a 6-item job across 40 workers: past the point where a worker's
	// share is a couple of seconds, adding another worker buys less than this
	// overhead costs. 2s is deliberately conservative for a WAN fleet (local
	// measurements are hundreds of ms; strangers' home links are not).
	plannerChunkOverheadSecs = 2.0

	// plannerDefaultColdLoadSecs is the modeled cold-load penalty for a worker
	// that does NOT have the job's model warm and has NO measured
	// benchmark_results.load_ms row to speak for it. Real measured cold GGUF
	// fetch+load on this project ranges from seconds (cached file, warm disk)
	// to minutes (multi-GB first download); 120s sits honestly in that band
	// without shielding cold workers from exclusion on small jobs. A real
	// load_ms row ALWAYS wins over this default (never fabricate when a
	// measurement exists).
	plannerDefaultColdLoadSecs = 120.0

	// plannerConservativeRateFactor degrades every worker's measured rate for
	// the conservative wall-clock band. Grounded in this project's real
	// measurements: the M3 Pro's serial rate spread is 91–111 tok/s (±10%) and
	// the thermal facet measured sustained throughput 20–40% below the
	// 20-second peak probe on fanless silicon. Planning at 75% of the measured
	// rate covers both without being uselessly pessimistic.
	plannerConservativeRateFactor = 0.75

	// plannerMinFleetSamples is how many live eligible workers must have a REAL
	// measured rate (worker_tps_cache tps > 0) for the job type before any
	// planner-backed path overrides the static fallback. Below it the cache is
	// too thin to say anything about the fleet, and the pre-existing static
	// map / blunt-average formulas remain in force (the honest fallback, not a
	// silent guess).
	plannerMinFleetSamples = 3
)

// PlannerWorker is the planner's view of one live, eligible worker.
// ItemsPerSec is the worker's measured rate for THIS job, already converted to
// job items per second by the caller (the planner is unit-agnostic: the L1 sim
// feeds tok/s÷tokens-per-item, the ETA path feeds task-units/s). ColdLoadSecs
// is the cold model-load penalty paid once iff !Warm — measured
// (benchmark_results.load_ms) where a row exists, plannerDefaultColdLoadSecs
// otherwise. A Throttled or zero-rate worker is never planned onto.
type PlannerWorker struct {
	ID           uuid.UUID
	ItemsPerSec  float64
	Warm         bool
	ColdLoadSecs float64
	Throttled    bool
}

// startCostSecs is the fixed cost this worker pays before its first item
// completes: the per-worker dispatch overhead plus the cold load iff cold.
func (w PlannerWorker) startCostSecs() float64 {
	c := plannerChunkOverheadSecs
	if !w.Warm {
		c += w.ColdLoadSecs
	}
	return c
}

// PlannerJob is the job shape being planned. Items is the only input the math
// uses; JobType/ModelRef ride along for the decision log line.
type PlannerJob struct {
	Items    int
	JobType  string
	ModelRef string
}

// PlannerAssignment is one worker's share of the plan.
type PlannerAssignment struct {
	WorkerID     uuid.UUID
	Items        int
	ExpectedSecs float64 // startCost + Items/rate, modeled
}

// Plan is the planner's output. Width==0 means "no plan" (empty/ineligible
// fleet or an empty job) and every caller falls back to pre-wave behavior.
// All *Secs numbers are MODELED from measured inputs — see the file comment.
type Plan struct {
	Width       int                 // recommended fan-out width (workers with Items > 0)
	Assignments []PlannerAssignment // per-worker shares, deterministic order
	// WallClockP50Secs is the modeled makespan of the assignment at the
	// measured rates (max over assigned workers of ExpectedSecs).
	WallClockP50Secs float64
	// WallClockConservativeSecs re-costs the SAME assignment with every rate
	// degraded to plannerConservativeRateFactor of measured — the honest band,
	// not a second optimism.
	WallClockConservativeSecs float64
	// SingleNodeSecs is the modeled wall-clock of the BEST single worker in the
	// snapshot running the whole job alone (the reference this fan-out is
	// compared against), and ModeledSpeedupVsSingle = SingleNodeSecs /
	// WallClockP50Secs. Explicitly MODELED — never reported as a measurement.
	SingleNodeSecs         float64
	ModeledSpeedupVsSingle float64
}

// planCapacity is the total items the eligible fleet can finish by time t:
// Σ_w max(0, (t - startCost(w)) · rate(w)). Continuous and nondecreasing in t —
// the monotone function the bisection in PlanFanout inverts.
func planCapacity(ws []PlannerWorker, t float64) float64 {
	var c float64
	for _, w := range ws {
		if dt := t - w.startCostSecs(); dt > 0 {
			c += dt * w.ItemsPerSec
		}
	}
	return c
}

// PlanFanout computes the speed-optimal fan-out plan for job over fleet.
// Deterministic: identical inputs (in any order) produce the identical Plan.
// See the file comment for the model and the water-filling/adaptive-N argument.
func PlanFanout(job PlannerJob, fleet []PlannerWorker) Plan {
	// Eligible workers only: a throttled worker is pausing for memory pressure
	// (never planned onto — mirrors the claim path's hard filter) and a
	// zero/negative rate carries no measurement to plan with.
	ws := make([]PlannerWorker, 0, len(fleet))
	for _, w := range fleet {
		if w.Throttled || w.ItemsPerSec <= 0 {
			continue
		}
		ws = append(ws, w)
	}
	if job.Items <= 0 || len(ws) == 0 {
		return Plan{}
	}
	// Canonical deterministic order regardless of input order: cheapest start
	// first, then fastest, then id — this is also the largest-remainder
	// tie-break order below.
	sort.Slice(ws, func(i, j int) bool {
		si, sj := ws[i].startCostSecs(), ws[j].startCostSecs()
		if si != sj {
			return si < sj
		}
		if ws[i].ItemsPerSec != ws[j].ItemsPerSec {
			return ws[i].ItemsPerSec > ws[j].ItemsPerSec
		}
		return ws[i].ID.String() < ws[j].ID.String()
	})

	items := float64(job.Items)

	// Upper bound: the best single worker running everything is always a
	// feasible schedule, so T* can never exceed the best single-node time.
	single := singleNodeSecs(ws, items)

	// Bisection for the critical finish time T*: smallest t with
	// planCapacity(t) >= items. 200 halvings of [0, single] leave floating-point
	// noise only.
	lo, hi := 0.0, single
	for i := 0; i < 200; i++ {
		mid := (lo + hi) / 2
		if planCapacity(ws, mid) >= items {
			hi = mid
		} else {
			lo = mid
		}
	}
	tStar := hi

	// Fractional water-fill at T*, then largest-remainder integer rounding.
	// Workers with startCost >= T* get share 0 — the adaptive-N exclusion: the
	// plan REFUSES to fan onto a worker whose cold load + overhead alone would
	// exceed the finish time the rest of the fleet achieves without it.
	type share struct {
		idx   int
		whole int
		frac  float64
	}
	shares := make([]share, len(ws))
	assignedWhole := 0
	for i, w := range ws {
		f := 0.0
		if dt := tStar - w.startCostSecs(); dt > 0 {
			f = dt * w.ItemsPerSec
		}
		wh := int(f)
		shares[i] = share{idx: i, whole: wh, frac: f - float64(wh)}
		assignedWhole += wh
	}
	remainder := job.Items - assignedWhole
	if remainder < 0 {
		// Floating-point overshoot (capacity a hair above items): trim from the
		// smallest-fraction end, never below zero. Deterministic.
		remainder = 0
		total := 0
		for i := range shares {
			total += shares[i].whole
		}
		for i := len(shares) - 1; i >= 0 && total > job.Items; i-- {
			trim := total - job.Items
			if trim > shares[i].whole {
				trim = shares[i].whole
			}
			shares[i].whole -= trim
			total -= trim
		}
	}
	if remainder > 0 {
		// Largest fractional remainder first; ties by the canonical worker
		// order (stable sort over the already-canonical slice). Only workers
		// the water-fill actually included (frac or whole > 0) receive
		// remainder items — an excluded worker (startCost >= T*) must never be
		// handed work by rounding, or the adaptive-N refusal would leak.
		var order []int
		for i := range shares {
			if shares[i].frac > 0 || shares[i].whole > 0 {
				order = append(order, i)
			}
		}
		if len(order) == 0 {
			order = []int{0} // degenerate: give everything to the cheapest-start worker
		}
		sort.SliceStable(order, func(a, b int) bool {
			return shares[order[a]].frac > shares[order[b]].frac
		})
		for r := 0; r < remainder; r++ {
			shares[order[r%len(order)]].whole++
		}
	}

	var plan Plan
	for _, sh := range shares {
		if sh.whole <= 0 {
			continue
		}
		w := ws[sh.idx]
		exp := w.startCostSecs() + float64(sh.whole)/w.ItemsPerSec
		plan.Assignments = append(plan.Assignments, PlannerAssignment{
			WorkerID: w.ID, Items: sh.whole, ExpectedSecs: exp,
		})
		if exp > plan.WallClockP50Secs {
			plan.WallClockP50Secs = exp
		}
		cons := w.startCostSecs() + float64(sh.whole)/(w.ItemsPerSec*plannerConservativeRateFactor)
		if cons > plan.WallClockConservativeSecs {
			plan.WallClockConservativeSecs = cons
		}
	}
	plan.Width = len(plan.Assignments)
	plan.SingleNodeSecs = single
	if plan.WallClockP50Secs > 0 {
		plan.ModeledSpeedupVsSingle = plan.SingleNodeSecs / plan.WallClockP50Secs
	}
	return plan
}

// singleNodeSecs is the modeled wall-clock of the best single worker running
// all items alone — the planner's reference comparison and its bisection upper
// bound. Callers pass the eligible (non-throttled, rate>0) set.
func singleNodeSecs(ws []PlannerWorker, items float64) float64 {
	best := 0.0
	for i, w := range ws {
		t := w.startCostSecs() + items/w.ItemsPerSec
		if i == 0 || t < best {
			best = t
		}
	}
	return best
}

// medianRate is the median of the positive rates in rows — the "typical live
// worker" figure the live chunk-sizing path uses in place of the static
// jobTypeThroughput map. Pure; returns 0 for an empty slice.
func medianRate(rates []float64) float64 {
	if len(rates) == 0 {
		return 0
	}
	s := append([]float64(nil), rates...)
	sort.Float64s(s)
	n := len(s)
	if n%2 == 1 {
		return s[n/2]
	}
	return (s[n/2-1] + s[n/2]) / 2
}

// rankPeersBySpeed re-orders Match's eligible same-class output for dispatch
// paths whose objective is WALL-CLOCK (straggler hedge, endgame race, tiebreak
// re-run): warm-for-the-model first — no measured tps advantage survives a
// minutes-long cold GGUF load — then the highest measured tps for the job
// type, then Match's own score order (stable sort keeps it as the residual
// tie-break). Pure; input is not mutated. This is wave-1B item 5: the hedge
// path used to take Match's reputation-weighted top scorer, which is a TRUST
// ordering, not a speed ordering — a slow high-reputation peer beat a fast
// ordinary one for a dispatch whose entire purpose is finishing sooner.
func rankPeersBySpeed(ranked []MatchWorker, jobType string) []MatchWorker {
	out := append([]MatchWorker(nil), ranked...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Warm != out[j].Warm {
			return out[i].Warm
		}
		return out[i].TPS[jobType] > out[j].TPS[jobType]
	})
	return out
}

// tokensPerItemEstimate models the tokens one job item costs a generative
// worker: the expected completion length (max_tokens, defaulting to the same
// defaultQuoteMaxTokens the quote itself assumes) plus the prompt read at ~4
// bytes/token. Used to convert a worker's measured tok/s into items/s for the
// live chunk-sizing path. Floors at 1 so a degenerate input can never divide
// by zero. Modeled, unit-tested.
func tokensPerItemEstimate(maxTokens uint32, avgLineBytes float64) float64 {
	out := float64(maxTokens)
	if out <= 0 {
		out = defaultQuoteMaxTokens
	}
	prompt := 0.0
	if avgLineBytes > 0 {
		prompt = avgLineBytes / 4.0
	}
	t := out + prompt
	if t < 1 {
		t = 1
	}
	return t
}

// plannerFleetFromRows converts the DB fleet snapshot (FleetRateSnapshot rows)
// into planner inputs. itemsPerSec converts a worker's cached tps into job
// items/s (the caller owns the unit conversion — tokens-per-item for chunk
// sizing, relative-rate-per-task for the ETA). A worker with no measured rate
// (tps 0) or currently throttled contributes nothing (the planner would skip
// it anyway; filtering here keeps len() == "workers with a real measurement"
// for the plannerMinFleetSamples gate). modelRef "" means no model to load —
// every worker counts as warm.
func plannerFleetFromRows(rows []FleetRateRow, modelRef string, itemsPerSec func(tps float32) float64) []PlannerWorker {
	out := make([]PlannerWorker, 0, len(rows))
	for _, r := range rows {
		if r.TPS <= 0 || r.Throttled {
			continue
		}
		cold := plannerDefaultColdLoadSecs
		if r.LoadMS > 0 {
			cold = float64(r.LoadMS) / 1000.0 // real measured load_ms beats the default
		}
		out = append(out, PlannerWorker{
			ID:           r.WorkerID,
			ItemsPerSec:  itemsPerSec(r.TPS),
			Warm:         modelRef == "" || r.Warm,
			ColdLoadSecs: cold,
		})
	}
	return out
}
