package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// api.go — all HTTP handlers, auth middleware, and job→task splitting.
//
// Routing is stdlib net/http with Go 1.22+ method+path patterns (no framework).
// Auth is real: every buyer/admin route looks the bearer key up in api_keys,
// every worker route looks the X-Worker-Token up in worker_tokens. Missing or
// invalid credentials → 401 JSON. There is no silent bypass.

// Server holds the dependencies every handler needs.
type Server struct {
	store    *Store
	storage  *Storage
	verifier *Verifier
	payout   Payout
	// Rate limiters (stdlib token buckets, see ratelimit.go). ipLimiter bounds the
	// whole surface per source IP (brute-force/flood); the credential limiters bound
	// authenticated spam per api_key / worker_token.
	ipLimiter     *rateLimiter
	buyerLimiter  *rateLimiter
	workerLimiter *rateLimiter
}

// NewServer wires the handler dependencies.
func NewServer(store *Store, storage *Storage, verifier *Verifier, payout Payout) *Server {
	return &Server{
		store: store, storage: storage, verifier: verifier, payout: payout,
		ipLimiter:     newRateLimiter(30, 60), // 30 req/s, burst 60, per IP
		buyerLimiter:  newRateLimiter(20, 40), // 20 req/s, burst 40, per api key
		workerLimiter: newRateLimiter(30, 60), // 30 req/s, burst 60, per worker token
	}
}

// ctxKey is the private type for request-context values.
type ctxKey int

const (
	ctxBuyer  ctxKey = iota // *AuthResult for buyer/admin routes
	ctxWorker               // *WorkerAuth for worker routes
)

// Routes builds the mux with every route registered. /healthz is unauthed;
// everything else is wrapped in the matching auth middleware. The whole surface is
// then wrapped in the per-IP rate limiter (health/metrics exempt).
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /metrics", s.handleMetrics)
	mux.HandleFunc("GET /{$}", s.handleDashboard) // operator dashboard at root (same-origin, no CORS)
	mux.HandleFunc("GET /app", s.handleApp)       // role-based app skeleton (Supplier/Buyer/Admin/Workflows)
	mux.HandleFunc("GET /demo", s.handleDemo)     // Launch/Earn product demo (monochrome, same-origin)

	// Buyer API (Bearer api_key).
	mux.Handle("POST /v1/jobs", s.authBuyer(http.HandlerFunc(s.handleCreateJob)))
	mux.Handle("GET /v1/jobs/{id}", s.authBuyer(http.HandlerFunc(s.handleGetJob)))
	mux.Handle("GET /v1/jobs/{id}/results", s.authBuyer(http.HandlerFunc(s.handleJobResults)))
	mux.Handle("GET /v1/jobs/{id}/invoice", s.authBuyer(http.HandlerFunc(s.handleJobInvoice)))
	mux.Handle("GET /v1/jobs/{id}/events", s.authBuyer(http.HandlerFunc(s.handleJobEvents)))     // Plane C/D: buyer timeline
	mux.Handle("GET /v1/jobs/{id}/failures", s.authBuyer(http.HandlerFunc(s.handleJobFailures))) // Plane C/D: typed failure history
	mux.Handle("DELETE /v1/jobs/{id}", s.authBuyer(http.HandlerFunc(s.handleCancelJob)))
	mux.Handle("GET /v1/models", s.authBuyer(http.HandlerFunc(s.handleModels)))
	mux.Handle("GET /v1/price-estimate", s.authBuyer(http.HandlerFunc(s.handlePriceEstimate)))
	mux.Handle("POST /v1/quote", s.authBuyer(http.HandlerFunc(s.handleQuote))) // Plane C: scan + price, no spend
	mux.Handle("POST /v1/webhooks", s.authBuyer(http.HandlerFunc(s.handleRegisterWebhook)))

	// Concierge intake + buyer billing (intake.go, billing.go). The callback is
	// unauthed — GitHub redirects to it with no bearer; the buyer is recovered from
	// the OAuth state parameter.
	mux.Handle("GET /v1/connect/github", s.authBuyer(http.HandlerFunc(s.handleGithubConnect)))
	mux.HandleFunc("GET /v1/connect/github/callback", s.handleGithubCallback)
	mux.Handle("GET /v1/sources", s.authBuyer(http.HandlerFunc(s.handleListSources)))
	mux.Handle("POST /v1/intake", s.authBuyer(http.HandlerFunc(s.handleCreateIntake)))
	mux.Handle("POST /v1/billing/setup", s.authBuyer(http.HandlerFunc(s.handleBillingSetup)))
	mux.Handle("GET /v1/billing/status", s.authBuyer(http.HandlerFunc(s.handleBillingStatus)))
	mux.Handle("GET /v1/sources/{id}/repos", s.authBuyer(http.HandlerFunc(s.handleListRepos)))
	mux.Handle("POST /v1/intake/launch", s.authBuyer(http.HandlerFunc(s.handleLaunchIntake)))
	mux.Handle("POST /v1/deliver", s.authBuyer(http.HandlerFunc(s.handleDeliver)))
	mux.HandleFunc("POST /v1/stripe/webhook", s.handleStripeWebhook) // unauthed; verified by signature

	// OpenAI-compatible Batch API (openai.go): upload a JSONL of requests, create a
	// batch over it, poll, and download the output — all mapped onto the native job
	// pipeline (one source of truth: createJob).
	mux.Handle("POST /v1/files", s.authBuyer(http.HandlerFunc(s.handleCreateFile)))
	mux.Handle("GET /v1/files/{id}/content", s.authBuyer(http.HandlerFunc(s.handleGetFileContent)))
	mux.Handle("POST /v1/batches", s.authBuyer(http.HandlerFunc(s.handleCreateBatch)))
	mux.Handle("GET /v1/batches/{id}", s.authBuyer(http.HandlerFunc(s.handleGetBatch)))

	// Worker protocol (X-Worker-Token).
	mux.Handle("POST /v1/worker/register", s.authWorker(http.HandlerFunc(s.handleWorkerRegister)))
	mux.Handle("POST /v1/worker/heartbeat", s.authWorker(http.HandlerFunc(s.handleWorkerHeartbeat)))
	mux.Handle("GET /v1/worker/poll", s.authWorker(http.HandlerFunc(s.handleWorkerPoll)))
	mux.Handle("POST /v1/worker/task/{id}/start", s.authWorker(http.HandlerFunc(s.handleWorkerStart)))
	mux.Handle("POST /v1/worker/task/{id}/commit", s.authWorker(http.HandlerFunc(s.handleWorkerCommit)))
	mux.Handle("POST /v1/worker/task/{id}/fail", s.authWorker(http.HandlerFunc(s.handleWorkerFail))) // Plane C/D: immediate typed failure
	mux.Handle("GET /v1/worker/earnings", s.authWorker(http.HandlerFunc(s.handleWorkerEarnings)))
	mux.Handle("POST /v1/worker/connect", s.authWorker(http.HandlerFunc(s.handleWorkerConnect)))
	mux.Handle("GET /v1/worker/connect/status", s.authWorker(http.HandlerFunc(s.handleWorkerConnectStatus)))

	// Admin (Bearer admin_key — same lookup, is_admin flag required).
	mux.Handle("GET /admin/workers", s.authAdmin(http.HandlerFunc(s.handleAdminWorkers)))
	mux.Handle("GET /admin/jobs", s.authAdmin(http.HandlerFunc(s.handleAdminJobs)))
	mux.Handle("GET /admin/payouts", s.authAdmin(http.HandlerFunc(s.handleAdminPayouts)))
	mux.Handle("GET /admin/fraud-flags", s.authAdmin(http.HandlerFunc(s.handleAdminFraudFlags)))
	mux.Handle("GET /admin/fraud", s.authAdmin(http.HandlerFunc(s.handleAdminFraud)))
	mux.Handle("GET /admin/drift", s.authAdmin(http.HandlerFunc(s.handleAdminDrift)))
	mux.Handle("GET /admin/scheduler/explain", s.authAdmin(http.HandlerFunc(s.handleAdminSchedulerExplain)))
	mux.Handle("POST /admin/workers/{id}/suspend", s.authAdmin(http.HandlerFunc(s.handleAdminSuspend)))

	return s.ipLimiter.limitByIP(mux)
}

// --- middleware ---

// authBuyer authenticates a Bearer api_key and stashes the AuthResult.
func (s *Server) authBuyer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key, ok := bearer(r)
		if !ok {
			writeErr(w, http.StatusUnauthorized, "missing or malformed Authorization bearer token")
			return
		}
		auth, err := s.store.LookupAPIKey(r.Context(), key)
		if err != nil {
			writeErr(w, http.StatusUnauthorized, "invalid api key")
			return
		}
		if isRemote(r) && !s.buyerLimiter.allow(auth.BuyerID.String()) {
			writeErr(w, http.StatusTooManyRequests, "rate limit exceeded")
			return
		}
		ctx := context.WithValue(r.Context(), ctxBuyer, &auth)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// authAdmin is authBuyer plus an is_admin requirement.
