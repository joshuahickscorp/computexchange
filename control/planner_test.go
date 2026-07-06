package main

// planner_test.go — L1 proofs for the fan-out planner (Speed Lane wave 1B).
//
// Layer discipline (docs/research/SPEED_LANE_GOAL_PROMPT.md, "three explicit
// layers"): everything in this file is LAYER 1 — planner MATH, proven locally,
// deterministic, no infrastructure. The simulation test at the bottom is
// calibrated with this project's REAL measured rates (M3 Pro 139 tok/s
// real-traffic batched, rented A100 2345 tok/s @ batch 64) but its outputs are
// MODELED numbers, labeled as such — never a measured speedup claim. Layer 2
// (real control plane, real Postgres/MinIO, measured wall-clock) lives in
// planner_integration_test.go; layer 3 (real multi-node vs a real A100) is the
// owner's runbook in docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md.

import (
	"math/rand"
	"reflect"
	"testing"

	"github.com/google/uuid"
)

// detID makes a deterministic uuid from an index so plans compare stably.
func detID(i int) uuid.UUID {
	var b [16]byte
	b[0] = byte(i >> 8)
	b[1] = byte(i)
	b[6] = 0x40 // version 4 shape, deterministic content
	b[8] = 0x80
	id, _ := uuid.FromBytes(b[:])
	return id
}

func warmWorker(i int, rate float64) PlannerWorker {
	return PlannerWorker{ID: detID(i), ItemsPerSec: rate, Warm: true}
}

func planItems(p Plan) int {
	n := 0
	for _, a := range p.Assignments {
		n += a.Items
	}
	return n
}

// uniformMakespan is the pre-wave baseline the planner must beat: split items
// evenly across the same workers (the fleet-average assumption) and take the
// slowest completion. Same cost model, same workers — only the assignment
// differs.
func uniformMakespan(items int, ws []PlannerWorker) float64 {
	per := items / len(ws)
	rem := items % len(ws)
	worst := 0.0
	for i, w := range ws {
		k := per
		if i < rem {
			k++
		}
		if k == 0 {
			continue
		}
		t := w.startCostSecs() + float64(k)/w.ItemsPerSec
		if t > worst {
			worst = t
		}
	}
	return worst
}

// Rate-weighted assignment must beat a uniform split on modeled wall-clock for
// a heterogeneous fleet — the core reason the planner exists (the pre-wave
// splitter sizes every chunk from the fleet-average rate, which is exactly the
// uniform assumption).
func TestPlanFanoutRateWeightedBeatsUniformOnHeterogeneousFleet(t *testing.T) {
	fleet := []PlannerWorker{
		warmWorker(1, 200), // fast node
		warmWorker(2, 100),
		warmWorker(3, 50), // slow node: uniform split makes it the whole job's tail
	}
	const items = 3500
	plan := PlanFanout(PlannerJob{Items: items, JobType: "batch_infer"}, fleet)
	if planItems(plan) != items {
		t.Fatalf("assignment must conserve items: got %d want %d", planItems(plan), items)
	}
	uni := uniformMakespan(items, fleet)
	if plan.WallClockP50Secs >= uni {
		t.Fatalf("rate-weighted plan (%.2fs) must beat uniform split (%.2fs) on a heterogeneous fleet", plan.WallClockP50Secs, uni)
	}
	// The optimal split puts ~4x the items on the 4x-faster node.
	byID := map[uuid.UUID]int{}
	for _, a := range plan.Assignments {
		byID[a.WorkerID] = a.Items
	}
	if byID[detID(1)] <= byID[detID(3)] {
		t.Fatalf("faster node must get more items: fast=%d slow=%d", byID[detID(1)], byID[detID(3)])
	}
}

// Adaptive-N: a small job with one warm fast node and only cold peers must NOT
// fan out — the cold loads would raise wall-clock past what the warm node does
// alone. Width 1 is the plan.
func TestPlanFanoutAdaptiveNSmallJobOnWarmFastNodeIsWidthOne(t *testing.T) {
	fleet := []PlannerWorker{
		warmWorker(1, 1.0), // warm: 200 items alone ≈ 202s
	}
	for i := 2; i <= 6; i++ {
		fleet = append(fleet, PlannerWorker{
			ID: detID(i), ItemsPerSec: 0.8, Warm: false, ColdLoadSecs: 300, // cold start alone > whole job on the warm node
		})
	}
	plan := PlanFanout(PlannerJob{Items: 200, JobType: "batch_infer"}, fleet)
	if plan.Width != 1 {
		t.Fatalf("small job + one warm node + cold peers: want width 1 (refuse to fan), got %d (%v)", plan.Width, plan.Assignments)
	}
	if plan.Assignments[0].WorkerID != detID(1) {
		t.Fatalf("the one assigned worker must be the warm node, got %s", plan.Assignments[0].WorkerID)
	}
	if planItems(plan) != 200 {
		t.Fatalf("conservation: got %d", planItems(plan))
	}
}

