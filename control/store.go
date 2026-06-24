package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store.go — the one concrete data layer. A *Store wraps a *pgxpool.Pool and
// owns every SQL query in the control plane. Deliberately NOT an interface:
// there is exactly one implementation, so an interface would be ceremony
// (BLACKHOLE: collapse the indirection). Column and table names MUST match
// db/schema.sql exactly — that schema is the contract.

// Store is the database access layer.
type Store struct {
	pool *pgxpool.Pool
}

// NewStore wraps an already-opened pool.
func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

// Ping verifies DB connectivity for /healthz.
func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

// Migrate applies the control-plane schema additions the V2 job-split + webhook
// contract needs, idempotently (IF NOT EXISTS everywhere). db/schema.sql is owned
// by the infra side and is the base contract; these statements only ADD the
// columns/tables this binary requires and that the mandate's schema names exactly
// (tasks.input_ref, tasks.result_key, the webhooks and models tables). Running
// twice is a no-op. Surfacing a failure here is fatal at startup, never silent.
func (s *Store) Migrate(ctx context.Context) error {
	stmts := []string{
		// Per-task input/result object keys. A task is a split of the job's input,
		// so it carries its own chunk key (input_ref) and result target key.
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS input_ref TEXT`,
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS result_key TEXT`,
		// Webhook registrations for job-completion delivery.
		`CREATE TABLE IF NOT EXISTS webhooks (
		   id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   buyer_id   UUID,
		   job_id     UUID,
		   url        TEXT NOT NULL,
		   created_at TIMESTAMPTZ DEFAULT now()
		 )`,
		// Real model + pricing catalogue (replaces the old static Go list).
		`CREATE TABLE IF NOT EXISTS models (
		   id            TEXT PRIMARY KEY,
		   family        TEXT,
		   quant         TEXT,
		   kind          TEXT,
		   dim           INT,
		   job_type      TEXT,
		   price_per_1k  NUMERIC(12,8),
		   price_per_unit NUMERIC(12,8),
		   min_memory_gb REAL,
		   hf_repo       TEXT
		 )`,
		`CREATE INDEX IF NOT EXISTS webhooks_job_idx ON webhooks (job_id)`,
		// Delivery flag for the completion-webhook sweep: a webhook is delivered
		// once for its job. NULL = not yet delivered.
		`ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ`,
		// Scheduler V2 / Turbo columns. These MIRROR db/schema.sql (owned by infra)
		// so a control plane that only ran Migrate (not the full schema.sql) still
		// self-migrates to the columns the hard-filter claim + result merge need.
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_memory_gb REAL DEFAULT 0`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_reputation REAL DEFAULT 0`,
		// Private Deployment tier (research §3): a private_pool job routes ONLY to the
		// buyer's own bound suppliers (their dedicated Mac/GPU fleet), so "data never
		// leaves our boxes" is contractual, not marketing. private_pool_members binds
		// a buyer to the suppliers allowed to run their private work.
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS private_pool BOOLEAN DEFAULT false`,
		`CREATE TABLE IF NOT EXISTS private_pool_members (
		   buyer_id    UUID NOT NULL,
		   supplier_id UUID NOT NULL,
		   created_at  TIMESTAMPTZ DEFAULT now(),
		   PRIMARY KEY (buyer_id, supplier_id)
		 )`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hw_classes TEXT[]`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS data_residency TEXT[]`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS split_size INT`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS offered_rate_usd_hr REAL`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS eta_secs INT`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_type_spec JSONB`,
		// Budget Governor (Plane C §12 / Plane D §14 D8): buyer hard spend cap +
		// the governor state machine. NULL max_usd = no cap (unchanged behavior).
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_usd NUMERIC(12,6)`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS budget_state TEXT DEFAULT 'tracking'`,
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chunk_index INT DEFAULT 0`,
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS hedged_from UUID`,
		`ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_jobs TEXT[]`,
		`ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_models TEXT[]`,
		`ALTER TABLE workers ADD COLUMN IF NOT EXISTS min_payout_usd_hr REAL DEFAULT 0`,
		`ALTER TABLE workers ADD COLUMN IF NOT EXISTS thermal_ok BOOLEAN DEFAULT true`,
		`ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS data_country TEXT`,
		`ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ`,
		`CREATE INDEX IF NOT EXISTS tasks_job_chunk_idx ON tasks (job_id, chunk_index)`,
		`CREATE INDEX IF NOT EXISTS workers_supplier_idx ON workers (supplier_id)`,
		// Quote-to-actual drift feedback (Plane D D6 / errata C-Errata-6). MIRRORS
		// db/schema.sql: real per-task durations of COMMITTED tasks only, so the
		// Exchange Brain learns an observed p90 the next quote's ETA can lean on.
		// Malformed/failed tasks never write a row, so they cannot poison the estimate.
		`CREATE TABLE IF NOT EXISTS task_durations (
		   id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   created_at  TIMESTAMPTZ DEFAULT now(),
		   job_id      UUID,
		   job_type    TEXT,
		   model_ref   TEXT,
		   split_size  INT,
		   duration_ms BIGINT
		 )`,
		`CREATE INDEX IF NOT EXISTS task_durations_type_model_idx ON task_durations (job_type, model_ref)`,
		// Concierge intake (intake.go) + buyer billing (billing.go): connected git
		// sources, detected-pipeline intakes, and the buyer→Stripe-customer map.
		`CREATE TABLE IF NOT EXISTS git_sources (
		   id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   buyer_id        UUID,
		   provider        TEXT DEFAULT 'github',
		   repo_full_name  TEXT,
		   default_branch  TEXT,
		   access_token    TEXT,
		   connected_at    TIMESTAMPTZ DEFAULT now()
		 )`,
		`CREATE INDEX IF NOT EXISTS git_sources_buyer_idx ON git_sources (buyer_id)`,
		`CREATE TABLE IF NOT EXISTS intakes (
		   id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   buyer_id    UUID,
		   source_id   UUID,
		   ref         TEXT,
		   status      TEXT DEFAULT 'inspecting',
		   pattern     TEXT,
		   pipeline    JSONB,
		   quote_id    UUID,
		   job_id      UUID,
		   created_at  TIMESTAMPTZ DEFAULT now()
		 )`,
		`CREATE INDEX IF NOT EXISTS intakes_buyer_idx ON intakes (buyer_id)`,
		`CREATE TABLE IF NOT EXISTS billing_customers (
		   buyer_id               UUID PRIMARY KEY,
		   stripe_customer_id     TEXT,
		   default_payment_method TEXT,
		   created_at             TIMESTAMPTZ DEFAULT now()
		 )`,
		// Supplier Connect account (the payout transfers' destination) + the
		// intake→job links that drive multi-stage pipeline execution.
		`ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS stripe_acct TEXT`,
		`CREATE TABLE IF NOT EXISTS intake_jobs (
		   job_id      UUID PRIMARY KEY,
		   intake_id   UUID,
		   stage_index INT
		 )`,
		`CREATE INDEX IF NOT EXISTS intake_jobs_intake_idx ON intake_jobs (intake_id)`,
	}
	for _, q := range stmts {
		if _, err := s.pool.Exec(ctx, q); err != nil {
			return fmt.Errorf("migrate: %q: %w", q, err)
		}
	}
	return nil
}

// errNotFound is returned when a lookup matches no row.
var errNotFound = errors.New("not found")

// --- intake + billing data access (git_sources, intakes, billing_customers) ---

// InsertGitSource records a connected source for a buyer with its access token
// (encrypted at rest in production — the KMS envelope is the external step). The
// repo/branch are filled in when the buyer picks one.
func (s *Store) InsertGitSource(ctx context.Context, buyerID uuid.UUID, token string) (uuid.UUID, error) {
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`INSERT INTO git_sources (buyer_id, provider, access_token) VALUES ($1, 'github', $2) RETURNING id`,
		buyerID, sealToken(token)).Scan(&id) // sealed at the data boundary (AES-GCM when CX_TOKEN_KEY set)
	return id, err
}

// ListGitSources returns a buyer's connected sources (the token is never selected).
func (s *Store) ListGitSources(ctx context.Context, buyerID uuid.UUID) ([]GitSource, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, provider, COALESCE(repo_full_name,''), COALESCE(default_branch,''), connected_at
		   FROM git_sources WHERE buyer_id=$1 ORDER BY connected_at DESC`, buyerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []GitSource
	for rows.Next() {
		var g GitSource
		if err := rows.Scan(&g.ID, &g.Provider, &g.RepoFullName, &g.DefaultBranch, &g.ConnectedAt); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	return out, rows.Err()
}

// GetGitSource loads one of the buyer's sources WITH its access token (for a
// server-side GitHub fetch). errNotFound when it is not the buyer's or absent.
func (s *Store) GetGitSource(ctx context.Context, buyerID uuid.UUID, id string) (GitSource, error) {
	gid, err := uuid.Parse(id)
	if err != nil {
		return GitSource{}, errNotFound
	}
	var g GitSource
	err = s.pool.QueryRow(ctx,
		`SELECT id, provider, COALESCE(repo_full_name,''), COALESCE(default_branch,''), COALESCE(access_token,'')
		   FROM git_sources WHERE id=$1 AND buyer_id=$2`, gid, buyerID).
		Scan(&g.ID, &g.Provider, &g.RepoFullName, &g.DefaultBranch, &g.AccessToken)
	if errors.Is(err, pgx.ErrNoRows) {
		return GitSource{}, errNotFound
	}
	if err != nil {
		return GitSource{}, err
	}
	g.AccessToken = openToken(g.AccessToken) // decrypt at the data boundary
	return g, nil
}

// InsertIntake records a detected-pipeline intake. pipelineJSON is the marshaled
// DetectedPipeline (marshaled by the caller, so this layer keeps no json import).
func (s *Store) InsertIntake(ctx context.Context, buyerID uuid.UUID, sourceID, ref, status, pattern string, pipelineJSON []byte) (uuid.UUID, error) {
	var sid any
	if sourceID != "" {
		if p, err := uuid.Parse(sourceID); err == nil {
			sid = p
		}
	}
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`INSERT INTO intakes (buyer_id, source_id, ref, status, pattern, pipeline)
		   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
		buyerID, sid, ref, status, pattern, pipelineJSON).Scan(&id)
	return id, err
}

// GetBillingCustomer returns the buyer's Stripe customer id + default payment
// method (both "" if none); errNotFound when the buyer has no billing row yet.
func (s *Store) GetBillingCustomer(ctx context.Context, buyerID uuid.UUID) (custID, pm string, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(stripe_customer_id,''), COALESCE(default_payment_method,'')
		   FROM billing_customers WHERE buyer_id=$1`, buyerID).Scan(&custID, &pm)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", errNotFound
	}
	return custID, pm, err
}

