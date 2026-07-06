package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
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
		// Verification-requeue worker exclusion (Scheduling & Matching Engine 8->9,
		// docs/internal/CREED_AND_PATH_TO_TEN.md): RequeueTask records the worker that
		// just failed a task here (until excluded_until) so the claim skips it for a
		// window, then the exclusion expires — a thin fleet is never permanently
		// starved of the retry. See RequeueTask (store.go) and ClaimTaskSQL (scheduler.go).
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS excluded_worker UUID`,
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS excluded_until  TIMESTAMPTZ`,
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
		// Stripe Connect payout readiness (suppliers.go): flipped by the account.updated
		// webhook once Stripe says the connected account can receive transfers.
		`ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS payouts_enabled BOOLEAN DEFAULT false`,
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
		// Performance Observability 6.5->7 (docs/internal/CREED_AND_PATH_TO_TEN.md):
		// the committing worker's identity + verification class, so a version bump
		// or a heterogeneous fleet can be sliced out of the same duration history
		// instead of one blended average hiding a regression on just one build.
		// build_hash already IS the version-sliced identity this repo tracks
		// (hardware::engine_build_hash folds in agent version + device backend +
		// kernel identity — see docs/DETERMINISM_CLASS.md), so it stands in for a
		// separate raw "agent_version" column.
		`ALTER TABLE task_durations ADD COLUMN IF NOT EXISTS worker_id UUID`,
		`ALTER TABLE task_durations ADD COLUMN IF NOT EXISTS engine TEXT`,
		`ALTER TABLE task_durations ADD COLUMN IF NOT EXISTS build_hash TEXT`,
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
		// Compute Autopilot: user-defined pipelines (pipeline.go). A pipeline is an ordered
		// stage spec; pipeline_jobs links each launched stage to its real CX job, exactly
		// like intake_jobs, so advancePipeline can chain output->input as stages complete.
		`CREATE TABLE IF NOT EXISTS pipelines (
		   id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   buyer_id   UUID,
		   name       TEXT,
		   spec       JSONB,
		   status     TEXT DEFAULT 'running',
		   created_at TIMESTAMPTZ DEFAULT now()
		 )`,
		`CREATE INDEX IF NOT EXISTS pipelines_buyer_idx ON pipelines (buyer_id)`,
		`CREATE TABLE IF NOT EXISTS pipeline_jobs (
		   job_id      UUID PRIMARY KEY,
		   pipeline_id UUID,
		   stage_index INT
		 )`,
		`CREATE INDEX IF NOT EXISTS pipeline_jobs_pipeline_idx ON pipeline_jobs (pipeline_id)`,
		// Self-serve buyer accounts (accounts.go) + opaque session tokens. MIRRORS
		// db/schema.sql so a control plane that only ran Migrate self-migrates. buyers
		// is the account of record: UNIQUE email + bcrypt password_hash + a sandbox
		// free_credit_usd grant. sessions are hashed-at-rest opaque tokens (like
		// api_keys/worker_tokens) the login flow issues and authBuyer accepts.
		`CREATE TABLE IF NOT EXISTS buyers (
		   id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   email           TEXT UNIQUE NOT NULL,
		   password_hash   TEXT,
		   free_credit_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
		   created_at      TIMESTAMPTZ DEFAULT now()
		 )`,
		`CREATE TABLE IF NOT EXISTS sessions (
		   token_hash TEXT PRIMARY KEY,
		   buyer_id   UUID NOT NULL,
		   created_at TIMESTAMPTZ DEFAULT now(),
		   expires_at TIMESTAMPTZ NOT NULL,
		   revoked    BOOLEAN DEFAULT false
		 )`,
		`CREATE INDEX IF NOT EXISTS sessions_buyer_idx ON sessions (buyer_id)`,
		// Stuck-run watchdog V2 (MIRRORS db/schema.sql): escalation strikes, the
		// buyer deadline knob, and the ETA calibration loop.
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS watchdog_strikes INT NOT NULL DEFAULT 0`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deadline_secs INT`,
		`CREATE TABLE IF NOT EXISTS eta_calibration (
		   id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   job_id         UUID,
		   job_type       TEXT,
		   tier           TEXT,
		   predicted_secs INT,
		   realized_secs  INT,
		   created_at     TIMESTAMPTZ DEFAULT now()
		 )`,
		`CREATE INDEX IF NOT EXISTS eta_calibration_type_idx ON eta_calibration (job_type, tier, created_at DESC)`,
		// Charge batching + Stripe fee truth (MIRRORS db/schema.sql; see collect.go).
		// A charge batch is ONE PaymentIntent covering many small deferred jobs of one
		// buyer; amount_usd is FROZEN at formation and every retry reuses the same
		// idempotency key, so an ambiguous prior attempt can never double-charge.
		`CREATE TABLE IF NOT EXISTS charge_batches (
		   id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   buyer_id   UUID NOT NULL,
		   amount_usd NUMERIC(10,6) NOT NULL,
		   status     TEXT NOT NULL DEFAULT 'attempting',
		   stripe_pi  TEXT,
		   created_at TIMESTAMPTZ DEFAULT now(),
		   charged_at TIMESTAMPTZ
		 )`,
		`CREATE INDEX IF NOT EXISTS charge_batches_status_idx ON charge_batches (status, created_at)`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_status TEXT NOT NULL DEFAULT 'not_attempted'`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_batch_id UUID`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_attempts INT NOT NULL DEFAULT 0`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_next_at TIMESTAMPTZ`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deferred_at TIMESTAMPTZ`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_attempt_usd NUMERIC(10,6)`,
		`ALTER TABLE charge_batches ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0`,
		`ALTER TABLE charge_batches ADD COLUMN IF NOT EXISTS next_at TIMESTAMPTZ`,
		`ALTER TABLE charge_batches ALTER COLUMN amount_usd TYPE NUMERIC(12,6)`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stripe_pi TEXT`,
		`CREATE INDEX IF NOT EXISTS jobs_charge_status_idx ON jobs (charge_status)`,
		// One stripe_fee ledger row per PaymentIntent, structurally (the fee recorder
		// is INSERT-if-absent by payout_ref; the partial unique index closes the race).
		`CREATE UNIQUE INDEX IF NOT EXISTS ledger_stripe_fee_ref_uniq ON ledger_entries (payout_ref) WHERE kind = 'stripe_fee'`,
		// Wake-on-work (docs/CREED_AND_PATH_TO_TEN.md, "Control plane hot path" 6→7 /
		// "Scalability headroom" 5→6 / "End-to-end job latency" 7.5→8 — the same fix
		// serves all three). Before this, every idle long-polling worker re-attempted
		// a full ClaimTask transaction every 250ms regardless of whether any work
		// existed. A STATEMENT-level trigger (fires once per statement, not once per
		// row, so a 5,500-chunk submit sends one notify, not 5,500) calls pg_notify
		// whenever a task is inserted (a new job) or its status/visible_at changes (a
		// requeue, a hedge, a rescue) — notify.go's listener wakes every waiting
		// long-poll goroutine on receipt, which then re-attempts its own ClaimTask
		// immediately instead of waiting out the rest of its poll tick. The 250ms
		// ticker in api.go's claimWithWait becomes a rare-case safety net (a missed
		// notification, e.g. across a brief connection drop), not the primary
		// wake mechanism — see notify.go for the listener + broadcast implementation.
		`CREATE OR REPLACE FUNCTION notify_task_available() RETURNS trigger AS $$
		 BEGIN
		   PERFORM pg_notify('cx_task_available', '');
		   RETURN NULL;
		 END;
		 $$ LANGUAGE plpgsql`,
		`DROP TRIGGER IF EXISTS tasks_notify_available ON tasks`,
		`CREATE TRIGGER tasks_notify_available
		   AFTER INSERT OR UPDATE OF status, visible_at ON tasks
		   FOR EACH STATEMENT EXECUTE FUNCTION notify_task_available()`,
		// Cold-load timing (docs/CREED_AND_PATH_TO_TEN.md, "Warm model pool" 6.5→7).
		`ALTER TABLE benchmark_results ADD COLUMN IF NOT EXISTS load_ms BIGINT DEFAULT 0`,
		// Maintained completed-task counter (Control Plane Hot Path 7->8,
		// docs/internal/CREED_AND_PATH_TO_TEN.md): ClaimTask used to re-derive a
		// supplier's lifetime completed-task count with a `count(*)` scan over
		// `tasks` on EVERY single claim (the trusted-tier gate). Now maintained as
		// a running column, incremented once per real commit (CommitTask) instead.
		`ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS completed_tasks BIGINT NOT NULL DEFAULT 0`,
		// One-time backfill for suppliers that already had completed tasks before
		// this column existed. Safe to run on every startup: once backfilled, a
		// real supplier's count is > 0 and this WHERE clause matches nothing for
		// them; it only ever does real work for a genuinely still-zero supplier,
		// where the inner count is cheap (no rows to scan for a fresh worker).
		`UPDATE suppliers s SET completed_tasks = (
		   SELECT count(*) FROM tasks t
		    WHERE t.worker_id IN (SELECT id FROM workers WHERE supplier_id = s.id)
		      AND t.status = 'complete'
		 ) WHERE completed_tasks = 0`,
		// Results-merge watermark (Data Transfer & Artifact I/O 4.5->5,
		// docs/internal/CREED_AND_PATH_TO_TEN.md, "Stop paying for every poll
		// twice"): set once a job's buyer-ready artifact has actually been merged,
		// so GET /v1/jobs/{id}/results only re-merges when no successful merge has
		// happened since completion instead of on every single poll.
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS results_merged_at TIMESTAMPTZ`,
		// Postgres Data Lifecycle 5->6 (docs/internal/CREED_AND_PATH_TO_TEN.md):
		// per-table autovacuum tuning for the telemetry tables sweepTelemetryRetention
		// now DELETEs from hourly (control/workers.go telemetryTables) — mirrors
		// db/schema.sql, here so a deployment that migrated the tables before this rung
		// existed still picks up the tuning on next startup.
		//
		// Since Postgres Data Lifecycle 6->7 these tables are PARTITIONED, and a
		// partitioned PARENT rejects storage parameters (they must sit on the leaves —
		// MigrateTelemetryPartitions below applies them per-leaf). So each ALTER is
		// guarded to run ONLY while the table is still a PLAIN table (relkind 'r'): on
		// an already-partitioned DB it is a no-op (relkind 'p'); on an older DB whose
		// tables are still plain, it tunes them exactly as before, right up until the
		// conversion below carries the same params onto every leaf. Without the guard,
		// a second startup after conversion would error ("cannot specify storage
		// parameters for a partitioned table"). The relkind check makes it idempotent.
		`DO $$ BEGIN
		   IF EXISTS (SELECT 1 FROM pg_class WHERE relname='worker_memory_samples' AND relnamespace='public'::regnamespace AND relkind='r') THEN
		     ALTER TABLE worker_memory_samples SET (
		       autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=200,
		       autovacuum_analyze_scale_factor=0.02, autovacuum_analyze_threshold=200,
		       autovacuum_vacuum_cost_limit=1000);
		   END IF;
		 END $$`,
		`DO $$ BEGIN
		   IF EXISTS (SELECT 1 FROM pg_class WHERE relname='task_durations' AND relnamespace='public'::regnamespace AND relkind='r') THEN
		     ALTER TABLE task_durations SET (
		       autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_threshold=100,
		       autovacuum_analyze_scale_factor=0.05, autovacuum_analyze_threshold=100,
		       autovacuum_vacuum_cost_limit=500);
		   END IF;
		 END $$`,
		`DO $$ BEGIN
		   IF EXISTS (SELECT 1 FROM pg_class WHERE relname='job_events' AND relnamespace='public'::regnamespace AND relkind='r') THEN
		     ALTER TABLE job_events SET (
		       autovacuum_vacuum_scale_factor=0.1, autovacuum_vacuum_threshold=100,
		       autovacuum_analyze_scale_factor=0.1, autovacuum_analyze_threshold=100,
		       autovacuum_vacuum_cost_limit=500);
		   END IF;
		 END $$`,
		// Buyer Advantage & Pricing Edge 4.5->5 (docs/internal/CREED_AND_PATH_TO_TEN.md,
		// "Reprice from real supplier economics, not hand-seeded constants"): price
		// provenance columns so a catalogue price is traceable to either the original
		// hand-typed launch constant or a real measured-throughput formula (see
		// control/pricing.go). Mirrors db/schema.sql for a DB that only ran Migrate.
		`ALTER TABLE models ADD COLUMN IF NOT EXISTS price_source TEXT DEFAULT 'seed'`,
		`ALTER TABLE models ADD COLUMN IF NOT EXISTS price_formula TEXT`,
		// Project Detection & Quotation 7->8 ("Ship a firm-quote tier: a real
		// commitment, not just an estimate"): an opt-in per-job flag that caps the
		// buyer's charge at the quote's stated maximum, with overage absorbed by the
		// platform rather than passed through (control/firmquote.go).
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS firm_quote BOOLEAN DEFAULT false`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS firm_quote_max_usd NUMERIC(12,6)`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS billed_usd NUMERIC(12,6)`,
		// Speed Lane wave 2A (docs/speed-lane-reports/SLA_QUOTE_WAVE2A.md): the
		// wall-clock speed-SLA binding + outcome, MIRRORS db/schema.sql (the quotes
		// table's own sla columns live only in schema.sql — quotes itself is
		// schema.sql-owned). sla_guarantee_secs/sla_premium_usd are stamped at
		// submit when firm_quote binds an SLA-bearing quote; sla_met is NULL until
		// the outcome is decided at finalize (true = met, false = missed → an
		// sla_refund ledger row, made once-only by the partial unique index).
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sla_guarantee_secs INT`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sla_premium_usd NUMERIC(12,6)`,
		`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sla_met BOOLEAN`,
		`CREATE UNIQUE INDEX IF NOT EXISTS ledger_sla_refund_ref_uniq ON ledger_entries (payout_ref) WHERE kind = 'sla_refund'`,
		// Control Plane Hot Path 7->8 ("hoist worker_tps into something computed
		// once per worker state change rather than recomputed per candidate row
		// per claim"): maintained cache, mirrors db/schema.sql for a DB that only
		// ran Migrate. See UpsertWorker (maintains it) and ClaimTaskSQL (reads it).
		`CREATE TABLE IF NOT EXISTS worker_tps_cache (
		   worker_id  UUID NOT NULL,
		   job_type   TEXT NOT NULL,
		   tps        REAL NOT NULL DEFAULT 0,
		   updated_at TIMESTAMPTZ DEFAULT now(),
		   PRIMARY KEY (worker_id, job_type)
		 )`,
		// Control Plane Hot Path 8->9 ("trust a buyer/worker-supplied SHA-256 for
		// redundancy/honeypot comparison where safe, instead of re-downloading
		// bytes the worker just uploaded synchronously inside the commit
		// transaction"): the worker-reported SHA-256 of its own committed result
		// bytes, persisted at CommitTask so a later commit's redundancy compare
		// can trust a hash-to-hash match for byte-exact job types without a
		// second S3 GetObject. Empty/NULL for an older agent that does not send
		// one (or a pre-migration row) — the commit handler's fallback is a real
		// GetObject, so correctness never depends on this column being populated.
		`ALTER TABLE tasks ADD COLUMN IF NOT EXISTS result_sha256 TEXT`,
		// Operator Tooling 7->8 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Add write
		// actions the operator currently has to reach into the database for"): an
		// append-only audit log for every admin write action that used to be a raw
		// psql UPDATE per RUNBOOKS.md (force-requeue a stuck task, adjust a
		// supplier's reputation, release a payout hold). Mirrors the job_events /
		// verification_events append-only pattern already established in this
		// schema: kind is the closed action set, detail carries the operator's
		// free-text reason plus whatever before/after values matter for that action
		// (e.g. old/new reputation), so a later operator can see WHO did WHAT and
		// WHY without re-deriving it from a diff of table state over time.
		`CREATE TABLE IF NOT EXISTS admin_actions (
		   id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
		   created_at  TIMESTAMPTZ DEFAULT now(),
		   kind        TEXT NOT NULL,  -- task_requeued|reputation_adjusted|payout_released
		   task_id     UUID,
		   supplier_id UUID,
		   ledger_entry_id UUID,
		   reason      TEXT,
		   detail      JSONB
		 )`,
	}
	for _, q := range stmts {
		if _, err := s.pool.Exec(ctx, q); err != nil {
			return fmt.Errorf("migrate: %q: %w", q, err)
		}
	}
	// Postgres Data Lifecycle 6->7 (docs/internal/CREED_AND_PATH_TO_TEN.md): convert the
	// three high-churn telemetry tables to declarative RANGE partitioning by created_at,
	// in place, preserving every existing row (control/partition.go). Runs LAST — after
	// the base tables above exist (or were just created) — and is idempotent: a table
	// already partitioned is a no-op, so this is safe on every startup. A failure here
	// is fatal at startup exactly like every statement above; a half-migrated table is
	// impossible because each table's conversion is its own transaction.
	if err := s.MigrateTelemetryPartitions(ctx); err != nil {
		return fmt.Errorf("migrate: telemetry partitions: %w", err)
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

// JobChargeInfo returns a job's buyer + the amount to actually CHARGE it (for the
// auto-charge). This is normally the settled actual_usd unchanged — but for a
// firm-quote job (Project Detection & Quotation 7->8,
// docs/internal/CREED_AND_PATH_TO_TEN.md, "a real commitment, not just an
// estimate") the charge is capped at firm_quote_max_usd: the buyer is NEVER
// charged more than the quoted maximum they committed budget against, even when
// the real actual_usd (what suppliers actually earned for the real work they
// did — untouched, see billing.go/CommitTask) exceeds it. The platform absorbs
// that difference; nothing here reduces what a supplier is owed.
func (s *Store) JobChargeInfo(ctx context.Context, jobID uuid.UUID) (buyerID uuid.UUID, chargeUSD float64, err error) {
	var actualUSD float64
	var firmQuote bool
	var firmMax float64
	var slaRefund float64
	// The sla_refund subquery is the Go-side twin of collect.go's
	// firmChargeAmountSQL netting (Speed Lane wave 2A): a missed speed-SLA's
	// premium refund (an sla_refund ledger credit keyed 'sla-<job_id>') comes off
	// the amount actually collected, on BOTH charge paths, so a refund can never
	// be bypassed by which path happens to collect the job.
	err = s.pool.QueryRow(ctx,
		`SELECT buyer_id, COALESCE(actual_usd,0), firm_quote, COALESCE(firm_quote_max_usd,0),
		        COALESCE((SELECT SUM(le.amount_usd) FROM ledger_entries le
		                  WHERE le.kind = 'sla_refund'
		                    AND le.payout_ref = 'sla-' || jobs.id::text), 0)::float8
		   FROM jobs WHERE id=$1`,
		jobID).Scan(&buyerID, &actualUSD, &firmQuote, &firmMax, &slaRefund)
	if errors.Is(err, pgx.ErrNoRows) {
		err = errNotFound
		return
	}
	if err != nil {
		return
	}
	chargeUSD = actualUSD
	if firmQuote && firmMax > 0 && actualUSD > firmMax {
		chargeUSD = firmMax
	}
	if slaRefund > 0 {
		chargeUSD -= slaRefund
		if chargeUSD < 0 {
			chargeUSD = 0 // the remedy nets the bill down, never into a negative charge
		}
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

// JobBuyerID returns the buyer who submitted jobID (Security Posture 6.5->7:
// resolveInput's s3_key ownership check — a buyer may only reference an object
// key under a job THEY submitted, never another buyer's).
func (s *Store) JobBuyerID(ctx context.Context, jobID uuid.UUID) (uuid.UUID, error) {
	var buyerID uuid.UUID
	err := s.pool.QueryRow(ctx, `SELECT buyer_id FROM jobs WHERE id=$1`, jobID).Scan(&buyerID)
	return buyerID, err
}

// --- Compute Autopilot pipelines (user-defined multi-step chains, pipeline.go) ---

// CreatePipeline persists a composed pipeline (its stage spec as JSONB) and returns its id.
func (s *Store) CreatePipeline(ctx context.Context, buyerID uuid.UUID, name string, spec []byte) (uuid.UUID, error) {
	if name == "" {
		name = "pipeline"
	}
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`INSERT INTO pipelines (buyer_id, name, spec, status) VALUES ($1, $2, $3, 'running') RETURNING id`,
		buyerID, name, spec,
	).Scan(&id)
	return id, err
}

// LinkPipelineJob links a launched stage job to its pipeline + stage index (mirrors
// InsertIntakeJobLink; one job belongs to at most one pipeline stage).
func (s *Store) LinkPipelineJob(ctx context.Context, jobID, pipelineID uuid.UUID, stageIndex int) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO pipeline_jobs (job_id, pipeline_id, stage_index) VALUES ($1, $2, $3) ON CONFLICT (job_id) DO NOTHING`,
		jobID, pipelineID, stageIndex)
	return err
}