func (s *Server) authAdmin(next http.Handler) http.Handler {
	return s.authBuyer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Context().Value(ctxBuyer).(*AuthResult)
		if !auth.IsAdmin {
			writeErr(w, http.StatusForbidden, "admin privilege required")
			return
		}
		next.ServeHTTP(w, r)
	}))
}

// authWorker authenticates an X-Worker-Token and stashes the WorkerAuth.
func (s *Server) authWorker(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tok := strings.TrimSpace(r.Header.Get("X-Worker-Token"))
		if tok == "" {
			writeErr(w, http.StatusUnauthorized, "missing X-Worker-Token")
			return
		}
		auth, err := s.store.LookupWorkerToken(r.Context(), tok)
		if err != nil {
			writeErr(w, http.StatusUnauthorized, "invalid worker token")
			return
		}
		if isRemote(r) && !s.workerLimiter.allow(auth.WorkerID.String()) {
			writeErr(w, http.StatusTooManyRequests, "rate limit exceeded")
			return
		}
		ctx := context.WithValue(r.Context(), ctxWorker, &auth)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// bearer extracts a Bearer token from the Authorization header.
func bearer(r *http.Request) (string, bool) {
	h := r.Header.Get("Authorization")
	const p = "Bearer "
	if len(h) <= len(p) || !strings.EqualFold(h[:len(p)], p) {
		return "", false
	}
	return strings.TrimSpace(h[len(p):]), true
}

// --- health ---

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if err := s.store.Ping(r.Context()); err != nil {
		writeErr(w, http.StatusServiceUnavailable, "database unreachable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// --- buyer handlers ---

// jobSubmit is the POST /v1/jobs request shape: the buyer-facing submission
// (not a raw JobManifest). input is polymorphic — a JSON string IS the inline
// JSONL, or {"s3_key":"..."} points at an already-uploaded object.
type jobSubmit struct {
	JobType      JobType            `json:"job_type"`
	Model        ModelRef           `json:"model"`
	Params       json.RawMessage    `json:"params"`
	Constraints  JobConstraints     `json:"constraints"`
	Verification VerificationPolicy `json:"verification"`
	Tier         string             `json:"tier"`
	Input        json.RawMessage    `json:"input"`
	WebhookURL   string             `json:"webhook_url"`
	// MaxUSD is the optional buyer hard spend cap (Budget Governor, Plane C §12 /
	// Plane D §14 D8). When > 0 the claim refuses to dispatch a new task once the
	// job's projected charge would breach it. omitempty/pointer-free zero (0) means
	// no cap — unchanged behavior for buyers who do not set one.
	MaxUSD float64 `json:"max_usd,omitempty"`
	// QuoteID optionally binds this submission to an advisory quote (Plane D D7): the
	// "q_<uuid>" (or bare uuid) returned by POST /v1/quote. When set, createJob checks
	// the quote is the buyer's, not expired, and matches this job's type/model/tier
	// (and best-effort input bytes) before persisting the binding; a mismatch is 409.
	// Empty (the default) keeps the unbound submission path unchanged.
	QuoteID string `json:"quote_id,omitempty"`
}

// defaultSplitSize is the JSONL chunk size (lines per task) when params omits it.
const defaultSplitSize = 256

// handleCreateJob is the real submission path: it resolves the input JSONL
// (inline or from object storage), uploads the canonical input, splits it into
// per-task chunks (each its own object), creates the job + task rows carrying
// the chunk keys, sizes honeypot/redundancy tasks from the verification policy,
// registers a webhook if given, and returns 202 with the estimate.
func (s *Server) handleCreateJob(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var sub jobSubmit
	if err := json.NewDecoder(r.Body).Decode(&sub); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid job submission json: "+err.Error())
		return
	}
	resp, herr := s.createJob(r.Context(), auth.BuyerID, sub)
	if herr != nil {
		writeErr(w, herr.status, herr.msg)
		return
	}
	writeJSON(w, http.StatusAccepted, resp)
}

// httpError carries an HTTP status + message from an internal helper back to the
// handler that writes the response, so job creation can be shared by POST /v1/jobs
// and the OpenAI batch endpoint (openai.go) without either duplicating the pipeline.
type httpError struct {
	status int
	msg    string
}

func (e *httpError) Error() string { return e.msg }

// createJob runs the full submission pipeline for an already-decoded, buyer-scoped
// jobSubmit and returns the 202 payload (or an httpError). The single source of
// truth for turning a submission into a job + tasks: it resolves the input JSONL,
// uploads the canonical input, splits it into per-task chunks, sizes
// honeypot/redundancy tasks from the verification policy, registers a webhook if
// given, and persists the job. Both the native API and the OpenAI batch endpoint
// go through here.
func (s *Server) createJob(ctx context.Context, buyerID uuid.UUID, sub jobSubmit) (JobSubmitResponse, *httpError) {
	if sub.JobType.Type == "" {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "job_type.type is required"}
	}
	if !validJobTypes[sub.JobType.Type] {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "invalid job_type.type: " + sub.JobType.Type}
	}
	if sub.Tier == "" {
		sub.Tier = "batch"
	}
	if !validTiers[sub.Tier] {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "invalid tier: " + sub.Tier}
	}
	for _, c := range sub.Constraints.HWClasses {
		if !validHWClasses[c] {
			return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "invalid hw_class: " + c}
		}
	}
	// Validate the webhook URL up front (before any S3 work) so a bad URL never
	// leaves a job created with no webhook.
	if sub.WebhookURL != "" && !strings.HasPrefix(sub.WebhookURL, "https://") {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "webhook_url must be https"}
	}

	// Require a saved payment method before accepting billable work WHEN billing is
	// configured. Without this gate a cardless buyer's completed job is owed forever
	// (the ledger records it but chargeForJob can never collect off-session). We
	// surface it at submit (402) rather than silently accruing uncollectable debt.
	// With no Stripe key (local/dev/test) the gate is skipped — behavior unchanged.
	if stripeKey() != "" {
		_, pm, berr := s.store.GetBillingCustomer(ctx, buyerID)
		switch {
		case errors.Is(berr, errNotFound), berr == nil && pm == "":
			return JobSubmitResponse{}, &httpError{http.StatusPaymentRequired, "no payment method on file — save a card via POST /v1/billing/setup before submitting a job"}
		case berr != nil:
			return JobSubmitResponse{}, &httpError{http.StatusServiceUnavailable, "billing lookup failed: " + berr.Error()}
		}
	}

	// Resolve the input JSONL bytes. fromKey is true when the input already lives
	// in object storage (we then skip re-uploading the canonical copy).
	inputBytes, srcKey, err := s.resolveInput(ctx, sub.Input)
	if err != nil {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "resolving input: " + err.Error()}
	}
	// Adaptive chunk sizing: an explicit params.split_size always wins; otherwise
	// pick a per-job-type default that targets ~30–60s/task (embeddings pack far
	// more items/chunk than generation). See adaptiveSplitSize.
	splitSize := adaptiveSplitSize(sub.JobType.Type, sub.Params)
	lines := splitJSONL(inputBytes, splitSize)
	if len(lines) == 0 {
		return JobSubmitResponse{}, &httpError{http.StatusBadRequest, "input is empty: at least one JSONL line is required"}
	}

	// Quote-to-submit binding (Plane D D7): if the buyer passed a quote_id, bind this
	// submission to that advisory quote so the invoice can say what they were told.
	// Validated BEFORE any storage writes so a stale/mismatched quote rejects cleanly
	// with no orphaned objects. boundQuoteID stays zero when no quote was supplied
	// (the unbound path is unchanged).
	var boundQuoteID uuid.UUID
	if sub.QuoteID != "" {
		qid, err := quoteIDToUUID(sub.QuoteID)
		if err != nil {
			return JobSubmitResponse{}, &httpError{http.StatusBadRequest, err.Error()}
		}
		q, err := s.store.GetBindableQuote(ctx, qid, buyerID)
		if errors.Is(err, errNotFound) {
			return JobSubmitResponse{}, &httpError{http.StatusNotFound, "quote not found"}
		}
		if err != nil {
			return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "loading quote: " + err.Error()}
		}
		if q.Expired {
			return JobSubmitResponse{}, &httpError{http.StatusConflict, "quote expired"}
		}
		// The quote must describe THIS submission: same job type, model, and tier. A
		// mismatch means the buyer is acting on a price they were not given — refuse it.
		if q.JobType != sub.JobType.Type || q.ModelRef != sub.Model.Ref || q.Tier != sub.Tier {
			return JobSubmitResponse{}, &httpError{http.StatusConflict, "quote does not match this submission"}
		}
		// Best-effort: confirm the bytes match what was scanned at quote time. We only
		// reject when BOTH sides have a fingerprint and they differ (a pre-D7 quote with
		// no stored sha still binds, leaning permissive rather than blocking older quotes).
		if q.InputSHA256 != "" {
			sum := sha256.Sum256(inputBytes)
			if q.InputSHA256 != hex.EncodeToString(sum[:]) {
				return JobSubmitResponse{}, &httpError{http.StatusConflict, "quote does not match this submission: input changed since the quote"}
			}
		}
		boundQuoteID = q.ID
	}

	jobID := uuid.New()
	inputKey := srcKey
	if inputKey == "" {
		// Upload the canonical job input only when it came inline.
		inputKey = fmt.Sprintf("jobs/%s/input.jsonl", jobID)
		if err := s.storage.PutObject(ctx, inputKey, inputBytes, "application/x-ndjson"); err != nil {
			return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "uploading input: " + err.Error()}
		}
	}
	outputKey := fmt.Sprintf("jobs/%s/output.jsonl", jobID)

	// Split the input into chunks; each chunk is its own object and one primary
	// task. The task's input_ref is the chunk key, result_key is its result
	// target.
	tasks := make([]taskRow, 0, len(lines))
	for i, chunk := range lines {
		chunkKey := fmt.Sprintf("jobs/%s/tasks/%d/input.jsonl", jobID, i)
		if err := s.storage.PutObject(ctx, chunkKey, chunk, "application/x-ndjson"); err != nil {
			return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "uploading chunk: " + err.Error()}
		}
		tasks = append(tasks, taskRow{
			ID:         uuid.New(),
			JobID:      jobID,
			InputRef:   chunkKey,
			ResultKey:  fmt.Sprintf("jobs/%s/tasks/%d/result.json", jobID, i),
			ChunkIndex: i, // 0-based input position, for the ordered result merge
		})
	}
	nPrimary := len(tasks) // primaries precede any redundancy/honeypot clones

	// Redundancy tasks: a same-class peer for redundancy_frac of the primaries.
	// Each clones a primary's input chunk so PeerResultKey can pair them by
	// shared input_ref, and reuses that primary's chunk_index.
	nRedundancy := fracCount(nPrimary, sub.Verification.RedundancyFrac)
	for i := 0; i < nRedundancy; i++ {
		p := tasks[i]
		tasks = append(tasks, taskRow{
			ID:           uuid.New(),
			JobID:        jobID,
			IsRedundancy: true,
			InputRef:     p.InputRef,
			ResultKey:    fmt.Sprintf("jobs/%s/redundancy/%d/result.json", jobID, i),
			ChunkIndex:   p.ChunkIndex,
		})
	}

	// Honeypot tasks: pull available known-answer honeypots for this job type and
	// inject them as tasks pointing at the honeypot's input_ref. They are probes,
	// not buyer output, so they reuse the matching primary's chunk_index (and are
	// excluded from the merge by is_honeypot).
	nHoneypot := fracCount(len(lines), sub.Verification.HoneypotFrac)
	if nHoneypot > 0 {
		hps, herr := s.store.AvailableHoneypots(ctx, sub.JobType.Type, nHoneypot)
		if herr != nil {
			return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "loading honeypots: " + herr.Error()}
		}
		for i, hp := range hps {
			tasks = append(tasks, taskRow{
				ID:         uuid.New(),
				JobID:      jobID,
				IsHoneypot: true,
				InputRef:   hp,
				ResultKey:  fmt.Sprintf("jobs/%s/honeypots/%d/result.json", jobID, i),
				ChunkIndex: i % nPrimary,
			})
		}
	}

	// Estimate cost from DB-backed model pricing × unit count.
	estimate := s.estimateJobUSD(ctx, sub.Model.Ref, inputBytes, len(lines), sub.Tier)
	// Price verification THROUGH to the buyer: the stored estimate — and thus the
	// total charge (scheduleTaskPayout splits EstimatedUSD across all TaskCount
	// tasks) — covers every task that will run: deliverable + redundancy + honeypot.
	// So the buyer pays the true cost of "verified", and each supplier is paid the
	// full per-deliverable-task rate for the compute they actually run, not a pool
	// diluted by the verification clones. (Empty jobs are rejected upstream; the
	// guard just never divides by zero.)
	if nPrimary > 0 && len(tasks) > nPrimary {
		estimate = roundUSD(estimate * float64(len(tasks)) / float64(nPrimary))
	}
	vp, _ := json.Marshal(sub.Verification)
	// Persist the FULL submitted JobType (tag + labels/schema/max_tokens/...) so the
	// poll dispatch can reconstruct manifest.job_type for the agent, not just the tag.
	spec, _ := json.Marshal(sub.JobType)
	// offered_rate_usd_hr: a price-derived $/hr a worker earns running this job —
	// model price_per_1k × representative units/hr (see offeredRateUsdHr). The
	// claim's min-payout gate compares it to the worker's reservation price.
	offeredRate := s.offeredRateUsdHr(ctx, sub.JobType.Type, sub.Model.Ref)
	// eta_secs: a simple queue-depth/throughput estimate (see s.estimateETASecs),
	// model-aware so a (job_type, model) with enough committed history uses its
	// observed p90 per-task duration instead of the static target (Plane D D6).
	etaSecs := s.estimateETASecs(ctx, sub.JobType.Type, sub.Model.Ref, len(tasks))

	jr := &jobRow{
		ID:                 jobID,
		BuyerID:            buyerID,
		JobType:            sub.JobType.Type,
		ModelRef:           sub.Model.Ref,
		InputRef:           inputKey,
		OutputRef:          outputKey,
		Tier:               sub.Tier,
		VerificationPolicy: vp,
		EstimatedUSD:       estimate,
		TaskCount:          len(tasks),
		MinMemoryGB:        sub.Constraints.MinMemoryGB,
		HWClasses:          sub.Constraints.HWClasses,
		DataResidency:      sub.Constraints.DataResidency,
		JobTypeSpec:        spec,
		SplitSize:          splitSize,
		OfferedRateUsdHr:   offeredRate,
		ETASecs:            etaSecs,
		MaxUSD:             sub.MaxUSD,   // Budget Governor cap (0 = none → persisted NULL)
		QuoteID:            boundQuoteID, // D7 quote binding (zero = none → persisted NULL)
	}
	if err := s.store.CreateJobWithTasks(ctx, jr, tasks); err != nil {
		return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "failed to create job: " + err.Error()}
	}

	// Register a completion webhook if one was supplied (URL already validated).
	if sub.WebhookURL != "" {
		if _, err := s.store.InsertWebhook(ctx, buyerID, &jobID, sub.WebhookURL); err != nil {
			return JobSubmitResponse{}, &httpError{http.StatusInternalServerError, "registering webhook: " + err.Error()}
		}
	}

	metrics.jobsSubmitted.Add(1)

	// Open the buyer-visible event timeline (Plane C/D). Best-effort: a timeline
	// write must never fail an accepted job — log via the error return being ignored.
	_ = s.store.InsertJobEvent(ctx, jobID, nil, "job_created",
		fmt.Sprintf("Job created: %d tasks, model %s, %s tier", len(tasks), jr.ModelRef, sub.Tier), nil)

	// Record the buyer's hard spend cap on the timeline (Budget Governor). Only when
	// a cap was set; best-effort like the job_created event.
	if sub.MaxUSD > 0 {
		_ = s.store.InsertJobEvent(ctx, jobID, nil, "budget_set",
			fmt.Sprintf("budget set: $%.2f cap", sub.MaxUSD), nil)
	}

	// Record the quote binding on the timeline (Plane D D7). Only when a quote was
	// bound; best-effort like the events above. The bare uuid is enough to trace the
	// invoice's quoted-vs-actual back to the originating quote.
	if boundQuoteID != (uuid.UUID{}) {
		_ = s.store.InsertJobEvent(ctx, jobID, nil, "quote_bound",
			fmt.Sprintf("bound to quote q_%s", boundQuoteID), nil)
	}

	// Estimated completion from the queue-depth/throughput ETA (priority work is
	// estimated to clear faster — see estimateETASecs), with a tier floor so the
	// human-facing RFC3339 timestamp stays sane.
	dur := time.Duration(etaSecs) * time.Second
	if min := tierMinCompletion(sub.Tier); dur < min {
		dur = min
	}
	return JobSubmitResponse{
		JobID:               jobID,
		TaskCount:           len(tasks),
		EstimatedUSD:        estimate,
		ETASecs:             etaSecs,
		EstimatedCompletion: time.Now().Add(dur).UTC().Format(time.RFC3339),
	}, nil
}