// UpsertBillingCustomer maps a buyer to a Stripe customer id (idempotent).
func (s *Store) UpsertBillingCustomer(ctx context.Context, buyerID uuid.UUID, custID string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO billing_customers (buyer_id, stripe_customer_id) VALUES ($1, $2)
		   ON CONFLICT (buyer_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id`,
		buyerID, custID)
	return err
}

// GetIntake loads a detected intake's source/ref/pattern/pipeline for the launch.
func (s *Store) GetIntake(ctx context.Context, buyerID uuid.UUID, intakeID string) (sourceID, ref, pattern string, pipeline []byte, err error) {
	gid, e := uuid.Parse(intakeID)
	if e != nil {
		err = errNotFound
		return
	}
	var sid *uuid.UUID
	err = s.pool.QueryRow(ctx,
		`SELECT source_id, COALESCE(ref,''), COALESCE(pattern,''), pipeline FROM intakes WHERE id=$1 AND buyer_id=$2`,
		gid, buyerID).Scan(&sid, &ref, &pattern, &pipeline)
	if errors.Is(err, pgx.ErrNoRows) {
		err = errNotFound
		return
	}
	if sid != nil {
		sourceID = sid.String()
	}
	return
}

// UpdateIntakeJob links a launched job to its intake and marks it launched.
func (s *Store) UpdateIntakeJob(ctx context.Context, intakeID, jobID uuid.UUID) error {
	_, err := s.pool.Exec(ctx, `UPDATE intakes SET job_id=$2, status='launched' WHERE id=$1`, intakeID, jobID)
	return err
}

// SetBillingPMByCustomer records a buyer's default payment method, keyed by their
// Stripe customer id (the webhook's view of who they are).
func (s *Store) SetBillingPMByCustomer(ctx context.Context, custID, pm string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE billing_customers SET default_payment_method=$2 WHERE stripe_customer_id=$1`, custID, pm)
	return err
}

// JobChargeInfo returns a job's buyer + settled actual cost (for the auto-charge).
func (s *Store) JobChargeInfo(ctx context.Context, jobID uuid.UUID) (buyerID uuid.UUID, actualUSD float64, err error) {
	err = s.pool.QueryRow(ctx, `SELECT buyer_id, COALESCE(actual_usd,0) FROM jobs WHERE id=$1`, jobID).Scan(&buyerID, &actualUSD)
	if errors.Is(err, pgx.ErrNoRows) {
		err = errNotFound
	}
	return
}

// SetSupplierStripeAcct records a supplier's Connect account id (the payout target).
func (s *Store) SetSupplierStripeAcct(ctx context.Context, supplierID uuid.UUID, acct string) error {
	_, err := s.pool.Exec(ctx, `UPDATE suppliers SET stripe_acct=$2 WHERE id=$1`, supplierID, acct)
	return err
}

// InsertIntakeJobLink links a launched stage job to its intake + stage index.
func (s *Store) InsertIntakeJobLink(ctx context.Context, jobID, intakeID uuid.UUID, stageIndex int) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO intake_jobs (job_id, intake_id, stage_index) VALUES ($1, $2, $3) ON CONFLICT (job_id) DO NOTHING`,
		jobID, intakeID, stageIndex)
	return err
}

// IntakeForJob resolves a completed job back to its intake + stage (ok=false when
// the job is not part of a pipeline — the common case, a plain job).
func (s *Store) IntakeForJob(ctx context.Context, jobID uuid.UUID) (intakeID uuid.UUID, stageIndex int, ok bool) {
	err := s.pool.QueryRow(ctx, `SELECT intake_id, stage_index FROM intake_jobs WHERE job_id=$1`, jobID).Scan(&intakeID, &stageIndex)
	return intakeID, stageIndex, err == nil
}

// IntakePipeline returns an intake's detected-pipeline JSON.
func (s *Store) IntakePipeline(ctx context.Context, intakeID uuid.UUID) ([]byte, error) {
	var pj []byte
	err := s.pool.QueryRow(ctx, `SELECT pipeline FROM intakes WHERE id=$1`, intakeID).Scan(&pj)
	return pj, err
}

// IntakeStageSubmitted reports whether a stage of an intake already has a job.
func (s *Store) IntakeStageSubmitted(ctx context.Context, intakeID uuid.UUID, stageIndex int) bool {
	var n int
	_ = s.pool.QueryRow(ctx, `SELECT COUNT(*) FROM intake_jobs WHERE intake_id=$1 AND stage_index=$2`, intakeID, stageIndex).Scan(&n)
	return n > 0
}

// JobOutputRef returns a completed job's merged-output object key (for chaining the
// next pipeline stage onto it).
func (s *Store) JobOutputRef(ctx context.Context, jobID uuid.UUID) (string, error) {
	var ref string
	err := s.pool.QueryRow(ctx, `SELECT COALESCE(output_ref,'') FROM jobs WHERE id=$1`, jobID).Scan(&ref)
	return ref, err
}

// nullStrSlice maps an empty/nil slice to nil so it encodes as a SQL NULL (not an
// empty array). The claim's filters treat NULL hw_classes/data_residency as "any",
// which is the wrong semantics for an empty {} array — so empty must become NULL.
func nullStrSlice(xs []string) any {
	if len(xs) == 0 {
		return nil
	}
	return xs
}

// nullJSON maps empty bytes to nil so an absent JSONB column stays NULL rather
// than an invalid empty document.
func nullJSON(b []byte) any {
	if len(b) == 0 {
		return nil
	}
	return b
}

// nullPosFloat maps a non-positive value to nil so the column stays SQL NULL
// (used for jobs.max_usd: 0 = "no cap" must be NULL, so the claim's budget gate
// distinguishes an unset cap from a real $0 cap via `IS NOT NULL`).
func nullPosFloat(v float64) any {
	if v <= 0 {
		return nil
	}
	return v
}

// nullUUID maps the zero UUID to nil so an unset reference stays SQL NULL (used for
// jobs.quote_id: no binding must be NULL so the partial index and the quote→invoice
// join cleanly skip unbound jobs).
func nullUUID(id uuid.UUID) any {
	if id == (uuid.UUID{}) {
		return nil
	}
	return id
}

// hashKey hashes a credential for comparison against the stored key_hash. We
// store and compare only the hash, never the raw key.
func hashKey(raw string) string {
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// --- auth ---

// AuthResult identifies the caller behind a credential.
type AuthResult struct {
	BuyerID uuid.UUID
	IsAdmin bool
}

// LookupAPIKey resolves a bearer API key to its buyer + admin flag. Missing or
// revoked → errNotFound (the handler turns that into 401). Never fake-accepts.
func (s *Store) LookupAPIKey(ctx context.Context, rawKey string) (AuthResult, error) {
	var r AuthResult
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id, is_admin FROM api_keys
		 WHERE key_hash = $1 AND revoked = false`,
		hashKey(rawKey),
	).Scan(&r.BuyerID, &r.IsAdmin)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, errNotFound
	}
	return r, err
}

// WorkerAuth identifies the worker/supplier behind a worker token.
type WorkerAuth struct {
	WorkerID   uuid.UUID
	SupplierID uuid.UUID
}

// LookupWorkerToken resolves an X-Worker-Token to its worker + supplier. Like
// api_keys, only the SHA-256 hash is stored and compared — the raw token never
// touches the DB, so a DB read can never leak a live supplier credential.
func (s *Store) LookupWorkerToken(ctx context.Context, token string) (WorkerAuth, error) {
	var w WorkerAuth
	err := s.pool.QueryRow(ctx,
		`SELECT worker_id, supplier_id FROM worker_tokens
		 WHERE token_hash = $1 AND revoked = false`,
		hashKey(token),
	).Scan(&w.WorkerID, &w.SupplierID)
	if errors.Is(err, pgx.ErrNoRows) {
		return w, errNotFound
	}
	return w, err
}

// CreateWorkerToken mints a worker token for a supplier's worker: it generates a
// random raw token, stores ONLY its hash, and returns the raw token once. The raw
// value can never be recovered (it is not stored) — the caller hands it to the
// supplier. This is the onboarding path real suppliers use instead of seeded tokens.
func (s *Store) CreateWorkerToken(ctx context.Context, workerID, supplierID uuid.UUID) (string, error) {
	raw := newSecret("cxw_")
	if raw == "" {
		return "", errors.New("worker token: entropy failure")
	}
	if _, err := s.pool.Exec(ctx,
		`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		 VALUES ($1, $2, $3, false)`,
		hashKey(raw), workerID, supplierID,
	); err != nil {
		return "", err
	}
	return raw, nil
}

// --- workers + benchmarks ---

