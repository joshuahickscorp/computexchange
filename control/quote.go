package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// quote.go — Plane C / Compute Autopilot (docs/PLANE_C.md): the buyer-facing
// quote. POST /v1/quote takes the same submission shape as /v1/jobs but spends no
// money and creates no job — it SCANS the input, assembles a conservative
// cost/ETA/supply/risk band from machinery that already exists (price estimate,
// adaptive split, queue/worker counts, the model catalogue), persists what it
// believed (quotes table), and returns a structured quote.
//
// Honesty rules (PLANE_C §26, BLACKHOLE): the quote never hides uncertainty,
// never promises an exact cost when the token count is sampled/heuristic, and
// never fakes supply — `eligible_now` is a real count of workers that would pass
// the claim hard filter for THIS job. Where a signal does not exist yet (warm
// model state, per-worker historical throughput), the quote says so in
// `confidence.reasons` and leans conservative, rather than inventing a number.

// --- input scanner (PLANE_C §8) -------------------------------------------------

// fieldSampleN is how many leading valid records we inspect to infer field names.
const fieldSampleN = 512

// bytesPerTokenHeuristic is the documented byte→token divisor for the estimate.
// It is intentionally crude (≈4 bytes/token for English-ish text) and surfaced as
// an estimate, never as an exact count — a real per-model tokenizer is a later
// upgrade (PLANE_C §28 open decision).
const bytesPerTokenHeuristic = 4.0

// QuoteInputScan is the preflight scan of a JSONL input (PLANE_C §8). Every figure
// is real; `estimated_tokens` is explicitly a heuristic.
type QuoteInputScan struct {
	Records          int      `json:"records"`          // non-blank JSONL lines
	Bytes            int      `json:"bytes"`            // total input bytes
	EstimatedTokens  int64    `json:"estimated_tokens"` // byte/token heuristic, not exact
	MalformedRecords int      `json:"malformed_records"`
	FirstBadLine     int      `json:"first_bad_line"` // 1-based line of the first malformed record; 0 = none
	MaxLineBytes     int      `json:"max_line_bytes"`
	SampledRecords   int      `json:"sampled_records"` // records inspected for field names
	DetectedFields   []string `json:"detected_fields"` // sorted union of top-level keys in the sample
}

// scanJSONL walks JSONL bytes once and reports a real preflight scan: record
// count, byte count, malformed count + first bad line, max line size, a token
// heuristic, and the union of top-level field names across the first `fieldSampleN`
// valid records. Pure — no I/O, fully unit-tested.
func scanJSONL(data []byte) QuoteInputScan {
	scan := QuoteInputScan{}
	fields := map[string]bool{}
	lineNo := 0
	for _, raw := range bytes.Split(data, []byte("\n")) {
		lineNo++
		ln := bytes.TrimRight(raw, "\r")
		if len(bytes.TrimSpace(ln)) == 0 {
			continue // blank line carries no record
		}
		scan.Records++
		scan.Bytes += len(ln)
		if len(ln) > scan.MaxLineBytes {
			scan.MaxLineBytes = len(ln)
		}
		if !json.Valid(ln) {
			scan.MalformedRecords++
			if scan.FirstBadLine == 0 {
				scan.FirstBadLine = lineNo
			}
			continue
		}
		if scan.SampledRecords < fieldSampleN {
			var obj map[string]json.RawMessage
			if json.Unmarshal(ln, &obj) == nil {
				for k := range obj {
					fields[k] = true
				}
			}
			scan.SampledRecords++
		}
	}
	scan.EstimatedTokens = int64(math.Ceil(float64(scan.Bytes) / bytesPerTokenHeuristic))
	scan.DetectedFields = sortedKeys(fields)
	return scan
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	// small N — insertion sort keeps it dependency-free and deterministic.
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j-1] > out[j]; j-- {
			out[j-1], out[j] = out[j], out[j-1]
		}
	}
	return out
}

// --- quote object (PLANE_C §7) --------------------------------------------------