// PipelineForJob resolves a completed job back to its pipeline + stage (ok=false for a job
// that is not part of any pipeline — the common case).
func (s *Store) PipelineForJob(ctx context.Context, jobID uuid.UUID) (pipelineID uuid.UUID, stageIndex int, ok bool) {
	err := s.pool.QueryRow(ctx, `SELECT pipeline_id, stage_index FROM pipeline_jobs WHERE job_id=$1`, jobID).Scan(&pipelineID, &stageIndex)
	return pipelineID, stageIndex, err == nil
}

// PipelineSpec returns a pipeline's stage spec JSON.
func (s *Store) PipelineSpec(ctx context.Context, pipelineID uuid.UUID) ([]byte, error) {
	var spec []byte
	err := s.pool.QueryRow(ctx, `SELECT spec FROM pipelines WHERE id=$1`, pipelineID).Scan(&spec)
	return spec, err
}

// PipelineStageSubmitted reports whether a stage of a pipeline already has a job.
func (s *Store) PipelineStageSubmitted(ctx context.Context, pipelineID uuid.UUID, stageIndex int) bool {
	var n int
	_ = s.pool.QueryRow(ctx, `SELECT COUNT(*) FROM pipeline_jobs WHERE pipeline_id=$1 AND stage_index=$2`, pipelineID, stageIndex).Scan(&n)
	return n > 0
}

// PipelineBuyer returns a pipeline's owning buyer (so a chained stage charges the right buyer).
func (s *Store) PipelineBuyer(ctx context.Context, pipelineID uuid.UUID) (uuid.UUID, error) {
	var b uuid.UUID
	err := s.pool.QueryRow(ctx, `SELECT buyer_id FROM pipelines WHERE id=$1`, pipelineID).Scan(&b)
	if errors.Is(err, pgx.ErrNoRows) {
		err = errNotFound
	}
	return b, err
}

// SetPipelineStatus updates the cached overall status; GetPipelineView derives the
// authoritative status from the stage jobs regardless.
func (s *Store) SetPipelineStatus(ctx context.Context, pipelineID uuid.UUID, status string) error {
	_, err := s.pool.Exec(ctx, `UPDATE pipelines SET status=$2 WHERE id=$1`, pipelineID, status)
	return err
}

