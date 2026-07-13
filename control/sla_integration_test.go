//go:build integration

package main

// sla_integration_test.go — REAL-INFRA proof of the wall-clock speed-SLA quote
// (Speed Lane wave 2A, docs/speed-lane-reports/SLA_QUOTE_WAVE2A.md): real
// Postgres + real MinIO + the real HTTP control plane (the shared TestMain in
// integration_test.go), concurrent fake workers driving the real
// register → benchmark → quote → firm submit → claim → commit → merge →
// finalize → settle pipeline. The workers fake the GPU (they sleep a declared
// per-chunk time), so — same boundary as the wave-1B L2 proof — this proves the
// SLA MACHINERY (offer, binding, enforcement, refund, idempotency), never tok/s.
//
// Three proofs:
//  1. HONEST DEGRADATION over real HTTP: thin supply → no sla block; planner
//     disabled (CX_DISABLE_FANOUT_PLANNER's switch) → no sla block even with
//     healthy supply; healthy supply + planner → an offer whose terms obey the
//     documented formula and are persisted on the quotes row.
//  2. GUARANTEE MET: SLA quote → firm submit (binds guarantee + premium + grown
//     price cap) → fast fleet completes → sla_met=true, ZERO sla_refund rows,
//     receipt clean, and re-running the settle sweep changes nothing.
//  3. FORCED MISS (tuned guarantee, per the wave plan): the job still COMPLETES
//     (a miss refunds, never kills), exactly ONE sla_refund ledger credit for
//     exactly the premium, exactly ONE sla_missed timeline event, buyer-visible
//     fields correct, the charge amount netted — and the settle sweep re-run
//     twice more can never double-refund (idempotency, the money-truth core).

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