// Quote is the central Plane C object returned by POST /v1/quote.
//
// QuoteID is the buyer-facing handle, shaped "q_<uuid>". The bare <uuid> is the
// quotes.id primary key (id is a real UUID column), so a buyer can bind a later
// submission by passing back either the "q_" handle or the bare uuid (the bind
// path strips the prefix — see quoteIDToUUID). ExpiresAt bounds how long the quote
// stays bindable (Plane D D7); InputSHA256 lets the submit path best-effort confirm
// the bytes match what was quoted. bareID carries the parsed uuid to InsertQuote
// without putting a redundant field on the wire.
type Quote struct {
	QuoteID     string          `json:"quote_id"`
	JobType     string          `json:"job_type"`
	Model       string          `json:"model"`
	Tier        string          `json:"tier"`
	Input       QuoteInputScan  `json:"input"`
	Execution   QuoteExecution  `json:"execution"`
	Cost        QuoteCost       `json:"cost"`
	Time        QuoteTime       `json:"time"`
	Confidence  QuoteConfidence `json:"confidence"`
	Budget      QuoteBudget     `json:"budget"`
	Warnings    []string        `json:"warnings"`
	ExpiresAt   time.Time       `json:"expires_at"`   // quote stops being bindable after this (Plane D D7)
	InputSHA256 string          `json:"input_sha256"` // sha256 of the scanned input bytes (best-effort submit match)

	bareID uuid.UUID // quotes.id primary key (the <uuid> inside QuoteID); not on the wire
}

// quoteTTL is how long an advisory quote stays bindable to a submission (Plane D
// D7). A submission carrying an expired quote_id is rejected 409.
const quoteTTL = 15 * time.Minute

type QuoteExecution struct {
	RecommendedSplitSize int     `json:"recommended_split_size"`
	EstimatedTasks       int     `json:"estimated_tasks"`
	EligibleWorkersNow   int     `json:"eligible_workers_now"`
	WarmEligibleWorkers  int     `json:"warm_eligible_workers"` // eligible workers that ALSO have the model warm (warm-routing, D3)
	ModelMinMemoryGB     float32 `json:"model_min_memory_gb"`   // catalogue floor; the per-task memory requirement
	OOMRisk              string  `json:"oom_risk"`              // low|medium|high
	ColdStartRisk        string  `json:"cold_start_risk"`       // low|medium|high
	SLAEligible          bool    `json:"sla_eligible"`          // supply >= threshold → a project-SLA ETA is offerable (research §6.2 launch gate)
	PoolReputation       float64 `json:"pool_reputation"`       // avg reputation (0..1) of the eligible supplier pool (routing transparency, research §4)
}

// slaMinEligibleWorkers is the minimum eligible supply before a project-priced SLA ETA
// is offered (DEEP_RESEARCH_V2 §6.2 launch gate; "probably 5-10"). Below it the quote is
// advisory only — the failure mode (SLA miss, no revenue, lost buyer) is worse than
// declining to promise.
const slaMinEligibleWorkers = 5

type QuoteCost struct {
	MinUSD                  float64 `json:"min_usd"`
	ExpectedUSD             float64 `json:"expected_usd"`
	MaxUSD                  float64 `json:"max_usd"`
	VerificationOverheadUSD float64 `json:"verification_overhead_usd"`
	PlatformTakeUSD         float64 `json:"platform_take_usd"`
}

type QuoteTime struct {
	P50Secs       int `json:"p50_secs"`
	P90Secs       int `json:"p90_secs"`
	WorstCaseSecs int `json:"worst_case_secs"`
}

type QuoteConfidence struct {
	Score   float64  `json:"score"`
	Reasons []string `json:"reasons"`
}

type QuoteBudget struct {
	SuggestedMaxUSD       float64 `json:"suggested_max_usd"`
	CancelBeforeExceeding bool    `json:"cancel_before_exceeding"`
}

// The quote's platform-take line uses the same configurable rate as the ledger
// (payment.go's platformTakeRate — a flat 1–5% via CX_PLATFORM_TAKE_PCT), so the
// quote and the eventual invoice agree on the cut.