// GetPipelineView assembles the buyer-facing pipeline: each spec stage joined to its job's
// live status, with an overall status DERIVED from the stages (failed if any stage failed,
// complete only when every stage completed). Buyer-scoped.
func (s *Store) GetPipelineView(ctx context.Context, buyerID, pipelineID uuid.UUID) (*PipelineView, error) {
	var name, status string
	var spec []byte
	var createdAt time.Time
	err := s.pool.QueryRow(ctx,
		`SELECT name, spec, status, created_at FROM pipelines WHERE id=$1 AND buyer_id=$2`,
		pipelineID, buyerID,
	).Scan(&name, &spec, &status, &createdAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	var stages []pipelineStage
	if err := json.Unmarshal(spec, &stages); err != nil {
		return nil, err
	}

	type jobInfo struct {
		id           uuid.UUID
		status       string
		tasksDone    int
		taskCount    int
		estimatedUSD float64
		actualUSD    float64
	}
	byStage := map[int]jobInfo{}
	rows, err := s.pool.Query(ctx,
		`SELECT pj.stage_index, j.id, j.status, COALESCE(j.tasks_done,0), COALESCE(j.task_count,0),
		        COALESCE(j.estimated_usd,0), COALESCE(j.actual_usd,0)
		 FROM pipeline_jobs pj JOIN jobs j ON j.id = pj.job_id
		 WHERE pj.pipeline_id=$1`, pipelineID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var idx int
		var ji jobInfo
		if err := rows.Scan(&idx, &ji.id, &ji.status, &ji.tasksDone, &ji.taskCount, &ji.estimatedUSD, &ji.actualUSD); err != nil {
			return nil, err
		}
		byStage[idx] = ji
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	views := make([]PipelineStageView, len(stages))
	anyFailed, allComplete := false, true
	for i, st := range stages {
		v := PipelineStageView{Index: i, Op: st.Op, Model: st.Model, From: st.From, Status: "pending"}
		if ji, ok := byStage[i]; ok {
			v.JobID = ji.id.String()
			v.Status = ji.status
			v.TasksDone = ji.tasksDone
			v.TaskCount = ji.taskCount
			v.EstimatedUSD = ji.estimatedUSD
			v.ActualUSD = ji.actualUSD
		}
		if v.Status == "failed" || v.Status == "cancelled" {
			anyFailed = true
		}
		if v.Status != "complete" {
			allComplete = false
		}
		views[i] = v
	}
	overall := "running"
	if anyFailed {
		overall = "failed"
	} else if allComplete {
		overall = "complete"
	}

	return &PipelineView{
		ID:        pipelineID.String(),
		Name:      name,
		Status:    overall,
		CreatedAt: createdAt.UTC().Format(time.RFC3339),
		Stages:    views,
	}, nil
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

// nullPosInt mirrors nullPosFloat for integer columns where 0 means "unset →
// persisted NULL" (jobs.sla_guarantee_secs, wave 2A): NULL cleanly means "no
// SLA" instead of a fake 0-second guarantee.
func nullPosInt(v int) any {
	if v <= 0 {
		return nil
	}
	return v
}

// nullSHA256Hex validates a worker-reported SHA-256 hex string (Control Plane
// Hot Path 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md "trust a
// buyer/worker-supplied SHA-256 ... where safe") before it is ever persisted or
// trusted for a hash-to-hash comparison: exactly 64 lowercase hex characters (a
// real SHA-256 digest), else nil (SQL NULL — the commit/verify path always falls
// back to a real GetObject when this is NULL, so a malformed or absent hash can
// never cause a wrong trust decision, only a missed speed optimization).
func nullSHA256Hex(h string) any {
	if len(h) != 64 {
		return nil
	}
	for i := 0; i < len(h); i++ {
		c := h[i]
		if !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f') {
			return nil
		}
	}
	return h
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

// CreateWorkerToken mints a worker token for a supplier's NEW worker: it generates
// a random raw token, stores ONLY its hash, and returns the raw token once. The raw
// value can never be recovered (it is not stored) — the caller hands it to the
// supplier. This is the onboarding path real suppliers use instead of seeded tokens.
//
// worker_tokens.worker_id has a foreign key into workers, but the real workers row
// (hw_class, benchmarks, ...) does not exist yet at mint time — the agent only
// creates/fills it via UpsertWorker's `ON CONFLICT (id) DO UPDATE` the first time it
// actually registers with this token. So this inserts a minimal PLACEHOLDER workers
// row first, in the same transaction: hw_class='cpu' (a valid enum member, used only
// to satisfy the NOT NULL column) with supported_jobs/supported_models left NULL.
// COALESCE(w.supported_jobs,'{}') @> ARRAY[job_type] in the claim query (scheduler.go)
// means an empty/NULL supported_jobs can never match ANY job type, so this
// placeholder is structurally inert — it can never be claimed against — until the
// agent's first real registration overwrites it with real capability data.
//
// It also activates the supplier (status 'pending' -> 'active') if this is their
// FIRST token. Discovered by testing, not by reading: UpsertSupplierByEmail leaves
// a brand-new supplier at status='pending', the claim query hard-requires
// s.status='active' (scheduler.go's quarantine gate), and — before this fix —
// nothing in production code ever performed that transition; only test fixtures
// and the demo seed set status='active' directly. Without this, a self-served
// supplier could authenticate and poll forever but their tasks could NEVER be
// claimed. The guard is `AND status = 'pending'`, deliberately: minting an
// ADDITIONAL token for a supplier who was later suspended/quarantined for fraud
// must never silently reactivate them — activation only ever happens once, on the
// way up from the initial pending state.
func (s *Store) CreateWorkerToken(ctx context.Context, workerID, supplierID uuid.UUID) (string, error) {
	raw := newSecret("cxw_")
	if raw == "" {
		return "", errors.New("worker token: entropy failure")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx,
		`INSERT INTO workers (id, supplier_id, hw_class) VALUES ($1, $2, 'cpu')
		 ON CONFLICT (id) DO NOTHING`,
		workerID, supplierID,
	); err != nil {
		return "", err
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO worker_tokens (token_hash, worker_id, supplier_id, revoked)
		 VALUES ($1, $2, $3, false)`,
		hashKey(raw), workerID, supplierID,
	); err != nil {
		return "", err
	}
	if _, err := tx.Exec(ctx,
		`UPDATE suppliers SET status = 'active' WHERE id = $1 AND status = 'pending'`,
		supplierID,
	); err != nil {
		return "", err
	}
	if err := tx.Commit(ctx); err != nil {
		return "", err
	}
	return raw, nil
}

// --- buyer API-key lifecycle (POST/GET/DELETE /v1/keys) ---

// APIKeyRow is one masked api_key as shown in the list view. `Masked` is the
// non-secret display hint (prefix + last4) captured at mint; the raw secret is
// NEVER reconstructable (only key_hash is stored).
type APIKeyRow struct {
	ID        uuid.UUID `json:"id"`
	Name      string    `json:"name"`
	Masked    string    `json:"masked"`
	CreatedAt time.Time `json:"created_at"`
	Revoked   bool      `json:"revoked"`
}

// CreateAPIKey mints a buyer API key the same way CreateWorkerToken mints a worker
// token: it generates a random raw secret, stores ONLY its SHA-256 hash plus a
// non-secret display hint (prefix + last4), and returns the raw secret ONCE. The
// raw value is unrecoverable afterwards (it is never stored). `test` selects the
// cx_test_ prefix (vs. cx_live_) so callers can tell environments apart; it does
// not change auth · both authenticate identically (no scopes/spend here by design).
// is_admin is left at its default (false): minting a key never grants admin.
func (s *Store) CreateAPIKey(ctx context.Context, buyerID uuid.UUID, name string, test bool) (id uuid.UUID, raw, masked string, err error) {
	prefix := "cx_live_"
	if test {
		prefix = "cx_test_"
	}
	raw = newSecret(prefix)
	if raw == "" {
		return uuid.Nil, "", "", errors.New("api key: entropy failure")
	}
	masked = maskKey(raw)
	err = s.pool.QueryRow(ctx,
		`INSERT INTO api_keys (buyer_id, key_hash, name, masked, is_admin, revoked)
		 VALUES ($1, $2, $3, $4, false, false)
		 RETURNING id`,
		buyerID, hashKey(raw), name, masked,
	).Scan(&id)
	if err != nil {
		return uuid.Nil, "", "", err
	}
	return id, raw, masked, nil
}

// ListAPIKeys returns the caller's keys as masked rows (never the raw secret ·
// only key_hash is stored, so there is nothing to leak even on a full DB read).
// Scoped to buyerID; revoked keys are included so the UI can show their state.
func (s *Store) ListAPIKeys(ctx context.Context, buyerID uuid.UUID) ([]APIKeyRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, COALESCE(name,''), COALESCE(masked,''), created_at, revoked
		   FROM api_keys
		  WHERE buyer_id = $1
		  ORDER BY created_at DESC`,
		buyerID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []APIKeyRow{}
	for rows.Next() {
		var r APIKeyRow
		if err := rows.Scan(&r.ID, &r.Name, &r.Masked, &r.CreatedAt, &r.Revoked); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// RevokeAPIKey revokes one key, scoped to the owning buyer so a caller can never
// revoke another buyer's key. Idempotent: revoking an already-revoked (or
// never-existed-for-this-buyer) key is a no-op success · the endpoint is 204
// either way, matching the DELETE contract. Returns whether a row was found so
// the handler can distinguish "revoked" from "not yours / not found" if needed.
func (s *Store) RevokeAPIKey(ctx context.Context, buyerID, id uuid.UUID) (bool, error) {
	tag, err := s.pool.Exec(ctx,
		`UPDATE api_keys SET revoked = true
		  WHERE id = $1 AND buyer_id = $2`,
		id, buyerID,
	)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() > 0, nil
}

// maskKey builds the non-secret display hint for an api_key: the cx_live_/cx_test_
// prefix plus the last 4 chars of the secret, joined by an ellipsis. It is derived
// from the raw secret ONLY at mint time and persisted; it is never enough to
// reconstruct the key (4 trailing chars of 32 bytes of entropy).
func maskKey(raw string) string {
	// CX key tags are "cx_live_" / "cx_test_" (two underscores); the random tail is
	// base64url and may itself contain "_", so take up to the SECOND underscore. Never
	// LastIndex · that would leak almost the whole secret into the masked hint.
	prefix := raw
	if i1 := strings.IndexByte(raw, '_'); i1 >= 0 {
		if i2 := strings.IndexByte(raw[i1+1:], '_'); i2 >= 0 {
			prefix = raw[:i1+1+i2+1]
		}
	}
	last4 := raw
	if len(raw) >= 4 {
		last4 = raw[len(raw)-4:]
	}
	return prefix + "..." + last4
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

	// engine + build_hash are the verification-class axes (handler-normalized:
	// engine is blank→candle + validated, build_hash is the opaque agent build tag).
	// They are persisted alongside hw_class so the redundancy matcher and the verifier
	// can pin byte-exact peers/honeypots to the same (hw_class, engine, build_hash).
	_, err = tx.Exec(ctx,
		`INSERT INTO workers
		   (id, supplier_id, hw_class, engine, build_hash, memory_gb, bw_gbps, last_seen_at, version,
		    supported_jobs, supported_models, min_payout_usd_hr, thermal_ok)
		 VALUES ($1,$2,$3,$4,$5,$6,$7, now(), $8,$9,$10,$11,$12)
		 ON CONFLICT (id) DO UPDATE SET
		   hw_class = EXCLUDED.hw_class,
		   engine = EXCLUDED.engine,
		   build_hash = EXCLUDED.build_hash,
		   memory_gb = EXCLUDED.memory_gb,
		   bw_gbps = EXCLUDED.bw_gbps,
		   last_seen_at = now(),
		   version = EXCLUDED.version,
		   supported_jobs = EXCLUDED.supported_jobs,
		   supported_models = EXCLUDED.supported_models,
		   min_payout_usd_hr = EXCLUDED.min_payout_usd_hr,
		   thermal_ok = EXCLUDED.thermal_ok`,
		cap.WorkerID, cap.SupplierID, cap.HWClass, cap.Engine, cap.BuildHash, cap.MemoryGB, cap.MemoryBwGbps, cap.AgentVersion,
		cap.SupportedJobs, cap.SupportedModels, cap.MinPayoutUsdHr, thermalOK,
	)
	if err != nil {
		return err
	}

	for _, b := range cap.Benchmarks {
		_, err = tx.Exec(ctx,
			`INSERT INTO benchmark_results
			   (worker_id, model_id, job_type, tps, eps, thermal_ok, p99_latency_ms, load_ms)
			 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
			cap.WorkerID, b.ModelID, b.JobType, b.TPS, b.EPS, b.ThermalOK, float32(b.P99MS), int64(b.LoadMS),
		)
		if err != nil {
			return err
		}
		// Maintain worker_tps_cache HERE, at the one real worker-state change
		// (Control Plane Hot Path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md
		// "hoist worker_tps into something computed once per worker state change
		// rather than recomputed per candidate row per claim"), instead of
		// ClaimTaskSQL re-deriving "this worker's most recent tps for this
		// job_type" with a correlated subquery over benchmark_results for EVERY
		// eligible candidate row on EVERY claim. UpsertWorker only ever appends
		// benchmark_results newest-first (ORDER BY measured_at DESC in the old
		// subquery), so a plain last-write-wins UPSERT here is equivalent to that
		// "most recent" semantics — the maintained cache and the historical
		// benchmark_results ledger (kept, unchanged, for HistoricalP90-style
		// analysis elsewhere) never disagree about which measurement is newest.
		_, err = tx.Exec(ctx,
			`INSERT INTO worker_tps_cache (worker_id, job_type, tps, updated_at)
			 VALUES ($1,$2,$3, now())
			 ON CONFLICT (worker_id, job_type) DO UPDATE SET
			   tps = EXCLUDED.tps, updated_at = now()`,
			cap.WorkerID, b.JobType, b.TPS,
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

// DeleteOldWorkerMemorySamples prunes rows older than `before` (docs/
// CREED_AND_PATH_TO_TEN.md, "Postgres data lifecycle" 3→4). worker_memory_samples
// appends one row per worker per heartbeat-with-memory-reporting — at 1k workers on
// a 30s heartbeat that is roughly 2.9M rows/day with no bound before this existed.
// Every real read of this table (ListWorkers above, memSampleWindow's admin/capacity
// view, and the quote-risk memory-floor check) only ever looks at the most recent
// memSampleWindow rows per worker, so deleting anything older than a modest
// retention window changes no query's result — this is pure bloat removal, not a
// behavior change. Returns the number of rows actually deleted so the caller (the
// retention ticker) can log real progress, not just "ran".
func (s *Store) DeleteOldWorkerMemorySamples(ctx context.Context, before time.Time) (int64, error) {
	tag, err := s.pool.Exec(ctx, `DELETE FROM worker_memory_samples WHERE created_at < $1`, before)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// DeleteOldTaskDurations prunes rows older than `before` (docs/
// CREED_AND_PATH_TO_TEN.md, "Postgres data lifecycle" 4→5, extending the same fix
// to the other unbounded telemetry table the audit named). task_durations is purely
// internal: HistoricalP90DurationMs and the admin drift rollup are its only readers,
// both aggregate queries with driftMinSamples gating thin history, neither pins to
// specific old rows by id. Both readers ALSO now bound themselves to driftWindow
// (24h, Performance Observability 7→8) independent of this retention window — this
// 30-day window is deliberately wider than driftWindow so retention is never the
// thing silently narrowing what the drift calculation can see; it just bounds how
// long the raw rows survive at all, and can be widened later without any code
// change if the drift/ETA calculation's own window is ever widened past it.
func (s *Store) DeleteOldTaskDurations(ctx context.Context, before time.Time) (int64, error) {
	tag, err := s.pool.Exec(ctx, `DELETE FROM task_durations WHERE created_at < $1`, before)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// DeleteOldJobEvents prunes rows older than `before`. UNLIKE the two telemetry
// tables above, job_events is buyer-VISIBLE history (GET /v1/jobs/{id}/events
// reads it directly, scoped to one job at a time) — a buyer auditing an old
// invoice or dispute may reasonably want to see it months later, so this uses a
// much longer window than task_durations/worker_memory_samples by design; the
// caller (sweepTelemetryRetention) passes a 180-day cutoff, not the 30-day one
// used for pure telemetry.
func (s *Store) DeleteOldJobEvents(ctx context.Context, before time.Time) (int64, error) {
	tag, err := s.pool.Exec(ctx, `DELETE FROM job_events WHERE created_at < $1`, before)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// TelemetryTableCounts returns the live row count of every table
// sweepTelemetryRetention prunes (Postgres Data Lifecycle 8->9), keyed by table
// name — the bloat-ratio half of the retention-health metric: a real count an
// operator can watch for "still climbing despite the sweep running", not just
// inferred from the sweep's own self-reported prune count. Table names here are
// this codebase's own fixed constants (telemetryTables), never request input, so
// the direct interpolation below carries no injection risk.
func (s *Store) TelemetryTableCounts(ctx context.Context) (map[string]int64, error) {
	out := make(map[string]int64, len(telemetryTables))
	for _, table := range telemetryTables {
		var n int64
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM `+table).Scan(&n); err != nil {
			return nil, fmt.Errorf("counting %s: %w", table, err)
		}
		out[table] = n
	}
	return out, nil
}

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

// ReinstateWorker closes the "reinstate after review" half of RUNBOOKS.md's Bad /
// fraudulent worker procedure (Operator Tooling 7->8): until now this was a raw
// `psql -c "UPDATE suppliers SET status='active', quarantined_at=NULL ..."`. Only
// clears quarantine on a supplier that IS quarantined/suspended (never touches an
// already-active supplier, and never reinstates a 'banned' supplier — a ban is a
// harder stop than a quarantine and must stay a deliberate, separate action, not a
// side effect of this endpoint).
func (s *Store) ReinstateWorker(ctx context.Context, workerID uuid.UUID) error {
	ct, err := s.pool.Exec(ctx,
		`UPDATE suppliers SET status = 'active', quarantined_at = NULL
		 WHERE id = (SELECT supplier_id FROM workers WHERE id = $1) AND status = 'suspended'`,
		workerID)
	if err != nil {
		return err
	}
	if ct.RowsAffected() == 0 {
		// Distinguish "no such worker" from "worker exists but isn't suspended" so
		// the handler can 404 vs 409 instead of collapsing both into one error.
		var exists bool
		if qerr := s.pool.QueryRow(ctx, `SELECT true FROM workers WHERE id = $1`, workerID).Scan(&exists); errors.Is(qerr, pgx.ErrNoRows) {
			return errNotFound
		} else if qerr != nil {
			return qerr
		}
		return errNotSuspended
	}
	return nil
}

// errNotSuspended distinguishes "worker exists but its supplier is not currently
// suspended" from errNotFound, so ReinstateWorker's caller can 409 instead of 404.
var errNotSuspended = errors.New("supplier is not suspended")

// AdminAction is one row of the append-only operator-write audit log (Operator
// Tooling 7->8): every admin write endpoint that mutates real operational state
// records one of these in the SAME transaction as the mutation, so "who did what,
// when, and why" never depends on re-deriving it from a diff of table state.
type AdminAction struct {
	ID            uuid.UUID       `json:"id"`
	CreatedAt     time.Time       `json:"created_at"`
	Kind          string          `json:"kind"`
	TaskID        *uuid.UUID      `json:"task_id,omitempty"`
	SupplierID    *uuid.UUID      `json:"supplier_id,omitempty"`
	LedgerEntryID *uuid.UUID      `json:"ledger_entry_id,omitempty"`
	Reason        string          `json:"reason,omitempty"`
	Detail        json.RawMessage `json:"detail,omitempty"`
}

// recordAdminAction inserts one audit row. Called INSIDE the same transaction as
// the mutation it documents (always a *pgxpool.Tx here) so the audit trail can
// never exist for a mutation that rolled back, or vice versa.
func recordAdminAction(ctx context.Context, tx pgx.Tx, kind string, taskID, supplierID, ledgerEntryID *uuid.UUID, reason string, detail any) error {
	var detailJSON []byte
	if detail != nil {
		b, err := json.Marshal(detail)
		if err != nil {
			return err
		}
		detailJSON = b
	}
	_, err := tx.Exec(ctx,
		`INSERT INTO admin_actions (kind, task_id, supplier_id, ledger_entry_id, reason, detail)
		 VALUES ($1,$2,$3,$4,$5,$6)`,
		kind, taskID, supplierID, ledgerEntryID, nullIfEmpty(reason), detailJSON)
	return err
}

// ListAdminActions returns the audit log, newest first, capped at 200 — the
// operator's "what happened and why" review surface for every write action this
// console now performs instead of raw SQL.
func (s *Store) ListAdminActions(ctx context.Context) ([]AdminAction, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, created_at, kind, task_id, supplier_id, ledger_entry_id, COALESCE(reason,''), detail
		 FROM admin_actions ORDER BY created_at DESC LIMIT 200`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []AdminAction
	for rows.Next() {
		var a AdminAction
		if err := rows.Scan(&a.ID, &a.CreatedAt, &a.Kind, &a.TaskID, &a.SupplierID, &a.LedgerEntryID, &a.Reason, &a.Detail); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// AdminForceRequeueTask closes the "Stuck job" runbook's manual fix (Operator
// Tooling 7->8): until now this was a raw
// `psql -c "UPDATE tasks SET status='queued', claimed_by=NULL, visible_at=now() ..."`.
// Scoped to the SAME status set the runbook names (running/retrying) — a queued
// task is already claimable and a complete/failed/cancelled task must never be
// silently resurrected by an operator fat-fingering an id. Records an audit row
// in the SAME transaction, so a requeue can never happen without a trace of who
// forced it and why.
func (s *Store) AdminForceRequeueTask(ctx context.Context, taskID uuid.UUID, reason string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var jobID uuid.UUID
	var prevStatus string
	if err := tx.QueryRow(ctx,
		`UPDATE tasks SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
		                  worker_id = NULL, visible_at = now()
		 WHERE id = $1 AND status IN ('running','retrying')
		 RETURNING job_id, status`,
		taskID,
	).Scan(&jobID, &prevStatus); errors.Is(err, pgx.ErrNoRows) {
		// Distinguish "no such task" from "task exists but isn't in a requeueable
		// state" so the handler can 404 vs 409 instead of collapsing both.
		var exists bool
		if qerr := tx.QueryRow(ctx, `SELECT true FROM tasks WHERE id = $1`, taskID).Scan(&exists); errors.Is(qerr, pgx.ErrNoRows) {
			return errNotFound
		} else if qerr != nil {
			return qerr
		}
		return errNotRequeueable
	} else if err != nil {
		return err
	}

	if err := recordAdminAction(ctx, tx, "task_requeued", &taskID, nil, nil, reason,
		map[string]any{"job_id": jobID}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// errNotRequeueable distinguishes "task exists but is not in a requeueable state
// (running/retrying)" from errNotFound.
var errNotRequeueable = errors.New("task is not in a requeueable state")

// AdminAdjustReputation closes the "manually adjust a supplier's reputation with
// an audit trail" gap named directly in the backlog rung (Operator Tooling 7->8).
// Unlike DockReputation/DockReputationMild (which apply a FIXED delta for a named
// verification event), this is an operator-driven, arbitrary, auditable
// adjustment — e.g. correcting a reputation score after a manual fraud review
// overturns or confirms an automated call. Clamped to [0,1] like every other
// reputation write in this codebase; the audit row records the exact before/after
// values and the operator's stated reason, so "why is this supplier's reputation
// what it is" is always answerable without asking anyone.
func (s *Store) AdminAdjustReputation(ctx context.Context, supplierID uuid.UUID, delta float32, reason string) (before, after float32, err error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback(ctx)

	if err := tx.QueryRow(ctx,
		`SELECT reputation FROM suppliers WHERE id = $1 FOR UPDATE`, supplierID,
	).Scan(&before); errors.Is(err, pgx.ErrNoRows) {
		return 0, 0, errNotFound
	} else if err != nil {
		return 0, 0, err
	}

	after = before + delta
	if after < 0 {
		after = 0
	} else if after > 1 {
		after = 1
	}
	if _, err := tx.Exec(ctx, `UPDATE suppliers SET reputation = $2 WHERE id = $1`, supplierID, after); err != nil {
		return 0, 0, err
	}

	if err := recordAdminAction(ctx, tx, "reputation_adjusted", nil, &supplierID, nil, reason,
		map[string]any{"delta": delta, "before": before, "after": after}); err != nil {
		return 0, 0, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, 0, err
	}
	return before, after, nil
}

// AdminReleasePayoutHold closes the "manually trigger a payout-hold release" gap
// named directly in the backlog rung (Operator Tooling 7->8). Accepts an entry in
// EITHER of the two real "money is owed but not moving" states this codebase
// produces: 'held' (a real hold whose release_at simply hasn't come due yet, or a
// buyer wants it released early) or 'ready' (the honest no-rail-configured stub
// state releasePayouts falls back to on a failed/deferred transfer — and that
// DuePayouts, the real release-worker sweep query, never revisits on its own:
// docs/RUNBOOKS.md's OWN earlier procedure re-set a stuck credit to 'ready' as
// its documented "fix", verified against the real DuePayouts query to silently
// never get retried — the exact bug this endpoint closes for good). Either way
// it (re-)establishes a genuine 'held' row with release_at=now(), so the very
// next sweep cycle picks it up; it never marks the entry 'released' itself (that
// still requires a real transfer reference, enforced structurally by
// ledger_released_requires_ref), so this can never fake a payout.
func (s *Store) AdminReleasePayoutHold(ctx context.Context, entryID uuid.UUID, reason string) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var supplierID uuid.UUID
	if err := tx.QueryRow(ctx,
		`UPDATE ledger_entries SET payout_status = 'held', release_at = now()
		 WHERE id = $1 AND kind = 'supplier_credit' AND payout_status IN ('held','ready')
		 RETURNING supplier_id`,
		entryID,
	).Scan(&supplierID); errors.Is(err, pgx.ErrNoRows) {
		var exists bool
		if qerr := tx.QueryRow(ctx, `SELECT true FROM ledger_entries WHERE id = $1`, entryID).Scan(&exists); errors.Is(qerr, pgx.ErrNoRows) {
			return errNotFound
		} else if qerr != nil {
			return qerr
		}
		return errNotHeld
	} else if err != nil {
		return err
	}

	if err := recordAdminAction(ctx, tx, "payout_released", nil, &supplierID, &entryID, reason, nil); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// errNotHeld distinguishes "ledger entry exists but is not currently held/ready"
// (e.g. already released, pending, or clawed back) from errNotFound.
var errNotHeld = errors.New("ledger entry is not held or ready")

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
	// `j.max_usd IS NOT NULL` to cleanly tell "no cap" from a real $0 cap. Same
	// pattern for firm_quote_max_usd (Project Detection & Quotation 7->8): NULL
	// means "not a firm quote", never a fake $0 ceiling.
	_, err = tx.Exec(ctx,
		`INSERT INTO jobs
		   (id, buyer_id, status, job_type, model_ref, input_ref, output_ref,
		    tier, verification_policy, estimated_usd, actual_usd, task_count, tasks_done,
		    min_memory_gb, hw_classes, data_residency, job_type_spec, split_size,
		    offered_rate_usd_hr, eta_secs, max_usd, budget_state, quote_id, min_reputation, private_pool,
		    deadline_secs, firm_quote, firm_quote_max_usd, sla_guarantee_secs, sla_premium_usd)
		 VALUES ($1,$2,'queued',$3,$4,$5,$6,$7,$8,$9,0,$10,0,
		         $11,$12,$13,$14,$15,$16,$17,$18,'tracking',$19,$20,$21,$22,$23,$24,$25,$26)`,
		j.ID, j.BuyerID, j.JobType, j.ModelRef, j.InputRef, j.OutputRef,
		j.Tier, j.VerificationPolicy, j.EstimatedUSD, j.TaskCount,
		j.MinMemoryGB, nullStrSlice(j.HWClasses), nullStrSlice(j.DataResidency),
		nullJSON(j.JobTypeSpec), j.SplitSize, j.OfferedRateUsdHr, j.ETASecs,
		nullPosFloat(j.MaxUSD), nullUUID(j.QuoteID), j.MinReputation, j.PrivatePool,
		j.DeadlineSecs, j.FirmQuote, nullPosFloat(j.FirmQuoteMaxUSD),
		nullPosInt(j.SLAGuaranteeSecs), nullPosFloat(j.SLAPremiumUSD),
	)
	if err != nil {
		return err
	}

	// PATCH (Control plane hot path 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md
	// "batch large-job inserts via pgx CopyFrom instead of row-by-row insert"):
	// this used to be one round-trip INSERT per task, inside the SAME transaction
	// a large job's buyer-facing submit request waits on — O(task count)
	// sequential round-trips synchronously in the request path, so a
	// multi-thousand-task job paid a multi-thousand-statement transaction before
	// the buyer's POST /v1/jobs ever returned. pgx's CopyFrom streams every row
	// over the wire in the Postgres COPY binary protocol in ONE operation —
	// still inside this same transaction (so a mid-copy failure rolls back the
	// whole job exactly as before; nothing partially lands), but without a
	// round-trip per row. visible_at/status/retry_count were server-side
	// DEFAULTs the old per-row INSERT relied on implicitly; CopyFrom has no
	// DEFAULT-substitution, so they are bound explicitly per row below —
	// status='queued', retry_count=0, and visible_at pinned to ONE now() read up
	// front so every row in the copy shares the identical timestamp a single
	// INSERT statement's now() would have produced.
	if len(tasks) > 0 {
		now := time.Now()
		_, err = tx.CopyFrom(ctx,
			pgx.Identifier{"tasks"},
			[]string{"id", "job_id", "status", "is_honeypot", "is_redundancy", "retry_count",
				"input_ref", "result_key", "chunk_index", "visible_at"},
			pgx.CopyFromSlice(len(tasks), func(i int) ([]any, error) {
				t := tasks[i]
				return []any{t.ID, t.JobID, "queued", t.IsHoneypot, t.IsRedundancy, int16(0),
					t.InputRef, t.ResultKey, t.ChunkIndex, now}, nil
			}),
		)
		if err != nil {
			return fmt.Errorf("copy tasks: %w", err)
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
	DeadlineSecs       int       // watchdog policy: -1 opt out, 0 default, 60..604800 explicit wall-clock deadline
	// FirmQuote / FirmQuoteMaxUSD: the firm-quote tier (Project Detection &
	// Quotation 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md). When FirmQuote is
	// true, FirmQuoteMaxUSD is the real ceiling the buyer's eventual charge is
	// capped at (JobChargeInfo), with any overage absorbed by the platform.
	FirmQuote       bool
	FirmQuoteMaxUSD float64
	// SLAGuaranteeSecs / SLAPremiumUSD: the wall-clock speed-SLA binding (Speed
	// Lane wave 2A). When > 0 the job's results are guaranteed merged within
	// SLAGuaranteeSecs of created_at; a miss auto-refunds SLAPremiumUSD via an
	// sla_refund ledger row (collect.go settleSLAOutcome). 0 = no SLA → NULL.
	SLAGuaranteeSecs int
	SLAPremiumUSD    float64
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
	ChargeStatus string
	Verification Verification
	// ResultsMergedAt is the results-merge watermark: nil until the buyer-ready
	// artifact has actually been merged at least once (Data Transfer & Artifact
	// I/O 4.5->5, "Stop paying for every poll twice"). handleJobResults skips
	// re-merging on read once this is set.
	ResultsMergedAt *time.Time
	// SLAGuaranteeSecs / SLAPremiumUSD / SLAMet: the wall-clock speed-SLA (wave
	// 2A). Guarantee 0 = no SLA bound. SLAMet is nil until the outcome is decided
	// at finalize (or forever, for a job with no SLA); true = met, false = missed
	// (an sla_refund ledger credit for the premium was recorded).
	SLAGuaranteeSecs int
	SLAPremiumUSD    float64
	SLAMet           *bool
}

// GetJob loads a job scoped to a buyer (buyers see only their own jobs).
func (s *Store) GetJob(ctx context.Context, jobID, buyerID uuid.UUID) (*JobView, error) {
	var j JobView
	err := s.pool.QueryRow(ctx,
		`SELECT id, buyer_id, status, job_type, tier, COALESCE(output_ref,''),
		        COALESCE(task_count,0), COALESCE(tasks_done,0),
		        COALESCE(estimated_usd,0), COALESCE(actual_usd,0),
		        COALESCE(eta_secs,0), created_at,
		        COALESCE(max_usd,0), COALESCE(budget_state,'tracking'),
		        COALESCE(charge_status,'not_attempted'), results_merged_at,
		        COALESCE(sla_guarantee_secs,0), COALESCE(sla_premium_usd,0)::float8, sla_met
		 FROM jobs WHERE id = $1 AND buyer_id = $2`,
		jobID, buyerID,
	).Scan(&j.ID, &j.BuyerID, &j.Status, &j.JobType, &j.Tier, &j.OutputRef,
		&j.TaskCount, &j.TasksDone, &j.EstimatedUSD, &j.ActualUSD, &j.ETASecs, &j.CreatedAt,
		&j.MaxUSD, &j.BudgetState, &j.ChargeStatus, &j.ResultsMergedAt,
		&j.SLAGuaranteeSecs, &j.SLAPremiumUSD, &j.SLAMet)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	// Assemble the verification receipt (counts from the append-only log + latest
	// dispute). A read failure here must not hide the job: log and leave the
	// zero-value aggregate (label "unverified"), never fabricate counts.
	vr, verr := s.JobVerification(ctx, j.ID)
	if verr != nil {
		log.Printf("job verification aggregate (job %s): %v", j.ID, verr)
		vr.Label = deriveVerificationLabel(vr)
	}
	j.Verification = vr
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
	// engine + buildHash are the finer verification-class axes of the COMMITTING
	// worker (alongside HWClass). The verifier uses the full (hw_class, engine,
	// build_hash) class to decide whether a byte-exact redundancy/honeypot
	// comparison is even meaningful: a pure byte mismatch ACROSS the class boundary
	// is not an auto-dock (two engines / two builds legitimately differ in bytes on
	// identical hardware) — it falls back to provisional trust, mirroring the
	// missing-third-worker pattern. Unexported: internal to verification.
	engine      string
	buildHash   string
	jobType     string // parent job's job_type, for honeypot answer lookup
	InputRef    string // this task's input chunk key (honeypot answer lookup)
	ResultKey   string // canonical server-side result key (verification fetch)
	ModelRef    string // parent job's model_ref (tiebreak peer selection)
	MinMemoryGB float32
	ChunkIndex  int // this task's chunk position (tiebreak pairing + N-way vote)
	// peerSupplierID is the redundancy peer's supplier, set by the commit handler
	// when a sibling result exists. Used only by the no-object-store verification
	// fallback so a 2-blob disagreement docks the RIGHT supplier (uuid.Nil = unknown).
	peerSupplierID uuid.UUID
	// peerEngine + peerBuildHash are the redundancy peer's finer verification class,
	// also set by the commit handler, so the no-object-store fallback can tell a
	// same-class byte mismatch (a real defect) from a cross-class one (provisional
	// trust). Blank = unknown peer class → a byte-exact pair is non-comparable.
	peerEngine    string
	peerBuildHash string
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

	// Control Plane Hot Path 8->9 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Get
	// result-commit off the S3 critical path"): validate the worker-supplied
	// SHA-256 shape before persisting it — a malformed value (wrong length,
	// non-hex) is stored as SQL NULL (never trusted downstream) rather than an
	// unusable garbage string a later hash-compare would silently never match.
	resultSHA256 := nullSHA256Hex(c.ResultSHA256)

	ct, err := tx.Exec(ctx,
		`UPDATE tasks
		   SET status = 'complete', completed_at = now(),
		       result_ref = COALESCE(NULLIF(result_key,''), $3),
		       worker_id = $2, result_sha256 = $4
		 WHERE id = $1 AND claimed_by = $2 AND status IN ('running','queued')`,
		taskID, workerID, c.ResultKey, resultSHA256)
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
		        w.supplier_id, COALESCE(w.hw_class,''),
		        COALESCE(w.engine,''), COALESCE(w.build_hash,''), j.job_type,
		        COALESCE(j.model_ref,''), COALESCE(j.min_memory_gb,0),
		        COALESCE(t.chunk_index,0), COALESCE(j.split_size,0)
		 FROM tasks t
		   JOIN workers w ON w.id = $2
		   JOIN jobs j ON j.id = t.job_id
		 WHERE t.id = $1`,
		taskID, workerID, c.ResultKey,
	).Scan(&info.JobID, &info.IsHoneypot, &info.IsRedundancy, &info.InputRef,
		&info.ResultKey, &info.SupplierID, &info.HWClass, &info.engine, &info.buildHash, &info.jobType,
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

	// Maintain the supplier's lifetime completed-task count HERE, at the one
	// real commit, instead of ClaimTask re-deriving it with a `count(*)` scan on
	// every single claim (Control Plane Hot Path 7->8,
	// docs/internal/CREED_AND_PATH_TO_TEN.md). Scoped inside this same
	// transaction: a commit that rolls back for any reason never leaves a
	// phantom increment behind.
	_, err = tx.Exec(ctx,
		`UPDATE suppliers SET completed_tasks = completed_tasks + 1 WHERE id = $1`, info.SupplierID)
	if err != nil {
		return nil, err
	}

	// Quote-to-actual drift feedback (Plane D D6 / errata C-Errata-6): record the
	// REAL committed-task wall-time so the Exchange Brain can learn an observed p90
	// the next quote's ETA leans on. This lives INSIDE the commit transaction so a
	// duration is recorded if and only if the task truly committed — a failed or
	// malformed task takes the fail path (failJobAndSettleOnce), never this one, so
	// it can never poison the estimate. duration_ms is the worker's reported value;
	// we store it verbatim (real telemetry, never fabricated).
	_, err = tx.Exec(ctx,
		`INSERT INTO task_durations (job_id, job_type, model_ref, split_size, duration_ms, worker_id, engine, build_hash)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		info.JobID, info.jobType, info.ModelRef, splitSize, int64(c.DurationMS),
		info.WorkerID, info.engine, info.buildHash)
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

// driftWindow bounds both HistoricalP90DurationMs and DriftRollup to recent history
// (Performance Observability 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md: "the
// historical p90 duration calculation... aggregates all-time history with no time
// window, so detection latency actually grows worse as the table grows"). Before
// this, a build shipped a month ago and a build shipped an hour ago were blended
// into the SAME p90 for as long as task_durations kept both rows — a fresh
// regression got diluted in direct proportion to how much older, healthy history
// sat in the same table, and that dilution could only get worse over time as more
// old rows accumulated. 24h matches the rung's own stated example window and is
// wide enough to still clear driftMinSamples on a realistically busy job_type
// while being narrow enough that a regression introduced this hour dominates the
// window instead of being averaged away by last month.
const driftWindow = 24 * time.Hour

// HistoricalP90DurationMs returns the observed 90th-percentile committed-task
// duration (ms) for a (job_type, model_ref) over the last driftWindow, and how
// many samples backed it. It uses percentile_disc(0.9) (a real recorded value, not
// an interpolation) over task_durations, filtered to created_at > now() -
// driftWindow so old, no-longer-representative history cannot dilute a recent
// regression. When fewer than driftMinSamples rows exist IN THE WINDOW the history
// is too thin to trust, so it returns (0, n) and the caller falls back to the
// static target — the quote NEVER invents an ETA from one lucky sample, and it
// never reaches past the window to manufacture samples either. A model_ref of ""
// matches rows regardless of model (job-type-only history).
func (s *Store) HistoricalP90DurationMs(ctx context.Context, jobType, modelRef string) (p90ms int64, samples int, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COUNT(*),
		        COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY duration_ms), 0)
		   FROM task_durations
		  WHERE job_type = $1
		    AND ($2 = '' OR model_ref = $2)
		    AND created_at > now() - make_interval(secs => $3)`,
		jobType, modelRef, int(driftWindow.Seconds()),
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
	Samples          int     `json:"samples"`             // committed-task durations recorded IN THE WINDOW (see WindowHours)
	AvgDurationMs    float64 `json:"avg_duration_ms"`     // mean actual per-task wall-time, windowed
	P90DurationMs    int64   `json:"p90_duration_ms"`     // observed p90 (what the ETA learns from), windowed
	AvgQuotedETASecs float64 `json:"avg_quoted_eta_secs"` // mean quoted whole-job eta_secs (reused jobs.eta_secs), windowed
	UsingObservedP90 bool    `json:"using_observed_p90"`  // true once samples >= the trust floor
	WindowHours      float64 `json:"window_hours"`        // the rolling window every figure above is bounded to (driftWindow) — named explicitly so a reader never mistakes this for all-time history
}

// DriftRollup returns the quoted-vs-actual rollup per (job_type, model_ref) for the
// admin drift surface, over the last driftWindow (Performance Observability 7->8:
// "the historical p90 duration calculation... aggregates all-time history with no
// time window, so detection latency actually grows worse as the table grows").
// Actuals (count, avg, p90) come from task_durations, filtered to created_at > now()
// - driftWindow; the quoted side is the average jobs.eta_secs over the SAME windowed
// set of jobs (LEFT JOIN so a duration whose job row was pruned still counts its
// actual side honestly rather than vanishing). Ordered by sample volume so the
// thickest recent history — the rows whose observed p90 is actually steering
// quotes right now — sorts first. A (job_type, model_ref) with zero rows in the
// window simply does not appear (it never did pre-window either — COUNT(*) was
// always >= 1 to produce a GROUP BY row), so an operator reading a shrunken table
// after a quiet period is expected behavior, not a bug.
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
		  WHERE td.created_at > now() - make_interval(secs => $1)
		  GROUP BY td.job_type, td.model_ref
		  ORDER BY COUNT(*) DESC, td.job_type, td.model_ref`,
		int(driftWindow.Seconds()))
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
		d.WindowHours = driftWindow.Hours()
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

// DockReputationMild applies an event's delta (the same pure reputationDelta the
// full DockReputation uses) clamped to [0, 1], WITHOUT the quarantine/ban side
// effects. Used by the dead-claim rescue: a machine that went silent mid-claim is
// a soft reliability signal (mildest dock), not fraud — a repeat offender's score
// still erodes toward the claim filter's reputation gates, but this path NEVER
// suspends or bans on its own.
func (s *Store) DockReputationMild(ctx context.Context, supplierID uuid.UUID, event ReputationEvent) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE suppliers
		   SET reputation = GREATEST(0.0, LEAST(1.0, reputation + $2))
		 WHERE id = $1`,
		supplierID, reputationDelta(event))
	return err
}

// Verification-requeue backoff + worker-exclusion tuning (Scheduling & Matching
// Engine 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md). A task that just FAILED
// verification (a bad honeypot answer) should not immediately return to the same
// worker with no delay: it costs the worker a re-dispatch it will likely fail
// again, and it costs the job wall-clock. So RequeueTask now (a) pushes visible_at
// out by an exponential-by-retry backoff, and (b) records the worker that just
// failed it in excluded_worker/excluded_until so the claim query prefers a
// DIFFERENT worker for the exclusion window. The exclusion deliberately EXPIRES
// (excluded_until = the backoff instant + a grace window): on a thin or
// single-worker fleet the retry is delayed and de-prioritized-away-from-the-failer,
// never permanently starved.
const (
	requeueBackoffBase = 30 * time.Second // first requeue delay; doubles per prior retry
	requeueBackoffCap  = 10 * time.Minute // ceiling so a high retry_count can't push a task far out
	// Grace window the failed worker stays excluded AFTER the task becomes visible,
	// so another worker gets first crack once the backoff elapses instead of the
	// failer racing back in the same instant. Bounded — see the comment above.
	requeueExclusionGrace = 2 * time.Minute
)

// requeueBackoff returns the visibility delay for a task being requeued after a
// failed verification, given how many times it has already been retried. Exponential
// (base << priorRetries) with a hard cap, mirroring the stale-task requeue ladder's
// shape (workers.go) so the two retry paths behave consistently.
func requeueBackoff(priorRetries int) time.Duration {
	if priorRetries < 0 {
		priorRetries = 0
	}
	// Cap the shift so the << can't overflow the duration on a pathological retry_count.
	shift := priorRetries
	if shift > 20 {
		shift = 20
	}
	d := requeueBackoffBase << uint(shift)
	if d <= 0 || d > requeueBackoffCap {
		return requeueBackoffCap
	}
	return d
}

// RequeueTask resets a failed-verification task to retrying and bumps the retry
// counter so the scheduler hands it to a DIFFERENT worker after a delay.
//
// PATCH (Scheduling & Matching Engine 8->9, docs/internal/CREED_AND_PATH_TO_TEN.md
// "add backoff plus worker-exclusion to verification-requeue"): this used to reset
// visible_at to now() (immediately reclaimable) and clear worker_id, so the exact
// worker that just failed the task's honeypot could reclaim it on its very next poll
// with zero delay — burning another dispatch on a machine that just proved it gets
// this task wrong. Now the task is pushed out by an exponential-per-retry backoff
// AND the just-failed worker is recorded in excluded_worker/excluded_until (read the
// worker off the row itself — CommitTask leaves worker_id/claimed_by set to the
// committing worker, so no caller change is needed), so the claim query prefers a
// different worker for the window. The exclusion expires (bounded), never starving a
// thin fleet's retry. Selection-affecting only — nothing about a task's bytes or the
// job's eventual result changes.
func (s *Store) RequeueTask(ctx context.Context, taskID uuid.UUID) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// Read the worker that just held this task (still set post-commit) and its retry
	// count, so the backoff scales and the failer is the one we exclude. FOR UPDATE
	// pins the row against a concurrent requeue/claim while we compute the delay.
	var failedWorker *uuid.UUID
	var priorRetries int
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(worker_id, claimed_by), retry_count FROM tasks WHERE id = $1 FOR UPDATE`,
		taskID,
	).Scan(&failedWorker, &priorRetries); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errNotFound
		}
		return err
	}
	backoff := requeueBackoff(priorRetries)

	if _, err := tx.Exec(ctx,
		`UPDATE tasks
		   SET status = 'retrying', claimed_by = NULL, claimed_at = NULL,
		       worker_id = NULL, retry_count = retry_count + 1,
		       visible_at = now() + make_interval(secs => $2),
		       excluded_worker = $3,
		       excluded_until  = now() + make_interval(secs => $4)
		 WHERE id = $1`,
		taskID, backoff.Seconds(), failedWorker,
		(backoff + requeueExclusionGrace).Seconds(),
	); err != nil {
		return err
	}
	return tx.Commit(ctx)
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

// GetHoneypotAnswer returns the known answer bytes for a honeypot input ref (if one
// exists for this job type) plus the verification class the answer was produced under
// ("engine|build_hash", or "" = unknown/class-blind). The verifier uses answerClass to
// decide whether a BYTE-EXACT honeypot mismatch is grounds to auto-quarantine: only
// when the committing worker shares the answer's class. A "" class means the answer is
// class-blind and never auto-quarantines a byte-exact job (the safe default).
func (s *Store) GetHoneypotAnswer(ctx context.Context, jobType, inputRef string) (answer []byte, answerClass string, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT known_answer, COALESCE(answer_class,'') FROM honeypots
		 WHERE job_type = $1 AND input_ref = $2 LIMIT 1`,
		jobType, inputRef,
	).Scan(&answer, &answerClass)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, "", errNotFound
	}
	return answer, answerClass, err
}

// InsertHoneypot seeds a honeypot probe idempotently, REFUSING a blank-class byte-exact
// write (validateHoneypotSeed / item 11) so the seed/admin path fails safely rather than
// writing dead or dangerous coverage. answerClass is the "engine|build_hash" of the worker
// that produced knownAnswer; tolerant job types may pass "" (class-blind is safe for them).
func (s *Store) InsertHoneypot(ctx context.Context, jobType, inputRef string, knownAnswer []byte, answerClass string) error {
	if err := validateHoneypotSeed(jobType, answerClass); err != nil {
		return err
	}
	_, err := s.pool.Exec(ctx,
		`INSERT INTO honeypots (job_type, input_ref, known_answer, answer_class)
		 SELECT $1, $2, $3, $4
		 WHERE NOT EXISTS (SELECT 1 FROM honeypots WHERE job_type=$1 AND input_ref=$2)`,
		jobType, inputRef, knownAnswer, answerClass)
	return err
}

// PeerResultKey finds a completed sibling task that ran the SAME input chunk for
// the same job as the given task but on a DIFFERENT worker, returning its result
// key for a within-class redundancy comparison plus the peer worker's finer
// verification class (engine + build_hash). The pairing is by shared input_ref
// (primary + its redundancy clone carry the same chunk). The class lets the
// verifier's no-object-store fallback decide whether a byte-exact disagreement is
// same-class (a real mismatch) or cross-class (provisional trust, not a dock).
// Returns errNotFound when no committed peer exists yet (the common case — the peer
// may not have finished, so verification simply has nothing to compare against).
// peerSHA256 is the peer's stored tasks.result_sha256 (Control Plane Hot Path
// 8->9): "" when the peer committed before this column existed, or with an
// agent build that never reported one — the caller always falls back to a real
// GetObject in that case, so an absent hash never changes correctness.
func (s *Store) PeerResultKey(ctx context.Context, taskID uuid.UUID) (peerResult string, peerSupplier uuid.UUID, peerEngine, peerBuildHash, peerSHA256 string, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(p.result_ref,''), w.supplier_id,
		        COALESCE(w.engine,''), COALESCE(w.build_hash,''), COALESCE(p.result_sha256,'')
		 FROM tasks t
		   JOIN tasks p ON p.job_id = t.job_id AND p.input_ref = t.input_ref
		                AND p.id <> t.id AND p.status = 'complete'
		                AND p.result_ref IS NOT NULL AND p.result_ref <> ''
		   JOIN workers w ON w.id = p.worker_id
		 WHERE t.id = $1
		 ORDER BY p.completed_at ASC
		 LIMIT 1`,
		taskID,
	).Scan(&peerResult, &peerSupplier, &peerEngine, &peerBuildHash, &peerSHA256)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", uuid.Nil, "", "", "", errNotFound
	}
	return peerResult, peerSupplier, peerEngine, peerBuildHash, peerSHA256, err
}

// ChunkResult is one committed result for a chunk: its result key plus the
// worker + supplier that produced it (so a majority vote can credit the winner
// and dock the losers by supplier).
type ChunkResult struct {
	TaskID     uuid.UUID
	WorkerID   uuid.UUID
	SupplierID uuid.UUID
	ResultRef  string
	// Engine + BuildHash are the finer verification-class axes of the worker behind
	// this result, so the N-way vote can tell whether two byte-exact results are even
	// in the same (hw_class, engine, build_hash) class before docking on a mismatch.
	Engine    string
	BuildHash string
}

// ChunkResults returns every committed result for a job's chunk (the primary and
// all its redundancy/tiebreak clones share input_ref + chunk_index), each with
// its worker + supplier. The 3-way tiebreak vote (Verification V2) gathers these
// once a tiebreak commits and does a real majorityVote over them.
func (s *Store) ChunkResults(ctx context.Context, jobID uuid.UUID, chunkIndex int) ([]ChunkResult, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.worker_id, w.supplier_id, t.result_ref,
		        COALESCE(w.engine,''), COALESCE(w.build_hash,'')
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
		if err := rows.Scan(&cr.TaskID, &cr.WorkerID, &cr.SupplierID, &cr.ResultRef,
			&cr.Engine, &cr.BuildHash); err != nil {
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
// = all positive supplier credits ever. LastPayout*/NextPayoutAt are Supplier
// onboarding & safety 7->8's real payout proof for the menu-bar trust panel:
// last = the most recent row whose hold has actually expired (payout_status IN
// ('released','ready') — 'ready' is the honest "owed, queued, no rail yet" state
// releasePayouts uses when the stub Payout errs, and its release_at is just as
// real as a fully 'released' row's), keyed by release_at (the real timestamp
// splitCharge computed as now()+holdSecs, and the exact instant releasePayouts
// acts on — never a fabricated "paid at" column this table doesn't have); next =
// the soonest still-'held' row's release_at. Both nil when there is no such row
// (a fresh or never-paid supplier), never a fabricated zero time.
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
	if err != nil {
		return e, err
	}

	var lastAmt float64
	var lastAt time.Time
	err = s.pool.QueryRow(ctx,
		`SELECT amount_usd, release_at FROM ledger_entries
		  WHERE supplier_id = $1 AND kind = 'supplier_credit'
		    AND payout_status IN ('released','ready') AND release_at IS NOT NULL
		  ORDER BY release_at DESC LIMIT 1`,
		supplierID,
	).Scan(&lastAmt, &lastAt)
	switch {
	case err == nil:
		e.LastPayoutUSD = &lastAmt
		t := lastAt.Unix()
		e.LastPayoutAt = &t
	case errors.Is(err, pgx.ErrNoRows):
		// No released/ready credit yet — leave both nil, not zero.
	default:
		return e, err
	}

	var nextAt time.Time
	err = s.pool.QueryRow(ctx,
		`SELECT release_at FROM ledger_entries
		  WHERE supplier_id = $1 AND kind = 'supplier_credit'
		    AND payout_status = 'held' AND release_at IS NOT NULL
		  ORDER BY release_at ASC LIMIT 1`,
		supplierID,
	).Scan(&nextAt)
	switch {
	case err == nil:
		t := nextAt.Unix()
		e.NextPayoutAt = &t
	case errors.Is(err, pgx.ErrNoRows):
		// Nothing currently held — no scheduled next payout, not a fabricated one.
	default:
		return e, err
	}
	return e, nil
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

// PendingWebhooks returns webhooks whose job has reached a terminal state
// (complete, failed, or cancelled — a watchdog stuck-cancel is a terminal outcome
// the buyer registered to hear about) but that have not yet been delivered
// (delivered_at IS NULL — the single flag that governs exactly-once firing).
// Job-scoped webhooks only — a webhook without a job_id is a buyer catch-all with
// no single terminal event to fire.
func (s *Store) PendingWebhooks(ctx context.Context, limit int) ([]PendingWebhook, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT wh.id, wh.job_id, wh.url, j.status
		 FROM webhooks wh JOIN jobs j ON j.id = wh.job_id
		 WHERE wh.delivered_at IS NULL
		   AND wh.job_id IS NOT NULL
		   AND j.status IN ('complete','failed','cancelled')
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
// SetChargeStatus records the outcome of a job's off-session buyer charge
// (not_attempted|charged|failed|no_payment_method) so charging state is queryable
// rather than log-only. Best-effort: a failure here never blocks the lifecycle.
func (s *Store) SetChargeStatus(ctx context.Context, jobID uuid.UUID, status string) error {
	_, err := s.pool.Exec(ctx, `UPDATE jobs SET charge_status = $2 WHERE id = $1`, jobID, status)
	return err
}

// RecordVerificationEvent appends one row to the append-only verification_events
// receipt log. It is BEST-EFFORT and must NEVER block the verify/money path: a write
// failure is logged and swallowed (return nil) so a flaky receipt insert can never
// fail a reputation dock or a payout. kind is one of the closed set
// {honeypot_pass|honeypot_fail|redundancy_match|redundancy_mismatch|tiebreak_win|
// tiebreak_loss}; taskID/supplierID may be uuid.Nil when not known, stored as NULL.
func (s *Store) RecordVerificationEvent(ctx context.Context, jobID, taskID, supplierID uuid.UUID, kind string) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO verification_events (job_id, task_id, supplier_id, kind) VALUES ($1,$2,$3,$4)`,
		jobID, nullUUID(taskID), nullUUID(supplierID), kind)
	if err != nil {
		log.Printf("verification event (job %s kind %s): %v", jobID, kind, err)
	}
	return nil
}

// JobVerification aggregates a job's verification_events log into the buyer-facing
// receipt block, plus the latest dispute's status (disputes table; ” when none).
// Counts come from a single grouped query; only outcomes that actually occurred are
// present, so the aggregate never overstates what was checked. `checked` is every
// task that underwent ANY verification (the sum of all event kinds). The honest label
// is derived from the counts by deriveVerificationLabel.
func (s *Store) JobVerification(ctx context.Context, jobID uuid.UUID) (Verification, error) {
	var v Verification
	rows, err := s.pool.Query(ctx,
		`SELECT kind, count(*) FROM verification_events WHERE job_id = $1 GROUP BY kind`, jobID)
	if err != nil {
		return v, err
	}
	defer rows.Close()
	for rows.Next() {
		var kind string
		var n int
		if err := rows.Scan(&kind, &n); err != nil {
			return v, err
		}
		switch kind {
		case "honeypot_pass":
			v.HoneypotsPassed += n
			v.Checked += n
		case "honeypot_fail":
			v.HoneypotsFailed += n
			v.Checked += n
		case "redundancy_match":
			v.RedundancyMatched += n
			v.Checked += n
		case "redundancy_mismatch":
			v.RedundancyMismatched += n
			v.Checked += n
		case "tiebreak_win", "tiebreak_loss":
			v.Tiebreaks += n
			v.Checked += n
		case "redundancy_same_supplier":
			// A same-supplier "peer" — NOT an independent cross-check (items 7, 9).
			// Surfaced as its own count and deliberately NOT added to Checked, so the
			// receipt can say "no-independent-peer" rather than "verified".
			v.SameSupplier += n
		case "redundancy_cross_class", "tiebreak_cross_class":
			// Cross-class coverage gap: the chunk had a redundant peer but in a
			// DIFFERENT verification class, so a byte-exact comparison could not be
			// performed. Surfaced as CrossClassSkipped for the receipt (item 9) but NOT
			// counted as "checked" — counting an uncheckable comparison as verified
			// would overstate the receipt (BLACKHOLE: surface the gap, never inflate).
			v.CrossClassSkipped += n
		}
	}
	if err := rows.Err(); err != nil {
		return v, err
	}
	// Latest dispute for the job ('' when none). A no-row scan is the normal "no
	// dispute" case, not an error.
	var disputeStatus string
	err = s.pool.QueryRow(ctx,
		`SELECT status FROM disputes WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1`, jobID,
	).Scan(&disputeStatus)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return v, err
	}
	v.DisputeStatus = disputeStatus
	v.Label = deriveVerificationLabel(v)
	return v, nil
}

