//go:build integration

package main

// routing_integration_test.go — REAL-INFRA proof of the substrate-routing
// quote block (Speed Lane road-to-ten rubric dimension 4, routing.go +
// quote.go): real Postgres + real MinIO + the real HTTP control plane (the
// shared TestMain in integration_test.go), a real registered fleet feeding
// worker_tps_cache through the production registration/heartbeat paths, and
// real POST /v1/quote round-trips. Proves over the wire:
//
//  1. a SMALL generative input (3 records, below the measured crossover)
//     quotes routing.substrate == "fleet" with a non-empty reason naming the
//     measured basis;
//  2. a LARGE generative input (500 records, past the crossover) quotes
//     routing.substrate == "gpu_recommend" — no lit GPU lane exists (the live
//     EligibleVLLMWorkerCount is 0 because no vLLM worker is online) — whose
//     reason carries the honest comparison (both numbers, the GPU's labeled
//     [modeled]) and states the job still runs on the fleet if submitted;
//  3. an EMBED job gets NO routing block at all (the sweep measured
//     generative decode only — the honesty boundary);
//  4. the routing block is persisted with the quote's other assumptions
//     (quotes.quote_json), so a later invoice can say what was believed;
//  5. once a REAL vLLM-engine worker registers, the SAME large input flips to
//     routing.substrate == "gpu_lane" — the live supply count lighting the lane
//     (TestQuoteRoutingLitGPULaneWhenVLLMSupplyOnline).

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

// routingQuote POSTs a body to /v1/quote and decodes the Quote.
func routingQuote(t *testing.T, body map[string]any) Quote {
	t.Helper()
	code, out := req(t, "POST", "/v1/quote", body, buyerKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("quote: want 200, got %d: %s", code, out)
	}
	var q Quote
	if err := json.Unmarshal(out, &q); err != nil {
		t.Fatalf("quote decode: %v (%s)", err, out)
	}
	return q
}

// TestQuoteSubstrateRouting drives the three routing shapes through the real
// HTTP quote path against a real registered fleet.
func TestQuoteSubstrateRouting(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)

	// A real fleet through the production registration + heartbeat paths, so
	// the quote's ETA is the planner's modeled makespan over real
	// worker_tps_cache rows (the same fixture shape the SLA proofs use).
	setupSLAFleet(t, ctx, 5, 200, 200*time.Millisecond)

	// (1) SMALL: 3 records is below the measured crossover (batch 8) — the
	// quote must route to the fleet, whatever the GPU numbers say.
	small := routingQuote(t, slaSubmitBody(slaInput(3)))
	if small.Routing == nil {
		t.Fatalf("a generative quote must carry a routing block: %+v", small)
	}
	if small.Routing.Substrate != "fleet" {
		t.Fatalf("3 records must route to the fleet, got %q (%s)",
			small.Routing.Substrate, small.Routing.Reason)
	}
	if small.Routing.Reason == "" {
		t.Fatal("routing reason must never be empty")
	}
	if !strings.Contains(small.Routing.Reason, "2026-07-06 a100 vllm sweep") {
		t.Errorf("reason must name the measured basis: %q", small.Routing.Reason)
	}
	if !strings.Contains(small.Routing.Basis, "A100_CAPABILITY_SWEEP") ||
		!strings.Contains(small.Routing.Basis, "[MODELED]") {
		t.Errorf("basis must name the sweep artifact and the [MODELED] label: %q", small.Routing.Basis)
	}
	if small.Routing.FleetETASecs != small.Time.P50Secs {
		t.Errorf("routing must compare the SAME fleet eta the quote shows: routing %ds vs quote p50 %ds",
			small.Routing.FleetETASecs, small.Time.P50Secs)
	}
	if small.Routing.GPUModeledSecs <= 0 {
		t.Errorf("gpu_modeled_secs must be a positive modeled figure: %v", small.Routing.GPUModeledSecs)
	}
	t.Logf("ROUTING small (3 records): substrate=%s fleet_eta=%ds gpu_modeled=%.2fs [MODELED]\n  reason: %s",
		small.Routing.Substrate, small.Routing.FleetETASecs, small.Routing.GPUModeledSecs, small.Routing.Reason)

	// (2) LARGE: 500 records is past the crossover; no lit GPU lane exists, so
	// the honest decision is a RECOMMENDATION that never refuses the job.
	large := routingQuote(t, slaSubmitBody(slaInput(500)))
	if large.Routing == nil {
		t.Fatalf("a generative quote must carry a routing block: %+v", large)
	}
	if large.Routing.Substrate != "gpu_recommend" {
		t.Fatalf("500 records with no lit gpu lane must recommend the gpu, got %q (%s)",
			large.Routing.Substrate, large.Routing.Reason)
	}
	for _, must := range []string{
		"[modeled]",                    // the numbers are labeled
		"no lit gpu lane",              // no supply is faked
		"still run this on the fleet",  // a recommendation, never a refusal
		"excludes rental/provisioning", // the comparison's honest tilt
	} {
		if !strings.Contains(large.Routing.Reason, must) {
			t.Errorf("gpu_recommend reason must contain %q: %q", must, large.Routing.Reason)
		}
	}
	if large.Routing.GPUModeledSecs <= 0 || large.Routing.FleetETASecs <= 0 {
		t.Errorf("both compared numbers must be present: %+v", large.Routing)
	}
	if float64(large.Routing.FleetETASecs) <= large.Routing.GPUModeledSecs {
		t.Errorf("gpu_recommend requires the gpu to actually model faster: fleet %ds vs gpu %.2fs",
			large.Routing.FleetETASecs, large.Routing.GPUModeledSecs)
	}
	t.Logf("ROUTING large (500 records): substrate=%s fleet_eta=%ds gpu_modeled=%.2fs [MODELED]\n  reason: %s",
		large.Routing.Substrate, large.Routing.FleetETASecs, large.Routing.GPUModeledSecs, large.Routing.Reason)

	// The decision is persisted with the quote's other assumptions
	// (quotes.quote_json) so what was believed at quote time is auditable.
	// Read the field back through a jsonb path (the column is jsonb; its text
	// form re-spaces the object, so a raw substring match would be brittle).
	var persisted string
	if err := itPool.QueryRow(ctx,
		`SELECT quote_json->'routing'->>'substrate' FROM quotes WHERE id=$1`,
		large.bareIDForTest()).Scan(&persisted); err != nil {
		t.Fatalf("read persisted quote routing: %v", err)
	}
	if persisted != "gpu_recommend" {
		t.Errorf("the routing decision must be persisted in quote_json: got %q", persisted)
	}

	// (3) EMBED: a non-generative shape gets NO routing block — the sweep
	// measured generative decode only, and we do not stretch a measurement
	// past what it measured.
	embed := routingQuote(t, map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"constraints":  map[string]any{"min_memory_gb": 2},
		"verification": map[string]any{"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true},
		"tier":         "batch",
		"input":        slaInput(20),
	})
	if embed.Routing != nil {
		t.Fatalf("an embed quote must carry NO routing block (unmeasured shape), got %+v", embed.Routing)
	}
	t.Logf("ROUTING embed (20 records): no routing block, as the honesty boundary requires")
}