// buildQuote assembles a conservative quote from the scanned input + live exchange
// state, reusing the same estimators the real submission path uses (so a quote and
// the eventual job agree). It performs NO writes and creates no job.
func (s *Server) buildQuote(ctx context.Context, sub jobSubmit, inputBytes []byte) Quote {
	jobType := sub.JobType.Type
	tier := sub.Tier
	scan := scanJSONL(inputBytes)

	avgLineBytes := 0.0
	if scan.Records > 0 {
		avgLineBytes = float64(len(inputBytes)) / float64(scan.Records)
	}
	split := adaptiveSplitSize(jobType, sub.Params, avgLineBytes)
	tasks := 0
	if scan.Records > 0 && split > 0 {
		tasks = (scan.Records + split - 1) / split
	}

	// Expected cost via the SAME estimator the submission uses; band it for honest
	// uncertainty (a sampled token estimate + variable verification/retry overhead).
	expected := s.estimateJobUSD(ctx, sub.Model.Ref, inputBytes, scan.Records, tier)
	verifOverhead := roundUSD(expected * float64(sub.Verification.RedundancyFrac+sub.Verification.HoneypotFrac))
	platformTake := roundUSD((expected + verifOverhead) * platformTakeRate)
	costMin := roundUSD(expected * 0.85)
	costMax := roundUSD((expected + verifOverhead) * 1.5)

	// ETA: the existing queue-depth/throughput estimate is the p50; band it up for
	// p90/worst (cold starts, retries, contention) rather than promising a point.
	// Model-aware: once this (job_type, model) has enough committed history the p50
	// is driven by the OBSERVED p90 per-task duration, not the static target (Plane
	// D D6 drift feedback — the quote and reality converge as the Brain learns).
	p50 := s.estimateETASecs(ctx, jobType, sub.Model.Ref, tasks)
	eta := QuoteTime{P50Secs: p50, P90Secs: p50 * 2, WorstCaseSecs: p50 * 4}

	// Real supply: workers that would pass the claim hard filter for THIS job.
	var minMem float32 = sub.Constraints.MinMemoryGB
	var modelMinMem float32
	if m, err := s.store.GetModel(ctx, sub.Model.Ref); err == nil {
		modelMinMem = m.MinMemoryGB
		if modelMinMem > minMem {
			minMem = modelMinMem // a job implicitly needs at least the model's floor
		}
	}
	eligibleNow, _ := s.store.EligibleWorkerCount(ctx, jobType, sub.Model.Ref, minMem)
	// Warm supply (warm-routing, D3): eligible workers that already have THIS model
	// loaded. Drives cold-start risk down + confidence up when present — these are the
	// workers the scheduler prefers, so the job likely starts without a cold load.
	warmEligible, _ := s.store.WarmEligibleWorkerCount(ctx, jobType, sub.Model.Ref, minMem)

	oomRisk, coldRisk, conf, warnings := assessRisk(scan, eligibleNow, warmEligible, modelMinMem)

	// Supply-density gate (DEEP_RESEARCH_V2 §6.2 launch gate) + routing transparency
	// (§4): only promise a project-SLA ETA when enough workers are eligible right now,
	// and surface the eligible pool's average reputation so the buyer sees the quality
	// of supply their job routes to.
	poolRep, _ := s.store.EligiblePoolReputation(ctx, jobType, sub.Model.Ref, minMem)
	slaEligible := eligibleNow >= slaMinEligibleWorkers
	if !slaEligible {
		warnings = append(warnings, fmt.Sprintf(
			"supply below the SLA threshold (%d eligible, need %d): ETA is advisory only, no project-SLA guarantee",
			eligibleNow, slaMinEligibleWorkers))
	}

	// Memory-floor feedback (Plane D D4): if the model's floor exceeds the MEDIAN
	// effective memory of eligible workers (from the rolling samples), the typical
	// eligible box is tight on this model even though the binary count passed —
	// bump oom_risk + lower confidence with an explainable reason. When no eligible
	// worker has reported a sample yet, ok is false and risk is left untouched (we
	// never invent a median).
	if modelMinMem > 0 {
		if median, ok, err := s.store.MedianEffectiveMemoryGB(ctx, jobType, sub.Model.Ref); err == nil && ok {
			oomRisk, conf = applyMemoryFloorRisk(oomRisk, conf, modelMinMem, median)
		}
	}

	// The quote id is a real UUID stored as quotes.id; the buyer-facing handle is
	// "q_<uuid>" (keeps the existing cx-quote display). input_sha256 fingerprints the
	// scanned bytes so a later submit can best-effort confirm it quoted THIS input.
	bareID := uuid.New()
	sum := sha256.Sum256(inputBytes)

	return Quote{
		QuoteID:     "q_" + bareID.String(),
		bareID:      bareID,
		ExpiresAt:   time.Now().Add(quoteTTL).UTC(),
		InputSHA256: hex.EncodeToString(sum[:]),
		JobType:     jobType,
		Model:       sub.Model.Ref,
		Tier:        tier,
		Input:       scan,
		Execution: QuoteExecution{
			RecommendedSplitSize: split,
			EstimatedTasks:       tasks,
			EligibleWorkersNow:   eligibleNow,
			WarmEligibleWorkers:  warmEligible,
			ModelMinMemoryGB:     modelMinMem,
			OOMRisk:              oomRisk,
			ColdStartRisk:        coldRisk,
			SLAEligible:          slaEligible,
			PoolReputation:       poolRep,
		},
		Cost: QuoteCost{
			MinUSD: costMin, ExpectedUSD: expected, MaxUSD: costMax,
			VerificationOverheadUSD: verifOverhead, PlatformTakeUSD: platformTake,
		},
		Time:       eta,
		Confidence: conf,
		Budget: QuoteBudget{
			// Suggest a cap at the conservative top of the band so a normal run
			// completes but a runaway is stopped (PLANE_C §12 budget cap).
			SuggestedMaxUSD:       costMax,
			CancelBeforeExceeding: true,
		},
		Warnings: warnings,
	}
}