// UpsertWorker inserts or refreshes a worker row and persists its benchmark
// results, all in one transaction. Called on POST /v1/worker/register.
func (s *Store) UpsertWorker(ctx context.Context, cap WorkerCapability) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// thermal_ok is the AND of every benchmark's thermal_ok, defaulting to true
	// when the worker reported no benchmarks (no evidence of throttling).
	thermalOK := true
	for _, b := range cap.Benchmarks {
		thermalOK = thermalOK && b.ThermalOK
	}

	_, err = tx.Exec(ctx,
		`INSERT INTO workers
		   (id, supplier_id, hw_class, memory_gb, bw_gbps, last_seen_at, version,
		    supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,$3,$4,$5, now(), $6,$7,$8,$9,$10)
		 ON CONFLICT (id) DO UPDATE SET
		   hw_class = EXCLUDED.hw_class,
		   memory_gb = EXCLUDED.memory_gb,
		   bw_gbps = EXCLUDED.bw_gbps,
		   last_seen_at = now(),
		   version = EXCLUDED.version,
		   supported_jobs = EXCLUDED.supported_jobs,
		   supported_models = EXCLUDED.supported_models,
		   min_payout_usd_hr = EXCLUDED.min_payout_usd_hr,
		   thermal_ok = EXCLUDED.thermal_ok`,
		cap.WorkerID, cap.SupplierID, cap.HWClass, cap.MemoryGB, cap.MemoryBwGbps, cap.AgentVersion,
		cap.SupportedJobs, cap.SupportedModels, cap.MinPayoutUsdHr, thermalOK,
	)
	if err != nil {
		return err
	}

	for _, b := range cap.Benchmarks {
		_, err = tx.Exec(ctx,
			`INSERT INTO benchmark_results
			   (worker_id, model_id, job_type, tps, eps, thermal_ok, p99_latency_ms)
			 VALUES ($1,$2,$3,$4,$5,$6,$7)`,
			cap.WorkerID, b.ModelID, b.JobType, b.TPS, b.EPS, b.ThermalOK, float32(b.P99MS),
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// WorkerResources is the live resource state a heartbeat carries (the supplier
// throttling signal). All GB; Throttled means the agent is currently pausing new
// claims for memory pressure. LoadedModels is the warm-routing delta (D3): the ids
// of models warm in the agent's pool right now, upserted into worker_model_state so
// the scheduler can prefer a warm worker (it only re-ranks; never a hard filter).
type WorkerResources struct {
	AvailableMemoryGB  float32
	EffectiveMemoryGB  float32
	ReservedHeadroomGB float32
	Throttled          bool
	LoadedModels       []string
}

// HeartbeatWorker refreshes last_seen_at AND the live resource state the
// safe-dispatch claim filter reads (effective memory + throttle). Called from
// POST /v1/worker/heartbeat. A reported memory value of 0 is treated as "not
// sent" (a pre-throttling agent omits these fields) and leaves the stored value
// untouched, so the claim falls back to total memory rather than wrongly
// excluding the worker. `throttled` is always written (false for older agents,
// which keeps them claimable).
func (s *Store) HeartbeatWorker(ctx context.Context, workerID uuid.UUID, r WorkerResources) error {
	// nil → COALESCE keeps the existing column value (NULL until first real beat).
	var effective, available, headroom *float32
	if r.EffectiveMemoryGB > 0 {
		effective = &r.EffectiveMemoryGB
	}
	if r.AvailableMemoryGB > 0 {
		available = &r.AvailableMemoryGB
	}
	if r.ReservedHeadroomGB > 0 {
		headroom = &r.ReservedHeadroomGB
	}
	// One transaction so a heartbeat is all-or-nothing: liveness + memory + warm
	// state never partially apply (a mid-beat failure would otherwise leave, say,
	// last_seen_at fresh but warm state stale). Matches the atomicity the claim path
	// already uses.
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx,
		`UPDATE workers SET last_seen_at = now(),
		   effective_memory_gb  = COALESCE($2, effective_memory_gb),
		   available_memory_gb  = COALESCE($3, available_memory_gb),
		   reserved_headroom_gb = COALESCE($4, reserved_headroom_gb),
		   throttled            = $5
		 WHERE id = $1`,
		workerID, effective, available, headroom, r.Throttled); err != nil {
		return err
	}
	// Append a rolling memory sample (Plane D D4): the UPDATE above keeps only the
	// LATEST beat; this row preserves the history /admin/capacity + quote risk read.
	// Only beats that actually reported memory write a sample — a pre-throttling
	// agent sends neither value, and an all-NULL row would just dilute the median.
	if effective != nil || available != nil {
		if _, err := tx.Exec(ctx,
			`INSERT INTO worker_memory_samples (worker_id, available_gb, effective_gb, throttled)
			 VALUES ($1, $2, $3, $4)`,
			workerID, available, effective, r.Throttled); err != nil {
			return err // a failed sample is a real failure, not silently swallowed (BLACKHOLE)
		}
	}
	// Warm-model state (D3): upsert one row per model the agent reported warm,
	// refreshing last_seen_warm so the scheduler/quote see which (worker, model)
	// pairs avoid a cold load right now. The unnest+ON CONFLICT writes all ids in a
	// single round-trip and is a no-op for a pre-warm agent (LoadedModels nil → the
	// array is empty). Staleness is read against last_seen_warm (a worker that stops
	// reporting a model ages out), so we never need to delete stale rows here. A
	// failed upsert is a real failure (BLACKHOLE), not silently swallowed.
	if len(r.LoadedModels) > 0 {
		if _, err := tx.Exec(ctx,
			`INSERT INTO worker_model_state (worker_id, model_id, last_seen_warm)
			 SELECT $1, m, now() FROM unnest($2::text[]) AS m
			 ON CONFLICT (worker_id, model_id)
			 DO UPDATE SET last_seen_warm = now()`,
			workerID, r.LoadedModels); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// MedianEffectiveMemoryGB returns the median effective memory (GB) across the most
// recent memory sample of each LIVE eligible worker for (jobType, modelRef): one
// row per worker (its latest sample), filtered to the same supported-job/model +
// active-supplier + not-throttled + seen-<60s predicate the claim uses, so the
// median reflects supply a job could actually land on. ok is false when no eligible
// worker has reported a sample yet (the caller then leaves quote risk unchanged
// rather than inventing a floor). Plane D D4 quote-risk feedback.
func (s *Store) MedianEffectiveMemoryGB(ctx context.Context, jobType, modelRef string) (median float32, ok bool, err error) {
	var med *float64
	err = s.pool.QueryRow(ctx,
		`WITH latest AS (
		     SELECT DISTINCT ON (m.worker_id) m.worker_id, m.effective_gb
		       FROM worker_memory_samples m
		       JOIN workers w   ON w.id = m.worker_id
		       JOIN suppliers s ON s.id = w.supplier_id
		      WHERE m.effective_gb IS NOT NULL
		        AND w.last_seen_at IS NOT NULL
		        AND w.last_seen_at > now() - interval '60 seconds'
		        AND s.status = 'active'
		        AND NOT COALESCE(w.throttled, false)
		        AND COALESCE(w.supported_jobs,'{}') @> ARRAY[$1]
		        AND ($2 = '' OR COALESCE(w.supported_models,'{}') @> ARRAY[$2])
		      ORDER BY m.worker_id, m.created_at DESC
		 )
		 SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY effective_gb) FROM latest`,
		jobType, modelRef,
	).Scan(&med)
	if err != nil {
		return 0, false, err
	}
	if med == nil {
		return 0, false, nil // no eligible worker has a sample yet
	}
	return float32(*med), true, nil
}

// AdminWorker is one row of GET /admin/workers. EffectiveMemoryGB + Throttled
// are the live throttling state (operator visibility): effective is the
// allocatable pool after the supplier's headroom (falls back to total memory
// before the first heartbeat), and Throttled is the worker pausing for memory
// pressure — the same signals the safe-dispatch claim filter enforces.
type AdminWorker struct {
	ID                uuid.UUID  `json:"id"`
	SupplierID        uuid.UUID  `json:"supplier_id"`
	HWClass           string     `json:"hw_class"`
	MemoryGB          float32    `json:"memory_gb"`
	EffectiveMemoryGB float32    `json:"effective_memory_gb"`
	Throttled         bool       `json:"throttled"`
	AvgAvailableGB    float32    `json:"avg_available_gb"` // mean available_gb over the last memSampleWindow samples (0 = no samples yet)
	MemorySamples     int        `json:"memory_samples"`   // how many recent samples backed AvgAvailableGB (operator can judge the average's weight)
	LastSeenAt        *time.Time `json:"last_seen_at"`
	Version           string     `json:"version"`
	Reputation        float32    `json:"reputation"`
	Tier              int16      `json:"tier"`
	Status            string     `json:"status"`
}

// memSampleWindow is how many of a worker's most-recent memory samples the admin
// rolling average + capacity view summarise. Small so the figure tracks RECENT
// pressure (a worker that just freed memory is not penalised for old beats).
const memSampleWindow = 20

// ListWorkers returns the worker fleet joined to supplier reputation/status, each
// annotated with its recent memory-pressure average (avg_available_gb over the last
// memSampleWindow worker_memory_samples — Plane D D4). The LATERAL subquery is a
// per-worker recent-N average; workers without samples yet report 0/0, never a faked
// number. The index (worker_id, created_at DESC) serves the ordered limit.
func (s *Store) ListWorkers(ctx context.Context) ([]AdminWorker, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT w.id, w.supplier_id, w.hw_class, w.memory_gb,
		        COALESCE(w.effective_memory_gb, w.memory_gb, 0),
		        COALESCE(w.throttled, false),
		        COALESCE(s2.avg_available_gb, 0), COALESCE(s2.n, 0),
		        w.last_seen_at,
		        COALESCE(w.version,''), s.reputation, s.tier, s.status
		 FROM workers w JOIN suppliers s ON s.id = w.supplier_id
		 LEFT JOIN LATERAL (
		     SELECT avg(recent.available_gb)::real AS avg_available_gb, count(*) AS n
		       FROM (SELECT available_gb FROM worker_memory_samples
		              WHERE worker_id = w.id AND available_gb IS NOT NULL
		              ORDER BY created_at DESC LIMIT $1) recent
		 ) s2 ON true
		 ORDER BY w.last_seen_at DESC NULLS LAST`,
		memSampleWindow)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AdminWorker
	for rows.Next() {
		var a AdminWorker
		if err := rows.Scan(&a.ID, &a.SupplierID, &a.HWClass, &a.MemoryGB,
			&a.EffectiveMemoryGB, &a.Throttled, &a.AvgAvailableGB, &a.MemorySamples,
			&a.LastSeenAt, &a.Version,
			&a.Reputation, &a.Tier, &a.Status); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// AdminJob is one row of the admin jobs view (across ALL buyers).
type AdminJob struct {
	ID           uuid.UUID `json:"id"`
	BuyerID      uuid.UUID `json:"buyer_id"`
	Status       string    `json:"status"`
	JobType      string    `json:"job_type"`
	ModelRef     string    `json:"model_ref"`
	Tier         string    `json:"tier"`
	TaskCount    int       `json:"task_count"`
	TasksDone    int       `json:"tasks_done"`
	EstimatedUSD float64   `json:"estimated_usd"`
	ActualUSD    float64   `json:"actual_usd"`
	CreatedAt    time.Time `json:"created_at"`
}

// ListJobsAdmin returns the most recent jobs across all buyers for the admin panel
// (newest first, capped at 200). Unlike GET /v1/jobs/{id} this is NOT buyer-scoped
// — it is gated by authAdmin.
func (s *Store) ListJobsAdmin(ctx context.Context) ([]AdminJob, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, buyer_id, status, job_type, COALESCE(model_ref,''), tier,
		        COALESCE(task_count,0), COALESCE(tasks_done,0),
		        COALESCE(estimated_usd,0), COALESCE(actual_usd,0), created_at
		 FROM jobs ORDER BY created_at DESC LIMIT 200`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AdminJob
	for rows.Next() {
		var a AdminJob
		if err := rows.Scan(&a.ID, &a.BuyerID, &a.Status, &a.JobType, &a.ModelRef,
			&a.Tier, &a.TaskCount, &a.TasksDone, &a.EstimatedUSD, &a.ActualUSD, &a.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// AdminPayout is one per-supplier, per-status rollup of supplier credits.
type AdminPayout struct {
	SupplierID   uuid.UUID `json:"supplier_id"`
	PayoutStatus string    `json:"payout_status"`
	Count        int       `json:"count"`
	AmountUSD    float64   `json:"amount_usd"`
}

// ListPayoutsAdmin rolls up supplier_credit ledger entries by (supplier, payout
// status) so the admin panel can see who is owed what and in which state
// (pending / held / released / clawed_back) — the payout review surface.
func (s *Store) ListPayoutsAdmin(ctx context.Context) ([]AdminPayout, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT COALESCE(supplier_id,'00000000-0000-0000-0000-000000000000'::uuid),
		        payout_status, COUNT(*), COALESCE(SUM(amount_usd),0)
		 FROM ledger_entries
		 WHERE kind = 'supplier_credit'
		 GROUP BY supplier_id, payout_status
		 ORDER BY supplier_id, payout_status`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AdminPayout
	for rows.Next() {
		var a AdminPayout
		if err := rows.Scan(&a.SupplierID, &a.PayoutStatus, &a.Count, &a.AmountUSD); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// SuspendWorker flags a worker's supplier as suspended (manual admin action).
func (s *Store) SuspendWorker(ctx context.Context, workerID uuid.UUID) error {
	ct, err := s.pool.Exec(ctx,
		`UPDATE suppliers SET status = 'suspended'
		 WHERE id = (SELECT supplier_id FROM workers WHERE id = $1)`,
		workerID)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return errNotFound
	}
	return nil
}

// --- jobs + tasks ---

// CreateJobWithTasks inserts the job row and its task rows in one transaction.
// Each task starts queued and immediately claimable (visible_at defaults to
// now() in the schema). Returns the job id.
func (s *Store) CreateJobWithTasks(ctx context.Context, j *jobRow, tasks []taskRow) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// max_usd is NULL when no cap was set (0), so the claim's budget gate can use
	// `j.max_usd IS NOT NULL` to cleanly tell "no cap" from a real $0 cap.
	_, err = tx.Exec(ctx,
		`INSERT INTO jobs
		   (id, buyer_id, status, job_type, model_ref, input_ref, output_ref,
		    tier, verification_policy, estimated_usd, actual_usd, task_count, tasks_done,
		    min_memory_gb, hw_classes, data_residency, job_type_spec, split_size,
		    offered_rate_usd_hr, eta_secs, max_usd, budget_state, quote_id, min_reputation, private_pool)
		 VALUES ($1,$2,'queued',$3,$4,$5,$6,$7,$8,$9,0,$10,0,
		         $11,$12,$13,$14,$15,$16,$17,$18,'tracking',$19,$20,$21)`,
		j.ID, j.BuyerID, j.JobType, j.ModelRef, j.InputRef, j.OutputRef,
		j.Tier, j.VerificationPolicy, j.EstimatedUSD, j.TaskCount,
		j.MinMemoryGB, nullStrSlice(j.HWClasses), nullStrSlice(j.DataResidency),
		nullJSON(j.JobTypeSpec), j.SplitSize, j.OfferedRateUsdHr, j.ETASecs,
		nullPosFloat(j.MaxUSD), nullUUID(j.QuoteID), j.MinReputation, j.PrivatePool,
	)
	if err != nil {
		return err
	}

	for _, t := range tasks {
		_, err = tx.Exec(ctx,
			`INSERT INTO tasks
			   (id, job_id, status, is_honeypot, is_redundancy, retry_count,
			    input_ref, result_key, chunk_index, visible_at)
			 VALUES ($1,$2,'queued',$3,$4,0,$5,$6,$7, now())`,
			t.ID, t.JobID, t.IsHoneypot, t.IsRedundancy, t.InputRef, t.ResultKey, t.ChunkIndex,
		)
		if err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// jobRow mirrors the jobs table columns we write. The Turbo fields (MinMemoryGB,
// HWClasses, DataResidency, JobTypeSpec, SplitSize, OfferedRateUsdHr, ETASecs)
// lift per-job constraints out of the manifest JSON into queryable columns so the
// SKIP-LOCKED claim can hard-filter on them.
type jobRow struct {
	ID                 uuid.UUID
	BuyerID            uuid.UUID
	JobType            string
	ModelRef           string
	InputRef           string
	OutputRef          string
	Tier               string
	VerificationPolicy []byte // jsonb
	EstimatedUSD       float64
	TaskCount          int
	MinMemoryGB        float32
	HWClasses          []string // nil = any class
	DataResidency      []string // nil = unrestricted
	JobTypeSpec        []byte   // jsonb: the full submitted JobType (tag + fields)
	SplitSize          int
	OfferedRateUsdHr   float32
	ETASecs            int
	MaxUSD             float64   // buyer hard spend cap (Budget Governor); 0 = no cap
	QuoteID            uuid.UUID // advisory quote bound to this job (Plane D D7); zero = none → persisted NULL
	MinReputation      float32   // Elite-supplier gate: claim only by suppliers with reputation >= this (0 = any)
	PrivatePool        bool      // Private Deployment: route ONLY to the buyer's bound suppliers (private_pool_members)
}

// taskRow mirrors the tasks columns we write at creation. Each task is one chunk
// of the job's split input, so it carries its own object keys: input_ref is the
// chunk's input.jsonl key, result_key is where the worker writes its result.json.
// ChunkIndex is the 0-based input position used to merge results back in order;
// redundancy/honeypot clones reuse their primary's chunk_index.
type taskRow struct {
	ID           uuid.UUID
	JobID        uuid.UUID
	IsHoneypot   bool
	IsRedundancy bool
	InputRef     string
	ResultKey    string
	ChunkIndex   int
}

// JobView is the GET /v1/jobs/{id} projection. MaxUSD/BudgetState expose the
// Budget Governor (Plane C §12 / Plane D §14 D8): MaxUSD is the buyer's hard cap
// (0 when unset) and BudgetState is the governor state machine.
type JobView struct {
	ID           uuid.UUID
	BuyerID      uuid.UUID
	Status       string
	JobType      string
	Tier         string
	OutputRef    string
	TaskCount    int
	TasksDone    int
	EstimatedUSD float64
	ActualUSD    float64
	ETASecs      int
	CreatedAt    time.Time
	MaxUSD       float64
	BudgetState  string
}

// GetJob loads a job scoped to a buyer (buyers see only their own jobs).
func (s *Store) GetJob(ctx context.Context, jobID, buyerID uuid.UUID) (*JobView, error) {
	var j JobView
	err := s.pool.QueryRow(ctx,
		`SELECT id, buyer_id, status, job_type, tier, COALESCE(output_ref,''),
		        COALESCE(task_count,0), COALESCE(tasks_done,0),
		        COALESCE(estimated_usd,0), COALESCE(actual_usd,0),
		        COALESCE(eta_secs,0), created_at,
		        COALESCE(max_usd,0), COALESCE(budget_state,'tracking')
		 FROM jobs WHERE id = $1 AND buyer_id = $2`,
		jobID, buyerID,
	).Scan(&j.ID, &j.BuyerID, &j.Status, &j.JobType, &j.Tier, &j.OutputRef,
		&j.TaskCount, &j.TasksDone, &j.EstimatedUSD, &j.ActualUSD, &j.ETASecs, &j.CreatedAt,
		&j.MaxUSD, &j.BudgetState)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	return &j, nil
}

// QueuedTaskCount is the number of claimable (queued/retrying, visible-now,
// unclaimed) tasks across all non-terminal jobs — the backlog the ETA estimate
// and the /metrics queue-depth gauge read.
func (s *Store) QueuedTaskCount(ctx context.Context) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM tasks t JOIN jobs j ON j.id = t.job_id
		 WHERE t.status IN ('queued','retrying')
		   AND t.claimed_by IS NULL
		   AND COALESCE(t.visible_at, t.created_at) <= now()
		   AND j.status NOT IN ('cancelled','failed','complete')`,
	).Scan(&n)
	return n, err
}

// jobInternal carries the fields payout scheduling needs (not buyer-scoped).
type jobInternal struct {
	BuyerID            uuid.UUID
	TaskCount          int
	EstimatedUSD       float64
	VerificationPolicy []byte
}

// getJobInternal loads the payout-relevant job fields by id (no buyer scope;
// called from the worker commit path).
func (s *Store) getJobInternal(ctx context.Context, jobID uuid.UUID) (*jobInternal, error) {
	var j jobInternal
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id, COALESCE(task_count,0), COALESCE(estimated_usd,0),
		        COALESCE(verification_policy,'{}'::jsonb)
		 FROM jobs WHERE id = $1`,
		jobID,
	).Scan(&j.BuyerID, &j.TaskCount, &j.EstimatedUSD, &j.VerificationPolicy)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	return &j, nil
}

// CancelJob cancels a job only if it has not started (still queued). Returns
// errNotFound if no such cancellable job exists for the buyer.
func (s *Store) CancelJob(ctx context.Context, jobID, buyerID uuid.UUID) error {
	ct, err := s.pool.Exec(ctx,
		`UPDATE jobs SET status = 'cancelled'
		 WHERE id = $1 AND buyer_id = $2 AND status = 'queued'`,
		jobID, buyerID)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return errNotFound
	}
	// Also drop the still-queued tasks so they cannot be claimed.
	_, err = s.pool.Exec(ctx,
		`UPDATE tasks SET status = 'failed'
		 WHERE job_id = $1 AND status = 'queued'`, jobID)
	return err
}

// StartTask marks a claimed task running. Scoped to the worker that owns the
// claim so a worker cannot start another worker's task.
func (s *Store) StartTask(ctx context.Context, taskID, workerID uuid.UUID) error {
	ct, err := s.pool.Exec(ctx,
		`UPDATE tasks SET status = 'running', started_at = now()
		 WHERE id = $1 AND claimed_by = $2 AND status IN ('queued','running')`,
		taskID, workerID)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		return errNotFound
	}
	// Reflect first task start on the parent job.
	_, err = s.pool.Exec(ctx,
		`UPDATE jobs SET status = 'running'
		 WHERE id = (SELECT job_id FROM tasks WHERE id = $1) AND status = 'queued'`,
		taskID)
	return err
}

// CommitTaskInfo is what CommitTask returns so verification can run.
type CommitTaskInfo struct {
	TaskID       uuid.UUID
	JobID        uuid.UUID
	WorkerID     uuid.UUID
	SupplierID   uuid.UUID
	IsHoneypot   bool
	IsRedundancy bool
	HWClass      string
	jobType      string // parent job's job_type, for honeypot answer lookup
	InputRef     string // this task's input chunk key (honeypot answer lookup)
	ResultKey    string // canonical server-side result key (verification fetch)
	ModelRef     string // parent job's model_ref (tiebreak peer selection)
	MinMemoryGB  float32
	ChunkIndex   int // this task's chunk position (tiebreak pairing + N-way vote)
}

// CommitTask stores the result ref and flips the task to complete. Returns the
// context verification needs. Scoped to the claiming worker. The stored
// result_ref is the canonical server-side result_key (the key the control plane
// presigned at dispatch), NOT whatever the worker echoes in the commit body — the
// worker's TaskCommit.result_key is a presigned URL in V1, so trusting it would
// store a URL where a key belongs. We trust the path + our own dispatch record.
func (s *Store) CommitTask(ctx context.Context, taskID, workerID uuid.UUID, c TaskCommit) (*CommitTaskInfo, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	ct, err := tx.Exec(ctx,
		`UPDATE tasks
		   SET status = 'complete', completed_at = now(),
		       result_ref = COALESCE(NULLIF(result_key,''), $3),
		       worker_id = $2
		 WHERE id = $1 AND claimed_by = $2 AND status IN ('running','queued')`,
		taskID, workerID, c.ResultKey)
	if err != nil {
		return nil, err
	}
	if ct.RowsAffected() == 0 {
		return nil, errNotFound
	}

	var info CommitTaskInfo
	info.TaskID = taskID
	info.WorkerID = workerID
	var splitSize int
	err = tx.QueryRow(ctx,
		`SELECT t.job_id, t.is_honeypot, t.is_redundancy,
		        COALESCE(t.input_ref,''),
		        COALESCE(NULLIF(t.result_key,''), $3),
		        w.supplier_id, COALESCE(w.hw_class,''), j.job_type,
		        COALESCE(j.model_ref,''), COALESCE(j.min_memory_gb,0),
		        COALESCE(t.chunk_index,0), COALESCE(j.split_size,0)
		 FROM tasks t
		   JOIN workers w ON w.id = $2
		   JOIN jobs j ON j.id = t.job_id
		 WHERE t.id = $1`,
		taskID, workerID, c.ResultKey,
	).Scan(&info.JobID, &info.IsHoneypot, &info.IsRedundancy, &info.InputRef,
		&info.ResultKey, &info.SupplierID, &info.HWClass, &info.jobType,
		&info.ModelRef, &info.MinMemoryGB, &info.ChunkIndex, &splitSize)
	if err != nil {
		return nil, err
	}

	// Increment the job's done counter.
	_, err = tx.Exec(ctx,
		`UPDATE jobs SET tasks_done = tasks_done + 1 WHERE id = $1`, info.JobID)
	if err != nil {
		return nil, err
	}

	// Quote-to-actual drift feedback (Plane D D6 / errata C-Errata-6): record the
	// REAL committed-task wall-time so the Exchange Brain can learn an observed p90
	// the next quote's ETA leans on. This lives INSIDE the commit transaction so a
	// duration is recorded if and only if the task truly committed — a failed or
	// malformed task takes the fail path (failJobAndRefundOnce), never this one, so
	// it can never poison the estimate. duration_ms is the worker's reported value;
	// we store it verbatim (real telemetry, never fabricated).
	_, err = tx.Exec(ctx,
		`INSERT INTO task_durations (job_id, job_type, model_ref, split_size, duration_ms)
		 VALUES ($1,$2,$3,$4,$5)`,
		info.JobID, info.jobType, info.ModelRef, splitSize, int64(c.DurationMS))
	if err != nil {
		return nil, err
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &info, nil
}

// driftMinSamples is how many committed durations a (job_type, model_ref) needs
// before an observed p90 is trusted enough to replace the static throughput target
// in the quote's ETA. Below it the history is too thin to be representative, and
// the estimate cleanly falls back to the target (HistoricalP90DurationMs reports 0).
const driftMinSamples = 5

// HistoricalP90DurationMs returns the observed 90th-percentile committed-task
// duration (ms) for a (job_type, model_ref), and how many samples backed it. It
// uses percentile_disc(0.9) (a real recorded value, not an interpolation) over
// task_durations. When fewer than driftMinSamples rows exist the history is too
// thin to trust, so it returns (0, n) and the caller falls back to the static
// target — the quote NEVER invents an ETA from one lucky sample. A model_ref of ""
// matches rows regardless of model (job-type-only history).
func (s *Store) HistoricalP90DurationMs(ctx context.Context, jobType, modelRef string) (p90ms int64, samples int, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COUNT(*),
		        COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY duration_ms), 0)
		   FROM task_durations
		  WHERE job_type = $1
		    AND ($2 = '' OR model_ref = $2)`,
		jobType, modelRef,
	).Scan(&samples, &p90ms)
	if err != nil {
		return 0, 0, err
	}
	if samples < driftMinSamples {
		return 0, samples, nil // too thin — caller falls back to the static target
	}
	return p90ms, samples, nil
}

// DriftRow is one per-(job_type, model) quoted-vs-actual rollup for GET /admin/drift
// (Plane D D6). Actuals come from recorded committed durations; AvgQuotedETASecs is
// the average of jobs.eta_secs (the quoted ETA, REUSED — not a duplicate column) over
// the jobs that produced those durations, so an operator can see how the static quote
// compares to reality and whether the observed p90 is now driving the estimate.
type DriftRow struct {
	JobType          string  `json:"job_type"`
	ModelRef         string  `json:"model_ref"`
	Samples          int     `json:"samples"`             // committed-task durations recorded
	AvgDurationMs    float64 `json:"avg_duration_ms"`     // mean actual per-task wall-time
	P90DurationMs    int64   `json:"p90_duration_ms"`     // observed p90 (what the ETA learns from)
	AvgQuotedETASecs float64 `json:"avg_quoted_eta_secs"` // mean quoted whole-job eta_secs (reused jobs.eta_secs)
	UsingObservedP90 bool    `json:"using_observed_p90"`  // true once samples >= the trust floor
}

// DriftRollup returns the quoted-vs-actual rollup per (job_type, model_ref) for the
// admin drift surface. Actuals (count, avg, p90) come from task_durations; the quoted
// side is the average jobs.eta_secs over the jobs that produced those durations
// (LEFT JOIN so a duration whose job row was pruned still counts its actual side
// honestly rather than vanishing). Ordered by sample volume so the thickest history —
// the rows whose observed p90 is actually steering quotes — sorts first.
func (s *Store) DriftRollup(ctx context.Context) ([]DriftRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT td.job_type,
		        COALESCE(td.model_ref,''),
		        COUNT(*),
		        COALESCE(AVG(td.duration_ms),0),
		        COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY td.duration_ms),0),
		        COALESCE(AVG(j.eta_secs),0)
		   FROM task_durations td
		   LEFT JOIN jobs j ON j.id = td.job_id
		  GROUP BY td.job_type, td.model_ref
		  ORDER BY COUNT(*) DESC, td.job_type, td.model_ref`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DriftRow
	for rows.Next() {
		var d DriftRow
		if err := rows.Scan(&d.JobType, &d.ModelRef, &d.Samples,
			&d.AvgDurationMs, &d.P90DurationMs, &d.AvgQuotedETASecs); err != nil {
			return nil, err
		}
		d.UsingObservedP90 = d.Samples >= driftMinSamples
		out = append(out, d)
	}
	return out, rows.Err()
}

// JobAllTasksDone reports whether every task of a job has finished
// (complete/failed) with at least one complete, i.e. the job is ready to finalize.
// Used by the commit path to merge-then-mark synchronously on the last commit.
func (s *Store) JobAllTasksDone(ctx context.Context, jobID uuid.UUID) (bool, error) {
	var done bool
	err := s.pool.QueryRow(ctx,
		`SELECT j.task_count > 0
		        AND NOT EXISTS (
		          SELECT 1 FROM tasks t
		          WHERE t.job_id = j.id AND t.status NOT IN ('complete','failed')
		        )
		        AND EXISTS (SELECT 1 FROM tasks t WHERE t.job_id = j.id AND t.status = 'complete')
		 FROM jobs j WHERE j.id = $1`,
		jobID,
	).Scan(&done)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, errNotFound
	}
	return done, err
}

// quarantineRepFloor is the reputation below which a supplier is auto-quarantined
// (suspended) so the claim's s.status='active' gate stops handing it work. Above
// the instant-ban threshold (0.0); a quarantined supplier can be reinstated.
const quarantineRepFloor = 0.2

// QuarantineSupplier suspends a supplier and stamps quarantined_at = now(),
// unconditionally (the honeypot-fail path quarantines on a single confirmed bad
// known-answer result, independent of the resulting reputation). A no-op on an
// already-banned supplier (don't downgrade a ban to a suspension). Idempotent for
// an already-suspended one (quarantined_at is only set if not already set).
func (s *Store) QuarantineSupplier(ctx context.Context, supplierID uuid.UUID) error {
	ct, err := s.pool.Exec(ctx,
		`UPDATE suppliers
		   SET status = 'suspended',
		       quarantined_at = COALESCE(quarantined_at, now())
		 WHERE id = $1 AND status <> 'banned'`,
		supplierID)
	if err != nil {
		return err
	}
	if ct.RowsAffected() > 0 {
		metrics.quarantines.Add(1)
	}
	return nil
}

// DockReputation applies a reputation event to a supplier, computing the new
// score with the pure updateReputation (the single source of the delta + clamp
// rules) in a read-modify-write transaction. A score that collapses to 0.0
// (e.g. a spoofing event, delta -1.0) also flips the supplier to banned, since
// that is the action plan's instant-ban threshold; a score below
// quarantineRepFloor auto-suspends (quarantines) the supplier. Used by
// verification on honeypot/redundancy outcomes.
func (s *Store) DockReputation(ctx context.Context, supplierID uuid.UUID, event ReputationEvent) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var current float32
	err = tx.QueryRow(ctx,
		`SELECT reputation FROM suppliers WHERE id = $1 FOR UPDATE`, supplierID,
	).Scan(&current)
	if errors.Is(err, pgx.ErrNoRows) {
		return errNotFound
	}
	if err != nil {
		return err
	}

	next := updateReputation(current, event)
	switch {
	case next <= 0.0:
		// Instant-ban threshold reached.
		_, err = tx.Exec(ctx,
			`UPDATE suppliers SET reputation = 0.0, status = 'banned' WHERE id = $1`,
			supplierID)
	case next < quarantineRepFloor:
		// Auto-quarantine: reputation collapsed below the trust floor. Suspend the
		// supplier and stamp quarantined_at so the claim's s.status='active' gate
		// excludes it (it can be reinstated by an admin). Only flip a still-active
		// supplier (don't clobber an existing ban).
		_, err = tx.Exec(ctx,
			`UPDATE suppliers
			   SET reputation = $2,
			       status = CASE WHEN status = 'active' THEN 'suspended' ELSE status END,
			       quarantined_at = CASE WHEN status = 'active' THEN now() ELSE quarantined_at END
			 WHERE id = $1`,
			supplierID, next)
	default:
		_, err = tx.Exec(ctx,
			`UPDATE suppliers SET reputation = $2 WHERE id = $1`,
			supplierID, next)
	}
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// RequeueTask resets a failed-verification task to retrying and bumps the retry
// counter so the scheduler hands it to a different worker.
func (s *Store) RequeueTask(ctx context.Context, taskID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE tasks
		   SET status = 'retrying', claimed_by = NULL, claimed_at = NULL,
		       worker_id = NULL, retry_count = retry_count + 1,
		       visible_at = now()
		 WHERE id = $1`,
		taskID)
	return err
}

// --- honeypots ---

// AvailableHoneypots returns up to limit honeypot input_refs for a job type, to
// inject as known-answer tasks at job submission. Fewer than limit (or none) is
// fine — honeypot coverage is best-effort, never fabricated.
func (s *Store) AvailableHoneypots(ctx context.Context, jobType string, limit int) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT input_ref FROM honeypots WHERE job_type = $1 ORDER BY created_at ASC LIMIT $2`,
		jobType, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var ref string
		if err := rows.Scan(&ref); err != nil {
			return nil, err
		}
		out = append(out, ref)
	}
	return out, rows.Err()
}

// GetHoneypotAnswer returns the base64-able known answer bytes for a honeypot
// input ref, if one exists for this job type.
func (s *Store) GetHoneypotAnswer(ctx context.Context, jobType, inputRef string) ([]byte, error) {
	var ans []byte
	err := s.pool.QueryRow(ctx,
		`SELECT known_answer FROM honeypots
		 WHERE job_type = $1 AND input_ref = $2 LIMIT 1`,
		jobType, inputRef,
	).Scan(&ans)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	return ans, err
}

// PeerResultKey finds a completed sibling task that ran the SAME input chunk for
// the same job as the given task but on a DIFFERENT worker, returning its result
// key for a within-class redundancy comparison. The pairing is by shared
// input_ref (primary + its redundancy clone carry the same chunk). Returns
// errNotFound when no committed peer exists yet (the common case — the peer may
// not have finished, so verification simply has nothing to compare against).
func (s *Store) PeerResultKey(ctx context.Context, taskID uuid.UUID) (peerResult string, peerSupplier uuid.UUID, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(p.result_ref,''), w.supplier_id
		 FROM tasks t
		   JOIN tasks p ON p.job_id = t.job_id AND p.input_ref = t.input_ref
		                AND p.id <> t.id AND p.status = 'complete'
		                AND p.result_ref IS NOT NULL AND p.result_ref <> ''
		   JOIN workers w ON w.id = p.worker_id
		 WHERE t.id = $1
		 ORDER BY p.completed_at ASC
		 LIMIT 1`,
		taskID,
	).Scan(&peerResult, &peerSupplier)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", uuid.Nil, errNotFound
	}
	return peerResult, peerSupplier, err
}