// registerVLLMWorker inserts + registers ONE real vLLM-engine worker eligible for
// (raceJobType, raceModel): an nvidia_80g box with the model + job in its
// supported sets and engine='vllm', live via the real registration path. This is
// the supply that EligibleVLLMWorkerCount counts and that lights the GPU lane.
func registerVLLMWorker(t *testing.T, ctx context.Context) {
	t.Helper()
	workerID, supplierID := uuid.New(), uuid.New()
	token := "vllm-worker-" + workerID.String()
	if _, err := itPool.Exec(ctx,
		`INSERT INTO suppliers (id, email, reputation, status, data_country)
		 VALUES ($1, $2, 0.95, 'active', 'US')`,
		supplierID, "vllm-"+workerID.String()+"@computexchange.test"); err != nil {
		t.Fatalf("insert vllm supplier: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, last_seen_at)
		 VALUES ($1, $2, 'nvidia_80g', 80, now())`,
		workerID, supplierID); err != nil {
		t.Fatalf("insert vllm worker: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		 VALUES ($1, $2, $3, false)`,
		hashKey(token), workerID, supplierID); err != nil {
		t.Fatalf("insert vllm token: %v", err)
	}
	code, body := req(t, "POST", "/v1/worker/register", WorkerCapability{
		HWClass: "nvidia_80g", MemoryGB: 80, Engine: "vllm",
		SupportedJobs: []string{raceJobType}, SupportedModels: []string{raceModel},
		Benchmarks: []BenchResult{{ModelID: raceModel, JobType: raceJobType,
			TPS: 5000, ThermalOK: true, LoadMS: 3000}},
	}, hdr{"X-Worker-Token", token}, jsonCT())
	if code != 200 {
		t.Fatalf("register vllm worker: %d %s", code, body)
	}
}

// TestQuoteRoutingLitGPULaneWhenVLLMSupplyOnline is the proof that the lit-lane
// count is LIVE, not a const: with a real vLLM-engine worker online and eligible,
// a large generative quote flips from "gpu_recommend" to "gpu_lane" — the honest
// switch that lights the GPU serving lane the moment verified supply exists.
func TestQuoteRoutingLitGPULaneWhenVLLMSupplyOnline(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)

	// A fleet (so the quote has a fleet ETA to compare) PLUS one vLLM worker.
	setupSLAFleet(t, ctx, 5, 200, 200*time.Millisecond)

	// Baseline: no vLLM supply → the large batch recommends the GPU (advisory).
	before := routingQuote(t, slaSubmitBody(slaInput(500)))
	if before.Routing == nil || before.Routing.Substrate != "gpu_recommend" {
		t.Fatalf("baseline (no vLLM supply) must be gpu_recommend, got %+v", before.Routing)
	}

	// Light the lane: register a real eligible vLLM worker.
	registerVLLMWorker(t, ctx)

	// Same large input now routes to the LIT gpu lane.
	after := routingQuote(t, slaSubmitBody(slaInput(500)))
	if after.Routing == nil {
		t.Fatal("a generative quote must carry a routing block")
	}
	if after.Routing.Substrate != "gpu_lane" {
		t.Fatalf("with a vLLM worker online, 500 records must route to gpu_lane, got %q (%s)",
			after.Routing.Substrate, after.Routing.Reason)
	}
	if !strings.Contains(after.Routing.Reason, "verified vllm-lane worker(s) are online") {
		t.Errorf("gpu_lane reason must state the live vLLM supply: %q", after.Routing.Reason)
	}
	// The comparison numbers are still surfaced and still [MODELED].
	if after.Routing.GPUModeledSecs <= 0 || after.Routing.FleetETASecs <= 0 {
		t.Errorf("gpu_lane must still carry both compared numbers: %+v", after.Routing)
	}
	t.Logf("LIT LANE: 500 records → substrate=%s (was gpu_recommend before the vLLM worker registered)\n  reason: %s",
		after.Routing.Substrate, after.Routing.Reason)
}