// assessRisk derives OOM/cold-start risk + a confidence score + warnings from the
// scan and live supply. Conservative and explainable: it leans toward higher risk
// and lower confidence when data is missing, and every downgrade carries a reason.
// warmEligible is the count of eligible workers that already have the model warm
// (warm-routing, D3): when > 0 the job likely starts without a cold model load, so
// cold-start risk drops to low and confidence rises; when 0 the cold-start estimate
// stays conservative (a load is possible) — never faked low.
func assessRisk(scan QuoteInputScan, eligibleNow, warmEligible int, modelMinMem float32) (oom, cold string, conf QuoteConfidence, warnings []string) {
	reasons := []string{}
	score := 0.8

	// Supply drives OOM + confidence: the eligible count already filters on
	// effective memory + not-throttled, so >0 eligible means real headroom exists.
	switch {
	case eligibleNow == 0:
		oom = "high"
		score -= 0.3
		reasons = append(reasons, "no workers currently pass the memory/model filter for this job")
		warnings = append(warnings, "no eligible workers online right now; the job may queue until supply appears")
	case eligibleNow < 3:
		oom = "medium"
		score -= 0.1
		reasons = append(reasons, fmt.Sprintf("%d eligible worker(s) with enough effective memory (thin supply)", eligibleNow))
	default:
		oom = "low"
		reasons = append(reasons, fmt.Sprintf("%d eligible workers have enough effective memory", eligibleNow))
	}
	if modelMinMem > 0 {
		reasons = append(reasons, fmt.Sprintf("model memory floor is %.0f GB; supply count is filtered against effective memory", modelMinMem))
	} else {
		reasons = append(reasons, "model not in the catalogue; using a conservative default price + no memory floor")
		score -= 0.1
	}

	// Cold-start risk from REAL warm-model state (warm-routing, D3). Warm eligible
	// workers already have the model loaded, so the job can start without a cold load:
	// risk drops to low and confidence rises. With no warm supply we stay conservative
	// (medium) and say so — never faked low.
	if warmEligible > 0 {
		cold = "low"
		score += 0.1
		reasons = append(reasons, fmt.Sprintf("%d eligible worker(s) already have this model warm; cold-start unlikely", warmEligible))
	} else {
		cold = "medium"
		reasons = append(reasons, "no eligible worker currently has this model warm; a cold model load is possible")
	}

	// Input quality.
	if scan.Records == 0 {
		score -= 0.4
		warnings = append(warnings, "input has no records")
	}
	if scan.MalformedRecords > 0 {
		score -= 0.2
		warnings = append(warnings, fmt.Sprintf("%d malformed JSONL record(s); first at line %d", scan.MalformedRecords, scan.FirstBadLine))
	}
	reasons = append(reasons, "token count is a byte heuristic, not an exact tokenizer count")

	if score < 0.05 {
		score = 0.05
	}
	if score > 0.95 {
		score = 0.95
	}
	return oom, cold, QuoteConfidence{Score: roundUSD(score), Reasons: reasons}, warnings
}

