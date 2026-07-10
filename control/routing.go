package main

// routing.go — substrate-routing intelligence (Speed Lane road-to-ten, rubric
// dimension 4 — docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md: "the planner
// does not READ job shape and choose a substrate. Insight exists; code doesn't
// act on it"). This file is the first real piece of the product promise: read
// the job's shape, pick the substrate that runs it fastest, and tell the buyer
// plainly "we're running this on X because Y".
//
// Like planner.go, this file is PURE decision logic: no database, no clock, no
// randomness — deterministic in its inputs and unit-tested in routing_test.go.
// The wiring (real records/tier/ETA from the live quote) lives in quote.go's
// buildQuote, which attaches the decision to POST /v1/quote's response.
//
// The measured basis. On 2026-07-06 we rented an NVIDIA A100-SXM4-80GB and
// swept it under vLLM fp16 (ignore_eos, max_tokens=128, distinct prefixes)
// across model size × batch width — docs/speed-lane-reports/
// A100_CAPABILITY_SWEEP.md, raw runs in artifacts/
// a100-sxm-capability-sweep-2026-07-06.jsonl. The sweep's routing rule:
//
//   - the A100's advantage is ENTIRELY a batching effect (~110× from batch 1
//     to the ceiling for ≤14B models, saturating around batch 512);
//   - at batch 1 one A100 is ordinary — 1-3 M3-Pro-class fleet nodes of
//     throughput (387 tok/s vs the fleet reference 139 tok/s real-traffic);
//   - the crossover where the GPU's batching starts to dominate is ~batch
//     8-64;
//   - at big batches a GPU dominates outright — the honest break-even against
//     the real vLLM aggregate is ~318 M3-Pro-class nodes (44269/139), so we
//     NEVER promise the fleet beats a well-driven GPU there.
//
// The fleet's honest lane is latency-sensitive / low-concurrency work; the GPU
// lane's is big batches. Every decision below is grounded in that measured
// curve, and every GPU number this file emits is labeled [MODELED]: it is the
// sweep's measured aggregate tok/s interpolated at this job's shape, NOT a
// measurement of this job — and it deliberately EXCLUDES rental/provisioning/
// queue time, so the conservatism points the right way (we understate our own
// case when recommending the competition).

import (
	"fmt"
	"math"
	"strings"
)

// The lit-lane supply count `litGPUWorkers` DecideSubstrate takes is now a LIVE
// figure: control/quote.go's EligibleVLLMWorkerCount (real online workers with
// engine='vllm' eligible for the job), passed by both the quote and submit
// routing call sites. It is 0 whenever no verified vLLM worker is online — so
// the router honestly says `gpu_recommend` (advisory) until real GPU supply
// registers, and `gpu_lane` the moment it does, without ever selling supply the
// exchange does not have. The within-nvidia_* byte-stability soak
// (docs/speed-lane-reports/VLLM_RESTART_SOAK_2026-07-06.md) is what makes such a
// worker's output trustworthy (tolerant (engine, build_hash) class + redundancy).

// gpuSweepPoint is one measured (batch → aggregate tok/s) point from the
// 2026-07-06 A100-SXM4-80GB vLLM sweep.
type gpuSweepPoint struct {
	batch   float64
	aggTokS float64
}

// gpuCompetitionCurve is the MEASURED A100 capability sweep, transcribed
// verbatim from docs/speed-lane-reports/A100_CAPABILITY_SWEEP.md (raw runs:
// docs/speed-lane-reports/artifacts/a100-sxm-capability-sweep-2026-07-06.jsonl;
// NVIDIA A100-SXM4-80GB, vLLM fp16, ignore_eos, max_tokens=128, distinct
// prefixes). Aggregate tok/s by (model class, batch). Only the two classes our
// fleet actually serves are encoded: the catalogue's generative models are
// ~1B (llama-3.2-1b-instruct-q4) and ~7B (qwen2.5-7b-instruct-q4), matching
// the sweep's 1.1B (TinyLlama fp16) and 7B (Qwen2.5 fp16) rows. The 14B/32B
// rows exist in the sweep but no catalogue model maps to them — encoding them
// here would be dead numbers waiting to rot.
var gpuCompetitionCurve = map[string][]gpuSweepPoint{
	"1b": {{1, 387}, {8, 2954}, {64, 19864}, {512, 43570}, {2048, 44852}},
	"7b": {{1, 100}, {8, 784}, {64, 5355}, {512, 11116}, {2048, 11310}},
}

