//go:build integration

package main

// routing_submit_integration_test.go — REAL-INFRA proof that the
// SUBSTRATE-ROUTING decision surfaces on the JOB SUBMISSION path and the
// CLEARING RECEIPT (Speed Lane road-to-ten rubric dimension 5, control/routing.go
// + quote.go + receipt.go): real Postgres + real MinIO + the real HTTP control
// plane (the shared TestMain in integration_test.go), a real registered fleet
// feeding worker_tps_cache through the production registration/heartbeat paths,
// and real POST /v1/jobs + GET /v1/jobs/{id}/receipt round-trips. It mirrors
// routing_integration_test.go's fixtures EXACTLY (setupSLAFleet, slaInput,
// slaSubmitBody, cleanSLADeterminism) so the submit-path routing block is proven
// against the SAME planner-backed fleet the quote-path proof uses — the fleet ETA
// is a real modeled makespan, not a blunt aggregate. Proves over the wire:
//
//  1. a SMALL generative submit (3 records, below the measured crossover) returns
//     response.routing.substrate == "fleet" with a non-empty reason naming the
//     measured basis;
//  2. a LARGE generative submit (500 records, past the crossover) returns
//     response.routing.substrate == "gpu_recommend" — no lit GPU lane exists
//     (the live EligibleVLLMWorkerCount is 0, no vLLM worker online) — whose
//     reason carries the honest comparison (both numbers, the GPU's labeled
//     [modeled]) and states the job still runs on the fleet if submitted;
//  3. GET /v1/jobs/{id}/receipt carries the SAME routing.substrate (a persisted
//     round-trip through jobs.routing_* → InvoiceView → receiptRouting);
//  4. an EMBED submit gets NO routing block on the submit response AND none on
//     the receipt (the sweep measured generative decode only — the honesty
//     boundary);
//  5. a "routed" job timeline event is recorded for a routed submit (the buyer
//     sees "we ran it on X because Y" in their own event stream).

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"
)

// routingSubmit POSTs a body to /v1/jobs and decodes the JobSubmitResponse,
// asserting the 202 accept the submit path returns.
func routingSubmit(t *testing.T, body map[string]any) JobSubmitResponse {
	t.Helper()
	code, out := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("submit: want 202, got %d: %s", code, out)
	}
	var resp JobSubmitResponse
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatalf("submit decode: %v (%s)", err, out)
	}
	return resp
}

// routingReceipt GETs /v1/jobs/{id}/receipt and decodes the ClearingReceipt.
func routingReceipt(t *testing.T, jobID string) ClearingReceipt {
	t.Helper()
	code, out := req(t, "GET", "/v1/jobs/"+jobID+"/receipt", nil, buyerKey())
	if code != http.StatusOK {
		t.Fatalf("receipt: want 200, got %d: %s", code, out)
	}
	var rc ClearingReceipt
	if err := json.Unmarshal(out, &rc); err != nil {
		t.Fatalf("receipt decode: %v (%s)", err, out)
	}
	return rc
}