// resolveInput turns the polymorphic `input` field into bytes. A JSON string IS
// the inline JSONL; an object {"s3_key":"..."} is fetched from storage and its
// key is returned (so the caller skips re-uploading). Anything else is an error.
func (s *Server) resolveInput(ctx context.Context, raw json.RawMessage) (data []byte, fromKey string, err error) {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || string(raw) == "null" {
		return nil, "", errors.New("input is required (inline JSONL string or {\"s3_key\":\"...\"})")
	}
	if raw[0] == '"' {
		var inline string
		if err := json.Unmarshal(raw, &inline); err != nil {
			return nil, "", fmt.Errorf("invalid inline input string: %w", err)
		}
		return []byte(inline), "", nil
	}
	var ref struct {
		S3Key string `json:"s3_key"`
	}
	if err := json.Unmarshal(raw, &ref); err != nil || ref.S3Key == "" {
		return nil, "", errors.New("input must be a JSONL string or an object with a non-empty s3_key")
	}
	b, err := s.storage.GetObject(ctx, ref.S3Key)
	if err != nil {
		return nil, "", fmt.Errorf("fetching input %q: %w", ref.S3Key, err)
	}
	return b, ref.S3Key, nil
}

func (s *Server) handleGetJob(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	j, err := s.store.GetJob(r.Context(), id, auth.BuyerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "job not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, JobStatus{
		JobID:        j.ID,
		Status:       j.Status,
		JobType:      j.JobType,
		Tier:         j.Tier,
		TaskCount:    j.TaskCount,
		TasksDone:    j.TasksDone,
		EstimatedUSD: j.EstimatedUSD,
		ActualUSD:    j.ActualUSD,
		ETASecs:      j.ETASecs,
		CreatedAt:    j.CreatedAt.UTC().Format(time.RFC3339),
		MaxUSD:       j.MaxUSD,
		BudgetState:  j.BudgetState,
	})
}

