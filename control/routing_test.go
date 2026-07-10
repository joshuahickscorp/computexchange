package main

// routing_test.go — pure unit proof of the substrate-routing decision
// (routing.go, Speed Lane road-to-ten rubric dimension 4). No DB, no clock:
// the curve interpolation is pinned to the exact measured sweep points
// (docs/speed-lane-reports/A100_CAPABILITY_SWEEP.md), the modeled GPU
// wall-clock is monotone in records, and DecideSubstrate's rule is exercised
// across the records × model-class × tier matrix, including the honesty
// properties: fleet always wins below the crossover, a GPU recommendation is
// never a refusal, no win is claimed without a planner-backed model, and every
// number-bearing reason carries the [modeled] label + the sweep citation.

import (
	"fmt"
	"strings"
	"testing"
)

func TestInterpolatedAggTokSExactMeasuredPoints(t *testing.T) {
	// Every encoded point must return EXACTLY the measured aggregate — the
	// curve is a transcription of the sweep, not a fit.
	cases := []struct {
		class string
		batch float64
		want  float64
	}{
		{"1b", 1, 387}, {"1b", 8, 2954}, {"1b", 64, 19864}, {"1b", 512, 43570}, {"1b", 2048, 44852},
		{"7b", 1, 100}, {"7b", 8, 784}, {"7b", 64, 5355}, {"7b", 512, 11116}, {"7b", 2048, 11310},
	}
	for _, c := range cases {
		if got := interpolatedAggTokS(c.class, c.batch); got != c.want {
			t.Errorf("interpolatedAggTokS(%s, %v) = %v, want the measured %v", c.class, c.batch, got, c.want)
		}
	}
}

func TestInterpolatedAggTokSPiecewiseLinearAndClamped(t *testing.T) {
	// Midpoint of the 1b (8, 2954)→(64, 19864) segment: linear interpolation.
	if got, want := interpolatedAggTokS("1b", 36), 2954+0.5*(19864-2954); got != want {
		t.Errorf("1b batch 36: got %v want linear midpoint %v", got, want)
	}
	// Midpoint of the 7b (64, 5355)→(512, 11116) segment.
	if got, want := interpolatedAggTokS("7b", 288), 5355+0.5*(11116-5355); got != want {
		t.Errorf("7b batch 288: got %v want linear midpoint %v", got, want)
	}
	// Below the first measured point: clamped at batch-1 (never extrapolated
	// downward), and above the last: clamped at the batch-2048 ceiling (the
	// sweep shows saturation; extrapolating would invent a number).
	if got := interpolatedAggTokS("1b", 0.25); got != 387 {
		t.Errorf("below batch 1 must clamp to the batch-1 value: got %v", got)
	}
	if got := interpolatedAggTokS("7b", 100000); got != 11310 {
		t.Errorf("above batch 2048 must clamp to the ceiling: got %v", got)
	}
	// Unknown class: no curve, 0 (the caller's "no decision" signal).
	if got := interpolatedAggTokS("70b", 8); got != 0 {
		t.Errorf("unknown class must return 0, got %v", got)
	}
	// Monotone non-decreasing in batch across the whole measured range —
	// batching never makes the GPU slower in the sweep.
	for _, class := range []string{"1b", "7b"} {
		prev := 0.0
		for b := 1; b <= 4096; b *= 2 {
			cur := interpolatedAggTokS(class, float64(b))
			if cur < prev {
				t.Fatalf("%s: curve not monotone at batch %d: %v < %v", class, b, cur, prev)
			}
			prev = cur
		}
	}
}