// The cold-load penalty must flip the SAME fleet from wide to narrow as the
// job shrinks: a huge job amortizes N cold loads (go wide), a small one does
// not (stay narrow). This is the planner "refusing to fan wider when added
// cold loads would raise wall-clock", exercised end to end.
func TestPlanFanoutColdLoadFlipsWideToNarrowWithJobSize(t *testing.T) {
	fleet := []PlannerWorker{warmWorker(1, 1.0)}
	for i := 2; i <= 8; i++ {
		fleet = append(fleet, PlannerWorker{ID: detID(i), ItemsPerSec: 1.0, Warm: false, ColdLoadSecs: 120})
	}
	big := PlanFanout(PlannerJob{Items: 100000, JobType: "batch_infer"}, fleet)
	if big.Width != len(fleet) {
		t.Fatalf("huge job must amortize the cold loads and use the whole fleet: width %d want %d", big.Width, len(fleet))
	}
	small := PlanFanout(PlannerJob{Items: 60, JobType: "batch_infer"}, fleet)
	if small.Width != 1 {
		t.Fatalf("small job must stay on the warm node: width %d want 1 (%v)", small.Width, small.Assignments)
	}
	// And the wide plan must actually be better than staying narrow for the big
	// job (modeled): the planner's whole claim.
	if big.WallClockP50Secs >= big.SingleNodeSecs {
		t.Fatalf("wide plan (%.1fs) must beat the best single node (%.1fs) on the big job", big.WallClockP50Secs, big.SingleNodeSecs)
	}
}

// A worker whose start cost alone exceeds the fleet's achievable finish time
// must be EXCLUDED even when the job has enough items to hand it some — fanning
// onto it would raise wall-clock. (The 4-items-4-workers shape also pins the
// integer rounding path: rounding must never leak items onto excluded workers.)
func TestPlanFanoutExcludesWorkersThatWouldRaiseWallClock(t *testing.T) {
	fleet := []PlannerWorker{
		warmWorker(1, 100), // finishes all 4 items in ~2.04s
		warmWorker(2, 0.5), // would need 2s start + 2s/item — inclusion raises the makespan
		warmWorker(3, 0.5),
		warmWorker(4, 0.5),
	}
	plan := PlanFanout(PlannerJob{Items: 4, JobType: "batch_infer"}, fleet)
	if plan.Width != 1 || plan.Assignments[0].WorkerID != detID(1) {
		t.Fatalf("want the fast node alone (width 1), got width %d: %v", plan.Width, plan.Assignments)
	}
	if planItems(plan) != 4 {
		t.Fatalf("conservation: got %d", planItems(plan))
	}
}

// Determinism: the same fleet in any input order produces the identical plan
// (assignments included, in canonical order). The planner is used at submit
// time — two submits over the same snapshot must plan identically.
func TestPlanFanoutDeterministicAcrossInputOrder(t *testing.T) {
	mk := func(order []int) Plan {
		var fleet []PlannerWorker
		for _, i := range order {
			w := PlannerWorker{ID: detID(i), ItemsPerSec: float64(10 + i*7%40), Warm: i%2 == 0, ColdLoadSecs: float64(30 + i*11%90)}
			fleet = append(fleet, w)
		}
		return PlanFanout(PlannerJob{Items: 12345, JobType: "batch_infer"}, fleet)
	}
	a := mk([]int{1, 2, 3, 4, 5, 6, 7, 8})
	b := mk([]int{8, 3, 1, 7, 5, 2, 6, 4})
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("plans differ across input order:\n%+v\nvs\n%+v", a, b)
	}
	c := mk([]int{1, 2, 3, 4, 5, 6, 7, 8})
	if !reflect.DeepEqual(a, c) {
		t.Fatalf("plan not reproducible on identical input")
	}
}