// applyMemoryFloorRisk bumps OOM risk + lowers confidence when the model's memory
// floor exceeds the MEDIAN effective memory of eligible workers (from the rolling
// worker_memory_samples — Plane D D4). The eligible COUNT can pass (some workers
// clear the floor) while the TYPICAL eligible box is still tight; this catches that
// honestly. Deterministic and explainable: a floor over the median escalates one
// step (low→medium→high) and shaves confidence with a stated reason; a floor that
// merely approaches the median (within memFloorTightMargin) adds a softer caution
// without escalating. Pure — no I/O, fully unit-tested. Called only when a real
// median exists; never fabricates supply.
func applyMemoryFloorRisk(oom string, conf QuoteConfidence, modelMinMem, medianEffectiveGB float32) (string, QuoteConfidence) {
	switch {
	case modelMinMem > medianEffectiveGB:
		oom = escalateRisk(oom)
		conf.Score = roundUSD(clampScore(conf.Score - 0.15))
		conf.Reasons = append(conf.Reasons, fmt.Sprintf(
			"model memory floor %.0f GB exceeds the median effective memory of eligible workers (%.1f GB); the typical eligible worker is tight on this model",
			modelMinMem, medianEffectiveGB))
	case medianEffectiveGB-modelMinMem <= memFloorTightMargin:
		conf.Score = roundUSD(clampScore(conf.Score - 0.05))
		conf.Reasons = append(conf.Reasons, fmt.Sprintf(
			"model memory floor %.0f GB is close to the median effective memory of eligible workers (%.1f GB); little headroom",
			modelMinMem, medianEffectiveGB))
	default:
		conf.Reasons = append(conf.Reasons, fmt.Sprintf(
			"median effective memory of eligible workers (%.1f GB) comfortably clears the model floor (%.0f GB)",
			medianEffectiveGB, modelMinMem))
	}
	return oom, conf
}

// memFloorTightMargin is the GB band above the model floor within which the median
// eligible worker counts as "tight" (a soft confidence shave, no risk escalation).
const memFloorTightMargin = 2.0

// escalateRisk bumps a low|medium|high risk one step toward high (high stays high).
func escalateRisk(r string) string {
	switch r {
	case "low":
		return "medium"
	case "medium":
		return "high"
	default:
		return "high"
	}
}

// clampScore keeps a confidence score inside the same [0.05, 0.95] band assessRisk
// uses, so memory-floor feedback can never push confidence to a fake 0 or 1.
func clampScore(s float64) float64 {
	if s < 0.05 {
		return 0.05
	}
	if s > 0.95 {
		return 0.95
	}
	return s
}

// --- handler --------------------------------------------------------------------

// handleQuote (POST /v1/quote, buyer-authed): scan + price a submission without
// spending. Validates the shape like createJob, resolves the input, builds the
// quote, persists the assumptions, and returns 200 with the Quote.
func (s *Server) handleQuote(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var sub jobSubmit
	if err := json.NewDecoder(r.Body).Decode(&sub); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid quote request json: "+err.Error())
		return
	}
	if sub.JobType.Type == "" || !validJobTypes[sub.JobType.Type] {
		writeErr(w, http.StatusBadRequest, "valid job_type.type is required")
		return
	}
	if sub.Tier == "" {
		sub.Tier = "batch"
	}
	if !validTiers[sub.Tier] {
		writeErr(w, http.StatusBadRequest, "invalid tier: "+sub.Tier)
		return
	}
	inputBytes, _, err := s.resolveInput(r.Context(), sub.Input)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "resolving input: "+err.Error())
		return
	}
	q := s.buildQuote(r.Context(), sub, inputBytes)
	if err := s.store.InsertQuote(r.Context(), auth.BuyerID, q); err != nil {
		// Persisting the quote is the load-bearing rule; a failure is a real error,
		// not silently swallowed (the buyer still gets the quote, but we log loudly).
		writeErr(w, http.StatusInternalServerError, "persisting quote: "+err.Error())
		return
	}
	metrics.quotes.Add(1) // observability (Plane D D21): a quote was priced + persisted
	writeJSON(w, http.StatusOK, q)
}

// --- store (quote-specific *Store methods live with the quote, per benchmark.go) ---