// SupplierVerification aggregates a SUPPLIER's own verification_events log — across
// every job they have ever worked, not one job — into the trust-panel receipt
// (Supplier onboarding & safety 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md:
// "Populate the trust panel with real data"). Only honeypot outcomes are counted
// (the trust panel's own vocabulary — payouts_configured/connected/enabled and
// honeypots_passed/failed/verification_label); redundancy/tiebreak counts stay
// job-scoped via JobVerification. Reuses deriveVerificationLabel so the derived
// label means exactly the same thing here as it does on a job receipt.
func (s *Store) SupplierVerification(ctx context.Context, supplierID uuid.UUID) (SupplierVerification, error) {
	var sv SupplierVerification
	rows, err := s.pool.Query(ctx,
		`SELECT kind, count(*) FROM verification_events
		  WHERE supplier_id = $1 AND kind IN ('honeypot_pass','honeypot_fail')
		  GROUP BY kind`, supplierID)
	if err != nil {
		return sv, err
	}
	defer rows.Close()
	var v Verification
	for rows.Next() {
		var kind string
		var n int
		if err := rows.Scan(&kind, &n); err != nil {
			return sv, err
		}
		switch kind {
		case "honeypot_pass":
			sv.HoneypotsPassed = n
			v.HoneypotsPassed = n
			v.Checked += n
		case "honeypot_fail":
			sv.HoneypotsFailed = n
			v.HoneypotsFailed = n
			v.Checked += n
		}
	}
	if err := rows.Err(); err != nil {
		return sv, err
	}
	sv.Label = deriveVerificationLabel(v)
	return sv, nil
}