func TestGPUModeledSecsMonotonicInRecords(t *testing.T) {
	// More records can never take LESS modeled wall-clock: total tokens grow
	// linearly while the aggregate rate saturates at the measured ceiling.
	for _, class := range []string{"1b", "7b"} {
		prev := 0.0
		for _, records := range []int{1, 2, 4, 8, 16, 32, 64, 100, 200, 512, 1000, 2048, 4096, 10000} {
			cur := gpuModeledSecs(class, records, 256)
			if cur < prev {
				t.Fatalf("%s: gpuModeledSecs not monotone at %d records: %v < %v", class, records, cur, prev)
			}
			prev = cur
		}
	}
	// Spot-check one exactly-computable point: 512 records × 256 tokens on 1b
	// = 131072 tokens / the measured 43570 tok/s.
	if got, want := gpuModeledSecs("1b", 512, 256), 512.0*256.0/43570.0; got != want {
		t.Errorf("gpuModeledSecs(1b, 512, 256) = %v, want %v", got, want)
	}
	// Degenerate inputs model nothing (0), never a fabricated figure.
	if gpuModeledSecs("1b", 0, 256) != 0 || gpuModeledSecs("1b", 10, 0) != 0 || gpuModeledSecs("70b", 10, 256) != 0 {
		t.Error("degenerate inputs must return 0 (no model)")
	}
}

func TestRoutingModelClass(t *testing.T) {
	cases := []struct {
		ref    string
		minMem float32
		want   string
	}{
		{"qwen2.5-7b-instruct-q4", 40, "7b"}, // the catalogue's 7B row (ref AND floor)
		{"some-7B-model", 0, "7b"},           // ref match is case-insensitive
		{"mystery-model", 40, "7b"},          // the 40GB catalogue floor alone
		{"llama-3.2-1b-instruct-q4", 4, "1b"},
		{"", 0, "1b"}, // unknown → the FASTER gpu row (overstates the competition, honest direction)
	}
	for _, c := range cases {
		if got := routingModelClass(c.ref, c.minMem); got != c.want {
			t.Errorf("routingModelClass(%q, %v) = %q, want %q", c.ref, c.minMem, got, c.want)
		}
	}
}

// TestDecideSubstrateMatrix sweeps the records × class × tier grid the goal
// prompt pins and checks the invariants every cell must satisfy: a valid
// substrate, a non-empty reason naming the sweep and carrying the [modeled]
// label, a positive GPU figure, fleet below the crossover — and determinism
// (an identical call returns the identical decision).
func TestDecideSubstrateMatrix(t *testing.T) {
	valid := map[string]bool{"fleet": true, "gpu_lane": true, "gpu_recommend": true}
	for _, records := range []int{1, 4, 8, 32, 64, 100, 512, 10000} {
		for _, class := range []string{"1b", "7b"} {
			for _, tier := range []string{"batch", "priority", "trusted"} {
				name := fmt.Sprintf("r%d_%s_%s", records, class, tier)
				t.Run(name, func(t *testing.T) {
					d := DecideSubstrate(records, tier, class, 256, 45, 60, true, 0)
					if !valid[d.Substrate] {
						t.Fatalf("invalid substrate %q", d.Substrate)
					}
					if d.Reason == "" {
						t.Fatal("reason must never be empty")
					}
					if !strings.Contains(d.Reason, "2026-07-06 a100 vllm sweep") {
						t.Errorf("reason must name the measured basis: %q", d.Reason)
					}
					if !strings.Contains(d.Reason, "[modeled]") {
						t.Errorf("a number-bearing reason must carry the [modeled] label: %q", d.Reason)
					}
					if d.GPUModeledSecs <= 0 {
						t.Errorf("gpu modeled secs must be positive for a generative shape, got %v", d.GPUModeledSecs)
					}
					if d.FleetSecs != 45 || d.FleetConservativeSecs != 60 {
						t.Errorf("decision must echo the fleet numbers it compared: %+v", d)
					}
					if records < 8 && d.Substrate != "fleet" {
						t.Errorf("below the measured crossover the fleet must win, got %q", d.Substrate)
					}
					if records > 64 && tier == "batch" && d.Substrate == "fleet" {
						// fleet 45s vs gpu (256 tok/item): even at the ceiling the gpu
						// models faster here, so a fleet claim would be an unmodeled win.
						if float64(d.FleetSecs) >= d.GPUModeledSecs {
							t.Errorf("fleet kept a big batch without modeling faster: %+v", d)
						}
					}
					// Determinism: same inputs, identical decision.
					if d2 := DecideSubstrate(records, tier, class, 256, 45, 60, true, 0); d2 != d {
						t.Errorf("non-deterministic decision:\n  %+v\n  %+v", d, d2)
					}
				})
			}
		}
	}
}