// Throttled and rate-less workers are never planned onto; an empty/ineligible
// fleet or an empty job yields Width 0 (the callers' fallback signal).
func TestPlanFanoutEligibilityAndZeroPlans(t *testing.T) {
	fleet := []PlannerWorker{
		warmWorker(1, 100),
		{ID: detID(2), ItemsPerSec: 500, Warm: true, Throttled: true}, // throttled: excluded
		{ID: detID(3), ItemsPerSec: 0, Warm: true},                    // no measurement: excluded
	}
	plan := PlanFanout(PlannerJob{Items: 100, JobType: "embed"}, fleet)
	for _, a := range plan.Assignments {
		if a.WorkerID == detID(2) || a.WorkerID == detID(3) {
			t.Fatalf("planned onto an ineligible worker: %+v", a)
		}
	}
	if planItems(plan) != 100 {
		t.Fatalf("conservation: got %d", planItems(plan))
	}
	if p := PlanFanout(PlannerJob{Items: 0, JobType: "embed"}, fleet); p.Width != 0 {
		t.Fatalf("empty job must yield Width 0, got %d", p.Width)
	}
	if p := PlanFanout(PlannerJob{Items: 5, JobType: "embed"}, nil); p.Width != 0 {
		t.Fatalf("empty fleet must yield Width 0, got %d", p.Width)
	}
}

// The conservative band re-costs the same assignment at degraded rates — it can
// never sit below the p50, and the modeled speedup-vs-single-node is populated.
func TestPlanFanoutConservativeBandAndReference(t *testing.T) {
	fleet := []PlannerWorker{warmWorker(1, 50), warmWorker(2, 25), warmWorker(3, 10)}
	plan := PlanFanout(PlannerJob{Items: 5000, JobType: "batch_infer"}, fleet)
	if plan.WallClockConservativeSecs < plan.WallClockP50Secs {
		t.Fatalf("conservative band (%.2fs) below p50 (%.2fs)", plan.WallClockConservativeSecs, plan.WallClockP50Secs)
	}
	if plan.SingleNodeSecs <= 0 || plan.ModeledSpeedupVsSingle <= 1.0 {
		t.Fatalf("reference comparison must show a modeled win here: single=%.2fs speedup=%.2fx", plan.SingleNodeSecs, plan.ModeledSpeedupVsSingle)
	}
}

// rankPeersBySpeed: warm beats faster-cold; among warm, higher measured tps
// wins; ties keep the incoming (Match score) order.
func TestRankPeersBySpeed(t *testing.T) {
	mk := func(i int, warm bool, tps float32) MatchWorker {
		return MatchWorker{ID: detID(i), Warm: warm, TPS: map[string]float32{"batch_infer": tps}}
	}
	in := []MatchWorker{
		mk(1, false, 500), // fastest but cold
		mk(2, true, 80),
		mk(3, true, 120),
		mk(4, false, 90),
		mk(5, true, 120), // tie with 3 — must stay after it (stable)
	}
	out := rankPeersBySpeed(in, "batch_infer")
	wantOrder := []uuid.UUID{detID(3), detID(5), detID(2), detID(1), detID(4)}
	for i, w := range out {
		if w.ID != wantOrder[i] {
			t.Fatalf("position %d: got %s want %s (full: %v)", i, w.ID, wantOrder[i], out)
		}
	}
	// Input must not be mutated.
	if in[0].ID != detID(1) {
		t.Fatalf("rankPeersBySpeed mutated its input")
	}
}

func TestTokensPerItemEstimateAndMedianRate(t *testing.T) {
	if got := tokensPerItemEstimate(0, 0); got != defaultQuoteMaxTokens {
		t.Fatalf("defaults: got %v", got)
	}
	if got := tokensPerItemEstimate(100, 400); got != 200 { // 100 out + 400/4 prompt
		t.Fatalf("100+100: got %v", got)
	}
	if got := medianRate(nil); got != 0 {
		t.Fatalf("empty median: got %v", got)
	}
	if got := medianRate([]float64{5, 1, 9}); got != 5 {
		t.Fatalf("odd median: got %v", got)
	}
	if got := medianRate([]float64{1, 9, 5, 3}); got != 4 {
		t.Fatalf("even median: got %v", got)
	}
}