// gpuBatchCeiling is the largest batch the sweep measured. Beyond it the curve
// is CLAMPED at the batch-2048 aggregate rather than extrapolated: the sweep
// shows throughput saturating (~batch 512 onward the gain is <3%), and
// extrapolating past the last measurement would be inventing a number.
const gpuBatchCeiling = 2048

// routingModelClass picks the GPU competition-curve row for a model: "7b" for
// the catalogue's 40GB-floor 7B class, "1b" otherwise. The same simple
// distinction the codebase already draws — the seeded catalogue's only heavy
// generative model is qwen2.5-7b-instruct-q4 (min_memory_gb=40, seed.go), so a
// "7b" substring in the ref OR a catalogue memory floor >= 40GB means the 7B
// row; everything else (the ~1B llama + unknown/uncatalogued small models)
// competes against the 1B row. Deliberately crude and documented: an unknown
// model defaulting to the FASTER GPU row ("1b") overstates the GPU, which is
// the honest direction for a routing recommendation.
func routingModelClass(modelRef string, minMemoryGB float32) string {
	if strings.Contains(strings.ToLower(modelRef), "7b") || minMemoryGB >= 40 {
		return "7b"
	}
	return "1b"
}

// interpolatedAggTokS is the sweep's aggregate tok/s at an arbitrary batch
// width: piecewise-LINEAR interpolation between the measured points, clamped
// to the batch-1 value below the first point and to the batch-2048 ceiling
// above the last (never extrapolated — see gpuBatchCeiling). Linear between
// measured points is a deliberate, documented simplification: the real curve
// is concave (batching gains taper), so linear interpolation OVERSTATES the
// GPU between points — again the honest direction. Returns 0 for an unknown
// class (callers treat that as "no curve, no decision").
func interpolatedAggTokS(modelClass string, batch float64) float64 {
	pts, ok := gpuCompetitionCurve[modelClass]
	if !ok || len(pts) == 0 {
		return 0
	}
	if batch <= pts[0].batch {
		return pts[0].aggTokS
	}
	if batch >= pts[len(pts)-1].batch {
		return pts[len(pts)-1].aggTokS
	}
	for i := 1; i < len(pts); i++ {
		lo, hi := pts[i-1], pts[i]
		if batch <= hi.batch {
			frac := (batch - lo.batch) / (hi.batch - lo.batch)
			return lo.aggTokS + frac*(hi.aggTokS-lo.aggTokS)
		}
	}
	return pts[len(pts)-1].aggTokS // unreachable; defensive
}

// gpuModeledSecs is the [MODELED] wall-clock of ONE A100-class GPU under vLLM
// given the whole job as one batch: records × tokensPerItem total tokens,
// divided by the sweep's interpolated aggregate tok/s at batch
// min(records, 2048). Explicitly a MODEL from the measured sweep, never a
// measurement of this job — and it EXCLUDES rental, provisioning and queue
// time entirely (a real rented A100 takes minutes just to come up), so the
// figure honestly FAVORS the GPU. Returns 0 (no model) for degenerate inputs.
func gpuModeledSecs(modelClass string, records int, tokensPerItem float64) float64 {
	if records <= 0 || tokensPerItem <= 0 {
		return 0
	}
	batch := float64(records)
	if batch > gpuBatchCeiling {
		batch = gpuBatchCeiling
	}
	agg := interpolatedAggTokS(modelClass, batch)
	if agg <= 0 {
		return 0
	}
	return float64(records) * tokensPerItem / agg
}

// The measured crossover band (A100_CAPABILITY_SWEEP.md): below batch 8 the
// GPU's batching advantage has not engaged (batch-1 throughput is 1-3 fleet
// nodes); by batch 64 it has decisively compounded. Inside the band the two
// substrates genuinely compete and the decision compares modeled numbers.
const (
	gpuCrossoverLow  = 8
	gpuCrossoverHigh = 64
)

// SubstrateDecision is the routing decision DecideSubstrate returns: which
// substrate the job's shape favors, the plain-English why (naming the measured
// basis, never promising to beat anything), and the numbers that were
// compared. GPUModeledSecs is always [MODELED] from the measured sweep;
// FleetSecs is the quote's own ETA (the planner's modeled makespan when
// PlannerBacked, the blunt pre-wave aggregate otherwise).
type SubstrateDecision struct {
	Substrate             string  // "fleet" | "gpu_lane" | "gpu_recommend"
	Reason                string  // lowercase plain english, quote-warnings voice
	ModelClass            string  // which competition-curve row was used
	FleetSecs             int     // the fleet ETA compared (p50)
	FleetConservativeSecs int     // planner conservative band (0 when not planner-backed)
	GPUModeledSecs        float64 // [MODELED] one A100-class GPU under vLLM, excludes provisioning
	PlannerBacked         bool    // whether FleetSecs is a real modeled makespan
}