// JobVerificationClasses returns the DISTINCT verification classes ("engine|build_hash")
// of the workers that produced this job's completed (non-honeypot) results — the
// "cleared under" provenance for the ClearingReceipt (items 13, 15). A blank class
// (unknown build) maps to "" via classKey. Read-only.
func (s *Store) JobVerificationClasses(ctx context.Context, jobID uuid.UUID) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT DISTINCT COALESCE(w.engine,''), COALESCE(w.build_hash,'')
		 FROM tasks t JOIN workers w ON w.id = t.worker_id
		 WHERE t.job_id = $1 AND t.status = 'complete' AND t.is_honeypot = false`,
		jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var engine, build string
		if err := rows.Scan(&engine, &build); err != nil {
			return nil, err
		}
		out = append(out, classKey(engine, build))
	}
	return out, rows.Err()
}

// JobTaskReceipts returns the per-task verification drilldown for a job (item 15): each
// task's chunk, status, honeypot flag, worker verification class, and its latest
// comparison event kind. It NEVER selects the honeypot known_answer, so the drilldown
// cannot leak the hidden probe answer. Read-only.
func (s *Store) JobTaskReceipts(ctx context.Context, jobID uuid.UUID) ([]TaskReceipt, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT COALESCE(t.chunk_index,0), t.status, t.is_honeypot,
		        COALESCE(w.engine,''), COALESCE(w.build_hash,''),
		        COALESCE((SELECT ve.kind FROM verification_events ve
		                  WHERE ve.task_id = t.id ORDER BY ve.created_at DESC LIMIT 1), '')
		 FROM tasks t LEFT JOIN workers w ON w.id = t.worker_id
		 WHERE t.job_id = $1
		 ORDER BY COALESCE(t.chunk_index,0), t.id`,
		jobID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TaskReceipt
	for rows.Next() {
		var (
			chunk         int
			status        string
			isHoneypot    bool
			engine, build string
			kind          string
		)
		if err := rows.Scan(&chunk, &status, &isHoneypot, &engine, &build, &kind); err != nil {
			return nil, err
		}
		out = append(out, taskReceiptRow(chunk, status, isHoneypot, engine, build, kind))
	}
	return out, rows.Err()
}