// handleJobResults returns a presigned GET of the merged buyer-ready artifact for
// a completed job, with the per-task result URLs as a fallback list. The merge is
// normally written by the completion sweep before the job is marked complete; if
// the merged object is not yet present (e.g. the buyer polls in the gap between
// the last commit and the next sweep), it is merged on read so the buyer always
// gets the single artifact. Every URL is a real time-limited signed URL.
func (s *Server) handleJobResults(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	ctx := r.Context()
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	j, err := s.store.GetJob(ctx, id, auth.BuyerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "job not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if j.Status != "complete" {
		writeErr(w, http.StatusConflict, "job not complete (status="+j.Status+")")
		return
	}

	keys, err := s.store.JobResultKeys(ctx, j.ID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "loading result keys: "+err.Error())
		return
	}
	urls := make([]string, 0, len(keys))
	for _, k := range keys {
		u, perr := s.storage.PresignGet(ctx, k, time.Hour)
		if perr != nil {
			writeErr(w, http.StatusInternalServerError, "presign result: "+perr.Error())
			return
		}
		urls = append(urls, u)
	}

	res := JobResults{JobID: j.ID, Status: j.Status, ResultURLs: urls}
	// Merge into the single buyer-ready artifact and presign it. The merge is
	// idempotent (it overwrites output_ref) and cheap, so we always run it on read:
	// the sweep is best-effort timing, this is the correctness guarantee.
	if j.OutputRef != "" {
		if _, merr := s.MergeJobResults(ctx, j.ID); merr != nil {
			// Surface a merge failure (e.g. a malformed result object) rather than
			// hand back a fallback that hides the problem.
			writeErr(w, http.StatusInternalServerError, "merging results: "+merr.Error())
			return
		}
		if u, perr := s.storage.PresignGet(ctx, j.OutputRef, time.Hour); perr == nil {
			res.ResultsURL = u
		}
	}
	writeJSON(w, http.StatusOK, res)
}

// MergeJobResults assembles a job's completed PRIMARY task results into ONE
// buyer-ready JSONL artifact and writes it to the job's output_ref. Each primary
// task's result object is fetched in chunk_index order (so the merged file reads
// in the buyer's original input order) and flattened to one JSON line per input
// item, with the per-job-type shape:
//
//	embed                → {"index":<global>,"vector":[...]}
//	batch_classification → {"index":<global>,"label":"..."}
//	rerank               → {"index":<global>,"order":[...]}
//	json_extraction      → the extracted object, with an "index" field stamped in
//	batch_infer / other  → the task's per-item record passed through (or, when the
//	                       result is not the documented JSONL-of-items shape, the
//	                       raw result object as a single line — never dropped)
//
// A primary whose object is missing or malformed is surfaced as an error (the
// merge is the deliverable; a silent gap would hand the buyer a short file).
// Returns the byte count written. Called by the completion sweep BEFORE the job
// is marked complete, and on read as the correctness fallback.
func (s *Server) MergeJobResults(ctx context.Context, jobID uuid.UUID) (int, error) {
	return mergeJobResults(ctx, s.store, s.storage, jobID)
}

// embedBinMagic marks a Computexchange binary embedding artifact (PLANE_D D5/D15).
// The agent writes it (agent/src/runners.rs EMBED_BIN_MAGIC) when an embed job opts
// into compact float32 output; the merge detects these 4 bytes to keep the chunk on
// the binary path instead of JSON-parsing it. Layout: [magic|version u32|dim u32|
// count u32| count*dim packed little-endian f32], all little-endian.
var embedBinMagic = []byte("CXEM")

// embedBinHeaderLen is the fixed binary embedding header size (magic+version+dim+count).
const embedBinHeaderLen = 16

// embedBinVersion is the binary embedding format version this build reads/writes.
const embedBinVersion = uint32(1)

// isEmbedBinary reports whether obj is a binary embedding artifact (magic prefix).
func isEmbedBinary(obj []byte) bool {
	return len(obj) >= 4 && bytes.Equal(obj[:4], embedBinMagic)
}

// mergeJobResults is the shared merge used by both the results handler (*Server)
// and the completion sweep (*Workers): it fetches the job's ordered primary
// results and writes ONE buyer-ready artifact to the job's output_ref. For every
// JSON job (the default) it flattens results to a JSONL file exactly as before. For
// an embed job that opted into the binary float32 artifact (PLANE_D D5/D15) it
// detects the magic prefix and does a real BINARY merge (one combined header +
// concatenated row bodies, row order preserved by chunk_index) so the output is a
// single valid binary embedding file the SDK reader decodes — never a binary blob
// spliced into a JSONL file. Surfaces any missing/malformed result loudly.
func mergeJobResults(ctx context.Context, store *Store, storage *Storage, jobID uuid.UUID) (int, error) {
	info, err := store.JobMergeInputs(ctx, jobID)
	if err != nil {
		return 0, err
	}
	if info.OutputRef == "" {
		return 0, fmt.Errorf("job %s has no output_ref to merge into", jobID)
	}

	// Fetch every primary chunk in input order. (Parity with the prior code, which
	// also buffered all results; the D5 "streaming merge" aspiration is out of scope
	// for this slice and is left as the documented next step.)
	objs := make([][]byte, len(info.Results))
	for i, pr := range info.Results {
		obj, gerr := storage.GetObject(ctx, pr.ResultRef)
		if gerr != nil {
			return 0, fmt.Errorf("merge: fetching result %q: %w", pr.ResultRef, gerr)
		}
		objs[i] = obj
	}

	// Binary embed merge: if the job is embed and the first chunk is binary, every
	// chunk must be binary (mixed shapes are a real bug, surfaced — never papered
	// over). The combined artifact re-headers the summed count and concatenates the
	// float bodies in chunk order.
	if info.JobType == "embed" && len(objs) > 0 && isEmbedBinary(objs[0]) {
		out, err := mergeEmbedBinary(objs, info.Results)
		if err != nil {
			return 0, err
		}
		if err := storage.PutObject(ctx, info.OutputRef, out, "application/octet-stream"); err != nil {
			return 0, fmt.Errorf("merge: writing binary output %q: %w", info.OutputRef, err)
		}
		metrics.resultMerges.Add(1)
		return len(out), nil
	}

	// JSON path (default): flatten each chunk to per-item JSONL lines, unchanged.
	var buf bytes.Buffer
	idx := 0 // running global item index across chunks
	for i, pr := range info.Results {
		n, merr := mergeResultObject(&buf, info.JobType, objs[i], idx)
		if merr != nil {
			return 0, fmt.Errorf("merge: chunk %d (%s): %w", pr.ChunkIndex, pr.ResultRef, merr)
		}
		idx += n
	}
	out := buf.Bytes()
	if err := storage.PutObject(ctx, info.OutputRef, out, "application/x-ndjson"); err != nil {
		return 0, fmt.Errorf("merge: writing output %q: %w", info.OutputRef, err)
	}
	metrics.resultMerges.Add(1)
	return len(out), nil
}