// TestFleetWinsBelowCrossoverRegardlessOfGPUNumbers pins the availability
// rule: under 8 records no GPU figure — however flattering — can flip the
// decision, because the modeled GPU seconds exclude the provisioning wait
// that dominates at that scale.
func TestFleetWinsBelowCrossoverRegardlessOfGPUNumbers(t *testing.T) {
	for records := 1; records < 8; records++ {
		// An absurdly slow fleet (1e6 secs) and a huge per-item token count
		// (flattering the batched GPU) must still route to the fleet.
		d := DecideSubstrate(records, "batch", "1b", 4096, 1000000, 1500000, true, 5)
		if d.Substrate != "fleet" {
			t.Fatalf("%d records: want fleet regardless of gpu numbers, got %q (%s)", records, d.Substrate, d.Reason)
		}
		if !strings.Contains(d.Reason, "below the measured gpu crossover") {
			t.Errorf("%d records: reason must state the crossover grounding: %q", records, d.Reason)
		}
		if !strings.Contains(d.Reason, "provisioning") {
			t.Errorf("%d records: reason must state why the gpu figure cannot flip this: %q", records, d.Reason)
		}
	}
}

// TestCrossoverBandComparison exercises the 8..64 band: the decision compares
// real numbers, prefers the fleet on ties and uncertainty, keeps priority-tier
// work on the fleet (its measured lane), and hands a decisively-faster GPU the
// recommendation.
func TestCrossoverBandComparison(t *testing.T) {
	// (a) planner-backed, gpu decisively faster (fleet 45s vs ~0.55s modeled)
	// → gpu_recommend (no lit lane), with BOTH numbers in the reason.
	d := DecideSubstrate(32, "batch", "1b", 256, 45, 60, true, 0)
	if d.Substrate != "gpu_recommend" {
		t.Fatalf("planner-backed crossover with a faster gpu must recommend: got %q (%s)", d.Substrate, d.Reason)
	}
	if !strings.Contains(d.Reason, fmt.Sprintf("~%.2fs", d.GPUModeledSecs)) || !strings.Contains(d.Reason, "~45s") {
		t.Errorf("reason must state the compared numbers: %q", d.Reason)
	}
	if !strings.Contains(d.Reason, "still run this on the fleet") {
		t.Errorf("a recommendation is never a refusal: %q", d.Reason)
	}

	// (b) same shape, NOT planner-backed → fleet: we do not switch substrates
	// on a blunt aggregate we cannot model.
	d = DecideSubstrate(32, "batch", "1b", 256, 45, 0, false, 0)
	if d.Substrate != "fleet" {
		t.Fatalf("uncertain fleet number must prefer fleet: got %q", d.Substrate)
	}
	if !strings.Contains(d.Reason, "blunt aggregate") {
		t.Errorf("reason must state the uncertainty grounds: %q", d.Reason)
	}

	// (c) priority tier stays on the fleet even when the gpu models faster —
	// latency-sensitive is the fleet's measured lane.
	d = DecideSubstrate(32, "priority", "1b", 256, 45, 60, true, 0)
	if d.Substrate != "fleet" {
		t.Fatalf("priority tier in the crossover band must stay fleet: got %q", d.Substrate)
	}
	if !strings.Contains(d.Reason, "priority-tier") {
		t.Errorf("reason must state the latency grounds: %q", d.Reason)
	}

	// (d) fleet models faster or ties → fleet with the comparison stated.
	// 64 records × 1000 tok on 7b models 64000/5355 ≈ 11.95s; fleet p50 5s.
	d = DecideSubstrate(64, "batch", "7b", 1000, 5, 8, true, 0)
	if d.Substrate != "fleet" {
		t.Fatalf("fleet modeling faster in-band must win: got %q (%s)", d.Substrate, d.Reason)
	}
	if !strings.Contains(d.Reason, "beats or ties") {
		t.Errorf("reason must state the comparison outcome: %q", d.Reason)
	}
	// Exact tie: 8 records × 738.5 tok on 1b = 5908 tok / 2954 = 2.00s vs
	// fleet 2s → the tie goes to the fleet (already online).
	d = DecideSubstrate(8, "batch", "1b", 738.5, 2, 3, true, 0)
	if d.Substrate != "fleet" {
		t.Fatalf("a tie must prefer the fleet: got %q (%s)", d.Substrate, d.Reason)
	}
}