// RecordDispute records a buyer's dispute against a job's result, atomically verifying
// the job belongs to that buyer (the INSERT...SELECT yields no row — errNotFound — when
// it does not, so a buyer can never dispute another's job). Returns the new dispute id.
// This is the foundation primitive for optimistic-verification recompute + the payout
// guarantee; resolution (operator bisection / tolerance-aware FP) is the frontier seam.
func (s *Store) RecordDispute(ctx context.Context, jobID, buyerID uuid.UUID, reason string) (uuid.UUID, error) {
	var id uuid.UUID
	err := s.pool.QueryRow(ctx,
		`INSERT INTO disputes (job_id, buyer_id, reason)
		 SELECT $1, $2, $3 WHERE EXISTS (SELECT 1 FROM jobs WHERE id = $1 AND buyer_id = $2)
		 RETURNING id`,
		jobID, buyerID, reason).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, errNotFound
	}
	return id, err
}

// DisputeRow is an unresolved dispute the resolver works on.
type DisputeRow struct {
	ID, JobID uuid.UUID
	Status    string
}

// ActiveDisputes returns disputes still needing resolution: 'open'/'no_peer' need a
// re-verify dispatched; 'reverifying' awaits the independent re-run's objective verdict.
func (s *Store) ActiveDisputes(ctx context.Context, limit int) ([]DisputeRow, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT id, job_id, status FROM disputes
		  WHERE status IN ('open','no_peer','reverifying') ORDER BY created_at LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DisputeRow
	for rows.Next() {
		var d DisputeRow
		if err := rows.Scan(&d.ID, &d.JobID, &d.Status); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// ReverifyTargetRow is the disputed job's primary completed task + the routing facts a
// re-verification peer needs.
type ReverifyTargetRow struct {
	TaskID, AnchorWorker uuid.UUID
	JobType, ModelRef    string
	InputRef             string
	MinMemGB             float32
	ChunkIndex           int
}

// ReverifyTarget returns the disputed job's primary completed task (chunk 0, not a
// redundancy/honeypot) to independently re-run. ok=false when the job has no such
// completed task to re-verify.
func (s *Store) ReverifyTarget(ctx context.Context, jobID uuid.UUID) (ReverifyTargetRow, bool, error) {
	var t ReverifyTargetRow
	err := s.pool.QueryRow(ctx,
		`SELECT tk.id, tk.worker_id, j.job_type, COALESCE(j.model_ref,''),
		        tk.input_ref, COALESCE(j.min_memory_gb,0), tk.chunk_index
		   FROM tasks tk JOIN jobs j ON j.id = tk.job_id
		  WHERE tk.job_id = $1 AND tk.status = 'complete'
		        AND COALESCE(tk.is_redundancy,false) = false
		        AND COALESCE(tk.is_honeypot,false) = false
		        AND tk.worker_id IS NOT NULL
		  ORDER BY tk.chunk_index LIMIT 1`, jobID).
		Scan(&t.TaskID, &t.AnchorWorker, &t.JobType, &t.ModelRef, &t.InputRef, &t.MinMemGB, &t.ChunkIndex)
	if errors.Is(err, pgx.ErrNoRows) {
		return t, false, nil
	}
	if err != nil {
		return t, false, err
	}
	return t, true, nil
}

// SetDisputeReverifying records the dispatched re-verify task and flips to 'reverifying'.
func (s *Store) SetDisputeReverifying(ctx context.Context, id, reverifyTaskID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE disputes SET status = 'reverifying', reverify_task_id = $2 WHERE id = $1`,
		id, reverifyTaskID)
	return err
}

// SetDisputeStatus updates a dispute's status (stamping resolved_at on a terminal one).
func (s *Store) SetDisputeStatus(ctx context.Context, id uuid.UUID, status string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE disputes SET status = $2,
		        resolved_at = CASE WHEN $2 IN ('resolved','rejected') THEN now() ELSE resolved_at END
		  WHERE id = $1`, id, status)
	return err
}

// JobHasPendingTasks reports whether a job still has queued/running tasks — the resolver
// waits on this so a re-verify (and any cascaded tiebreak) fully settles before verdict.
func (s *Store) JobHasPendingTasks(ctx context.Context, jobID uuid.UUID) (bool, error) {
	var n int
	err := s.pool.QueryRow(ctx,
		`SELECT count(*) FROM tasks WHERE job_id = $1 AND status IN ('queued','retrying','running')`,
		jobID).Scan(&n)
	return n > 0, err
}

// TaskHasClawback reports whether a confirmed-bad clawback was recorded against a task —
// the OBJECTIVE verdict signal for a dispute (the original result was wrong).
func (s *Store) TaskHasClawback(ctx context.Context, taskID uuid.UUID) (bool, error) {
	var exists bool
	err := s.pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM ledger_entries WHERE kind = 'clawback' AND task_id = $1)`,
		taskID).Scan(&exists)
	return exists, err
}

func (s *Store) MarkPayout(ctx context.Context, entryID uuid.UUID, status, ref string) error {
	// Invariant (BLACKHOLE: never fake a transfer): a credit may only be marked
	// 'released' WITH a real rail reference. Enforced here and, structurally, by the
	// ledger_released_requires_ref CHECK constraint in db/schema.sql.
	if status == PayoutReleased && ref == "" {
		return fmt.Errorf("refusing to mark ledger entry %s 'released' without a payout reference", entryID)
	}
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
	// ThrottledHedge is true when this straggler was selected via the SHORT
	// throttled-worker path (docs/internal/CREED_AND_PATH_TO_TEN.md, "Thermal
	// sustained-vs-peak throughput on fanless Apple Silicon" 7→8: "detect
	// throttling live... the same way a stalled worker triggers a hedge
	// today") rather than the normal elapsed-time `after` path — purely
	// informational (logging/metrics), never changes hedge mechanics.
	ThrottledHedge bool
}

// StragglerTasks finds running PRIMARY tasks (not honeypot, not redundancy, not
// themselves a hedge) that warrant a hedge via EITHER of two independent paths:
//
//  1. elapsed time exceeds `after` (the original, pre-existing path — a slow
//     worker, regardless of why).
//  2. the CLAIMING WORKER's own most recent heartbeat currently reports
//     `throttled = true` (memory pressure OR a live sustained-throughput drop —
//     see agent/src/runners.rs's LiveThroughputMonitor / main.rs's `throttled:
//     throttle.throttled || live_throttling`) AND the task has run at least
//     `throttledAfter` (a short floor — never zero — so a task that started a
//     heartbeat-tick ago isn't hedged before it could possibly have produced a
//     result either way). This is deliberately MUCH shorter than `after`: a
//     worker that is DEMONSTRABLY throttling right now, live, is a stronger and
//     earlier signal than "this task has simply been running a while", which is
//     exactly the facet's own proof artifact ("triggers a hedge before the
//     stale-worker watchdog would have caught it" — here, before even the
//     normal elapsed-time hedge would have caught it).
//
// Excludes any chunk that already has a hedge in flight (so a straggler is
// hedged at most once) and any whose job already has >= maxInFlight hedges
// running (hedge sparingly). Ordered oldest-start first, capped at limit.
func (s *Store) StragglerTasks(ctx context.Context, after, throttledAfter time.Duration, maxInFlight, limit int) ([]Straggler, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.job_id, t.worker_id, j.job_type, COALESCE(j.model_ref,''),
		        COALESCE(t.input_ref,''), COALESCE(t.chunk_index,0), COALESCE(j.min_memory_gb,0),
		        COALESCE(w.throttled, false)
		 FROM tasks t JOIN jobs j ON j.id = t.job_id
		 LEFT JOIN workers w ON w.id = t.worker_id
		 WHERE t.status = 'running'
		   AND t.is_honeypot = false AND t.is_redundancy = false
		   AND t.hedged_from IS NULL
		   AND t.started_at IS NOT NULL
		   AND (
		     t.started_at < now() - make_interval(secs => $1)
		     OR (
		       COALESCE(w.throttled, false)
		       AND t.started_at < now() - make_interval(secs => $2)
		     )
		   )
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
		   ) < $3
		 ORDER BY t.started_at ASC
		 LIMIT $4`,
		after.Seconds(), throttledAfter.Seconds(), maxInFlight, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Straggler
	for rows.Next() {
		var s Straggler
		var throttled bool
		if err := rows.Scan(&s.TaskID, &s.JobID, &s.WorkerID, &s.JobType, &s.ModelRef,
			&s.InputRef, &s.ChunkIndex, &s.MinMemGB, &throttled); err != nil {
			return nil, err
		}
		// ThrottledHedge is set whenever the worker is currently throttled AND
		// this task hasn't yet crossed the normal `after` threshold — i.e. it is
		// a candidate ONLY because of the throttled-worker path, not the
		// elapsed-time path too (both can independently be true for an
		// old-enough task; that's still "elapsed" for attribution purposes).
		s.ThrottledHedge = throttled
		out = append(out, s)
	}
	return out, rows.Err()
}

// EndgameTailTasks finds the ENDGAME RACE candidates (Speed Lane wave 1B,
// workers.go raceEndgameTails): running PRIMARY tasks (not honeypot, not
// redundancy, not themselves a hedge) of a running job that has ZERO unclaimed
// work left — no queued/retrying task with claimed_by IS NULL, visible or not
// (a backoff-hidden retry still counts as work coming back, so its job is
// conservatively NOT in endgame). At that point the job's wall-clock IS the
// slowest running chunk, so each candidate is worth duplicating onto idle
// spare capacity IMMEDIATELY instead of waiting out the 90s elapsed-time
// hedge. minRun is a small floor (a chunk that just started is about to finish
// anyway — duplicating it is pure waste); the not-already-hedged-chunk and
// per-job in-flight-hedge-cap guards are byte-identical to StragglerTasks so
// the race and the hedge can never double-duplicate one chunk or blow the same
// cap. Ordered oldest-start first — the longest-running chunk is the tail.
func (s *Store) EndgameTailTasks(ctx context.Context, minRun time.Duration, maxInFlight, limit int) ([]Straggler, error) {
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
		   -- ENDGAME: no unclaimed queued/retrying work remains on this job.
		   AND NOT EXISTS (
		     SELECT 1 FROM tasks q
		     WHERE q.job_id = t.job_id AND q.status IN ('queued','retrying')
		       AND q.claimed_by IS NULL
		   )
		   -- this chunk is not already hedged (same guard as StragglerTasks):
		   AND NOT EXISTS (
		     SELECT 1 FROM tasks h
		     WHERE h.job_id = t.job_id AND COALESCE(h.chunk_index,0) = COALESCE(t.chunk_index,0)
		       AND h.hedged_from IS NOT NULL AND h.is_redundancy = false
		       AND h.status NOT IN ('failed','cancelled')
		   )
		   -- and the job is under its in-flight hedge cap (same as StragglerTasks):
		   AND (
		     SELECT count(*) FROM tasks h2
		     WHERE h2.job_id = t.job_id AND h2.hedged_from IS NOT NULL
		       AND h2.is_redundancy = false AND h2.status IN ('queued','running')
		   ) < $2
		 ORDER BY t.started_at ASC
		 LIMIT $3`,
		minRun.Seconds(), maxInFlight, limit)
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