// mergeEmbedBinary concatenates per-chunk binary embedding artifacts (PLANE_D
// D5/D15) into ONE binary file: a single header carrying the summed row count and
// the shared dim, followed by every chunk's float body in chunk order (so rows read
// in the buyer's original input order). Every chunk must be a valid binary artifact
// of the SAME dim; a missing magic, a version we do not read, a size that disagrees
// with its header, or a dim mismatch is surfaced as an error — the merge is the
// deliverable, so a corrupt chunk must fail loudly, never silently shorten the file.
func mergeEmbedBinary(objs [][]byte, results []PrimaryResult) ([]byte, error) {
	var dim uint32
	var total uint64
	bodies := make([][]byte, 0, len(objs))
	for i, obj := range objs {
		ref := ""
		if i < len(results) {
			ref = results[i].ResultRef
		}
		if !isEmbedBinary(obj) {
			return nil, fmt.Errorf("merge: chunk %d (%s): mixed embed output — chunk is not a binary artifact while the job is binary", i, ref)
		}
		if len(obj) < embedBinHeaderLen {
			return nil, fmt.Errorf("merge: chunk %d (%s): binary artifact shorter than header", i, ref)
		}
		ver := binary.LittleEndian.Uint32(obj[4:8])
		if ver != embedBinVersion {
			return nil, fmt.Errorf("merge: chunk %d (%s): unsupported binary embedding version %d", i, ref, ver)
		}
		cdim := binary.LittleEndian.Uint32(obj[8:12])
		ccount := binary.LittleEndian.Uint32(obj[12:16])
		want := embedBinHeaderLen + int(uint64(cdim)*uint64(ccount)*4)
		if len(obj) != want {
			return nil, fmt.Errorf("merge: chunk %d (%s): binary body is %d bytes, header implies %d (%dx%d f32)", i, ref, len(obj), want, ccount, cdim)
		}
		if i == 0 {
			dim = cdim
		} else if cdim != dim {
			return nil, fmt.Errorf("merge: chunk %d (%s): dim %d != job dim %d (cannot merge embeddings of different width)", i, ref, cdim, dim)
		}
		total += uint64(ccount)
		bodies = append(bodies, obj[embedBinHeaderLen:])
	}
	if total > math.MaxUint32 {
		return nil, fmt.Errorf("merge: %d total embedding rows exceeds the binary format's uint32 count", total)
	}

	var out bytes.Buffer
	out.Grow(embedBinHeaderLen + int(uint64(dim)*total*4))
	out.Write(embedBinMagic)
	var hdr [12]byte
	binary.LittleEndian.PutUint32(hdr[0:4], embedBinVersion)
	binary.LittleEndian.PutUint32(hdr[4:8], dim)
	binary.LittleEndian.PutUint32(hdr[8:12], uint32(total))
	out.Write(hdr[:])
	for _, b := range bodies {
		out.Write(b)
	}
	return out.Bytes(), nil
}

// mergeResultObject flattens one task result object into per-item JSONL lines on
// buf, returning the number of items emitted (so the caller can keep a running
// global index). It rejects a malformed object loudly rather than skipping it.
func mergeResultObject(buf *bytes.Buffer, jobType string, obj []byte, base int) (int, error) {
	writeLine := func(v any) error {
		b, err := json.Marshal(v)
		if err != nil {
			return err
		}
		buf.Write(b)
		buf.WriteByte('\n')
		return nil
	}
	switch jobType {
	case "embed":
		var r struct {
			Vectors [][]float64 `json:"vectors"`
		}
		if err := json.Unmarshal(obj, &r); err != nil || len(r.Vectors) == 0 {
			return 0, fmt.Errorf("not a valid embed result")
		}
		for i, v := range r.Vectors {
			if err := writeLine(map[string]any{"index": base + i, "vector": v}); err != nil {
				return 0, err
			}
		}
		return len(r.Vectors), nil
	case "batch_classification":
		var r classificationResult
		if err := json.Unmarshal(obj, &r); err != nil || len(r.Labels) == 0 {
			return 0, fmt.Errorf("not a valid batch_classification result")
		}
		for i, it := range r.Labels {
			if err := writeLine(map[string]any{"index": base + i, "label": it.Label}); err != nil {
				return 0, err
			}
		}
		return len(r.Labels), nil
	case "rerank":
		var r rerankResult
		if err := json.Unmarshal(obj, &r); err != nil || len(r.Rankings) == 0 {
			return 0, fmt.Errorf("not a valid rerank result")
		}
		for i, it := range r.Rankings {
			if err := writeLine(map[string]any{"index": base + i, "order": it.Order}); err != nil {
				return 0, err
			}
		}
		return len(r.Rankings), nil
	case "json_extraction":
		var r jsonExtractionResult
		if err := json.Unmarshal(obj, &r); err != nil || len(r.Items) == 0 {
			return 0, fmt.Errorf("not a valid json_extraction result")
		}
		for i, it := range r.Items {
			// Stamp the global index into the extracted object so the buyer can
			// realign lines with their input even after chunk concatenation.
			var m map[string]json.RawMessage
			if err := json.Unmarshal(it.JSON, &m); err != nil {
				return 0, fmt.Errorf("item %d not a JSON object", it.Index)
			}
			ib, _ := json.Marshal(base + i)
			m["index"] = ib
			if err := writeLine(m); err != nil {
				return 0, err
			}
		}
		return len(r.Items), nil
	default:
		// batch_infer and any other type: the agent writes one completion per input
		// item. The documented shape is {"completions":[...]} (or {"items":[...]});
		// when present we flatten it, otherwise we pass the whole object through as a
		// single line so nothing is ever silently dropped.
		var r struct {
			Completions []json.RawMessage `json:"completions"`
			Items       []json.RawMessage `json:"items"`
		}
		if err := json.Unmarshal(obj, &r); err == nil {
			list := r.Completions
			if list == nil {
				list = r.Items
			}
			if len(list) > 0 {
				for _, c := range list {
					buf.Write(bytes.TrimSpace(c))
					buf.WriteByte('\n')
				}
				return len(list), nil
			}
		}
		buf.Write(bytes.TrimSpace(obj))
		buf.WriteByte('\n')
		return 1, nil
	}
}

func (s *Server) handleCancelJob(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	err := s.store.CancelJob(r.Context(), id, auth.BuyerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusConflict, "job not found or already started")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "cancelled"})
}

// handleModels serves the DB-backed model + pricing catalogue (the single
// source of truth — no static Go list). Prices come straight from the models
// table.
func (s *Server) handleModels(w http.ResponseWriter, r *http.Request) {
	rows, err := s.store.ListModels(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	out := make([]ModelInfo, 0, len(rows))
	for _, m := range rows {
		out = append(out, ModelInfo{
			ID:            m.ID,
			Kind:          m.Kind,
			MinMemoryGB:   m.MinMemoryGB,
			PricePer1KUSD: modelPrice(m),
			JobType:       m.JobType,
		})
	}
	writeJSON(w, http.StatusOK, out)
}

// handlePriceEstimate estimates cost from query params: model, units, tier. The
// price comes from the DB models table, not a static list.
func (s *Server) handlePriceEstimate(w http.ResponseWriter, r *http.Request) {
	model := r.URL.Query().Get("model")
	tier := r.URL.Query().Get("tier")
	if tier == "" {
		tier = "batch"
	}
	units, err := strconv.ParseUint(r.URL.Query().Get("units"), 10, 64)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "units must be a positive integer")
		return
	}
	m, err := s.store.GetModel(r.Context(), model)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusBadRequest, "unknown model: "+model)
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	price := modelPrice(*m)
	est := float64(units) / 1000.0 * price * tierMultiplier(tier)
	writeJSON(w, http.StatusOK, PriceEstimate{
		Model:         m.ID,
		Units:         units,
		PricePer1KUSD: price,
		EstimateUSD:   roundUSD(est),
		Tier:          tier,
	})
}