// TestBigBatchGPUDecision exercises records > 64: the batching advantage
// compounds, so the GPU side wins unless the planner-backed fleet actually
// models faster — and a lit lane turns the recommendation into a route.
func TestBigBatchGPUDecision(t *testing.T) {
	// (a) no lit lane → gpu_recommend with the full honest comparison.
	d := DecideSubstrate(512, "batch", "1b", 256, 300, 400, true, 0)
	if d.Substrate != "gpu_recommend" {
		t.Fatalf("big batch, no lit lane: want gpu_recommend, got %q", d.Substrate)
	}
	for _, must := range []string{"no lit gpu lane", "still run this on the fleet", "excludes rental/provisioning", "[modeled]"} {
		if !strings.Contains(d.Reason, must) {
			t.Errorf("gpu_recommend reason must contain %q: %q", must, d.Reason)
		}
	}

	// (b) the gpu_lane branch when a lit lane supply count is passed.
	d = DecideSubstrate(512, "batch", "1b", 256, 300, 400, true, 3)
	if d.Substrate != "gpu_lane" {
		t.Fatalf("big batch with a lit lane must route to it: got %q", d.Substrate)
	}
	if !strings.Contains(d.Reason, "3 verified vllm-lane worker(s) are online and eligible") {
		t.Errorf("gpu_lane reason must state the live supply: %q", d.Reason)
	}

	// (c) planner-backed fleet models FASTER than the gpu → fleet, both
	// numbers stated (never claim a win we can't model — here we CAN).
	// 10000 records × 256 tok on 7b models 2,560,000/11310 ≈ 226s; fleet 100s.
	d = DecideSubstrate(10000, "batch", "7b", 256, 100, 140, true, 0)
	if d.Substrate != "fleet" {
		t.Fatalf("planner-backed faster fleet must keep a big batch: got %q (%s)", d.Substrate, d.Reason)
	}
	if !strings.Contains(d.Reason, "~100s") || !strings.Contains(d.Reason, fmt.Sprintf("~%.2fs", d.GPUModeledSecs)) {
		t.Errorf("fleet-win reason must state both numbers: %q", d.Reason)
	}

	// (d) the SAME shape without planner backing must NOT claim the fleet win
	// (the blunt aggregate models nothing) → gpu_recommend.
	d = DecideSubstrate(10000, "batch", "7b", 256, 100, 0, false, 0)
	if d.Substrate != "gpu_recommend" {
		t.Fatalf("an unmodeled fleet number must never claim a big-batch win: got %q", d.Substrate)
	}
}

// TestDecideSubstrateReasonsAreQuoteVoice pins the reason strings to the
// quote's existing warnings voice: lowercase plain english (no leading
// capital), and the GPU figure labeled [modeled] wherever it appears.
func TestDecideSubstrateReasonsAreQuoteVoice(t *testing.T) {
	for _, d := range []SubstrateDecision{
		DecideSubstrate(3, "batch", "1b", 256, 45, 60, true, 0),
		DecideSubstrate(32, "priority", "7b", 256, 45, 60, true, 0),
		DecideSubstrate(512, "batch", "1b", 256, 300, 400, true, 0),
		DecideSubstrate(512, "batch", "1b", 256, 300, 400, true, 2),
		DecideSubstrate(10000, "batch", "7b", 256, 100, 140, true, 0),
	} {
		if d.Reason == "" {
			t.Fatal("empty reason")
		}
		if first := d.Reason[0]; first >= 'A' && first <= 'Z' {
			t.Errorf("reason must be lowercase like the quote's warnings: %q", d.Reason)
		}
		if !strings.Contains(d.Reason, "[modeled]") {
			t.Errorf("reason with numbers must carry [modeled]: %q", d.Reason)
		}
	}
}