// ChunkResult is one committed result for a chunk: its result key plus the
// worker + supplier that produced it (so a majority vote can credit the winner
// and dock the losers by supplier).
type ChunkResult struct {
	TaskID     uuid.UUID
	WorkerID   uuid.UUID
	SupplierID uuid.UUID
	ResultRef  string
}

// ChunkResults returns every committed result for a job's chunk (the primary and
// all its redundancy/tiebreak clones share input_ref + chunk_index), each with
// its worker + supplier. The 3-way tiebreak vote (Verification V2) gathers these
// once a tiebreak commits and does a real majorityVote over them.
func (s *Store) ChunkResults(ctx context.Context, jobID uuid.UUID, chunkIndex int) ([]ChunkResult, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.worker_id, w.supplier_id, t.result_ref
		 FROM tasks t JOIN workers w ON w.id = t.worker_id
		 WHERE t.job_id = $1 AND COALESCE(t.chunk_index,0) = $2
		   AND t.status = 'complete' AND t.is_honeypot = false
		   AND t.result_ref IS NOT NULL AND t.result_ref <> ''
		 ORDER BY t.completed_at ASC NULLS LAST, t.id ASC`,
		jobID, chunkIndex)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ChunkResult
	for rows.Next() {
		var cr ChunkResult
		if err := rows.Scan(&cr.TaskID, &cr.WorkerID, &cr.SupplierID, &cr.ResultRef); err != nil {
			return nil, err
		}
		out = append(out, cr)
	}
	return out, rows.Err()
}

// TiebreakExists reports whether a tiebreak (a redundancy task carrying
// hedged_from) already exists for a chunk, so a mismatch never spawns more than
// one third opinion.
func (s *Store) TiebreakExists(ctx context.Context, jobID uuid.UUID, chunkIndex int) (bool, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM tasks
		 WHERE job_id = $1 AND COALESCE(chunk_index,0) = $2
		   AND is_redundancy = true AND hedged_from IS NOT NULL`,
		jobID, chunkIndex).Scan(&n)
	return n > 0, err
}