// TestSubmitSubstrateRouting drives the routing shapes through the real HTTP
// SUBMIT + RECEIPT paths against a real planner-backed fleet.
func TestSubmitSubstrateRouting(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)

	// The SAME planner-backed fleet the quote-path routing proof uses, through the
	// production registration + heartbeat paths, so the submit's ETA is the
	// planner's modeled makespan over real worker_tps_cache rows.
	setupSLAFleet(t, ctx, 5, 200, 200*time.Millisecond)

	// (1) SMALL: 3 records is below the measured crossover (batch 8) — the submit
	// must route to the fleet, whatever the GPU numbers say.
	small := routingSubmit(t, slaSubmitBody(slaInput(3)))
	if small.Routing == nil {
		t.Fatalf("a generative submit must carry a routing block: %+v", small)
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
	// The routing block compares the SAME fleet eta the submit response quotes.
	if small.Routing.FleetETASecs != small.ETASecs {
		t.Errorf("routing must compare the SAME fleet eta the submit returns: routing %ds vs response eta %ds",
			small.Routing.FleetETASecs, small.ETASecs)
	}
	if small.Routing.GPUModeledSecs <= 0 {
		t.Errorf("gpu_modeled_secs must be a positive modeled figure: %v", small.Routing.GPUModeledSecs)
	}
	t.Logf("SUBMIT-ROUTING small (3 records): substrate=%s fleet_eta=%ds gpu_modeled=%.2fs [MODELED]\n  reason: %s",
		small.Routing.Substrate, small.Routing.FleetETASecs, small.Routing.GPUModeledSecs, small.Routing.Reason)

	// (3, small half) The receipt carries the SAME routing.substrate — a persisted
	// round-trip through jobs.routing_* → JobInvoice → receiptRouting.
	smallRC := routingReceipt(t, small.JobID.String())
	if smallRC.Routing == nil || smallRC.Routing.Substrate != small.Routing.Substrate {
		t.Fatalf("receipt must carry the SAME routing substrate as submit (%q); got %+v",
			small.Routing.Substrate, smallRC.Routing)
	}
	if smallRC.Routing.FleetETASecs != small.Routing.FleetETASecs ||
		smallRC.Routing.GPUModeledSecs != small.Routing.GPUModeledSecs {
		t.Errorf("receipt routing numbers must round-trip the persisted decision: submit %+v vs receipt %+v",
			small.Routing, smallRC.Routing)
	}
	if smallRC.Routing.Reason == "" || smallRC.Routing.Basis != small.Routing.Basis {
		t.Errorf("receipt routing must carry the reason + shared [MODELED] basis: %+v", smallRC.Routing)
	}

	// (5) A "routed" timeline event is recorded for the routed submit.
	var routedEvents int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM job_events WHERE job_id=$1 AND event='routed'`, small.JobID).Scan(&routedEvents); err != nil {
		t.Fatalf("count routed events: %v", err)
	}
	if routedEvents != 1 {
		t.Fatalf("want exactly one 'routed' timeline event, got %d", routedEvents)
	}

	// (2) LARGE: 500 records is past the crossover; no lit GPU lane exists, so the
	// honest decision is a RECOMMENDATION that never refuses the job.
	large := routingSubmit(t, slaSubmitBody(slaInput(500)))
	if large.Routing == nil {
		t.Fatalf("a generative submit must carry a routing block: %+v", large)
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
	// The submit response eta_secs is unchanged by routing (pricing/eta untouched):
	// it is a positive queue-depth/throughput estimate, and the routing block
	// compares exactly it.
	if large.ETASecs <= 0 || large.Routing.FleetETASecs != large.ETASecs {
		t.Errorf("routing must not change eta_secs; response eta %ds vs routing fleet_eta %ds",
			large.ETASecs, large.Routing.FleetETASecs)
	}
	t.Logf("SUBMIT-ROUTING large (500 records): substrate=%s fleet_eta=%ds gpu_modeled=%.2fs [MODELED]\n  reason: %s",
		large.Routing.Substrate, large.Routing.FleetETASecs, large.Routing.GPUModeledSecs, large.Routing.Reason)

	// (3, large half) The receipt carries the SAME gpu_recommend substrate.
	largeRC := routingReceipt(t, large.JobID.String())
	if largeRC.Routing == nil || largeRC.Routing.Substrate != "gpu_recommend" {
		t.Fatalf("receipt must carry the persisted gpu_recommend substrate; got %+v", largeRC.Routing)
	}
	if !strings.Contains(largeRC.Routing.Reason, "still run this on the fleet") {
		t.Errorf("receipt routing reason must round-trip the honest recommendation: %q", largeRC.Routing.Reason)
	}

	// Prove the persisted row directly (the receipt reads it, but confirm the
	// column round-trip independently of the projection).
	var persistedSub string
	var persistedFleet int
	var persistedGPU float64
	if err := itPool.QueryRow(ctx,
		`SELECT routing_substrate, routing_fleet_eta_secs, routing_gpu_modeled_secs
		   FROM jobs WHERE id=$1`, large.JobID).Scan(&persistedSub, &persistedFleet, &persistedGPU); err != nil {
		t.Fatalf("read persisted job routing: %v", err)
	}
	if persistedSub != "gpu_recommend" || persistedFleet != large.Routing.FleetETASecs || persistedGPU != large.Routing.GPUModeledSecs {
		t.Errorf("jobs.routing_* must persist the decision verbatim: (%q,%d,%.2f) vs response %+v",
			persistedSub, persistedFleet, persistedGPU, large.Routing)
	}

	// (4) EMBED: a non-generative shape gets NO routing block on the submit
	// response AND none on the receipt — the sweep measured generative decode
	// only, and we never stretch a measurement past what it measured. The job
	// row's routing columns must be NULL (routing_substrate IS NULL).
	embed := routingSubmit(t, map[string]any{
		"job_type":     map[string]any{"type": "embed"},
		"model":        map[string]any{"kind": "gguf", "ref": "all-minilm-l6-v2"},
		"constraints":  map[string]any{"min_memory_gb": 2},
		"verification": map[string]any{"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true},
		"tier":         "batch",
		"input":        slaInput(20),
	})
	if embed.Routing != nil {
		t.Fatalf("an embed submit must carry NO routing block (unmeasured shape), got %+v", embed.Routing)
	}
	embedRC := routingReceipt(t, embed.JobID.String())
	if embedRC.Routing != nil {
		t.Fatalf("an embed receipt must carry NO routing block, got %+v", embedRC.Routing)
	}
	var embedRoutingNull bool
	if err := itPool.QueryRow(ctx,
		`SELECT routing_substrate IS NULL FROM jobs WHERE id=$1`, embed.JobID).Scan(&embedRoutingNull); err != nil {
		t.Fatalf("read embed job routing null: %v", err)
	}
	if !embedRoutingNull {
		t.Errorf("an embed job must persist NULL routing columns (the honesty boundary)")
	}
	var embedRoutedEvents int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM job_events WHERE job_id=$1 AND event='routed'`, embed.JobID).Scan(&embedRoutedEvents); err != nil {
		t.Fatalf("count embed routed events: %v", err)
	}
	if embedRoutedEvents != 0 {
		t.Errorf("an embed submit must record NO 'routed' event, got %d", embedRoutedEvents)
	}
	t.Logf("SUBMIT-ROUTING embed (20 records): no routing block on submit or receipt, as the honesty boundary requires")
}