// --- L1 SIMULATION: the modeled fleet-vs-A100 curve, calibrated with REAL ---
// --- measured rates. Every number here is MODELED (labeled), the qualitative -
// --- shape is what the test pins; the quantitative table goes in the report. -
//
// Calibration (real measurements, this project):
//   - M3 Pro, Llama-3.2-1B Q4_K_M, real-traffic continuous batching: 139 tok/s
//     (serial 91–111 tok/s → ±10% node-to-node jitter, seeded below).
//   - Rented A100, same model: 2345 tok/s aggregate @ batch 64.
//     (docs/GPU_CAPABILITY.md / SPEED_LANE_CURRENT_STATE.md)
//
// Modeled job: 10,000 prompts × 256 completion tokens. Both sides pay the same
// dispatch overhead; both warm (steady-state comparison — cold-load asymmetry
// is covered by the adaptive-N tests above).
func TestPlanFanoutModeledFleetVsA100Curve(t *testing.T) {
	const (
		items        = 10000
		tokPerItem   = 256.0
		macTokPerSec = 139.0  // real-traffic batched, measured
		a100TokPerS  = 2345.0 // rented A100 spike, measured
		jitter       = 0.10   // measured serial spread 91–111 ≈ ±10%
	)
	rng := rand.New(rand.NewSource(42)) // seeded: deterministic run-to-run

	a100 := PlanFanout(PlannerJob{Items: items, JobType: "batch_infer"},
		[]PlannerWorker{warmWorker(0, a100TokPerS/tokPerItem)})
	if a100.Width != 1 {
		t.Fatalf("A100 reference must be a single node")
	}

	// One shared jittered fleet, sliced by N so the curve is over nested fleets.
	maxN := 50
	fleet := make([]PlannerWorker, maxN)
	for i := range fleet {
		f := 1 + jitter*(2*rng.Float64()-1)
		fleet[i] = warmWorker(i+1, macTokPerSec*f/tokPerItem)
	}

	wall := make([]float64, maxN+1)
	breakEven := -1
	for n := 1; n <= maxN; n++ {
		p := PlanFanout(PlannerJob{Items: items, JobType: "batch_infer"}, fleet[:n])
		if planItems(p) != items {
			t.Fatalf("N=%d: conservation failed (%d)", n, planItems(p))
		}
		wall[n] = p.WallClockP50Secs
		if breakEven < 0 && p.WallClockP50Secs <= a100.WallClockP50Secs {
			breakEven = n
		}
		// The planner must not leave measurable capacity idle on a big batch:
		// its makespan stays within 2% + one item of the ideal aggregate-rate
		// bound for the nested fleet.
		var agg float64
		for _, w := range fleet[:n] {
			agg += w.ItemsPerSec
		}
		ideal := plannerChunkOverheadSecs + items/agg
		if p.WallClockP50Secs > ideal*1.02+1 {
			t.Fatalf("N=%d: planner makespan %.1fs is far off the ideal %.1fs — capacity left idle", n, p.WallClockP50Secs, ideal)
		}
	}

	// Qualitative shape (the L1 assertion): monotone non-increasing wall-clock
	// (tiny fp/integer tolerance), a break-even near the measured-rate ratio
	// 2345/139 ≈ 16.9, and a ~3x margin at 50 nodes.
	for n := 2; n <= maxN; n++ {
		if wall[n] > wall[n-1]+0.5 {
			t.Fatalf("modeled wall-clock must not rise with fleet size: N=%d %.1fs > N=%d %.1fs", n, wall[n], n-1, wall[n-1])
		}
	}
	if breakEven < 15 || breakEven > 19 {
		t.Fatalf("modeled break-even fleet size %d outside the calibrated band [15,19] (A100 %.1fs)", breakEven, a100.WallClockP50Secs)
	}
	margin50 := a100.WallClockP50Secs / wall[50]
	if margin50 < 2.5 || margin50 > 3.4 {
		t.Fatalf("modeled 50-node margin %.2fx outside [2.5,3.4]", margin50)
	}

	// The quantitative table for the report (MODELED — run with -v to print).
	t.Logf("MODELED fleet-vs-A100 curve (10k prompts × 256 tok, M3 Pro 139 tok/s ±10%%, A100 %.0f tok/s → %.1fs):", a100TokPerS, a100.WallClockP50Secs)
	for _, n := range []int{1, 5, 10, 15, breakEven, 20, 30, 40, 50} {
		t.Logf("  N=%2d Macs: modeled wall-clock %7.1fs  vs A100 %.2fx", n, wall[n],
			a100.WallClockP50Secs/wall[n])
	}
	t.Logf("  MODELED break-even: %d Macs; 50-node margin %.2fx", breakEven, margin50)
}