// routingSweepCitation names the measured basis every Reason cites — the one
// string a buyer can follow to the raw numbers.
const routingSweepCitation = "the 2026-07-06 a100 vllm sweep (docs/speed-lane-reports/A100_CAPABILITY_SWEEP.md)"

// DecideSubstrate reads the job's shape and picks the substrate the measured
// curve says runs it fastest. Pure and deterministic. The caller (buildQuote)
// only invokes it for GENERATIVE jobs with records > 0 — the sweep measured
// generative decode throughput only, so extending its curve to embed/rerank/
// transcribe shapes would be an unmeasured guess (an honesty boundary the
// caller enforces by omitting the routing block entirely).
//
// The rule, grounded point by point in the measured sweep:
//
//   - records < 8 (below the measured crossover): "fleet". At this concurrency
//     the GPU's batching advantage has not engaged — batch-1 is ordinary (1-3
//     fleet nodes of throughput) — and the fleet wins on availability: it is
//     online now, while the GPU figure excludes provisioning entirely.
//   - records 8..64 (inside the measured crossover band): compare the fleet
//     ETA against the modeled GPU wall-clock, preferring "fleet" on ties and
//     on uncertainty (a non-planner-backed fleet ETA is a blunt aggregate we
//     refuse to switch substrates on; the GPU number excludes provisioning
//     while our fleet is already online). A priority-tier job stays "fleet"
//     here too: latency-sensitive work is the fleet's measured honest lane,
//     and a GPU that must first be provisioned cannot serve latency.
//   - records > 64: the GPU's batching advantage compounds (the sweep's ~110×
//     climb) — "gpu_lane" when a lit lane exists, else "gpu_recommend" —
//     UNLESS the planner-backed fleet ETA actually models faster than the
//     GPU's modeled wall-clock (then "fleet", with both numbers stated; we
//     never claim a win we cannot model).
//
// "gpu_recommend" is a RECOMMENDATION, never a refusal: its Reason states the
// honest comparison, that no lit GPU lane is online yet, and that we will
// still run the job on the fleet if submitted.
func DecideSubstrate(records int, tier, modelClass string, tokensPerItem float64,
	fleetP50Secs, fleetConservativeSecs int, plannerBacked bool, litGPUWorkers int) SubstrateDecision {

	gpuSecs := gpuModeledSecs(modelClass, records, tokensPerItem)
	d := SubstrateDecision{
		ModelClass:            modelClass,
		FleetSecs:             fleetP50Secs,
		FleetConservativeSecs: fleetConservativeSecs,
		GPUModeledSecs:        roundSecs(gpuSecs),
		PlannerBacked:         plannerBacked,
	}

	switch {
	case records < gpuCrossoverLow:
		// Below the measured crossover the GPU is ordinary and offline; the
		// fleet is online. No number comparison can flip this — the modeled
		// GPU seconds exclude the provisioning time that dominates at this
		// scale, so comparing them here would be comparing against a fiction.
		d.Substrate = "fleet"
		d.Reason = fmt.Sprintf(
			"running on the fleet: %d record(s) is below the measured gpu crossover (batch 8) in %s — at batch 1 a single a100-class gpu is ordinary (1-3 fleet nodes of throughput) and its ~%.2fs [modeled] figure excludes the provisioning wait a rented gpu actually costs, while the fleet is online now",
			records, routingSweepCitation, d.GPUModeledSecs)

	case records <= gpuCrossoverHigh:
		// Inside the measured crossover band the substrates genuinely compete;
		// the decision compares real numbers and prefers the fleet on ties and
		// on uncertainty.
		switch {
		case tier == "priority":
			// Latency-sensitive is the fleet's measured honest lane — the shared
			// routing rule's own conclusion. A GPU that must be provisioned
			// cannot serve a latency tier, whatever its batched throughput.
			d.Substrate = "fleet"
			d.Reason = fmt.Sprintf(
				"running on the fleet: %d records sits inside the measured crossover band (batch 8-64, %s) but this is a priority-tier job — latency-sensitive work is the fleet's measured lane (online now, ~%ds eta) and the gpu's ~%.2fs [modeled] excludes provisioning",
				records, routingSweepCitation, fleetP50Secs, d.GPUModeledSecs)
		case !plannerBacked:
			// The fleet ETA is the blunt pre-wave aggregate, not a modeled
			// makespan — we do not switch substrates on a number we cannot
			// model (the same honesty gate the speed-SLA applies).
			d.Substrate = "fleet"
			d.Reason = fmt.Sprintf(
				"running on the fleet: %d records is inside the measured crossover band (batch 8-64, %s) but the fleet eta (~%ds) is a blunt aggregate, not a modeled makespan — we do not switch substrates on numbers we cannot model, and the gpu's ~%.2fs [modeled] excludes provisioning while the fleet is online now",
				records, routingSweepCitation, fleetP50Secs, d.GPUModeledSecs)
		case float64(fleetP50Secs) <= gpuSecs:
			d.Substrate = "fleet"
			d.Reason = fmt.Sprintf(
				"running on the fleet: %d records is inside the measured crossover band (batch 8-64, %s) and the fleet's modeled ~%ds beats or ties a single a100-class gpu's ~%.2fs [modeled] — and the gpu figure excludes provisioning",
				records, routingSweepCitation, fleetP50Secs, d.GPUModeledSecs)
		default:
			d.Substrate = gpuSubstrate(litGPUWorkers)
			d.Reason = gpuReason(d.Substrate, records, fleetP50Secs, gpuSecs, litGPUWorkers,
				fmt.Sprintf("%d records is inside the measured crossover band (batch 8-64, %s) and the batched gpu models faster", records, routingSweepCitation))
		}

	default: // records > gpuCrossoverHigh
		// Past the crossover the batching advantage compounds — the sweep's
		// ~110× batch-1→ceiling climb. The fleet keeps the job only when its
		// PLANNER-BACKED makespan actually models faster: we never claim a win
		// we cannot model (and the blunt aggregate models nothing).
		if plannerBacked && float64(fleetP50Secs) < gpuSecs {
			d.Substrate = "fleet"
			d.Reason = fmt.Sprintf(
				"running on the fleet: %d records is past the measured gpu crossover (%s) but our fleet's modeled ~%ds (conservative ~%ds) is faster than a single a100-class gpu's ~%.2fs [modeled] for this shape — both numbers modeled, the gpu's excluding provisioning",
				records, routingSweepCitation, fleetP50Secs, fleetConservativeSecs, d.GPUModeledSecs)
		} else {
			d.Substrate = gpuSubstrate(litGPUWorkers)
			d.Reason = gpuReason(d.Substrate, records, fleetP50Secs, gpuSecs, litGPUWorkers,
				fmt.Sprintf("at %d records the gpu's batching advantage compounds (~110x batch-1 to ceiling, %s)", records, routingSweepCitation))
		}
	}
	return d
}