// InsertTiebreakTask inserts a third-opinion redundancy task for a chunk, pinned
// (pre-claimed, not yet started) to a chosen same-class peer so only that worker
// runs it, carrying hedged_from = the primary task and the chunk's input_ref +
// chunk_index. It also bumps the parent job's task_count so the completion sweep
// waits for this extra opinion before finalizing. Returns the new task id.
func (s *Store) InsertTiebreakTask(ctx context.Context, jobID, primaryTaskID, peerWorker uuid.UUID, inputRef string, chunkIndex int) (uuid.UUID, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return uuid.Nil, err
	}
	defer tx.Rollback(ctx)

	id := uuid.New()
	resultKey := fmt.Sprintf("jobs/%s/tiebreak/%s/result.json", jobID, id)
	if _, err := tx.Exec(ctx,
		`INSERT INTO tasks
		   (id, job_id, status, is_honeypot, is_redundancy, retry_count,
		    input_ref, result_key, chunk_index, hedged_from,
		    claimed_by, claimed_at, visible_at)
		 VALUES ($1,$2,'queued',false,true,0,$3,$4,$5,$6,$7, now(), now())`,
		id, jobID, inputRef, resultKey, chunkIndex, primaryTaskID, peerWorker,
	); err != nil {
		return uuid.Nil, err
	}
	// One more opinion to wait for before the job is "all tasks done".
	if _, err := tx.Exec(ctx,
		`UPDATE jobs SET task_count = task_count + 1 WHERE id = $1`, jobID); err != nil {
		return uuid.Nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return uuid.Nil, err
	}
	return id, nil
}