// handleRegisterWebhook persists a completion-webhook registration. The webhooks
// table is part of the schema, so this is real: it validates the URL, stores the
// row, and returns 201 truthfully (delivery is done by the background sweep).
func (s *Server) handleRegisterWebhook(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	var body struct {
		URL   string `json:"url"`
		JobID string `json:"job_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid webhook json")
		return
	}
	if !strings.HasPrefix(body.URL, "https://") {
		writeErr(w, http.StatusBadRequest, "webhook url must be https")
		return
	}
	var jobID *uuid.UUID
	if body.JobID != "" {
		id, perr := uuid.Parse(body.JobID)
		if perr != nil {
			writeErr(w, http.StatusBadRequest, "job_id must be a uuid")
			return
		}
		jobID = &id
	}
	hookID, err := s.store.InsertWebhook(r.Context(), auth.BuyerID, jobID, body.URL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "registering webhook: "+err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"status":     "registered",
		"webhook_id": hookID,
	})
}

// --- worker handlers ---

func (s *Server) handleWorkerRegister(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	var cap WorkerCapability
	if err := json.NewDecoder(r.Body).Decode(&cap); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid capability json: "+err.Error())
		return
	}
	if !validHWClasses[cap.HWClass] {
		writeErr(w, http.StatusBadRequest, "invalid hw_class: "+cap.HWClass)
		return
	}
	// Bind the capability to the authenticated worker/supplier — never trust
	// the body's ids over the token's.
	cap.WorkerID = auth.WorkerID
	cap.SupplierID = auth.SupplierID
	if err := s.store.UpsertWorker(r.Context(), cap); err != nil {
		writeErr(w, http.StatusInternalServerError, "register failed: "+err.Error())
		return
	}
	// Echo the capability back with the server-bound worker_id/supplier_id. The
	// agent deserializes this response into its WorkerCapability to confirm the
	// binding, so it must be the full object, not a status envelope.
	writeJSON(w, http.StatusOK, cap)
}

func (s *Server) handleWorkerHeartbeat(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	var hb Heartbeat
	if err := json.NewDecoder(r.Body).Decode(&hb); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid heartbeat json")
		return
	}
	// Refresh liveness + the live resource state the safe-dispatch filter reads.
	if err := s.store.HeartbeatWorker(r.Context(), auth.WorkerID, WorkerResources{
		AvailableMemoryGB:  hb.AvailableMemoryGB,
		EffectiveMemoryGB:  hb.EffectiveMemoryGB,
		ReservedHeadroomGB: hb.ReservedHeadroomGB,
		Throttled:          hb.Throttled,
		LoadedModels:       hb.LoadedModels,
	}); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// longPollCap bounds the server-side wait so a misbehaving (or hostile) wait_ms
// can never pin a request goroutine indefinitely. The agent's transport ceiling
// is 35s (protocol.rs POLL_TIMEOUT), so 25s leaves headroom for the final claim +
// response to land before the client times out (Plane D §7 D1).
const longPollCap = 25 * time.Second

// longPollInterval is the re-attempt cadence while waiting: each tick is its own
// ClaimTask (no DB transaction is held across the wait). 250ms keeps idle pickup
// well under a second without hammering the claim query.
const longPollInterval = 250 * time.Millisecond

// parseWaitMs reads the optional ?wait_ms long-poll budget, clamped to
// [0, longPollCap]. A missing, empty, malformed, or non-positive value yields 0 —
// the original single-shot poll (no wait) — so the param is purely additive and an
// older client that never sends it is unaffected.
func parseWaitMs(r *http.Request) time.Duration {
	raw := r.URL.Query().Get("wait_ms")
	if raw == "" {
		return 0
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return 0
	}
	d := time.Duration(n) * time.Millisecond
	if d > longPollCap {
		d = longPollCap
	}
	return d
}

// claimWithWait is ClaimTask with an optional long-poll wait (Plane D §7 D1). With
// wait<=0 it is a single ClaimTask — identical to the pre-long-poll behavior. With
// wait>0 and nothing immediately claimable, it re-attempts ClaimTask every
// longPollInterval until a task is found or the wait elapses, then returns (nil,
// nil) for the caller's 204. Each attempt is its own short-lived transaction (the
// wait never holds a DB transaction open). ctx (the request context) is honored on
// every tick, so a client disconnect aborts the wait immediately. A timed-out empty
// return bumps metrics.longPollTimeouts; errNotFound (unregistered worker) and any
// real claim error surface at once without waiting.
func (s *Server) claimWithWait(ctx context.Context, auth WorkerAuth, wait time.Duration) (*ClaimedTask, error) {
	c, err := s.store.ClaimTask(ctx, auth)
	if err != nil || c != nil || wait <= 0 {
		return c, err
	}

	deadline := time.NewTimer(wait)
	defer deadline.Stop()
	tick := time.NewTicker(longPollInterval)
	defer tick.Stop()

	for {
		select {
		case <-ctx.Done():
			// Client hung up (or the server is shutting down): stop waiting. Not a
			// timeout — no task was withheld, so do not count it as one.
			return nil, ctx.Err()
		case <-deadline.C:
			metrics.longPollTimeouts.Add(1)
			return nil, nil
		case <-tick.C:
			c, err := s.store.ClaimTask(ctx, auth)
			if err != nil || c != nil {
				return c, err
			}
		}
	}
}

// handleWorkerPoll claims the next eligible task via the SKIP-LOCKED queue and
// returns it as a TaskDispatch, or 204 when nothing is available.
//
// Long-poll (Plane D §7 D1): an optional ?wait_ms=N turns an otherwise-empty poll
// into a server-side wait (see claimWithWait) — re-attempting the claim until a
// task appears or wait_ms (capped at longPollCap) elapses. No wait_ms keeps the
// original single-shot behavior, so an older agent is unaffected.
func (s *Server) handleWorkerPoll(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	ctx := r.Context()
	c, err := s.claimWithWait(ctx, *auth, parseWaitMs(r))
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusForbidden, "worker not registered — call /v1/worker/register first")
		return
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		// The client disconnected (or its deadline passed) mid-wait. There is no one
		// left to answer; just stop. Writing a body would race a closed connection.
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	if c == nil {
		w.WriteHeader(http.StatusNoContent) // no work (immediately, or after the long-poll wait elapsed)
		return
	}

	// NOTE: a honeypot is NOT signalled to the worker, and its expected answer is
	// never sent. The server knows the task is a honeypot (from the DB) and verifies
	// the worker's uploaded result against the stored known answer in verifyTaskResult.
	// Leaking is_honeypot/honeypot_ans would let a hostile worker ace every probe.

	// Presign the input for download and the result key for upload. The agent
	// fetches input_url, runs the task, and PUTs its result to output_url (the
	// result_key presigned). result_key is the canonical key it echoes in commit.
	inputURL, err := s.storage.PresignGet(ctx, c.InputRef, 15*time.Minute)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "presign input: "+err.Error())
		return
	}
	outputURL, err := s.storage.PresignPut(ctx, c.ResultKey, time.Hour)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "presign result: "+err.Error())
		return
	}

	// Reconstruct a minimal manifest for the dispatch from the stored job
	// fields. The verification policy round-trips from jsonb.
	var vp VerificationPolicy
	_ = json.Unmarshal(c.VerifPolicy, &vp)
	// Reconstruct the FULL job_type from the stored spec so the agent receives the
	// buyer's params (labels/schema/max_tokens/temperature), not just the bare tag.
	// Falls back to {"type":tag} when no spec was persisted (older jobs / null).
	jt := JobType{Type: c.JobType}
	if len(c.JobTypeSpec) > 0 && string(c.JobTypeSpec) != "null" {
		var parsed JobType
		if err := json.Unmarshal(c.JobTypeSpec, &parsed); err == nil && parsed.Type != "" {
			jt = parsed
		}
	}
	disp := TaskDispatch{
		TaskID: c.TaskID,
		JobID:  c.JobID,
		Manifest: JobManifest{
			ID:      c.JobID,
			JobType: jt,
			// kind is a coarse backend gate the agent checks in can_run; "gguf" is
			// accepted by every runner (the real model files are resolved by ref).
			Model:        ModelRef{Kind: "gguf", Ref: c.ModelRef},
			Inputs:       []InputRef{}, // real inputs travel via the presigned input_url, not the manifest
			Verification: vp,
			Tier:         c.Tier,
		},
		InputURL:         inputURL,
		OutputURL:        outputURL,
		ResultKey:        c.ResultKey,
		OfferedRateUsdHr: c.OfferedRateUsdHr,
		Deadline:         uint64(time.Now().Add(time.Hour).Unix()),
	}
	metrics.tasksDispatched.Add(1)
	writeJSON(w, http.StatusOK, disp)
}

func (s *Server) handleWorkerStart(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	err := s.store.StartTask(r.Context(), id, auth.WorkerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusConflict, "task not claimed by this worker or not startable")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleWorkerCommit stores the result, fetches the committed result bytes from
// object storage, runs real verification (honeypot / redundancy with
// embedding-aware comparison), and on a clean pass writes the ledger entries with
// the payout hold.
func (s *Server) handleWorkerCommit(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	ctx := r.Context()
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	var c TaskCommit
	if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid commit json: "+err.Error())
		return
	}
	c.TaskID = id // trust the path, not the body

	info, err := s.store.CommitTask(ctx, id, auth.WorkerID, c)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusConflict, "task not claimed by this worker or not committable")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}

	// Fetch the committed result bytes from object storage by the canonical
	// result key. A honeypot or redundancy task MUST have a real object to
	// compare; a missing object there is a hard error, never a silent pass.
	var commitBytes []byte
	if info.ResultKey != "" {
		b, gerr := s.storage.GetObject(ctx, info.ResultKey)
		if gerr != nil && (info.IsHoneypot || info.IsRedundancy) {
			writeErr(w, http.StatusBadRequest, "result object missing for verifiable task: "+gerr.Error())
			return
		}
		commitBytes = b // nil for a plain task whose object is absent — honest
	}

	// If a committed redundancy sibling exists for the same input chunk on a
	// different worker, fetch its result too for a within-class comparison. None
	// yet → nil, which the verifier treats as "nothing to compare" (honest).
	var redundancyBytes []byte
	if peerKey, _, perr := s.store.PeerResultKey(ctx, info.TaskID); perr == nil && peerKey != "" {
		if b, gerr := s.storage.GetObject(ctx, peerKey); gerr == nil {
			redundancyBytes = b
		}
	}

	outcome, verr := s.verifier.verifyTaskResult(ctx, info, c, commitBytes, redundancyBytes)
	if verr != nil {
		writeErr(w, http.StatusInternalServerError, "verification error: "+verr.Error())
		return
	}
	switch outcome {
	case OutcomeFail:
		metrics.verificationMismatch.Add(1)
	case OutcomePassWithPenalty:
		metrics.verificationMismatch.Add(1)
		metrics.tasksCompleted.Add(1)
	default:
		metrics.tasksCompleted.Add(1)
	}

	// On pass (or pass-with-penalty), schedule payout: real ledger rows with
	// the hold window from the job's verification policy.
	if outcome != OutcomeFail {
		// First commit wins: cancel any still-running straggler hedge sibling for
		// this chunk so it stops blocking completion and frees its worker. Logged,
		// not fatal — a stale sibling is also caught by the stale-task reaper.
		if cerr := s.store.CancelStragglerSiblings(ctx, info.JobID, info.ChunkIndex, info.TaskID); cerr != nil {
			log.Printf("commit: cancelling hedge siblings for job %s chunk %d: %v", info.JobID, info.ChunkIndex, cerr)
		}
		if err := s.scheduleTaskPayout(ctx, info); err != nil {
			writeErr(w, http.StatusInternalServerError, "ledger error: "+err.Error())
			return
		}
		// If this commit completed the job, finalize it now: merge the buyer-ready
		// artifact BEFORE marking complete + settling. A merge failure is surfaced
		// (the result the worker committed is malformed) rather than hidden. If the
		// job is not yet fully done, the background sweep finalizes it later.
		if err := s.finalizeJobIfDone(ctx, info.JobID); err != nil {
			writeErr(w, http.StatusInternalServerError, "finalizing job: "+err.Error())
			return
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

// finalizeJobIfDone finalizes a job synchronously when the just-committed task
// was its last: merge the buyer-ready artifact, THEN mark the job complete and
// settle actual_usd. Merge-before-mark guarantees a buyer never sees
// status=complete with a missing or short merged output. A not-yet-done job is a
// clean no-op (the background sweep finalizes it later); a merge failure is
// returned so the commit surfaces it (never marked complete on bad output).
func (s *Server) finalizeJobIfDone(ctx context.Context, jobID uuid.UUID) error {
	done, err := s.store.JobAllTasksDone(ctx, jobID)
	if err != nil {
		return err
	}
	if !done {
		return nil
	}
	if _, err := s.MergeJobResults(ctx, jobID); err != nil {
		return err
	}
	if err := s.store.MarkJobComplete(ctx, jobID); err != nil {
		return err
	}
	if err := s.store.SetJobActualUSD(ctx, jobID); err != nil {
		return err
	}
	// Best-effort external charge: gated on Stripe + a saved card, idempotent by
	// job id. A no-op (and unchanged lifecycle) when billing isn't configured.
	s.chargeForJob(ctx, jobID)
	s.advanceIntake(ctx, jobID) // multi-stage chain: no-op unless this job is an intake stage
	return nil
}

// scheduleTaskPayout writes the buyer_charge / supplier_credit / platform_take
// ledger rows for a completed task, using the per-task share of the job's
// estimate and the policy's payout hold.
func (s *Server) scheduleTaskPayout(ctx context.Context, info *CommitTaskInfo) error {
	j, err := s.store.getJobInternal(ctx, info.JobID)
	if err != nil {
		return err
	}
	perTask := 0.0
	if j.TaskCount > 0 {
		perTask = j.EstimatedUSD / float64(j.TaskCount)
	}
	var vp VerificationPolicy
	_ = json.Unmarshal(j.VerificationPolicy, &vp)
	entries := splitCharge(j.BuyerID, info.SupplierID, info.TaskID, perTask, vp.PayoutHoldSecs, time.Now())
	return s.store.InsertLedgerEntries(ctx, entries)
}

func (s *Server) handleWorkerEarnings(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxWorker).(*WorkerAuth)
	e, err := s.store.WorkerEarnings(r.Context(), auth.SupplierID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, e)
}

// --- admin handlers ---

func (s *Server) handleAdminWorkers(w http.ResponseWriter, r *http.Request) {
	ws, err := s.store.ListWorkers(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, ws)
}

func (s *Server) handleAdminFraudFlags(w http.ResponseWriter, r *http.Request) {
	f, err := s.store.ListFraudFlags(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, f)
}

// handleAdminFraud surfaces the richer fraud report: per-supplier reputation,
// tier, status, quarantine timestamp, and confirmed-fraud clawback/mismatch
// counts — the auto-quarantine (Verification V2) review queue.
func (s *Server) handleAdminFraud(w http.ResponseWriter, r *http.Request) {
	f, err := s.store.ListFraud(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, f)
}

// handleAdminDrift surfaces the per-(job_type, model) quoted-vs-actual rollup
// (Plane D D6 / errata C-Errata-6): observed avg + p90 committed-task duration, the
// sample count behind it, and the average quoted whole-job eta_secs — so an operator
// can see how the static quote tracks reality and which slices have enough history
// that their observed p90 is now driving the ETA.
func (s *Server) handleAdminDrift(w http.ResponseWriter, r *http.Request) {
	d, err := s.store.DriftRollup(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, d)
}

// handleAdminSchedulerExplain answers "why is this worker getting no work?" (Plane D
// §17 D11). Many "slow" workers are not slow at all — there is simply nothing
// ELIGIBLE for them in the queue. For the worker in ?worker_id=, it runs the SAME
// hard-filter predicates as ClaimTask against every currently-claimable task and
// returns per-reason COUNTS (memory/model/job-type/hw-class/residency/throttle/payout/
// supplier gates) plus how many tasks it COULD claim (eligible). Read-only: it never
// claims and never mutates. 400 on a missing/malformed worker_id, 404 when the worker
// is unregistered.
func (s *Server) handleAdminSchedulerExplain(w http.ResponseWriter, r *http.Request) {
	raw := r.URL.Query().Get("worker_id")
	if raw == "" {
		writeErr(w, http.StatusBadRequest, "worker_id query parameter required")
		return
	}
	workerID, err := uuid.Parse(raw)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "invalid worker_id: must be a uuid")
		return
	}
	exp, err := s.store.SchedulerExplain(r.Context(), workerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "worker not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, exp)
}

// handleAdminJobs lists recent jobs across all buyers (admin-only).
func (s *Server) handleAdminJobs(w http.ResponseWriter, r *http.Request) {
	j, err := s.store.ListJobsAdmin(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, j)
}

// handleAdminPayouts surfaces the per-supplier payout rollup by state (the payout
// review surface: pending / held / released / clawed_back credits).
func (s *Server) handleAdminPayouts(w http.ResponseWriter, r *http.Request) {
	p, err := s.store.ListPayoutsAdmin(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, p)
}

func (s *Server) handleAdminSuspend(w http.ResponseWriter, r *http.Request) {
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	err := s.store.SuspendWorker(r.Context(), id)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "worker not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "suspended"})
}

// handleJobInvoice returns the buyer-facing invoice for one job (scoped to the
// authenticated buyer): estimated vs actual charge plus the realized ledger
// breakdown (supplier credit, platform take).
func (s *Server) handleJobInvoice(w http.ResponseWriter, r *http.Request) {
	auth := r.Context().Value(ctxBuyer).(*AuthResult)
	id, ok := pathUUID(w, r)
	if !ok {
		return
	}
	inv, err := s.store.JobInvoice(r.Context(), id, auth.BuyerID)
	if errors.Is(err, errNotFound) {
		writeErr(w, http.StatusNotFound, "job not found")
		return
	}
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, inv)
}

// handleDashboard serves the static operator dashboard at the root, same-origin
// with the API so its fetches need no CORS. Path is web/dashboard.html, overridable
// via DASHBOARD_PATH; a missing file is a clear 404, never a faked page.
func (s *Server) handleDashboard(w http.ResponseWriter, r *http.Request) {
	path := os.Getenv("DASHBOARD_PATH")
	if path == "" {
		path = "web/dashboard.html"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		writeErr(w, http.StatusNotFound, "dashboard not found at "+path+" (set DASHBOARD_PATH)")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(b)
}

// handleApp serves the role-based app skeleton (Supplier / Buyer / Admin /
// Workflows) at /app, same-origin with the API so its fetches need no CORS. Path
// is web/skeleton.html, overridable via APP_PATH; a missing file is a clear 404,
// never a faked page. This is skeleton structure + wiring only — final design is
// deferred (see docs/PRODUCT_SHAPE.md).
func (s *Server) handleApp(w http.ResponseWriter, r *http.Request) {
	path := os.Getenv("APP_PATH")
	if path == "" {
		path = "web/skeleton.html"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		writeErr(w, http.StatusNotFound, "app skeleton not found at "+path+" (set APP_PATH)")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(b)
}

// handleDemo serves the Launch/Earn product demo at /demo, same-origin with the
// API so its fetches need no CORS. Path is web/demo.html, overridable via
// DEMO_PATH; a missing file is a clear 404, never a faked page. It is a navigable
// monochrome prototype of the two windows (buyer Launch + seller Earn): it pings
// /healthz live and simulates the auditor / job / earnings flows.
func (s *Server) handleDemo(w http.ResponseWriter, r *http.Request) {
	path := os.Getenv("DEMO_PATH")
	if path == "" {
		path = "web/demo.html"
	}
	b, err := os.ReadFile(path)
	if err != nil {
		writeErr(w, http.StatusNotFound, "demo not found at "+path+" (set DEMO_PATH)")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(b)
}

// --- pricing + split helpers ---

// modelPrice returns a model's effective per-1K price. Most models price per
// 1,000 units (tokens/embeddings); some (e.g. audio transcription) price per
// discrete unit, in which case price_per_unit is scaled to a per-1K figure so a
// single number drives both the catalogue and the estimate.
func modelPrice(m ModelRow) float64 {
	if m.PricePer1K > 0 {
		return m.PricePer1K
	}
	return m.PricePerUnit * 1000.0
}

// tierMultiplier prices the service tiers: priority +50%, trusted +100%.
func tierMultiplier(tier string) float64 {
	switch tier {
	case "priority":
		return 1.5
	case "trusted":
		return 2.0
	default:
		return 1.0
	}
}

// estimateJobUSD estimates a job's pre-execution cost from the DB-backed model
// price and the unit count. Units = JSONL lines (one record per line) for the
// per-1K pricing, with a byte-derived floor (~4 bytes/token) so a few very large
// records are not under-counted. actual_usd is set from the real ledger on
// completion; this is the honest up-front estimate.
func (s *Server) estimateJobUSD(ctx context.Context, modelRef string, inputBytes []byte, nLines int, tier string) float64 {
	price := 0.002 // default per-1K when the model is unknown to the catalogue
	if m, err := s.store.GetModel(ctx, modelRef); err == nil {
		price = modelPrice(*m)
	}
	units := float64(nLines)
	if byteUnits := float64(len(inputBytes)) / 4.0; byteUnits > units {
		units = byteUnits
	}
	est := units / 1000.0 * price * tierMultiplier(tier)
	return roundUSD(est)
}

// splitSizeOf reads params.split_size (lines per task chunk), defaulting to
// defaultSplitSize when absent or non-positive.
func splitSizeOf(params json.RawMessage) int {
	if len(params) == 0 {
		return defaultSplitSize
	}
	var p struct {
		SplitSize int `json:"split_size"`
	}
	if err := json.Unmarshal(params, &p); err != nil || p.SplitSize <= 0 {
		return defaultSplitSize
	}
	return p.SplitSize
}

// jobTypeThroughput is the representative items processed per SECOND for one
// task of a job type on a typical worker. It drives both adaptive chunk sizing
// (items/chunk = throughput × targetTaskSecs) and the offered-rate estimate
// (units/hr = throughput × 3600). These are deliberate, documented sane defaults
// — not measured per-worker numbers — chosen so each task lands in ~30–60s.
// Embeddings are cheap per item (hundreds/s); token generation is dear (a handful
// of completions/s). Unknown types fall back to a conservative middle figure.
var jobTypeThroughput = map[string]float64{
	"embed":                200, // short texts → embeddings, very fast
	"batch_classification": 80,  // one classification per item, cheap
	"rerank":               40,  // a scored pass over a candidate list per item
	"json_extraction":      8,   // structured generation per item, dearer
	"batch_infer":          4,   // free-form completion per item, dearest
	"audio_transcribe":     2,   // realtime-ish per clip
	"image_gen":            0.2, // multi-step diffusion per image
}

// throughputOf returns a job type's items/sec, defaulting to a conservative 10.
func throughputOf(jobType string) float64 {
	if v, ok := jobTypeThroughput[jobType]; ok && v > 0 {
		return v
	}
	return 10
}

// targetTaskSecs is the per-task duration adaptive chunk sizing aims for, so a
// task is big enough to amortize dispatch overhead but small enough to hedge and
// retry cheaply (~30–60s; we target the midpoint).
const targetTaskSecs = 45

// adaptiveSplitSize picks the JSONL lines-per-task. An explicit, positive
// params.split_size always wins (the buyer override). Otherwise the size is
// throughput(jobType) × targetTaskSecs, clamped to a sane [1,4096] band, so an
// embed job packs far more items per chunk than a generation job for the same
// ~45s target.
func adaptiveSplitSize(jobType string, params json.RawMessage) int {
	if len(params) > 0 {
		var p struct {
			SplitSize int `json:"split_size"`
		}
		if err := json.Unmarshal(params, &p); err == nil && p.SplitSize > 0 {
			return p.SplitSize
		}
	}
	n := int(throughputOf(jobType) * targetTaskSecs)
	if n < 1 {
		n = 1
	}
	if n > 4096 {
		n = 4096
	}
	return n
}

// offeredRateUsdHr derives the $/hr a worker earns running this job from the
// DB-backed model price and the job type's representative throughput:
//
//	units/hr      = throughput(jobType) × 3600
//	$/hr          = (units/hr / 1000) × price_per_1k_usd
//
// This is the rate the claim's min-payout gate compares to the worker's
// reservation price. When the model is unknown to the catalogue we fall back to
// the same default per-1K price estimateJobUSD uses, so the gate is never zeroed.
func (s *Server) offeredRateUsdHr(ctx context.Context, jobType, modelRef string) float32 {
	price := 0.002
	if m, err := s.store.GetModel(ctx, modelRef); err == nil {
		price = modelPrice(*m)
	}
	unitsPerHr := throughputOf(jobType) * 3600.0
	return float32(unitsPerHr / 1000.0 * price)
}

// perTaskSecsFromP90 converts an observed p90 committed-task duration (ms) into the
// per-task seconds the ETA uses. p90ms <= 0 means "no trustworthy history" (the store
// returns 0 below the sample floor or on error), so we fall back to the static
// throughput target. A positive p90 rounds UP to whole seconds (a sub-second p90 still
// counts as one second of work). Pure — unit-tested without a DB.
func perTaskSecsFromP90(p90ms int64) int {
	if p90ms <= 0 {
		return targetTaskSecs // thin/empty history → static target (Plane D D6 fallback)
	}
	secs := int((p90ms + 999) / 1000)
	if secs < 1 {
		secs = 1
	}
	return secs
}

// estimateETASecs is a simple queue-depth/throughput estimate: the time for this
// job's tasks plus everything already queued ahead of them to drain across the
// live fleet. perTaskSecs is the per-task duration: we use the OBSERVED p90 of past
// committed tasks for this (job_type, model_ref) once enough have been recorded
// (Plane D D6 drift feedback — the Exchange Brain learns reality), and fall back to
// the static throughput target when history is too thin (or a DB error). The queue
// depth and active-worker count come from the store (a DB error degrades gracefully
// to a single-worker, no-backlog estimate rather than failing the submission).
// Never returns < perTaskSecs (a job always takes at least one task's worth of time).
func (s *Server) estimateETASecs(ctx context.Context, jobType, modelRef string, nTasks int) int {
	// Drift feedback: prefer the observed p90 per-task duration once history is thick
	// enough (HistoricalP90DurationMs returns 0 below the sample floor, so a thin or
	// empty history — or a DB error — cleanly falls back to the static target).
	p90ms, _, err := s.store.HistoricalP90DurationMs(ctx, jobType, modelRef)
	if err != nil {
		p90ms = 0
	}
	perTaskSecs := perTaskSecsFromP90(p90ms)
	queued, _ := s.store.QueuedTaskCount(ctx)
	workers, _ := s.store.ActiveWorkerCount(ctx)
	if workers < 1 {
		workers = 1
	}
	// This job's tasks land behind whatever is already queued; spread across the
	// fleet. round up so a partial wave still counts as a full pass.
	ahead := queued // tasks already waiting
	totalTasks := ahead + nTasks
	waves := (totalTasks + workers - 1) / workers
	eta := waves * perTaskSecs
	if eta < perTaskSecs {
		eta = perTaskSecs
	}
	return eta
}

// tierMinCompletion is the human-facing completion floor per tier, so the RFC3339
// estimated_completion is never absurdly near even when the queue is empty.
func tierMinCompletion(tier string) time.Duration {
	if tier == "priority" {
		return 5 * time.Minute
	}
	return 15 * time.Minute
}

// splitJSONL breaks JSONL bytes into chunks of at most n lines each, preserving
// line content (newline-terminated). Blank lines are dropped (they carry no
// record). The remainder forms a final, smaller chunk. Returns nil for empty
// input. n<=0 is treated as defaultSplitSize.
func splitJSONL(data []byte, n int) [][]byte {
	if n <= 0 {
		n = defaultSplitSize
	}
	var lines [][]byte
	for _, ln := range bytes.Split(data, []byte("\n")) {
		ln = bytes.TrimRight(ln, "\r")
		if len(bytes.TrimSpace(ln)) == 0 {
			continue
		}
		lines = append(lines, ln)
	}
	if len(lines) == 0 {
		return nil
	}
	var chunks [][]byte
	for i := 0; i < len(lines); i += n {
		end := i + n
		if end > len(lines) {
			end = len(lines)
		}
		var buf bytes.Buffer
		for _, ln := range lines[i:end] {
			buf.Write(ln)
			buf.WriteByte('\n')
		}
		chunks = append(chunks, buf.Bytes())
	}
	return chunks
}

// fracCount returns round(n * frac), clamped to [0, n]. Used to size honeypot
// and redundancy task counts from the policy fractions.
func fracCount(n int, frac float32) int {
	if frac <= 0 || n <= 0 {
		return 0
	}
	c := int(math.Round(float64(n) * float64(frac)))
	if c > n {
		c = n
	}
	return c
}

func roundUSD(v float64) float64 { return math.Round(v*1e6) / 1e6 }

// pathUUID parses the {id} path value as a UUID, writing a 400 on failure.
func pathUUID(w http.ResponseWriter, r *http.Request) (uuid.UUID, bool) {
	id, err := uuid.Parse(r.PathValue("id"))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "invalid id: must be a uuid")
		return uuid.Nil, false
	}
	return id, true
}

// writeJSON serializes v as a JSON response with the given status.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		// Best-effort: the status is already written; log to stderr via the
		// caller is not possible here, so surface nothing more than the failed
		// encode (the client will see a truncated body).
		_ = err
	}
}

// writeErr writes a uniform JSON error body.
func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, APIError{Error: msg})
}