// EligibleWorkerCount counts workers that would pass the claim hard filter for a
// job of (jobType, modelRef, minMemGB) AND are live (seen <60s): supported job +
// model, enough effective memory (falling back to total pre-heartbeat), not
// throttled, supplier active. This is the SAME predicate ClaimTask uses, so the
// quote's `eligible_now` is the honest current supply, not a raw worker count.
func (s *Store) EligibleWorkerCount(ctx context.Context, jobType, modelRef string, minMemGB float32) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*)
		   FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		  WHERE w.last_seen_at IS NOT NULL
		    AND w.last_seen_at > now() - interval '60 seconds'
		    AND s.status = 'active'
		    AND NOT COALESCE(w.throttled, false)
		    AND COALESCE($3,0) <= COALESCE(w.effective_memory_gb, w.memory_gb, 0)
		    AND COALESCE(w.supported_jobs,'{}') @> ARRAY[$1]
		    AND ($2 = '' OR COALESCE(w.supported_models,'{}') @> ARRAY[$2])`,
		jobType, modelRef, minMemGB,
	).Scan(&n)
	return n, err
}

// EligiblePoolReputation is the AVERAGE supplier reputation across the workers that
// would pass the claim hard filter for this job right now (the same predicate as
// EligibleWorkerCount). Surfaced to the buyer as routing transparency (DEEP_RESEARCH_V2
// §4): the quality of the pool their project routes to. 0 when the pool is empty.
func (s *Store) EligiblePoolReputation(ctx context.Context, jobType, modelRef string, minMemGB float32) (float64, error) {
	var r float64
	err := s.pool.QueryRow(ctx,
		`SELECT COALESCE(AVG(s.reputation), 0)
		   FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		  WHERE w.last_seen_at IS NOT NULL
		    AND w.last_seen_at > now() - interval '60 seconds'
		    AND s.status = 'active'
		    AND NOT COALESCE(w.throttled, false)
		    AND COALESCE($3,0) <= COALESCE(w.effective_memory_gb, w.memory_gb, 0)
		    AND COALESCE(w.supported_jobs,'{}') @> ARRAY[$1]
		    AND ($2 = '' OR COALESCE(w.supported_models,'{}') @> ARRAY[$2])`,
		jobType, modelRef, minMemGB,
	).Scan(&r)
	return r, err
}

// AddPrivatePoolMember binds a supplier to a buyer's Private Deployment pool (research
// §3): only bound suppliers may claim that buyer's private_pool jobs. Idempotent.
func (s *Store) AddPrivatePoolMember(ctx context.Context, buyerID, supplierID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO private_pool_members (buyer_id, supplier_id) VALUES ($1,$2)
		 ON CONFLICT (buyer_id, supplier_id) DO NOTHING`,
		buyerID, supplierID)
	return err
}