// --- ledger ---

// InsertLedgerEntries writes a batch of ledger rows in one transaction.
func (s *Store) InsertLedgerEntries(ctx context.Context, entries []LedgerEntry) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	for _, e := range entries {
		_, err = tx.Exec(ctx,
			`INSERT INTO ledger_entries
			   (kind, supplier_id, buyer_id, task_id, amount_usd, payout_status, release_at)
			 VALUES ($1,$2,$3,$4,$5,$6,$7)
			 ON CONFLICT (task_id, kind) DO NOTHING`,
			e.Kind, e.SupplierID, e.BuyerID, e.TaskID, e.AmountUSD, e.PayoutStatus, e.ReleaseAt)
		if err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// ClawbackTaskCredit reverses the supplier credit already written for a task on
// confirmed fraud. It sums the supplier's prior credit for that task and writes
// a matching negative clawback row via clawbackEntry, then marks the original
// credit rows clawed_back so they no longer count toward the balance. No-op (no
// error) if there was no prior credit.
func (s *Store) ClawbackTaskCredit(ctx context.Context, supplierID, taskID uuid.UUID) error {
	var credited float64
	err := s.pool.QueryRow(ctx,
		`SELECT COALESCE(SUM(amount_usd),0) FROM ledger_entries
		 WHERE supplier_id = $1 AND task_id = $2 AND kind = 'supplier_credit'
		   AND amount_usd > 0`,
		supplierID, taskID,
	).Scan(&credited)
	if err != nil {
		return err
	}
	if credited <= 0 {
		return nil // nothing to claw back
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	cb := clawbackEntry(supplierID, taskID, credited)
	if _, err := tx.Exec(ctx,
		`INSERT INTO ledger_entries
		   (kind, supplier_id, task_id, amount_usd, payout_status)
		 VALUES ($1,$2,$3,$4,$5)
		 ON CONFLICT (task_id, kind) DO NOTHING`,
		cb.Kind, cb.SupplierID, cb.TaskID, cb.AmountUSD, cb.PayoutStatus,
	); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE ledger_entries SET payout_status = 'clawed_back'
		 WHERE supplier_id = $1 AND task_id = $2 AND kind = 'supplier_credit'`,
		supplierID, taskID,
	); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// WorkerEarnings sums a supplier's released balance and lifetime credits for
// GET /v1/worker/earnings. Balance = released-but-not-clawed credits; lifetime
// = all positive supplier credits ever.
func (s *Store) WorkerEarnings(ctx context.Context, supplierID uuid.UUID) (Earnings, error) {
	var e Earnings
	err := s.pool.QueryRow(ctx,
		`SELECT
		   COALESCE(SUM(amount_usd) FILTER (
		     WHERE payout_status = 'released' AND amount_usd > 0), 0),
		   COALESCE(SUM(amount_usd) FILTER (WHERE amount_usd > 0), 0)
		 FROM ledger_entries
		 WHERE supplier_id = $1 AND kind = 'supplier_credit'`,
		supplierID,
	).Scan(&e.BalanceUSD, &e.LifetimeUSD)
	return e, err
}

// FraudFlag is one row of GET /admin/fraud-flags: suppliers below the trust
// floor or already suspended/banned.
type FraudFlag struct {
	SupplierID uuid.UUID `json:"supplier_id"`
	Reputation float32   `json:"reputation"`
	Tier       int16     `json:"tier"`
	Status     string    `json:"status"`
}

// ListFraudFlags returns suppliers whose reputation dropped below 0.5 or whose
// status is non-active — the set an admin should review.
func (s *Store) ListFraudFlags(ctx context.Context) ([]FraudFlag, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, reputation, tier, status FROM suppliers
		 WHERE reputation < 0.5 OR status IN ('suspended','banned')
		 ORDER BY reputation ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []FraudFlag
	for rows.Next() {
		var f FraudFlag
		if err := rows.Scan(&f.SupplierID, &f.Reputation, &f.Tier, &f.Status); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// FraudReport is one row of GET /admin/fraud: the full trust picture for a
// flagged supplier — reputation, tier, status, when (if) it was quarantined, and
// its confirmed-fraud signal (clawback count = reversed credits on bad results).
type FraudReport struct {
	SupplierID    uuid.UUID  `json:"supplier_id"`
	Reputation    float32    `json:"reputation"`
	Tier          int16      `json:"tier"`
	Status        string     `json:"status"`
	QuarantinedAt *time.Time `json:"quarantined_at"`
	Clawbacks     int        `json:"clawbacks"`      // confirmed-fraud clawback rows
	MismatchTasks int        `json:"mismatch_tasks"` // tasks this supplier's clawbacks span
}

// ListFraud returns the fraud report for every supplier that is quarantined,
// banned, below the trust floor, or has any clawback on record — the admin's
// review queue. Ordered worst-first (lowest reputation). The clawback/mismatch
// counts come from the ledger so they reflect real reversed credit, not a guess.
func (s *Store) ListFraud(ctx context.Context) ([]FraudReport, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT s.id, s.reputation, s.tier, s.status, s.quarantined_at,
		        COALESCE(cb.n,0), COALESCE(cb.tasks,0)
		 FROM suppliers s
		 LEFT JOIN (
		   SELECT supplier_id, count(*) AS n, count(DISTINCT task_id) AS tasks
		   FROM ledger_entries WHERE kind = 'clawback' GROUP BY supplier_id
		 ) cb ON cb.supplier_id = s.id
		 WHERE s.reputation < 0.5
		    OR s.status IN ('suspended','banned')
		    OR s.quarantined_at IS NOT NULL
		    OR COALESCE(cb.n,0) > 0
		 ORDER BY s.reputation ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []FraudReport
	for rows.Next() {
		var f FraudReport
		if err := rows.Scan(&f.SupplierID, &f.Reputation, &f.Tier, &f.Status,
			&f.QuarantinedAt, &f.Clawbacks, &f.MismatchTasks); err != nil {
			return nil, err
		}
		out = append(out, f)
	}
	return out, rows.Err()
}

// --- models catalogue (real pricing) ---

// ModelRow is one row of the models table: the real pricing catalogue.
type ModelRow struct {
	ID           string
	Family       string
	Quant        string
	Kind         string
	Dim          int
	JobType      string
	PricePer1K   float64
	PricePerUnit float64
	MinMemoryGB  float32
	HFRepo       string
}

// ListModels returns the full models catalogue ordered by price.
func (s *Store) ListModels(ctx context.Context) ([]ModelRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, COALESCE(family,''), COALESCE(quant,''), COALESCE(kind,''),
		        COALESCE(dim,0), COALESCE(job_type,''),
		        COALESCE(price_per_1k,0), COALESCE(price_per_unit,0),
		        COALESCE(min_memory_gb,0), COALESCE(hf_repo,'')
		 FROM models ORDER BY price_per_1k ASC, id ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ModelRow
	for rows.Next() {
		var m ModelRow
		if err := rows.Scan(&m.ID, &m.Family, &m.Quant, &m.Kind, &m.Dim, &m.JobType,
			&m.PricePer1K, &m.PricePerUnit, &m.MinMemoryGB, &m.HFRepo); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// GetModel loads one model by id. errNotFound when the id is unknown.
func (s *Store) GetModel(ctx context.Context, id string) (*ModelRow, error) {
	var m ModelRow
	err := s.pool.QueryRow(ctx,
		`SELECT id, COALESCE(family,''), COALESCE(quant,''), COALESCE(kind,''),
		        COALESCE(dim,0), COALESCE(job_type,''),
		        COALESCE(price_per_1k,0), COALESCE(price_per_unit,0),
		        COALESCE(min_memory_gb,0), COALESCE(hf_repo,'')
		 FROM models WHERE id = $1`, id,
	).Scan(&m.ID, &m.Family, &m.Quant, &m.Kind, &m.Dim, &m.JobType,
		&m.PricePer1K, &m.PricePerUnit, &m.MinMemoryGB, &m.HFRepo)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	return &m, nil
}

// --- webhooks ---

// InsertWebhook persists a completion-webhook registration for a job.
func (s *Store) InsertWebhook(ctx context.Context, buyerID uuid.UUID, jobID *uuid.UUID, url string) (uuid.UUID, error) {
	id := uuid.New()
	_, err := s.pool.Exec(ctx,
		`INSERT INTO webhooks (id, buyer_id, job_id, url) VALUES ($1,$2,$3,$4)`,
		id, buyerID, jobID, url)
	return id, err
}

// JobWebhooks returns the registered webhook URLs for a job (job-scoped plus the
// buyer's catch-all webhooks with a NULL job_id).
func (s *Store) JobWebhooks(ctx context.Context, jobID, buyerID uuid.UUID) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT url FROM webhooks
		 WHERE job_id = $1 OR (job_id IS NULL AND buyer_id = $2)`,
		jobID, buyerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var u string
		if err := rows.Scan(&u); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// PendingWebhook is one undelivered completion webhook for a complete job.
type PendingWebhook struct {
	ID     uuid.UUID
	JobID  uuid.UUID
	URL    string
	Status string
}

// PendingWebhooks returns webhooks whose job has completed but that have not yet
// been delivered (delivered_at IS NULL). Job-scoped webhooks only — a webhook
// without a job_id is a buyer catch-all with no single completion event to fire.
func (s *Store) PendingWebhooks(ctx context.Context, limit int) ([]PendingWebhook, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT wh.id, wh.job_id, wh.url, j.status
		 FROM webhooks wh JOIN jobs j ON j.id = wh.job_id
		 WHERE wh.delivered_at IS NULL
		   AND wh.job_id IS NOT NULL
		   AND j.status IN ('complete','failed')
		 ORDER BY j.created_at ASC LIMIT $1`,
		limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PendingWebhook
	for rows.Next() {
		var p PendingWebhook
		if err := rows.Scan(&p.ID, &p.JobID, &p.URL, &p.Status); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// MarkWebhookDelivered stamps delivered_at so the sweep does not re-fire it.
func (s *Store) MarkWebhookDelivered(ctx context.Context, id uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE webhooks SET delivered_at = now() WHERE id = $1`, id)
	return err
}

// --- background-worker support ---

// DueHeldEntry is a supplier credit whose hold has expired and is due for payout.
type DueHeldEntry struct {
	ID         uuid.UUID
	SupplierID uuid.UUID
	AmountUSD  float64
}

// DuePayouts returns held supplier credits with release_at <= now(): the set the
// payout-release loop should attempt to send.
func (s *Store) DuePayouts(ctx context.Context, limit int) ([]DueHeldEntry, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, supplier_id, amount_usd FROM ledger_entries
		 WHERE kind = 'supplier_credit' AND payout_status = 'held'
		   AND release_at IS NOT NULL AND release_at <= now()
		 ORDER BY release_at ASC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DueHeldEntry
	for rows.Next() {
		var e DueHeldEntry
		if err := rows.Scan(&e.ID, &e.SupplierID, &e.AmountUSD); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// MarkPayout sets a ledger entry's payout_status (and optional payout_ref). Used
// by the payout-release loop: 'released' with a ref on a real transfer, or
// 'ready' (no ref) when the rail is unconfigured and the transfer is deferred.
func (s *Store) MarkPayout(ctx context.Context, entryID uuid.UUID, status, ref string) error {
	if ref == "" {
		_, err := s.pool.Exec(ctx,
			`UPDATE ledger_entries SET payout_status = $2 WHERE id = $1`, entryID, status)
		return err
	}
	_, err := s.pool.Exec(ctx,
		`UPDATE ledger_entries SET payout_status = $2, payout_ref = $3 WHERE id = $1`,
		entryID, status, ref)
	return err
}

// StaleTask is a running task whose claim has outlived its timeout.
type StaleTask struct {
	ID         uuid.UUID
	JobID      uuid.UUID
	RetryCount int16
}

// StaleRunningTasks finds tasks stuck in 'running' whose claim is older than
// timeout — the worker claimed but never committed (crash, network loss). The
// timeout is a single grace window applied uniformly; per-job max_duration lives
// in the manifest, not a queryable column, so this is the honest queue-level
// reaper. Returns the set to requeue or fail.
func (s *Store) StaleRunningTasks(ctx context.Context, timeout time.Duration, limit int) ([]StaleTask, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, job_id, retry_count FROM tasks
		 WHERE status = 'running' AND claimed_at IS NOT NULL
		   AND claimed_at < now() - make_interval(secs => $1)
		 ORDER BY claimed_at ASC LIMIT $2`,
		timeout.Seconds(), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StaleTask
	for rows.Next() {
		var t StaleTask
		if err := rows.Scan(&t.ID, &t.JobID, &t.RetryCount); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// Straggler is a running task that has run long enough to warrant a hedge: a
// duplicate copy dispatched to a second worker so the buyer is not held hostage
// by one slow node. Carries the chunk identity the hedge clones.
type Straggler struct {
	TaskID     uuid.UUID
	JobID      uuid.UUID
	WorkerID   uuid.UUID
	JobType    string
	ModelRef   string
	InputRef   string
	ChunkIndex int
	MinMemGB   float32
}

// StragglerTasks finds running PRIMARY tasks (not honeypot, not redundancy, not
// themselves a hedge) whose elapsed time exceeds `after` — the candidates for a
// straggler hedge. It excludes any chunk that already has a hedge in flight (so a
// straggler is hedged at most once) and any whose job already has >= maxInFlight
// hedges running (hedge sparingly). Ordered oldest-start first, capped at limit.
func (s *Store) StragglerTasks(ctx context.Context, after time.Duration, maxInFlight, limit int) ([]Straggler, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.job_id, t.worker_id, j.job_type, COALESCE(j.model_ref,''),
		        COALESCE(t.input_ref,''), COALESCE(t.chunk_index,0), COALESCE(j.min_memory_gb,0)
		 FROM tasks t JOIN jobs j ON j.id = t.job_id
		 WHERE t.status = 'running'
		   AND t.is_honeypot = false AND t.is_redundancy = false
		   AND t.hedged_from IS NULL
		   AND t.started_at IS NOT NULL
		   AND t.started_at < now() - make_interval(secs => $1)
		   AND j.status = 'running'
		   -- this chunk is not already hedged:
		   AND NOT EXISTS (
		     SELECT 1 FROM tasks h
		     WHERE h.job_id = t.job_id AND COALESCE(h.chunk_index,0) = COALESCE(t.chunk_index,0)
		       AND h.hedged_from IS NOT NULL AND h.is_redundancy = false
		       AND h.status NOT IN ('failed','cancelled')
		   )
		   -- and the job is under its in-flight hedge cap:
		   AND (
		     SELECT count(*) FROM tasks h2
		     WHERE h2.job_id = t.job_id AND h2.hedged_from IS NOT NULL
		       AND h2.is_redundancy = false AND h2.status IN ('queued','running')
		   ) < $2
		 ORDER BY t.started_at ASC
		 LIMIT $3`,
		after.Seconds(), maxInFlight, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Straggler
	for rows.Next() {
		var s Straggler
		if err := rows.Scan(&s.TaskID, &s.JobID, &s.WorkerID, &s.JobType, &s.ModelRef,
			&s.InputRef, &s.ChunkIndex, &s.MinMemGB); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// InsertHedgeTask inserts a straggler hedge: a DUPLICATE primary (is_redundancy =
// false so the merge will accept its result, hedged_from = the slow task) for the
// same chunk, pinned (pre-claimed, not started) to a chosen distinct same-class
// peer. It does NOT bump task_count — a hedge is a duplicate of work already
// counted, and "first commit wins" (the merge dedupes per chunk; the loser is
// cancelled on the winner's commit). Returns the new task id.
func (s *Store) InsertHedgeTask(ctx context.Context, jobID, primaryTaskID, peerWorker uuid.UUID, inputRef string, chunkIndex int) (uuid.UUID, error) {
	id := uuid.New()
	resultKey := fmt.Sprintf("jobs/%s/hedge/%s/result.json", jobID, id)
	_, err := s.pool.Exec(ctx,
		`INSERT INTO tasks
		   (id, job_id, status, is_honeypot, is_redundancy, retry_count,
		    input_ref, result_key, chunk_index, hedged_from,
		    claimed_by, claimed_at, visible_at)
		 VALUES ($1,$2,'queued',false,false,0,$3,$4,$5,$6,$7, now(), now())`,
		id, jobID, inputRef, resultKey, chunkIndex, primaryTaskID, peerWorker)
	if err != nil {
		return uuid.Nil, err
	}
	return id, nil
}

// CancelStragglerSiblings implements "first commit wins": once a task for a chunk
// commits, any OTHER not-complete HEDGE (or hedged primary) for that same chunk is
// marked failed so it stops blocking job completion and frees its worker. It never
// touches the just-committed task, completed tasks, or verification-redundancy
// tasks (is_redundancy=true with no hedged_from). Idempotent.
func (s *Store) CancelStragglerSiblings(ctx context.Context, jobID uuid.UUID, chunkIndex int, keepTaskID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE tasks
		   SET status = 'failed', claimed_by = NULL
		 WHERE job_id = $1 AND COALESCE(chunk_index,0) = $2
		   AND id <> $3
		   AND status IN ('queued','running','retrying')
		   AND hedged_from IS NOT NULL AND is_redundancy = false`,
		jobID, chunkIndex, keepTaskID)
	return err
}

// RequeueStaleTask pushes a stale running task back to the queue with a backoff:
// clears the claim, increments retry_count, and sets visible_at = now()+backoff.
func (s *Store) RequeueStaleTask(ctx context.Context, taskID uuid.UUID, backoff time.Duration) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE tasks
		   SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
		       worker_id = NULL, retry_count = retry_count + 1,
		       visible_at = now() + make_interval(secs => $2)
		 WHERE id = $1 AND status = 'running'`,
		taskID, backoff.Seconds())
	return err
}

// FailTaskAndRefundJob marks a task permanently failed (retries exhausted) and
// fails its parent job, writing a refund ledger row for any buyer charges already
// taken on that job. This is the job-level refund the action plan calls for when
// a task cannot be completed by any worker.
func (s *Store) FailTaskAndRefundJob(ctx context.Context, taskID, jobID uuid.UUID) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET status = 'failed' WHERE id = $1`, taskID); err != nil {
		return err
	}
	if _, err := failJobAndRefundOnce(ctx, tx, jobID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// refundJobChargesTx writes a refund ledger row (released) for any buyer_charge
// debits already taken on a job's tasks, so the buyer's net for a failed job is
// zero. Tx-scoped so the failure path and the stale reaper share one refund rule
// (no duplicated money math). No-op when nothing was charged.
func refundJobChargesTx(ctx context.Context, tx pgx.Tx, jobID uuid.UUID) error {
	var buyer uuid.UUID
	if err := tx.QueryRow(ctx, `SELECT buyer_id FROM jobs WHERE id = $1`, jobID).Scan(&buyer); err != nil {
		return err
	}
	var charged float64
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(SUM(-amount_usd),0) FROM ledger_entries
		 WHERE kind = 'buyer_charge' AND buyer_id = $1
		   AND task_id IN (SELECT id FROM tasks WHERE job_id = $2)`,
		buyer, jobID,
	).Scan(&charged); err != nil {
		return err
	}
	if charged > 0 {
		if _, err := tx.Exec(ctx,
			`INSERT INTO ledger_entries (kind, buyer_id, amount_usd, payout_status)
			 VALUES ('refund', $1, $2, 'released')`,
			buyer, charged); err != nil {
			return err
		}
	}
	return nil
}

// failJobAndRefundOnce flips a job to 'failed' and refunds the buyer EXACTLY once,
// even when several of the job's tasks fail terminally (e.g. multiple workers each
// report bad input, or the stale reaper and the fail endpoint both fire). It is a
// no-op when the job is already terminal — refunding per-failed-task would
// over-refund a job that had prior committed charges. `flipped` is true only on the
// call that actually transitioned the job, so the caller emits the job_failed event
// (and the refund) exactly once.
func failJobAndRefundOnce(ctx context.Context, tx pgx.Tx, jobID uuid.UUID) (flipped bool, err error) {
	ct, err := tx.Exec(ctx,
		`UPDATE jobs SET status = 'failed' WHERE id = $1 AND status NOT IN ('complete','cancelled','failed')`,
		jobID)
	if err != nil {
		return false, err
	}
	if ct.RowsAffected() == 0 {
		return false, nil // already terminal — do not re-refund
	}
	if err := refundJobChargesTx(ctx, tx, jobID); err != nil {
		return false, err
	}
	return true, nil
}

// CompletableJob is a job ready to finalize: all tasks done, status not yet
// terminal. Carries its buyer + output ref for the merge + webhook payload.
type CompletableJob struct {
	ID        uuid.UUID
	BuyerID   uuid.UUID
	OutputRef string
}

// FinalizableJobs returns jobs whose every task has finished (complete/failed,
// with at least one complete) but whose status is still running/verifying — the
// set the completion sweep should MERGE then finalize. Read-only: it does NOT
// flip status, so the sweep can write the merged artifact BEFORE marking the job
// complete (the buyer must never see status=complete with no merged output).
func (s *Store) FinalizableJobs(ctx context.Context, limit int) ([]CompletableJob, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT j.id, j.buyer_id, COALESCE(j.output_ref,'')
		 FROM jobs j
		 WHERE j.status IN ('running','verifying')
		   AND j.task_count > 0
		   AND NOT EXISTS (
		     SELECT 1 FROM tasks t
		     WHERE t.job_id = j.id AND t.status NOT IN ('complete','failed')
		   )
		   AND EXISTS (SELECT 1 FROM tasks t WHERE t.job_id = j.id AND t.status = 'complete')
		 ORDER BY j.created_at ASC LIMIT $1`,
		limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CompletableJob
	for rows.Next() {
		var c CompletableJob
		if err := rows.Scan(&c.ID, &c.BuyerID, &c.OutputRef); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// MarkJobComplete flips one job to 'complete', only from a non-terminal state.
// Idempotent (a no-op once already complete). Called by the sweep AFTER the
// merged artifact is written.
func (s *Store) MarkJobComplete(ctx context.Context, jobID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE jobs SET status = 'complete'
		 WHERE id = $1 AND status IN ('running','verifying')`,
		jobID)
	return err
}

// SetJobActualUSD recomputes a job's actual_usd from the ledger (sum of buyer
// charges on its tasks) — the real settled cost, set when the job finalizes.
func (s *Store) SetJobActualUSD(ctx context.Context, jobID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE jobs SET actual_usd = COALESCE((
		   SELECT SUM(-amount_usd) FROM ledger_entries
		   WHERE kind = 'buyer_charge'
		     AND task_id IN (SELECT id FROM tasks WHERE job_id = $1)
		 ),0)
		 WHERE id = $1`, jobID)
	return err
}

// JobResultKeys returns the result object keys of a job's completed primary
// tasks — the buyer's actual deliverable, excluding honeypot probes and
// redundancy clones (which exist for verification, not delivery). Ordered by
// completion so the list reads in a stable order.
func (s *Store) JobResultKeys(ctx context.Context, jobID uuid.UUID) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT result_ref FROM tasks
		 WHERE job_id = $1 AND status = 'complete'
		   AND is_honeypot = false AND is_redundancy = false
		   AND result_ref IS NOT NULL AND result_ref <> ''
		 ORDER BY completed_at ASC NULLS LAST, id ASC`,
		jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var k string
		if err := rows.Scan(&k); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rows.Err()
}

// PrimaryResult is one completed primary task's result location, in input order.
type PrimaryResult struct {
	ChunkIndex int
	ResultRef  string
}

// JobMergeInfo carries what MergeJobResults needs: the job's type + output key
// and the ordered list of its completed PRIMARY task result keys (honeypot and
// redundancy clones excluded — they verify, they are not the deliverable).
type JobMergeInfo struct {
	JobType   string
	OutputRef string
	Results   []PrimaryResult
}

// JobMergeInputs loads the job type, output ref, and the ordered completed
// primary-task results for the buyer-ready merge (MergeJobResults). Ordered by
// chunk_index so the merged artifact reads in the buyer's original input order.
func (s *Store) JobMergeInputs(ctx context.Context, jobID uuid.UUID) (*JobMergeInfo, error) {
	var info JobMergeInfo
	err := s.pool.QueryRow(ctx,
		`SELECT job_type, COALESCE(output_ref,'') FROM jobs WHERE id = $1`, jobID,
	).Scan(&info.JobType, &info.OutputRef)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	// One result per chunk_index: the FIRST completed primary (or its winning
	// straggler hedge — a hedge is a duplicate primary, is_redundancy=false). The
	// DISTINCT ON dedupes the case where both the original and its hedge complete
	// ("first commit wins"). Honeypots and verification redundancy are excluded.
	rows, err := s.pool.Query(ctx,
		`SELECT DISTINCT ON (COALESCE(chunk_index,0))
		        COALESCE(chunk_index,0), result_ref
		 FROM tasks
		 WHERE job_id = $1 AND status = 'complete'
		   AND is_honeypot = false AND is_redundancy = false
		   AND result_ref IS NOT NULL AND result_ref <> ''
		 ORDER BY COALESCE(chunk_index,0) ASC, completed_at ASC NULLS LAST, id ASC`,
		jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var pr PrimaryResult
		if err := rows.Scan(&pr.ChunkIndex, &pr.ResultRef); err != nil {
			return nil, err
		}
		info.Results = append(info.Results, pr)
	}
	return &info, rows.Err()
}

// QueueDepthRow is one (tier, job_type) bucket of the claimable-task backlog.
type QueueDepthRow struct {
	Tier    string
	JobType string
	Count   int
}

// QueueDepth returns the claimable-task backlog grouped by job tier and job type,
// for the /metrics cx_queue_depth gauge. "Claimable" matches the claim's own
// predicate (queued/retrying, visible-now, unclaimed, non-terminal job).
func (s *Store) QueueDepth(ctx context.Context) ([]QueueDepthRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT j.tier, j.job_type, count(*)
		 FROM tasks t JOIN jobs j ON j.id = t.job_id
		 WHERE t.status IN ('queued','retrying')
		   AND t.claimed_by IS NULL
		   AND COALESCE(t.visible_at, t.created_at) <= now()
		   AND j.status NOT IN ('cancelled','failed','complete')
		 GROUP BY j.tier, j.job_type
		 ORDER BY j.tier, j.job_type`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []QueueDepthRow
	for rows.Next() {
		var qd QueueDepthRow
		if err := rows.Scan(&qd.Tier, &qd.JobType, &qd.Count); err != nil {
			return nil, err
		}
		out = append(out, qd)
	}
	return out, rows.Err()
}

// ActiveWorkerCount is the number of workers seen within the last 60s, for the
// /metrics active_workers gauge.
func (s *Store) ActiveWorkerCount(ctx context.Context) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM workers
		 WHERE last_seen_at IS NOT NULL AND last_seen_at > now() - interval '60 seconds'`,
	).Scan(&n)
	return n, err
}

// InvoiceView is the buyer-facing invoice for one job: the job header plus the
// realized ledger breakdown for its tasks. All money is computed from real rows;
// nothing is fabricated.
type InvoiceView struct {
	JobID           uuid.UUID `json:"job_id"`
	BuyerID         uuid.UUID `json:"buyer_id"`
	Status          string    `json:"status"`
	JobType         string    `json:"job_type"`
	CreatedAt       time.Time `json:"created_at"`
	EstimatedUSD    float64   `json:"estimated_usd"`
	ActualUSD       float64   `json:"actual_usd"`
	ChargedUSD      float64   `json:"charged_usd"`
	SupplierPaidUSD float64   `json:"supplier_credit_usd"`
	PlatformTakeUSD float64   `json:"platform_take_usd"`
	// QuotedUSD is the cost_expected_usd of the advisory quote this job was bound to
	// (Plane D D7), so the invoice shows quoted-vs-actual. omitempty + a pointer keeps
	// it off the wire for unbound jobs (a literal 0.0 would falsely read as "quoted $0").
	QuotedUSD *float64 `json:"quoted_usd,omitempty"`
}

// JobInvoice builds an invoice for a job scoped to its buyer (buyers see only
// their own jobs). It reads the job header and aggregates the realized ledger
// entries for the job's tasks by kind. Returns errNotFound when the job is not
// the buyer's.
func (s *Store) JobInvoice(ctx context.Context, jobID, buyerID uuid.UUID) (*InvoiceView, error) {
	iv := InvoiceView{JobID: jobID}
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id, status, job_type, created_at,
		        COALESCE(estimated_usd,0), COALESCE(actual_usd,0)
		 FROM jobs WHERE id = $1 AND buyer_id = $2`,
		jobID, buyerID,
	).Scan(&iv.BuyerID, &iv.Status, &iv.JobType, &iv.CreatedAt, &iv.EstimatedUSD, &iv.ActualUSD)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	rows, err := s.pool.Query(ctx,
		`SELECT le.kind, COALESCE(SUM(le.amount_usd),0)
		 FROM ledger_entries le JOIN tasks t ON t.id = le.task_id
		 WHERE t.job_id = $1 GROUP BY le.kind`, jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var kind string
		var amt float64
		if err := rows.Scan(&kind, &amt); err != nil {
			return nil, err
		}
		switch kind {
		case "supplier_credit", "clawback":
			iv.SupplierPaidUSD += amt // clawback is negative → reduces net paid
		case "platform_take":
			iv.PlatformTakeUSD += amt
		case "buyer_charge":
			iv.ChargedUSD += amt
		}
	}
	// No explicit buyer_charge rows → the charge is the job's actual (else
	// estimated) cost. Never fabricate; fall back to what the job already knows.
	if iv.ChargedUSD == 0 {
		if iv.ActualUSD > 0 {
			iv.ChargedUSD = iv.ActualUSD
		} else {
			iv.ChargedUSD = iv.EstimatedUSD
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// If the job was bound to a quote (Plane D D7), surface what the buyer was quoted
	// alongside what they were charged. Absent binding leaves QuotedUSD nil (omitted).
	if quoted, ok, qerr := s.QuotedUSDForJob(ctx, jobID); qerr != nil {
		return nil, qerr
	} else if ok {
		iv.QuotedUSD = &quoted
	}
	return &iv, nil
}

// SupplierStripeAcct returns a supplier's connected Stripe account id (empty when
// unset). Used by StripePayout to target a real transfer. errNotFound when the
// supplier row is missing.
func (s *Store) SupplierStripeAcct(ctx context.Context, supplierID uuid.UUID) (string, error) {
	var acct *string
	err := s.pool.QueryRow(ctx, `SELECT stripe_acct FROM suppliers WHERE id = $1`, supplierID).Scan(&acct)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", errNotFound
	}
	if err != nil {
		return "", err
	}
	if acct == nil {
		return "", nil
	}
	return *acct, nil
}