// BusyWorkerIDs reports which of the given workers currently hold work: a
// RUNNING task, or a queued/retrying task pinned to them (claimed_by — a
// tiebreak/hedge/race dispatch they are about to pick up). Used by
// SelectEndgameRacePeer to restrict the race to genuinely IDLE spare capacity
// in one query instead of a per-candidate probe. Workers absent from the map
// are idle.
func (s *Store) BusyWorkerIDs(ctx context.Context, ids []uuid.UUID) (map[uuid.UUID]bool, error) {
	busy := make(map[uuid.UUID]bool, len(ids))
	if len(ids) == 0 {
		return busy, nil
	}
	rows, err := s.pool.Query(ctx,
		`SELECT t.worker_id FROM tasks t
		 WHERE t.status = 'running' AND t.worker_id = ANY($1)
		 UNION
		 SELECT t.claimed_by FROM tasks t
		 WHERE t.status IN ('queued','retrying') AND t.claimed_by = ANY($1)`,
		ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		busy[id] = true
	}
	return busy, rows.Err()
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
//
// PATCH (Speed Lane wave 1B, planner.go / raceEndgameTails): "first commit
// wins" now works in BOTH directions. The original predicate matched ONLY
// hedge copies (hedged_from IS NOT NULL), so when the HEDGE/RACE duplicate
// committed FIRST, the hedged ORIGINAL kept running — and since job
// completion (JobAllTasksDone) requires every task terminal, the job STILL
// waited out the slow original, which nullified the entire wall-clock point
// of duplicating the tail. The predicate now also cancels the hedged ORIGINAL
// — but ONLY when the just-committed keep task ($3) is that original's OWN
// winning duplicate (h.id = $3 AND h.hedged_from = tasks.id AND
// h.is_redundancy = false). Deliberately NOT any broader trigger: a
// verification-redundancy clone or tiebreak commit for the same chunk also
// flows through this function, and neither may ever cancel a still-running
// primary (their results are never the deliverable — the chunk would be left
// with no primary result at all). The cancelled original was never committed,
// so no payout was ever scheduled for it — money is untouched. A worker that
// later tries to commit the cancelled task gets the pre-existing 409 conflict
// (CommitTask requires status running/queued), the same contract a losing
// hedge has always had.
func (s *Store) CancelStragglerSiblings(ctx context.Context, jobID uuid.UUID, chunkIndex int, keepTaskID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE tasks
		   SET status = 'failed', claimed_by = NULL
		 WHERE job_id = $1 AND COALESCE(chunk_index,0) = $2
		   AND id <> $3
		   AND status IN ('queued','running','retrying')
		   AND is_redundancy = false AND is_honeypot = false
		   AND (hedged_from IS NOT NULL
		        OR EXISTS (
		          SELECT 1 FROM tasks h
		          WHERE h.id = $3 AND h.hedged_from = tasks.id
		            AND h.is_redundancy = false
		        ))`,
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

// FailTaskAndSettleJob marks a task permanently failed (retries exhausted) and
// fails its parent job, settling the job at the work that actually completed
// (partial-settle everywhere — see failJobAndSettleOnce). The caller checkpoints
// completed chunks BEFORE calling this (merge-before-mark), so delivered work is
// downloadable even off the failure path.
func (s *Store) FailTaskAndSettleJob(ctx context.Context, taskID, jobID uuid.UUID) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET status = 'failed' WHERE id = $1`, taskID); err != nil {
		return err
	}
	if _, err := failJobAndSettleOnce(ctx, tx, jobID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// JobCheckpointInfo returns what the merge-before-fail discipline needs to decide
// whether a checkpoint merge is worth attempting: the job's output_ref (empty =
// nowhere to write) and its completed-task count (0 = nothing to checkpoint).
func (s *Store) JobCheckpointInfo(ctx context.Context, jobID uuid.UUID) (outputRef string, tasksDone int, err error) {
	err = s.pool.QueryRow(ctx,
		`SELECT COALESCE(output_ref,''), COALESCE(tasks_done,0) FROM jobs WHERE id = $1`,
		jobID).Scan(&outputRef, &tasksDone)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", 0, errNotFound
	}
	return outputRef, tasksDone, err
}

// TaskJobID resolves a task's parent job. Used by the fail endpoint to checkpoint
// delivered chunks AFTER a validated terminal failure (the checkpoint needs the job,
// FailTask returns only the outcome). errNotFound when the task does not exist.
func (s *Store) TaskJobID(ctx context.Context, taskID uuid.UUID) (uuid.UUID, error) {
	var jobID uuid.UUID
	err := s.pool.QueryRow(ctx, `SELECT job_id FROM tasks WHERE id = $1`, taskID).Scan(&jobID)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, errNotFound
	}
	return jobID, err
}

// failJobAndSettleOnce flips a job to 'failed' EXACTLY once, even when several of
// the job's tasks fail terminally (e.g. multiple workers each report bad input, or
// the stale reaper and the fail endpoint both fire). It is a no-op when the job is
// already terminal. `flipped` is true only on the call that actually transitioned
// the job, so the caller emits the job_failed event exactly once.
//
// MONEY (partial-settle everywhere, same rule as the stuck-run watchdog): nothing
// is refunded via ledger rows, because per-task charges settle only at a verified
// commit — completed chunks were DELIVERED and stay charged (the supplier earned
// them), and the un-run remainder was never charged in the first place. The job's
// actual_usd is settled here to the sum of those completed-task charges, so a job
// with ZERO delivered chunks honestly settles at $0 (nothing charged, nothing owed)
// with no refund row needed.
func failJobAndSettleOnce(ctx context.Context, tx pgx.Tx, jobID uuid.UUID) (flipped bool, err error) {
	ct, err := tx.Exec(ctx,
		`UPDATE jobs SET status = 'failed' WHERE id = $1 AND status NOT IN ('complete','cancelled','failed')`,
		jobID)
	if err != nil {
		return false, err
	}
	if ct.RowsAffected() == 0 {
		return false, nil // already terminal — nothing to settle again
	}
	// Settle at completed work (the tx-scoped twin of SetJobActualUSD).
	if _, err := tx.Exec(ctx,
		`UPDATE jobs SET actual_usd = COALESCE((
		   SELECT SUM(-amount_usd) FROM ledger_entries
		   WHERE kind = 'buyer_charge'
		     AND task_id IN (SELECT id FROM tasks WHERE job_id = $1)
		 ),0)
		 WHERE id = $1`, jobID); err != nil {
		return false, err
	}
	return true, nil
}

// StuckJob is a running job the watchdog judged stuck: past its deadline with no
// task progress. Carries what the reaper needs to escalate (rescue or kill),
// checkpoint, cancel + settle, and attribute the stall.
type StuckJob struct {
	ID        uuid.UUID
	BuyerID   uuid.UUID
	OutputRef string
	EtaSecs   int
	TasksDone int
	TaskCount int
	Strikes   int  // watchdog_strikes: 0 → rescue next, >=1 → kill next
	DeadClaim bool // an unfinished task is claimed by a DEAD worker (machine-stuck, not workload-stuck)
}

// StuckRunningJobs returns running jobs past their deadline with NO task progress
// (no commit, no fresh claim, and no recently-scheduled retry visibility) within
// grace. Progress within grace — even slow progress — exempts a job: the watchdog
// regulates stuck runs, never merely slow ones (hedging already covers slow).
//
// The deadline is, in precedence order (each case OWNS its jobs — a later clause
// never overrides an earlier one, so an explicit 3-day deadline is never cut short
// by the fallback cap):
//   - an explicit buyer deadline_secs (> 0): a hard wall-clock deadline;
//   - otherwise the ETA-derived deadline: factor × eta_secs, FLOORED at
//     eta_secs + 120s so a tiny prediction (eta 10s → 15s at 1.5×) cannot judge a
//     job faster than a human could blink;
//   - otherwise (no explicit deadline AND no ETA) a 24-hour wall-clock cap — the
//     only deadline a no-prediction job can honestly be held to.
//
// deadline_secs = -1 is the buyer's opt-out: the job is NEVER judged, not even by
// the 24h cap (they asked for run-to-completion and get exactly that).
//
// The visible_at term in the progress check makes a just-rescued/requeued task
// count as progress: its visibility backoff sits in the near future, which proves
// the queue is actively re-placing the work — without it, a rescue would be judged
// "still no progress" on the very next sweep and killed before any worker could
// claim. deadAfter bounds the worker-liveness attribution: DeadClaim is true when
// an unfinished task's claiming worker has not heartbeated within it (or ever).
func (s *Store) StuckRunningJobs(ctx context.Context, factor float64, grace, deadAfter time.Duration, limit int) ([]StuckJob, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT j.id, j.buyer_id, COALESCE(j.output_ref,''), COALESCE(j.eta_secs,0),
		        j.tasks_done, j.task_count, COALESCE(j.watchdog_strikes,0),
		        EXISTS (
		          SELECT 1 FROM tasks t JOIN workers w ON w.id = t.claimed_by
		          WHERE t.job_id = j.id AND t.status = 'running'
		            AND (w.last_seen_at IS NULL OR w.last_seen_at < now() - make_interval(secs => $4::float8))
		        ) AS dead_claim
		 FROM jobs j
		 WHERE j.status = 'running'
		   AND COALESCE(j.deadline_secs, 0) <> -1
		   AND (
		     (j.deadline_secs IS NOT NULL AND j.deadline_secs > 0
		       AND now() > j.created_at + make_interval(secs => j.deadline_secs::float8))
		     OR (COALESCE(j.deadline_secs, 0) = 0 AND COALESCE(j.eta_secs, 0) > 0
		       AND now() > j.created_at + GREATEST(
		             make_interval(secs => j.eta_secs::float8 * $1),
		             make_interval(secs => j.eta_secs::float8 + 120)))
		     OR (COALESCE(j.deadline_secs, 0) = 0 AND COALESCE(j.eta_secs, 0) = 0
		       AND now() > j.created_at + interval '24 hours')
		   )
		   AND NOT EXISTS (
		     SELECT 1 FROM tasks t
		     WHERE t.job_id = j.id
		       AND (t.completed_at > now() - make_interval(secs => $2::float8)
		         OR t.claimed_at   > now() - make_interval(secs => $2::float8)
		         OR t.visible_at   > now() - make_interval(secs => $2::float8))
		   )
		 ORDER BY j.created_at ASC
		 LIMIT $3`,
		factor, grace.Seconds(), limit, deadAfter.Seconds())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StuckJob
	for rows.Next() {
		var j StuckJob
		if err := rows.Scan(&j.ID, &j.BuyerID, &j.OutputRef, &j.EtaSecs, &j.TasksDone, &j.TaskCount,
			&j.Strikes, &j.DeadClaim); err != nil {
			return nil, err
		}
		out = append(out, j)
	}
	return out, rows.Err()
}

// RescueStuckJob is the watchdog's FIRST strike: instead of killing a stuck job it
// moves every unfinished task back to the queue for a different machine — the claim
// is cleared, a small visibility backoff applied (same mechanics as the stale
// requeue), and retry_count is deliberately NOT incremented (the stall is not the
// task's fault; burning its retries here would fast-track it to a terminal fail).
// The strike transition is guarded (status='running' AND watchdog_strikes=0), so
// concurrent sweeps rescue at most once: flipped=false means another sweep won the
// race (or the job progressed/went terminal) and NOTHING was touched.
func (s *Store) RescueStuckJob(ctx context.Context, jobID uuid.UUID, backoff time.Duration) (flipped bool, err error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	ct, err := tx.Exec(ctx,
		`UPDATE jobs SET watchdog_strikes = 1
		 WHERE id = $1 AND status = 'running' AND COALESCE(watchdog_strikes, 0) = 0`, jobID)
	if err != nil {
		return false, err
	}
	if ct.RowsAffected() == 0 {
		return false, nil
	}
	if _, err := tx.Exec(ctx,
		`UPDATE tasks
		   SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
		       started_at = NULL, worker_id = NULL,
		       visible_at = now() + make_interval(secs => $2)
		 WHERE job_id = $1 AND status IN ('running','retrying')`,
		jobID, backoff.Seconds()); err != nil {
		return false, err
	}
	// Already-queued tasks with a STALE visible_at get it refreshed too. Without
	// this, a job whose unfinished work is all sitting unclaimed in the queue (e.g.
	// no capacity for its hw_class) gets a "rescue" that touches zero rows and no
	// fresh progress term — so the very next sweep would judge it stuck again and
	// kill it 30s after promising a second chance. The refresh makes the second
	// window real; if capacity never appears, the deadline clause catches it again
	// honestly at strike 1.
	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET visible_at = now() + make_interval(secs => $2)
		 WHERE job_id = $1 AND status = 'queued'
		   AND visible_at < now() + make_interval(secs => $2)`,
		jobID, backoff.Seconds()); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}

// DeadClaim is a running task held by a worker that stopped heartbeating: the
// machine is gone (crash, sleep, network loss), so waiting for a commit is
// hopeless. Carries who to dock and where to requeue.
type DeadClaim struct {
	TaskID     uuid.UUID
	JobID      uuid.UUID
	WorkerID   uuid.UUID
	SupplierID uuid.UUID // zero when the worker row has no supplier (never docked)
	JobType    string
}