// WarmEligibleWorkerCount counts the eligible workers (same predicate as
// EligibleWorkerCount) that ALSO have modelRef WARM right now — a fresh
// worker_model_state row for the model (last_seen_warm within the 60s liveness
// window). This is the supply that would START this job without a cold model load
// (warm-routing, D3); the scheduler prefers exactly these workers. Returns 0 when
// modelRef is "" (no model → "warm" is undefined). Honest supply: a warm worker is
// counted only because it reported the model warm, never assumed.
func (s *Store) WarmEligibleWorkerCount(ctx context.Context, jobType, modelRef string, minMemGB float32) (int, error) {
	if modelRef == "" {
		return 0, nil
	}
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*)
		   FROM workers w
		   JOIN suppliers s ON s.id = w.supplier_id
		   JOIN worker_model_state wms
		     ON wms.worker_id = w.id
		    AND wms.model_id = $2
		    AND wms.last_seen_warm > now() - interval '60 seconds'
		  WHERE w.last_seen_at IS NOT NULL
		    AND w.last_seen_at > now() - interval '60 seconds'
		    AND s.status = 'active'
		    AND NOT COALESCE(w.throttled, false)
		    AND COALESCE($3,0) <= COALESCE(w.effective_memory_gb, w.memory_gb, 0)
		    AND COALESCE(w.supported_jobs,'{}') @> ARRAY[$1]
		    AND COALESCE(w.supported_models,'{}') @> ARRAY[$2]`,
		jobType, modelRef, minMemGB,
	).Scan(&n)
	return n, err
}

// InsertQuote persists a quote's assumptions (PLANE_C §6: a later invoice must be
// able to say what was believed at quote time). Scalars are queryable; the full
// object is kept in quote_json.
func (s *Store) InsertQuote(ctx context.Context, buyerID uuid.UUID, q Quote) error {
	blob, err := json.Marshal(q)
	if err != nil {
		return err
	}
	// id is the quote's bare UUID (the <uuid> in the "q_<uuid>" handle) so a buyer can
	// bind a later submission back to this exact row. expires_at/input_sha256 back the
	// D7 binding check (not-expired + bytes-match).
	_, err = s.pool.Exec(ctx,
		`INSERT INTO quotes
		   (id, buyer_id, job_type, model_ref, tier, records, input_bytes,
		    estimated_tokens, malformed_records, split_size, task_count, eligible_now,
		    cost_expected_usd, cost_min_usd, cost_max_usd, eta_p50_secs, eta_p90_secs,
		    oom_risk, confidence, quote_json, expires_at, input_sha256)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)`,
		q.bareID, buyerID, q.JobType, q.Model, q.Tier, q.Input.Records, q.Input.Bytes,
		q.Input.EstimatedTokens, q.Input.MalformedRecords, q.Execution.RecommendedSplitSize,
		q.Execution.EstimatedTasks, q.Execution.EligibleWorkersNow,
		q.Cost.ExpectedUSD, q.Cost.MinUSD, q.Cost.MaxUSD, q.Time.P50Secs, q.Time.P90Secs,
		q.Execution.OOMRisk, q.Confidence.Score, blob, q.ExpiresAt, q.InputSHA256,
	)
	return err
}

// --- quote → submit binding (Plane D D7, errata C-Errata-4) ---------------------

// quoteIDToUUID parses a buyer-supplied quote handle into the bare quotes.id UUID.
// The handle is normally "q_<uuid>" (what POST /v1/quote returns), but a bare uuid
// is accepted too so a buyer can pass back either shape. An unparseable handle is a
// caller error, surfaced verbatim — never silently dropped.
func quoteIDToUUID(handle string) (uuid.UUID, error) {
	id, err := uuid.Parse(strings.TrimPrefix(strings.TrimSpace(handle), "q_"))
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("invalid quote_id %q", handle)
	}
	return id, nil
}

// boundQuote is the slice of a persisted quote the submit path needs to validate a
// binding: the original (job_type, model, tier) the buyer was quoted on, the input
// fingerprint, the expected cost (echoed onto the invoice), and whether the quote
// has expired (computed in SQL against now() so it does not depend on clock skew).
type boundQuote struct {
	ID          uuid.UUID
	JobType     string
	ModelRef    string
	Tier        string
	InputSHA256 string
	CostExpUSD  float64
	Expired     bool
}

// GetBindableQuote loads a buyer's quote by id for binding a submission to it.
// Buyer-scoped (a buyer can only bind their OWN quotes); returns errNotFound when
// the id is not this buyer's quote. `expired` is derived from expires_at vs now()
// in the DB (a NULL expires_at — a pre-D7 quote — is treated as never-expiring so
// older rows still bind). The caller decides what a match requires.
func (s *Store) GetBindableQuote(ctx context.Context, quoteID, buyerID uuid.UUID) (*boundQuote, error) {
	var q boundQuote
	err := s.pool.QueryRow(ctx,
		`SELECT id, job_type, COALESCE(model_ref,''), COALESCE(tier,''),
		        COALESCE(input_sha256,''), COALESCE(cost_expected_usd,0),
		        (expires_at IS NOT NULL AND expires_at <= now()) AS expired
		   FROM quotes
		  WHERE id = $1 AND buyer_id = $2`,
		quoteID, buyerID,
	).Scan(&q.ID, &q.JobType, &q.ModelRef, &q.Tier, &q.InputSHA256, &q.CostExpUSD, &q.Expired)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	return &q, nil
}

// QuotedUSDForJob returns the cost_expected_usd of the quote a job was bound to, if
// any. ok is false when the job has no quote_id (the invoice then omits the quoted
// figure rather than inventing one). Buyer-scoped via the jobs row the caller already
// owns; the join keeps it a single round-trip.
func (s *Store) QuotedUSDForJob(ctx context.Context, jobID uuid.UUID) (usd float64, ok bool, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT q.cost_expected_usd
		   FROM jobs j JOIN quotes q ON q.id = j.quote_id
		  WHERE j.id = $1 AND j.quote_id IS NOT NULL`,
		jobID,
	).Scan(&usd)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return usd, true, nil
}