// gpuSubstrate maps the lit-lane supply count to the GPU-side substrate:
// a lit lane routes ("gpu_lane"), no lit lane recommends ("gpu_recommend").
func gpuSubstrate(litGPUWorkers int) string {
	if litGPUWorkers > 0 {
		return "gpu_lane"
	}
	return "gpu_recommend"
}

// gpuReason writes the GPU-side Reason: the honest comparison (both numbers,
// the GPU's labeled [modeled]), plus — for gpu_recommend — that no lit lane is
// online yet and that the fleet will still run the job if submitted (a
// recommendation is never a refusal).
func gpuReason(substrate string, records, fleetP50Secs int, gpuSecs float64, litGPUWorkers int, shape string) string {
	cmp := fmt.Sprintf(
		"a single a100-class gpu under vllm models ~%.2fs for this shape vs our fleet's ~%ds [modeled]; the gpu figure excludes rental/provisioning time",
		roundSecs(gpuSecs), fleetP50Secs)
	if substrate == "gpu_lane" {
		// Honest about the ADVISORY reality: verified vLLM supply exists and the
		// GPU lane is the faster substrate, but the platform does NOT yet pin the
		// job to that lane (the claim path filters on job/model/memory, not engine),
		// so an eligible fleet worker could still claim it. Say so plainly rather
		// than implying the GPU will run it.
		return fmt.Sprintf(
			"the gpu lane is the faster substrate for this shape: %s — %s; %d verified vllm-lane worker(s) are online and eligible. routing is advisory today — the platform does not pin your job to the vllm lane, so it may still run on the fleet at the quoted eta",
			shape, cmp, litGPUWorkers)
	}
	return fmt.Sprintf(
		"gpu recommended: %s — %s. no lit gpu lane is online on the exchange yet; if you submit anyway we will still run this on the fleet at the quoted eta",
		shape, cmp)
}

// roundSecs rounds a modeled seconds figure to 2 decimals for the wire — a
// modeled number printed to microseconds would be false precision.
func roundSecs(s float64) float64 { return math.Round(s*100) / 100 }