// DeadClaimedTasks finds running tasks whose claiming worker has been silent past
// olderThan (last_seen_at older than it, or never seen) AND whose claim itself is
// older than olderThan — the double condition so a task claimed a moment before a
// heartbeat lull is not misread as dead. These are the fast-rescue set: a dead
// machine is a certainty, so its tasks requeue immediately instead of waiting for
// the 30-min stale reaper or the job-level watchdog.
func (s *Store) DeadClaimedTasks(ctx context.Context, olderThan time.Duration, limit int) ([]DeadClaim, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT t.id, t.job_id, w.id, w.supplier_id, j.job_type
		 FROM tasks t
		 JOIN workers w ON w.id = t.claimed_by
		 JOIN jobs j ON j.id = t.job_id
		 WHERE t.status = 'running'
		   AND t.claimed_at IS NOT NULL
		   AND t.claimed_at < now() - make_interval(secs => $1)
		   AND (w.last_seen_at IS NULL OR w.last_seen_at < now() - make_interval(secs => $1))
		   AND j.status = 'running'
		 ORDER BY t.claimed_at ASC
		 LIMIT $2`,
		olderThan.Seconds(), limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DeadClaim
	for rows.Next() {
		var d DeadClaim
		var sup *uuid.UUID
		if err := rows.Scan(&d.TaskID, &d.JobID, &d.WorkerID, &sup, &d.JobType); err != nil {
			return nil, err
		}
		if sup != nil {
			d.SupplierID = *sup
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// RescueRunningTask requeues ONE running task with the rescue mechanics (claim
// cleared, small visibility backoff, retry_count NOT incremented — the worker died
// or wedged; the task did nothing wrong). Guarded by status='running' so a task
// that committed/failed between selection and here is untouched; rescued=false
// reports exactly that, so the caller only events/docks on a real rescue.
func (s *Store) RescueRunningTask(ctx context.Context, taskID uuid.UUID, backoff time.Duration) (rescued bool, err error) {
	ct, err := s.pool.Exec(ctx,
		`UPDATE tasks
		   SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
		       started_at = NULL, worker_id = NULL,
		       visible_at = now() + make_interval(secs => $2)
		 WHERE id = $1 AND status = 'running'`,
		taskID, backoff.Seconds())
	if err != nil {
		return false, err
	}
	return ct.RowsAffected() > 0, nil
}

// CancelledTaskResultKeys returns the result_key of every cancelled PRIMARY task
// of a job — the keys whose "<result_key>.partial" objects the watchdog's kill
// path checks for mid-chunk checkpoint documents. Honeypot/redundancy clones are
// excluded (verification probes, never buyer output).
func (s *Store) CancelledTaskResultKeys(ctx context.Context, jobID uuid.UUID) ([]string, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT result_key FROM tasks
		 WHERE job_id = $1 AND status = 'cancelled'
		   AND is_honeypot = false AND is_redundancy = false
		   AND result_key IS NOT NULL AND result_key <> ''
		 ORDER BY chunk_index ASC, id ASC`,
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

// RecordEtaCalibration inserts one eta_calibration row for a finalized job —
// predicted_secs = the eta_secs persisted at submit, realized_secs = wall-clock
// seconds from created_at to now (the finalize moment). A job with no prediction
// (eta_secs NULL/0) inserts NOTHING (there is no predicted value to calibrate,
// and fabricating one would poison the loop), and a job already recorded is a
// no-op (both finalize sites can fire once each in a race). Returns the recorded
// pair — (0, 0) when nothing was inserted — so the caller can count near-misses.
func (s *Store) RecordEtaCalibration(ctx context.Context, jobID uuid.UUID) (predicted, realized int, err error) {
	// ON CONFLICT (job_id) DO NOTHING makes the once-only guarantee ATOMIC (the
	// eta_calibration_job_uniq index): when the two finalize sites race, exactly one
	// insert wins and returns the row; the loser scans ErrNoRows and records nothing
	// — so the near-miss counter can never double-count a job.
	err = s.pool.QueryRow(ctx,
		`INSERT INTO eta_calibration (job_id, job_type, tier, predicted_secs, realized_secs)
		 SELECT id, job_type, tier, eta_secs,
		        GREATEST(0, EXTRACT(EPOCH FROM (now() - created_at)))::int
		 FROM jobs
		 WHERE id = $1 AND COALESCE(eta_secs, 0) > 0
		 ON CONFLICT (job_id) DO NOTHING
		 RETURNING predicted_secs, realized_secs`,
		jobID).Scan(&predicted, &realized)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, 0, nil // no ETA prediction (or already recorded) — nothing to calibrate
	}
	return predicted, realized, err
}

// CancelStuckJob flips a stuck job to 'cancelled' and cancels its unfinished tasks.
// Deliberately NOT the full-refund fail path: buyer charges settle per task at
// commit, so completed work stays charged (the supplier earned it — users owe each
// other) and the un-run remainder was never charged. flipped=false when the job
// went terminal (or progressed) between selection and here, in which case nothing
// is touched.
func (s *Store) CancelStuckJob(ctx context.Context, jobID uuid.UUID) (flipped bool, err error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	ct, err := tx.Exec(ctx,
		`UPDATE jobs SET status = 'cancelled' WHERE id = $1 AND status = 'running'`, jobID)
	if err != nil {
		return false, err
	}
	if ct.RowsAffected() == 0 {
		return false, nil
	}
	if _, err := tx.Exec(ctx,
		`UPDATE tasks SET status = 'cancelled'
		 WHERE job_id = $1 AND status NOT IN ('complete','failed','cancelled')`, jobID); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
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

// MarkResultsMerged stamps the results-merge watermark (results_merged_at =
// now()) right after a real successful merge writes the buyer-ready artifact.
// handleJobResults reads this to skip the re-merge on every poll once it is
// set (Data Transfer & Artifact I/O 4.5->5, "Stop paying for every poll
// twice") — it is set unconditionally (not COALESCE-guarded) so a later real
// merge (e.g. by the completion-sweep fallback) always advances it.
func (s *Store) MarkResultsMerged(ctx context.Context, jobID uuid.UUID) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE jobs SET results_merged_at = now() WHERE id = $1`,
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

// taskDurationBucketsMs are the fixed histogram bucket upper bounds (milliseconds)
// for cx_task_duration_ms (docs/CREED_AND_PATH_TO_TEN.md, "Performance
// observability" 6→6.5). Chosen to span a real batch task's plausible range: a
// fast embed chunk (low hundreds of ms) through a slow generative chunk nearing
// the straggler-hedge threshold (hedgeAfter = 90s) and beyond.
var taskDurationBucketsMs = []float64{100, 500, 1000, 2500, 5000, 15000, 30000, 60000, 120000}

// TaskDurationHistogramRow is one job_type's histogram: Buckets holds the
// CUMULATIVE count of observations with duration_ms <= taskDurationBucketsMs[i]
// (the Prometheus histogram convention — le is "less than or equal", cumulative,
// not per-bin), alongside the total Count and SumMs Prometheus's _sum/_count lines
// need.
type TaskDurationHistogramRow struct {
	JobType string
	Buckets []int64 // cumulative, same order/length as taskDurationBucketsMs
	Count   int64
	SumMs   int64
}

// TaskDurationHistogram computes a real cx_task_duration_ms histogram per
// job_type straight from task_durations — the same table the drift/ETA rollup
// already reads, so this adds zero new instrumentation, only a new query over
// data that was already being recorded (docs/CREED_AND_PATH_TO_TEN.md,
// "Performance observability" 6→6.5: "zero latency histograms anywhere"). The
// per-bucket FILTER counts are computed in one aggregate query, not fetched row by
// row, so this scales with job_type cardinality, not with row count.
func (s *Store) TaskDurationHistogram(ctx context.Context) ([]TaskDurationHistogramRow, error) {
	// Build "count(*) FILTER (WHERE duration_ms <= $N)" once per bucket boundary,
	// parameterized (never string-interpolated) even though the boundaries are a
	// fixed Go slice, not user input — consistent with how every other query in
	// this file binds values.
	selectCols := make([]string, 0, len(taskDurationBucketsMs))
	args := make([]any, 0, len(taskDurationBucketsMs))
	for i, b := range taskDurationBucketsMs {
		args = append(args, b)
		selectCols = append(selectCols, fmt.Sprintf("count(*) FILTER (WHERE duration_ms <= $%d)", i+1))
	}
	query := fmt.Sprintf(
		`SELECT job_type, count(*), COALESCE(sum(duration_ms),0), %s
		 FROM task_durations
		 GROUP BY job_type
		 ORDER BY job_type`,
		strings.Join(selectCols, ", "),
	)
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TaskDurationHistogramRow
	for rows.Next() {
		var r TaskDurationHistogramRow
		r.Buckets = make([]int64, len(taskDurationBucketsMs))
		scanArgs := make([]any, 0, 3+len(r.Buckets))
		scanArgs = append(scanArgs, &r.JobType, &r.Count, &r.SumMs)
		for i := range r.Buckets {
			scanArgs = append(scanArgs, &r.Buckets[i])
		}
		if err := rows.Scan(scanArgs...); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// LatencyPhaseRow is one job_type's p50/p90 (milliseconds) for each of the three
// phases end-to-end task latency decomposes into. Backs cx_latency_phase_ms
// (End-to-End Job Latency Decomposition 7->7.5, docs/internal/CREED_AND_PATH_TO_TEN.md).
type LatencyPhaseRow struct {
	JobType               string
	QueueWaitP50Ms        float64
	QueueWaitP90Ms        float64
	DispatchOverheadP50Ms float64
	DispatchOverheadP90Ms float64
	RunP50Ms              float64
	RunP90Ms              float64
	Count                 int64
}

// LatencyPhaseDecomposition computes, per job_type, real p50/p90 millisecond
// figures for the three phases a completed task's end-to-end latency decomposes
// into: QUEUE-WAIT (submitted/eligible -> claimed — idle-fleet pickup cost),
// DISPATCH OVERHEAD (claimed -> started — time the worker spent between taking
// the claim and actually beginning work, e.g. a cold model load), and RUN
// (started -> completed — the actual work, including verification + result
// commit). Turns the existing created_at/visible_at/claimed_at/started_at/
// completed_at timestamps — already recorded on every task, no new
// instrumentation — into the first real decomposition of WHERE end-to-end
// latency goes, rather than just the single total task_duration_ms histogram.
// Only 'complete' tasks are included (a failed/retrying task's timestamps don't
// represent a real finished trip through all three phases).
func (s *Store) LatencyPhaseDecomposition(ctx context.Context) ([]LatencyPhaseRow, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT j.job_type, count(*),
		       COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY queue_wait_ms), 0),
		       COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY queue_wait_ms), 0),
		       COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY dispatch_overhead_ms), 0),
		       COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY dispatch_overhead_ms), 0),
		       COALESCE(percentile_disc(0.5) WITHIN GROUP (ORDER BY run_ms), 0),
		       COALESCE(percentile_disc(0.9) WITHIN GROUP (ORDER BY run_ms), 0)
		  FROM (
		    SELECT t.job_id,
		           EXTRACT(EPOCH FROM (t.claimed_at - GREATEST(t.created_at, t.visible_at))) * 1000 AS queue_wait_ms,
		           EXTRACT(EPOCH FROM (t.started_at - t.claimed_at)) * 1000 AS dispatch_overhead_ms,
		           EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) * 1000 AS run_ms
		      FROM tasks t
		     WHERE t.status = 'complete'
		       AND t.claimed_at IS NOT NULL AND t.started_at IS NOT NULL AND t.completed_at IS NOT NULL
		  ) phases
		  JOIN jobs j ON j.id = phases.job_id
		 GROUP BY j.job_type
		 ORDER BY j.job_type`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []LatencyPhaseRow
	for rows.Next() {
		var r LatencyPhaseRow
		if err := rows.Scan(&r.JobType, &r.Count,
			&r.QueueWaitP50Ms, &r.QueueWaitP90Ms,
			&r.DispatchOverheadP50Ms, &r.DispatchOverheadP90Ms,
			&r.RunP50Ms, &r.RunP90Ms); err != nil {
			return nil, err
		}
		out = append(out, r)
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
	// FirmQuote / FirmQuoteMaxUSD / BilledUSD (Project Detection & Quotation 7->8,
	// docs/internal/CREED_AND_PATH_TO_TEN.md, "Ship a firm-quote tier: a real
	// commitment, not just an estimate"): when FirmQuote is true, BilledUSD is what
	// the buyer's Stripe charge was ACTUALLY capped at — the real proof artifact
	// that "actual cost exceeds its firm quote, still charges the buyer only the
	// quoted maximum". BilledUSD is nil until a charge is actually attempted
	// (FreezeChargeAmount/FormChargeBatch stamp it), same never-fabricate discipline
	// as QuotedUSD. ChargedUSD above stays the pre-existing per-task ledger sum
	// (the real value of work delivered) — BilledUSD is deliberately the SEPARATE,
	// possibly-lower number Stripe actually collected.
	FirmQuote       bool     `json:"firm_quote,omitempty"`
	FirmQuoteMaxUSD *float64 `json:"firm_quote_max_usd,omitempty"`
	BilledUSD       *float64 `json:"billed_usd,omitempty"`
	// Speed-SLA facts (wave 2A), surfaced on the invoice — and therefore on the
	// ClearingReceipt, which embeds this view. SLAPremiumUSD is the surcharge the
	// bound guarantee carried (folded into the job's estimate/actual);
	// SLARefundUSD is the real sla_refund ledger credit recorded on a miss (nil
	// until one exists — never fabricated); SLAMet is the recorded outcome (nil
	// = no SLA, or not yet decided). Same never-fabricate discipline as
	// QuotedUSD/BilledUSD above.
	SLAGuaranteeSecs int      `json:"sla_guarantee_secs,omitempty"`
	SLAPremiumUSD    *float64 `json:"sla_premium_usd,omitempty"`
	SLARefundUSD     *float64 `json:"sla_refund_usd,omitempty"`
	SLAMet           *bool    `json:"sla_met,omitempty"`
}

// JobInvoice builds an invoice for a job scoped to its buyer (buyers see only
// their own jobs). It reads the job header and aggregates the realized ledger
// entries for the job's tasks by kind. Returns errNotFound when the job is not
// the buyer's.
func (s *Store) JobInvoice(ctx context.Context, jobID, buyerID uuid.UUID) (*InvoiceView, error) {
	iv := InvoiceView{JobID: jobID}
	var firmMax, billed *float64
	var slaGuarantee int
	var slaPremium *float64
	err := s.pool.QueryRow(ctx,
		`SELECT buyer_id, status, job_type, created_at,
		        COALESCE(estimated_usd,0), COALESCE(actual_usd,0),
		        firm_quote, firm_quote_max_usd, billed_usd,
		        COALESCE(sla_guarantee_secs,0), sla_premium_usd, sla_met
		 FROM jobs WHERE id = $1 AND buyer_id = $2`,
		jobID, buyerID,
	).Scan(&iv.BuyerID, &iv.Status, &iv.JobType, &iv.CreatedAt, &iv.EstimatedUSD, &iv.ActualUSD,
		&iv.FirmQuote, &firmMax, &billed, &slaGuarantee, &slaPremium, &iv.SLAMet)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errNotFound
	}
	if err != nil {
		return nil, err
	}
	iv.FirmQuoteMaxUSD = firmMax
	iv.BilledUSD = billed
	iv.SLAGuaranteeSecs = slaGuarantee
	iv.SLAPremiumUSD = slaPremium
	// Surface the REAL recorded refund (the sla_refund ledger credit keyed
	// 'sla-<job_id>'), only when one exists — the invoice shows what actually
	// happened, never a predicted remedy.
	if slaGuarantee > 0 {
		var refund float64
		if rerr := s.pool.QueryRow(ctx,
			`SELECT COALESCE(SUM(amount_usd),0)::float8 FROM ledger_entries
			  WHERE kind = 'sla_refund' AND payout_ref = $1`,
			"sla-"+jobID.String()).Scan(&refund); rerr != nil {
			return nil, rerr
		}
		if refund > 0 {
			iv.SLARefundUSD = &refund
		}
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