// setupSLAFleet registers n fake workers (same shape as the wave-1B race
// fleet: direct supplier/token rows + the REAL registration and heartbeat HTTP
// paths, so worker_tps_cache / benchmark_results / worker_model_state are
// populated by production code). All n share one declared tps + per-chunk time
// — SLA tests care about supply width and wall-clock, not heterogeneity (the
// planner's heterogeneity handling is wave 1B's own proof).
func setupSLAFleet(t *testing.T, ctx context.Context, n int, declTPS float32, perChunk time.Duration) []*raceWorker {
	t.Helper()
	fleet := make([]*raceWorker, 0, n)
	for i := 0; i < n; i++ {
		w := &raceWorker{
			name:     fmt.Sprintf("sla%d", i),
			workerID: uuid.New(),
			supplier: uuid.New(),
			declTPS:  declTPS,
			perChunk: perChunk,
		}
		w.token = fmt.Sprintf("sla-worker-%d-%s", i, w.workerID)
		if _, err := itPool.Exec(ctx,
			`INSERT INTO suppliers (id, email, reputation, status, data_country)
			 VALUES ($1, $2, 0.90, 'active', 'US')`,
			w.supplier, fmt.Sprintf("sla-%s-%s@computexchange.test", w.name, w.workerID)); err != nil {
			t.Fatalf("insert supplier %s: %v", w.name, err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO workers (id, supplier_id, hw_class, memory_gb, last_seen_at)
			 VALUES ($1, $2, 'apple_silicon_pro', 36, now())`,
			w.workerID, w.supplier); err != nil {
			t.Fatalf("insert worker %s: %v", w.name, err)
		}
		if _, err := itPool.Exec(ctx,
			`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
			 VALUES ($1, $2, $3, false)`,
			hashKey(w.token), w.workerID, w.supplier); err != nil {
			t.Fatalf("insert token %s: %v", w.name, err)
		}
		code, body := req(t, "POST", "/v1/worker/register", WorkerCapability{
			HWClass: "apple_silicon_pro", MemoryGB: 36,
			SupportedJobs: []string{raceJobType}, SupportedModels: []string{raceModel},
			Benchmarks: []BenchResult{{ModelID: raceModel, JobType: raceJobType,
				TPS: w.declTPS, ThermalOK: true, LoadMS: 2500}},
		}, hdr{"X-Worker-Token", w.token}, jsonCT())
		if code != 200 {
			t.Fatalf("register %s: %d %s", w.name, code, body)
		}
		sendRaceHeartbeat(t, w) // warm the model via the real heartbeat path
		fleet = append(fleet, w)
	}
	return fleet
}

// cleanSLADeterminism removes the cross-test state that would perturb the
// quote's planner inputs: drift history for the (type, model) and any stale
// demo-worker rate rows (same discipline as the wave-1B L2 test).
func cleanSLADeterminism(t *testing.T, ctx context.Context) {
	t.Helper()
	if _, err := itPool.Exec(ctx,
		`DELETE FROM task_durations WHERE job_type=$1 AND model_ref=$2`, raceJobType, raceModel); err != nil {
		t.Fatalf("clean task_durations: %v", err)
	}
	if _, err := itPool.Exec(ctx,
		`DELETE FROM worker_tps_cache WHERE worker_id=$1`, demoWorkerUUID); err != nil {
		t.Fatalf("clean demo tps cache: %v", err)
	}
}

// slaInput builds the n-record batch_infer JSONL used by both the quote and the
// bound submission (the sha256 binding requires byte-identical input).
func slaInput(n int) string {
	var sb strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&sb, `{"prompt":"sla p%d"}`+"\n", i)
	}
	return sb.String()
}

func slaSubmitBody(input string) map[string]any {
	return map[string]any{
		"job_type":     map[string]any{"type": raceJobType},
		"model":        map[string]any{"kind": "gguf", "ref": raceModel},
		"constraints":  map[string]any{"min_memory_gb": 8},
		"verification": map[string]any{"redundancy_frac": 0, "honeypot_frac": 0, "skip_verification_floor": true},
		"tier":         "batch",
		"input":        input,
	}
}

// quoteSLAOffer POSTs /v1/quote and returns the decoded quote.
func quoteSLAOffer(t *testing.T, input string) Quote {
	t.Helper()
	code, out := req(t, "POST", "/v1/quote", slaSubmitBody(input), buyerKey(), jsonCT())
	if code != http.StatusOK {
		t.Fatalf("quote: want 200, got %d: %s", code, out)
	}
	var q Quote
	if err := json.Unmarshal(out, &q); err != nil {
		t.Fatalf("quote decode: %v (%s)", err, out)
	}
	return q
}

func countSLARefunds(t *testing.T, ctx context.Context, jobID uuid.UUID) (n int, sum float64) {
	t.Helper()
	if err := itPool.QueryRow(ctx,
		`SELECT count(*), COALESCE(SUM(amount_usd),0)::float8 FROM ledger_entries
		 WHERE kind='sla_refund' AND payout_ref=$1`, "sla-"+jobID.String()).Scan(&n, &sum); err != nil {
		t.Fatalf("count sla refunds: %v", err)
	}
	return n, sum
}

func getJobStatus(t *testing.T, jobID uuid.UUID) JobStatus {
	t.Helper()
	code, body := req(t, "GET", "/v1/jobs/"+jobID.String(), nil, buyerKey())
	if code != 200 {
		t.Fatalf("get job: %d %s", code, body)
	}
	var js JobStatus
	if err := json.Unmarshal(body, &js); err != nil {
		t.Fatalf("job status decode: %v", err)
	}
	return js
}

// TestSLAQuoteHonestDegradation proves — over the real HTTP quote path — that
// no guarantee is ever offered without its preconditions:
//
//	a) supply below slaMinEligibleWorkers → no sla block (even with real rates),
//	b) planner disabled (the CX_DISABLE_FANOUT_PLANNER switch) → no sla block
//	   even with healthy supply,
//	c) healthy supply + planner → an offer obeying the documented formula,
//	   persisted on the quotes row exactly as returned.
func TestSLAQuoteHonestDegradation(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)
	input := slaInput(6)

	// (a) three registered workers (+ the demo worker = 4 eligible) is real
	// planner-grade supply (>= plannerMinFleetSamples measured rates) but BELOW
	// the SLA threshold of 5 — the quote must offer no guarantee.
	setupSLAFleet(t, ctx, 3, 200, 200*time.Millisecond)
	q := quoteSLAOffer(t, input)
	if q.Execution.SLAEligible {
		t.Fatalf("4 eligible workers must be below the SLA supply gate (5): %+v", q.Execution)
	}
	if q.SLA != nil {
		t.Fatalf("no guarantee may be offered below the supply gate, got %+v", q.SLA)
	}

	// (b) two more workers clear the supply gate — but with the planner disabled
	// there is no measured basis, so still no guarantee (the ETA falls back to
	// the blunt pre-wave formula; a promise on it would be a guess).
	setupSLAFleet(t, ctx, 2, 200, 200*time.Millisecond)
	fanoutPlannerEnabled.Store(false)
	q = quoteSLAOffer(t, input)
	if !q.Execution.SLAEligible {
		t.Fatalf("6 eligible workers must clear the SLA supply gate: %+v", q.Execution)
	}
	if q.SLA != nil {
		t.Fatalf("no guarantee may be offered with the planner disabled, got %+v", q.SLA)
	}

	// (c) planner back on: the offer appears and its terms obey the formula.
	fanoutPlannerEnabled.Store(true)
	q = quoteSLAOffer(t, input)
	if q.SLA == nil {
		t.Fatalf("healthy supply + planner-backed ETA must offer a guarantee: %+v", q)
	}
	if q.SLA.GuaranteedSecs != slaGuaranteedSecs(q.SLA.ConservativeModelSecs) {
		t.Fatalf("guarantee %ds must equal the documented formula over its own conservative band %ds",
			q.SLA.GuaranteedSecs, q.SLA.ConservativeModelSecs)
	}
	if q.SLA.ConservativeModelSecs < q.Time.P50Secs {
		t.Fatalf("the guarantee basis must be the CONSERVATIVE band (>= p50): band %ds p50 %ds",
			q.SLA.ConservativeModelSecs, q.Time.P50Secs)
	}
	if want := roundUSD(q.Cost.ExpectedUSD * slaPremiumRate); q.SLA.PremiumUSD != want {
		t.Fatalf("premium %v must be the documented %.0f%% of expected (%v)", q.SLA.PremiumUSD, slaPremiumRate*100, want)
	}
	// The offer is persisted with the quote's other assumptions.
	var gotSecs int
	var gotPremium float64
	if err := itPool.QueryRow(ctx,
		`SELECT COALESCE(sla_guaranteed_secs,0), COALESCE(sla_premium_usd,0)::float8
		   FROM quotes WHERE id=$1`, q.bareIDForTest()).Scan(&gotSecs, &gotPremium); err != nil {
		t.Fatalf("read persisted quote sla: %v", err)
	}
	if gotSecs != q.SLA.GuaranteedSecs || gotPremium != q.SLA.PremiumUSD {
		t.Fatalf("persisted offer (%d, %v) must match the returned offer (%d, %v)",
			gotSecs, gotPremium, q.SLA.GuaranteedSecs, q.SLA.PremiumUSD)
	}
	t.Logf("SLA offer over real HTTP: guaranteed=%ds (band %ds × %.2f + %ds) premium=$%.6f",
		q.SLA.GuaranteedSecs, q.SLA.ConservativeModelSecs, q.SLA.SafetyMarginFactor,
		q.SLA.MergeAllowanceSecs, q.SLA.PremiumUSD)
}

// bareIDForTest recovers the quotes.id UUID from the wire handle (the bareID
// field is deliberately not serialized).
func (q Quote) bareIDForTest() uuid.UUID {
	id, err := quoteIDToUUID(q.QuoteID)
	if err != nil {
		panic(err)
	}
	return id
}

// runSLAFleet starts the fleet's sim workers and returns a stop function.
func runSLAFleet(t *testing.T, fleet []*raceWorker) (stop func()) {
	t.Helper()
	wctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	for _, w := range fleet {
		wg.Add(1)
		go runSimWorker(wctx, t, w, &wg)
	}
	return func() {
		cancel()
		wg.Wait()
	}
}

// TestSLAQuoteFirmSubmitGuaranteeMet is proof (2): the full happy path. The
// guarantee binds at submit (jobs row + grown firm cap + timeline event), a
// fast fleet completes well inside it, the commit-path finalize records
// sla_met=true, no refund exists, the receipt is clean — and re-running the
// settle sweep is a no-op.
func TestSLAQuoteFirmSubmitGuaranteeMet(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)

	fleet := setupSLAFleet(t, ctx, 5, 200, 200*time.Millisecond)
	input := slaInput(6)
	q := quoteSLAOffer(t, input)
	if q.SLA == nil {
		t.Fatalf("precondition: quote must carry an SLA offer: %+v", q.Execution)
	}

	// Firm submit binds price cap + time guarantee in one commitment package.
	body := slaSubmitBody(input)
	body["quote_id"] = q.QuoteID
	body["firm_quote"] = true
	code, out := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("firm submit: want 202, got %d: %s", code, out)
	}
	var resp JobSubmitResponse
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatalf("submit decode: %v", err)
	}

	// The binding is on the jobs row: guarantee + premium stamped, firm cap grown
	// by exactly the premium, estimate carrying the premium.
	var gSecs int
	var gPremium, firmMax, estimated float64
	if err := itPool.QueryRow(ctx,
		`SELECT COALESCE(sla_guarantee_secs,0), COALESCE(sla_premium_usd,0)::float8,
		        COALESCE(firm_quote_max_usd,0)::float8, COALESCE(estimated_usd,0)::float8
		   FROM jobs WHERE id=$1`, resp.JobID).Scan(&gSecs, &gPremium, &firmMax, &estimated); err != nil {
		t.Fatalf("read bound job: %v", err)
	}
	if gSecs != q.SLA.GuaranteedSecs || gPremium != q.SLA.PremiumUSD {
		t.Fatalf("binding must stamp exactly the quoted offer: job (%d, %v) vs quote (%d, %v)",
			gSecs, gPremium, q.SLA.GuaranteedSecs, q.SLA.PremiumUSD)
	}
	if want := roundUSD(q.Cost.MaxUSD + q.SLA.PremiumUSD); roundUSD(firmMax) != want {
		t.Fatalf("firm cap must grow by exactly the premium: got %v want %v", firmMax, want)
	}
	if estimated <= 0 {
		t.Fatalf("estimate must be positive (and carries the premium): %v", estimated)
	}
	var slaBoundEvents int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM job_events WHERE job_id=$1 AND event='sla_bound'`, resp.JobID).Scan(&slaBoundEvents); err != nil {
		t.Fatalf("count sla_bound events: %v", err)
	}
	if slaBoundEvents != 1 {
		t.Fatalf("want exactly one sla_bound timeline event, got %d", slaBoundEvents)
	}

	// Fast fleet completes far inside the guarantee (>= 62s by construction).
	stop := runSLAFleet(t, fleet)
	elapsed := waitJobComplete(t, resp.JobID, time.Now(), 120*time.Second)
	stop()
	t.Logf("MEASURED buyer-visible completion: %s (guarantee %ds)", elapsed.Round(10*time.Millisecond), gSecs)

	// The commit-path finalize settled the outcome synchronously: met, no refund.
	js := getJobStatus(t, resp.JobID)
	if js.SLAMet == nil || !*js.SLAMet {
		t.Fatalf("sla_met must be true after an in-guarantee completion: %+v", js)
	}
	if js.SLAGuaranteeSecs != gSecs || js.SLAPremiumUSD != gPremium {
		t.Fatalf("GET /v1/jobs must expose the bound guarantee: %+v", js)
	}
	if n, sum := countSLARefunds(t, ctx, resp.JobID); n != 0 || sum != 0 {
		t.Fatalf("a met SLA must record NO refund, got %d rows ($%v)", n, sum)
	}

	// Re-running the settle sweep (the backstop) is a pure no-op.
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	for i := 0; i < 2; i++ {
		if err := wk.settleSLAOutcomes(ctx); err != nil {
			t.Fatalf("settle sweep %d: %v", i, err)
		}
	}
	if n, _ := countSLARefunds(t, ctx, resp.JobID); n != 0 {
		t.Fatalf("settle sweep re-run must not invent a refund, got %d rows", n)
	}
	js = getJobStatus(t, resp.JobID)
	if js.SLAMet == nil || !*js.SLAMet {
		t.Fatalf("sla_met must remain true after sweep re-runs: %+v", js)
	}

	// The receipt (which embeds the invoice) shows the SLA facts and no refund.
	code, out = req(t, "GET", "/v1/jobs/"+resp.JobID.String()+"/receipt", nil, buyerKey())
	if code != 200 {
		t.Fatalf("receipt: %d %s", code, out)
	}
	var rc ClearingReceipt
	if err := json.Unmarshal(out, &rc); err != nil {
		t.Fatalf("receipt decode: %v", err)
	}
	if rc.Invoice == nil || rc.Invoice.SLAMet == nil || !*rc.Invoice.SLAMet {
		t.Fatalf("receipt invoice must note the SLA was met: %+v", rc.Invoice)
	}
	if rc.Invoice.SLARefundUSD != nil {
		t.Fatalf("receipt must show no refund on a met SLA: %+v", rc.Invoice)
	}
}

// TestSLAForcedMissRefundsExactlyOnce is proof (3): the miss remedy, forced by
// a tuned guarantee (1s — the wave plan's sanctioned forcing lever) against a
// deliberately slow fleet. The job still completes; the refund is recorded
// EXACTLY once no matter how many settle sites/sweep re-runs observe the miss;
// the buyer sees the outcome everywhere it should appear; and the collectable
// charge is netted by exactly the refund.
func TestSLAForcedMissRefundsExactlyOnce(t *testing.T) {
	reset(t)
	ctx := context.Background()
	cleanSLADeterminism(t, ctx)
	prev := fanoutPlannerEnabled.Load()
	t.Cleanup(func() { fanoutPlannerEnabled.Store(prev) })
	fanoutPlannerEnabled.Store(true)

	// Slow fleet: every chunk takes 1.5s of declared wall-clock, so the
	// buyer-visible span is guaranteed to exceed the tuned 1s guarantee.
	fleet := setupSLAFleet(t, ctx, 5, 200, 1500*time.Millisecond)
	input := slaInput(6)
	q := quoteSLAOffer(t, input)
	if q.SLA == nil {
		t.Fatalf("precondition: quote must carry an SLA offer: %+v", q.Execution)
	}

	body := slaSubmitBody(input)
	body["quote_id"] = q.QuoteID
	body["firm_quote"] = true
	code, out := req(t, "POST", "/v1/jobs", body, buyerKey(), jsonCT())
	if code != http.StatusAccepted {
		t.Fatalf("firm submit: want 202, got %d: %s", code, out)
	}
	var resp JobSubmitResponse
	if err := json.Unmarshal(out, &resp); err != nil {
		t.Fatalf("submit decode: %v", err)
	}

	// Force the miss: tune the bound guarantee down to the 1s floor. The quoted
	// premium (the remedy) stays exactly as bound.
	if _, err := itPool.Exec(ctx,
		`UPDATE jobs SET sla_guarantee_secs = 1 WHERE id=$1`, resp.JobID); err != nil {
		t.Fatalf("tune guarantee: %v", err)
	}

	stop := runSLAFleet(t, fleet)
	elapsed := waitJobComplete(t, resp.JobID, time.Now(), 120*time.Second)
	stop()
	if elapsed <= 1500*time.Millisecond {
		t.Fatalf("sanity: the slow fleet's span must exceed one declared chunk, got %s", elapsed)
	}
	t.Logf("MEASURED buyer-visible completion: %s vs tuned 1s guarantee (forced miss)", elapsed.Round(10*time.Millisecond))

	// The job COMPLETED — a miss refunds, never kills.
	js := getJobStatus(t, resp.JobID)
	if js.Status != "complete" {
		t.Fatalf("a missed SLA must still complete the job, status=%s", js.Status)
	}
	if js.SLAMet == nil || *js.SLAMet {
		t.Fatalf("sla_met must be false after the miss: %+v", js)
	}

	// Exactly one refund, for exactly the bound premium (premium < actual here,
	// so the cap in slaRefundAmount does not bite).
	n, sum := countSLARefunds(t, ctx, resp.JobID)
	if n != 1 {
		t.Fatalf("want exactly 1 sla_refund ledger row, got %d", n)
	}
	if roundUSD(sum) != q.SLA.PremiumUSD {
		t.Fatalf("refund must be exactly the premium: got $%v want $%v", sum, q.SLA.PremiumUSD)
	}

	// IDEMPOTENCY — the money-truth core: every settle surface re-observes the
	// same miss and must change nothing. Direct re-settle + two sweep re-runs.
	settleSLAOutcome(ctx, itStore, resp.JobID)
	wk := NewWorkers(itStore, itStorage, stubPayout{})
	for i := 0; i < 2; i++ {
		if err := wk.settleSLAOutcomes(ctx); err != nil {
			t.Fatalf("settle sweep %d: %v", i, err)
		}
	}
	if n, _ = countSLARefunds(t, ctx, resp.JobID); n != 1 {
		t.Fatalf("re-running the settle sweep double-refunded: %d rows", n)
	}
	var missEvents int
	if err := itPool.QueryRow(ctx,
		`SELECT count(*) FROM job_events WHERE job_id=$1 AND event='sla_missed'`, resp.JobID).Scan(&missEvents); err != nil {
		t.Fatalf("count sla_missed events: %v", err)
	}
	if missEvents != 1 {
		t.Fatalf("want exactly one sla_missed timeline event, got %d", missEvents)
	}

	// The refund nets the collectable amount on BOTH charge paths' shared math.
	var actual float64
	if err := itPool.QueryRow(ctx,
		`SELECT COALESCE(actual_usd,0)::float8 FROM jobs WHERE id=$1`, resp.JobID).Scan(&actual); err != nil {
		t.Fatalf("read actual: %v", err)
	}
	_, charge, err := itStore.JobChargeInfo(ctx, resp.JobID)
	if err != nil {
		t.Fatalf("JobChargeInfo: %v", err)
	}
	if want := roundUSD(actual - sum); roundUSD(charge) != want {
		t.Fatalf("collectable charge must be actual minus the refund: got %v want %v (actual %v refund %v)",
			charge, want, actual, sum)
	}

	// Buyer-visible money story on the receipt: premium + refund + outcome.
	code, out = req(t, "GET", "/v1/jobs/"+resp.JobID.String()+"/receipt", nil, buyerKey())
	if code != 200 {
		t.Fatalf("receipt: %d %s", code, out)
	}
	var rc ClearingReceipt
	if err := json.Unmarshal(out, &rc); err != nil {
		t.Fatalf("receipt decode: %v", err)
	}
	inv := rc.Invoice
	if inv == nil || inv.SLAMet == nil || *inv.SLAMet {
		t.Fatalf("receipt invoice must record the miss: %+v", inv)
	}
	if inv.SLARefundUSD == nil || roundUSD(*inv.SLARefundUSD) != q.SLA.PremiumUSD {
		t.Fatalf("receipt must show the real refund ($%v): %+v", q.SLA.PremiumUSD, inv)
	}
	if inv.SLAPremiumUSD == nil || *inv.SLAPremiumUSD != q.SLA.PremiumUSD {
		t.Fatalf("receipt must show the premium that was bound: %+v", inv)
	}
	t.Logf("MISS remedy proven: 1 refund row ($%.6f), 1 timeline event, charge netted %v→%v, %d sweep re-runs changed nothing",
		sum, actual, charge, 3)
}
