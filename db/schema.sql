-- Computexchange — single authoritative schema (PostgreSQL).
--
-- One file, applied via `make migrate` (psql against $DATABASE_URL). Transcribed
-- from the action plan's "Data model sketches" plus the control-plane additions
-- (queue/auth/honeypot tables). Re-runnable: every object uses IF NOT EXISTS, so
-- applying twice is a no-op rather than an error.
--
-- The job queue is Postgres, not NATS (BLACKHOLE compression): workers claim work
-- via `SELECT ... FOR UPDATE SKIP LOCKED` over `tasks`, gated on (status, visible_at).

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ─────────────────────────────────────────────────────────────────────────────
-- Core domain
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS suppliers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   TIMESTAMPTZ DEFAULT now(),
    email        TEXT NOT NULL UNIQUE,
    tax_id       TEXT,               -- W-9/W-8BEN/T4A info collected at signup
    tax_country  TEXT,
    stripe_acct  TEXT,               -- Stripe Connect account ID
    reputation   REAL DEFAULT 0.5,   -- 0.0–1.0
    tier         SMALLINT DEFAULT 0, -- 0–3
    status       TEXT DEFAULT 'pending'  -- pending|active|suspended|banned
);

CREATE TABLE IF NOT EXISTS workers (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id    UUID REFERENCES suppliers,
    hw_class       TEXT NOT NULL,   -- 'apple_silicon_max', 'apple_silicon_pro', etc.
    memory_gb      REAL,
    bw_gbps        REAL,            -- measured memory bandwidth
    created_at     TIMESTAMPTZ DEFAULT now(),
    last_seen_at   TIMESTAMPTZ,
    version        TEXT             -- agent binary version
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    worker_id      UUID REFERENCES workers,
    measured_at    TIMESTAMPTZ DEFAULT now(),
    model_id       TEXT,            -- e.g. 'llama3.1-70b-q4_k_m'
    job_type       TEXT,            -- 'embed', 'infer', 'transcribe'
    tps            REAL,            -- tokens/sec
    eps            REAL,            -- embeddings/sec
    thermal_ok     BOOLEAN,
    p99_latency_ms REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id            UUID NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    status              TEXT DEFAULT 'queued',  -- queued|running|verifying|complete|failed|cancelled
    job_type            TEXT NOT NULL,
    model_ref           TEXT,
    input_ref           TEXT NOT NULL,          -- object storage key
    output_ref          TEXT,                   -- object storage key
    tier                TEXT DEFAULT 'batch',   -- batch|priority|trusted
    verification_policy JSONB,
    estimated_usd       NUMERIC(10,6),
    actual_usd          NUMERIC(10,6),
    task_count          INT,
    tasks_done          INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        UUID REFERENCES jobs,
    worker_id     UUID REFERENCES workers,
    created_at    TIMESTAMPTZ DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    status        TEXT DEFAULT 'queued',  -- queued|running|complete|failed|retrying
    result_ref    TEXT,
    input_ref     TEXT,                   -- per-task input chunk object key (null = inherit job.input_ref)
    is_honeypot   BOOLEAN DEFAULT false,
    is_redundancy BOOLEAN DEFAULT false,
    retry_count   SMALLINT DEFAULT 0,
    -- Postgres-queue columns (SKIP LOCKED claim + retry visibility):
    claimed_by    UUID,                       -- worker currently holding the task
    claimed_at    TIMESTAMPTZ,                -- when the claim was taken
    visible_at    TIMESTAMPTZ DEFAULT now()   -- task is claimable only once now() >= visible_at
);
-- input_ref added after the fact for already-created tables (idempotent ALTER).
-- A task is a split of its job; when its own input_ref is null the dispatch uses
-- the parent job's input_ref. This column lets a job fan out into per-chunk
-- inputs without a second table.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS input_ref TEXT;

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ DEFAULT now(),
    kind          TEXT NOT NULL,  -- 'buyer_charge'|'supplier_credit'|'platform_take'|'clawback'
    supplier_id   UUID REFERENCES suppliers,
    buyer_id      UUID,
    task_id       UUID REFERENCES tasks,
    amount_usd    NUMERIC(10,6) NOT NULL,  -- positive = credit, negative = debit
    payout_status TEXT DEFAULT 'pending',  -- pending|held|released|clawed_back
    release_at    TIMESTAMPTZ,             -- when payout hold expires
    payout_ref    TEXT                     -- Stripe/Trolley transfer ID
);

-- A SUPPLIER payout may only be 'released' WITH a real rail reference — never a faked
-- transfer (BLACKHOLE). Enforced structurally so no code path or bug can record a payout
-- without proof of money movement. Scoped to supplier_credit: buyer_charge and
-- platform_take are internal bookkeeping rows marked 'released' (settled, no transfer ref).
-- Idempotent (drop+add) so re-applying the schema is safe.
ALTER TABLE ledger_entries DROP CONSTRAINT IF EXISTS ledger_released_requires_ref;
ALTER TABLE ledger_entries ADD CONSTRAINT ledger_released_requires_ref
    CHECK (kind <> 'supplier_credit' OR payout_status <> 'released' OR payout_ref IS NOT NULL);

-- A task produces exactly one ledger entry per kind (buyer_charge / supplier_credit
-- / platform_take / clawback). This uniqueness makes InsertLedgerEntries idempotent
-- under retry (ON CONFLICT DO NOTHING) so a double-commit can never double-charge a
-- buyer or double-pay a supplier. Job-level entries (NULL task_id) stay unconstrained
-- (SQL NULLs are distinct) and are guarded by their own once-only logic.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_task_kind_uniq ON ledger_entries (task_id, kind);

-- ─────────────────────────────────────────────────────────────────────────────
-- Auth + verification (control-plane additions; no silent auth bypass — rows
-- in api_keys/worker_tokens MUST be seeded for any request to authenticate).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id   UUID,
    key_hash   TEXT UNIQUE NOT NULL,      -- store a hash, never the raw key
    is_admin   BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    revoked    BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS worker_tokens (
    token_hash  TEXT PRIMARY KEY,         -- SHA-256 hash of the raw token; raw is shown once at mint, never stored
    worker_id   UUID REFERENCES workers,
    supplier_id UUID REFERENCES suppliers,
    created_at  TIMESTAMPTZ DEFAULT now(),
    revoked     BOOLEAN DEFAULT false
);
-- Migration for DBs created before tokens were hashed (column was `token`, held the
-- raw value). Rename to token_hash; pre-launch there are no real tokens to migrate,
-- so re-seed / re-mint after deploy. Idempotent — only fires if the old column exists.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'worker_tokens' AND column_name = 'token') THEN
    ALTER TABLE worker_tokens RENAME COLUMN token TO token_hash;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS honeypots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type     TEXT NOT NULL,
    input_ref    TEXT NOT NULL,
    known_answer BYTEA,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Webhooks + model catalogue
-- ─────────────────────────────────────────────────────────────────────────────
-- Completion-webhook registrations. Delivery IS wired: the background sweep
-- (control/workers.go) POSTs job.completed to job-scoped webhooks once their job
-- reaches complete/failed, retries with backoff, and stamps delivered_at so each
-- fires exactly once. A NULL job_id is a buyer catch-all (no single fire event).

CREATE TABLE IF NOT EXISTS webhooks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id     UUID,
    job_id       UUID,
    url          TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    delivered_at TIMESTAMPTZ              -- set once the completion POST succeeds (exactly-once)
);
-- delivered_at added after the fact for already-created tables (idempotent).
ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;

-- Model + pricing catalogue — the DB-backed source of truth GET /v1/models and
-- /v1/price-estimate read live (control/api.go ListModels/GetModel; no static Go
-- list). price_per_1k is USD per 1,000 units (tokens/embeddings);
-- price_per_unit is USD per discrete unit (e.g. audio-minute for transcription).
-- kind matches the wire model_kind/runner domain (embed|gguf|whisper|hf|mlx).

CREATE TABLE IF NOT EXISTS models (
    id             TEXT PRIMARY KEY,
    family         TEXT,
    quant          TEXT,
    kind           TEXT,
    dim            INT,                 -- embedding dimensionality (embed models)
    job_type       TEXT,
    price_per_1k   NUMERIC(12,8),       -- USD / 1,000 units
    price_per_unit NUMERIC(12,8),       -- USD / discrete unit (e.g. audio-minute)
    min_memory_gb  REAL,
    hf_repo        TEXT                 -- HuggingFace repo to resolve weights from
);

-- Seed the V1 catalogue. ON CONFLICT DO NOTHING keeps this idempotent and lets
-- operators edit rows without a re-seed clobbering them.
-- hf_repo / model id values are the ones the Rust agent actually resolves
-- (agent/src/models.rs): MiniLM embeddings, an unsloth Llama-3.2-1B GGUF, and
-- whisper-tiny|base. Keep these in lockstep with the agent's resolver.
INSERT INTO models (id, family, quant, kind, dim, job_type, price_per_1k, price_per_unit, min_memory_gb, hf_repo) VALUES
    ('all-minilm-l6-v2', 'minilm', NULL,   'embed',   384,  'embed',            0.00100000, NULL,        2, 'sentence-transformers/all-MiniLM-L6-v2'),
    ('llama-3.2-1b-instruct-q4', 'llama', 'q4_k_m', 'gguf', NULL, 'batch_infer', 0.00200000, NULL,        4, 'unsloth/Llama-3.2-1B-Instruct-GGUF'),
    ('qwen2.5-7b-instruct-q4', 'qwen', 'q4_k_m', 'gguf', NULL, 'batch_infer',    0.00800000, NULL,       40, 'Qwen/Qwen2.5-7B-Instruct-GGUF'),
    ('whisper-tiny',     'whisper', NULL,  'whisper', NULL,  'audio_transcribe', NULL,       0.00400000,  1, 'openai/whisper-tiny'),
    ('whisper-base',     'whisper', NULL,  'whisper', NULL,  'audio_transcribe', NULL,       0.00500000,  2, 'openai/whisper-base')
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- Scheduler V2 / Turbo additions (idempotent ALTERs).
-- ─────────────────────────────────────────────────────────────────────────────
-- The V1 claim filtered only on status/visibility/tier; per-job hardware/model
-- constraints lived only in the manifest JSON, so a worker could claim a task it
-- could not run. Scheduler V2 lifts those constraints into queryable columns and
-- hard-filters them in the SKIP-LOCKED claim (store.go ClaimTask), so an
-- incompatible worker can NEVER claim a task. These mirror JobConstraints + the
-- worker capability the agent already advertises on registration.

-- Job-level queryable constraints (lifted out of verification_policy/manifest):
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_memory_gb      REAL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_reputation     REAL DEFAULT 0;    -- Elite-supplier gate (research §6.4): claim only by reputation >= this
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS private_pool        BOOLEAN DEFAULT false; -- Private Deployment (research §3): route only to the buyer's bound suppliers
CREATE TABLE IF NOT EXISTS private_pool_members (
    buyer_id    UUID NOT NULL,
    supplier_id UUID NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (buyer_id, supplier_id)
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hw_classes         TEXT[];           -- NULL = any class
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS data_residency     TEXT[];           -- NULL = unrestricted
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS split_size         INT;              -- adaptive chunk size chosen at submit
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS offered_rate_usd_hr REAL;            -- price-derived $/hr a worker earns running this (min-payout gate)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS eta_secs           INT;              -- predicted completion seconds at submit
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_status      TEXT NOT NULL DEFAULT 'not_attempted'; -- not_attempted|charged|failed|no_payment_method (queryable charge state, not log-only)
-- Full submitted JobType (tag + variant fields: labels/schema/max_tokens/...), so
-- the poll dispatch can carry buyer params to the agent, not just the bare tag.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_type_spec      JSONB;

-- Plane C §12 / Plane D §14 D8 — Budget Governor. max_usd is the buyer's hard
-- spend cap: the SKIP-LOCKED claim refuses to dispatch a NEW task once the job's
-- projected charge (already-charged tasks + one more task's estimate) would breach
-- it, so a runaway is STOPPED before money is spent (the cap prevents dispatch, it
-- never refunds). budget_state is the buyer-visible governor state machine
-- (tracking|near_limit|paused_for_budget|cancelled_by_budget); NULL max_usd = no
-- cap (unchanged behavior for every existing job).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_usd            NUMERIC(12,6);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS budget_state       TEXT DEFAULT 'tracking';

-- Task ordering (buyer-ready merge in input order) + straggler-hedge lineage.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chunk_index INT DEFAULT 0;          -- position within the job's input
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS hedged_from UUID;                   -- original task this is a hedge/tiebreak of

-- Worker capability, queryable (the agent already advertises these on register).
ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_jobs     TEXT[];        -- job_type tags this worker can run
ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_models   TEXT[];        -- model ids resident/runnable locally
ALTER TABLE workers ADD COLUMN IF NOT EXISTS min_payout_usd_hr  REAL DEFAULT 0;-- operator reservation price ($/hr)
ALTER TABLE workers ADD COLUMN IF NOT EXISTS thermal_ok         BOOLEAN DEFAULT true;

-- Dynamic-throttling resource state, refreshed on every heartbeat (agent reads
-- REAL available memory each cycle). The SKIP-LOCKED claim filters on these so a
-- worker is never handed a task it cannot SAFELY run: effective_memory_gb is the
-- allocatable pool AFTER the supplier's reserved headroom, and `throttled` is the
-- agent pausing for memory pressure. effective_memory_gb stays NULL until the
-- first heartbeat, so the claim falls back to total memory_gb (no regression for
-- a just-registered worker); `throttled` defaults false (claimable).
ALTER TABLE workers ADD COLUMN IF NOT EXISTS effective_memory_gb REAL;          -- allocatable for jobs = available − headroom (NULL → fall back to memory_gb)
ALTER TABLE workers ADD COLUMN IF NOT EXISTS available_memory_gb REAL;          -- live free + reclaimable memory (GB)
ALTER TABLE workers ADD COLUMN IF NOT EXISTS reserved_headroom_gb REAL;         -- GB the operator reserves for their own use
ALTER TABLE workers ADD COLUMN IF NOT EXISTS throttled          BOOLEAN DEFAULT false; -- agent currently pausing new claims (memory pressure)

-- Supplier jurisdiction (data-residency match) + quarantine timestamp.
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS data_country   TEXT;            -- ISO country the supplier operates in
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ;     -- set by auto-quarantine (Verification V2)

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane C / Compute Autopilot (docs/PLANE_C.md) — quote intelligence.
-- ─────────────────────────────────────────────────────────────────────────────
-- A quote is the central Plane C object: what the system BELIEVED about a job
-- before the buyer spent money (cost band, ETA band, eligible supply, OOM risk,
-- the input scan). Persisting the assumptions is the load-bearing rule (PLANE_C
-- §6): a later invoice can say what was believed at quote time, and quote-to-actual
-- drift can be measured. The full structured quote is kept in quote_json; the
-- scalar columns make assumptions queryable (admin drift views). The normalized
-- quote_inputs / quote_supply_snapshot tables (PLANE_C §6) are folded into
-- quote_json for the MVP and can be split out later without a data migration.

CREATE TABLE IF NOT EXISTS quotes (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at         TIMESTAMPTZ DEFAULT now(),
    buyer_id           UUID,
    job_type           TEXT NOT NULL,
    model_ref          TEXT,
    tier               TEXT,
    records            BIGINT,            -- non-blank JSONL records scanned
    input_bytes        BIGINT,
    estimated_tokens   BIGINT,            -- byte/token heuristic (documented, not exact)
    malformed_records  INT,
    split_size         INT,              -- recommended lines/task
    task_count         INT,              -- estimated primary tasks
    eligible_now       INT,              -- workers passing the claim filter for this job, seen <60s
    cost_expected_usd  NUMERIC(12,6),
    cost_min_usd       NUMERIC(12,6),
    cost_max_usd       NUMERIC(12,6),
    eta_p50_secs       INT,
    eta_p90_secs       INT,
    oom_risk           TEXT,             -- low|medium|high (conservative, explainable)
    confidence         REAL,             -- 0.0–1.0
    quote_json         JSONB             -- the full quote object returned to the buyer
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane C errata / Plane D D0 (docs/PLANE_C_ERRATA.md, docs/PLANE_D.md §6) —
-- immediate typed failure + buyer-visible event timeline.
-- ─────────────────────────────────────────────────────────────────────────────
-- task_failures is the structural fix for silent OOM + money drain: when a worker
-- KNOWS a task cannot complete it reports a typed failure (POST /v1/worker/task/
-- {id}/fail) instead of stranding it for the 30-min stale reaper. failure_class is
-- the shared taxonomy (control/failure.go + agent/src/failure.rs); retryable +
-- buyer_fault drive immediate-requeue vs terminal-refund. memory is the agent's
-- snapshot at failure (real, never faked) for OOM diagnosis + quote-risk feedback.

CREATE TABLE IF NOT EXISTS task_failures (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ DEFAULT now(),
    task_id       UUID REFERENCES tasks,
    job_id        UUID,
    worker_id     UUID,
    failure_class TEXT NOT NULL,    -- shared taxonomy: oom|bad_input|model_load_failed|timeout|...
    retryable     BOOLEAN NOT NULL,
    buyer_fault   BOOLEAN NOT NULL,
    message       TEXT,             -- short, buyer-safe summary (operator detail stays in logs)
    backend       TEXT,
    model_ref     TEXT,
    duration_ms   BIGINT,
    memory        JSONB             -- {total_gb, available_gb, effective_gb, reserved_headroom_gb} at failure
);

-- job_events is the append-only buyer-visible timeline (PLANE_C §16, errata C-3):
-- the buyer should not infer state from status fields alone. event is the shared
-- enum (job_created|task_failed|task_requeued|job_failed|...); buyer_text is a
-- safe-to-show summary, detail holds operator context (ids/class).
CREATE TABLE IF NOT EXISTS job_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    job_id      UUID NOT NULL,
    task_id     UUID,
    event       TEXT NOT NULL,
    buyer_text  TEXT,
    detail      JSONB
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS tasks_job_status_idx     ON tasks (job_id, status);
CREATE INDEX IF NOT EXISTS tasks_worker_status_idx  ON tasks (worker_id, status);
CREATE INDEX IF NOT EXISTS tasks_status_visible_idx ON tasks (status, visible_at);  -- queue claim path
CREATE INDEX IF NOT EXISTS tasks_ready_unclaimed_idx ON tasks (status, (COALESCE(visible_at, created_at)), created_at)
    WHERE claimed_by IS NULL AND status IN ('queued','retrying');  -- hot SKIP-LOCKED claim path
CREATE INDEX IF NOT EXISTS ledger_supplier_payout_idx ON ledger_entries (supplier_id, payout_status);
CREATE INDEX IF NOT EXISTS ledger_kind_idx             ON ledger_entries (kind);  -- reconcile/audit sums by kind
CREATE INDEX IF NOT EXISTS workers_hwclass_seen_idx  ON workers (hw_class, last_seen_at);
CREATE INDEX IF NOT EXISTS webhooks_job_idx          ON webhooks (job_id);
CREATE INDEX IF NOT EXISTS models_job_type_idx       ON models (job_type);
CREATE INDEX IF NOT EXISTS tasks_job_chunk_idx        ON tasks (job_id, chunk_index);  -- ordered merge
CREATE INDEX IF NOT EXISTS workers_supplier_idx       ON workers (supplier_id);
CREATE INDEX IF NOT EXISTS quotes_buyer_created_idx   ON quotes (buyer_id, created_at DESC);  -- buyer quote history + admin drift
CREATE INDEX IF NOT EXISTS job_events_job_idx         ON job_events (job_id, created_at);       -- buyer event timeline (ordered)

-- disputes: the buyer-dispute record — the anti-defection / optimistic-verification
-- primitive ROADMAP_STATUS flags as missing ("a buyer-dispute mechanism — none exists
-- today"). A buyer files a dispute on a completed job's result; RESOLUTION by optimistic
-- recompute (Verde-style operator bisection / TAO tolerance-aware FP verification —
-- docs/PRODUCTION_AUDIT.md §2.3) is the frontier seam: recorded here, not yet auto-resolved.
CREATE TABLE IF NOT EXISTS disputes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID NOT NULL REFERENCES jobs,
    buyer_id    UUID NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open|resolved|rejected
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS disputes_job_idx ON disputes (job_id, created_at);

-- verification_events is the append-only RECEIPT log: the verification machinery
-- (control/verification.go) already applies reputation deltas on every outcome, but
-- the OUTCOMES themselves were not persisted, so a buyer could not see what was
-- checked. Each row is one verification fact emitted co-located with its reputation
-- dock (honeypot pass/fail, redundancy match/mismatch, tiebreak win/loss). Writes are
-- best-effort and NEVER block the verify/money path; the aggregate is grouped by
-- job_id for the buyer-facing job-status `verification` block. kind is the closed set
-- {honeypot_pass|honeypot_fail|redundancy_match|redundancy_mismatch|tiebreak_win|tiebreak_loss}.
CREATE TABLE IF NOT EXISTS verification_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id      UUID NOT NULL REFERENCES jobs,
    task_id     UUID,
    supplier_id UUID,
    kind        TEXT NOT NULL,  -- honeypot_pass|honeypot_fail|redundancy_match|redundancy_mismatch|tiebreak_win|tiebreak_loss
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS verification_events_job_idx ON verification_events (job_id, created_at);
-- reverify_task_id links a dispute to the independent re-run dispatched to resolve it
-- (status flow: open|no_peer -> reverifying -> resolved|rejected|unresolvable).
ALTER TABLE disputes ADD COLUMN IF NOT EXISTS reverify_task_id UUID;
CREATE INDEX IF NOT EXISTS task_failures_job_idx      ON task_failures (job_id, created_at DESC); -- failure drill-down by job

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane D D7 / Plane C errata C-Errata-4 (docs/PLANE_D.md §13) — quote-to-submit
-- binding. An advisory quote can be bound to the submission that acts on it, so a
-- later invoice can say "here is what you were told". expires_at bounds how long a
-- quote stays bindable (15 min); input_sha256 lets the submit path best-effort
-- confirm the bytes match what was quoted; jobs.quote_id records the binding.
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS expires_at    TIMESTAMPTZ;             -- quote stops being bindable after this (now()+15m at insert)
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS input_sha256  TEXT;                    -- sha256 of the scanned input bytes, for best-effort submit match
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS quote_id      UUID;                    -- the advisory quote this job was bound to (NULL = none)
CREATE INDEX IF NOT EXISTS jobs_quote_idx ON jobs (quote_id) WHERE quote_id IS NOT NULL;  -- quote→job lookups (invoice/admin drift)

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane D D6 / Plane C errata C-Errata-6 (docs/PLANE_D.md §12) — quote-to-actual
-- drift feedback. The quote's ETA is only as good as the static throughput target
-- until we measure reality; task_durations records the REAL per-task duration of
-- every COMMITTED task (malformed/failed tasks never write a row, so they cannot
-- poison the estimate) so the Exchange Brain can learn an observed p90 and the next
-- quote's ETA leans on it instead of the target. jobs.eta_secs (above) already holds
-- the quoted ETA — it is REUSED as the quoted side of the drift rollup, not duplicated.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_durations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    job_id      UUID,
    job_type    TEXT,
    model_ref   TEXT,
    split_size  INT,               -- the job's lines/task, so drift can be sliced by chunk size
    duration_ms BIGINT             -- the committing worker's reported task wall-time (never faked)
);
CREATE INDEX IF NOT EXISTS task_durations_type_model_idx ON task_durations (job_type, model_ref);  -- p90 history + drift rollup lookups

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane D D4 (docs/PLANE_D.md §10) — memory telemetry persistence + quote risk.
-- ─────────────────────────────────────────────────────────────────────────────
-- The heartbeat already carries the worker's live available/effective memory and
-- whether it is throttled (workers.* columns hold only the LATEST beat, which the
-- claim filter reads). worker_memory_samples appends a ROLLING sample each beat so
-- the system has a memory-pressure history, not just a snapshot: GET /admin/capacity
-- reports recent avg available/effective per worker, and quote risk (quote.go
-- assessRisk → applyMemoryFloorRisk) compares a model's memory floor against the
-- MEDIAN effective memory of eligible workers (from these samples) to bump oom_risk
-- when the floor is tight. All real telemetry — every value is the agent's reported
-- number, never faked. Retention/capping is out of scope (the rows are cheap; a
-- later sweeper can trim by created_at, which the index already orders on).
CREATE TABLE IF NOT EXISTS worker_memory_samples (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    worker_id    UUID,
    available_gb REAL,              -- live free + reclaimable memory the beat reported (GB)
    effective_gb REAL,              -- allocatable-for-jobs pool after the supplier's headroom (GB)
    throttled    BOOLEAN,           -- the worker was pausing new claims at sample time (memory pressure)
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS worker_memory_samples_worker_time_idx
    ON worker_memory_samples (worker_id, created_at DESC);  -- recent-N-per-worker + median-effective lookups

-- ─────────────────────────────────────────────────────────────────────────────
-- Plane D D3 (docs/PLANE_D.md §9) — warm-model state + routing preference.
-- ─────────────────────────────────────────────────────────────────────────────
-- The heartbeat carries loaded_models: the ids of models currently WARM in the
-- agent's pool. HeartbeatWorker upserts one row here per warm id (last_seen_warm =
-- now()), so the control plane knows which (worker, model) pairs avoid a cold model
-- load right now. The scheduler reads this as a SMALL re-rank bonus: a worker with
-- the job's model already warm sorts ahead of an otherwise-equal cold worker (the
-- fastest task avoids a load). It NEVER overrides the claim's hard filter — warm
-- only re-ranks fit/throttle-eligible workers. Rows are cheap and self-refreshing;
-- staleness is read against last_seen_warm (a worker that stops reporting a model
-- ages out), so no separate eviction is required. All real telemetry — a row exists
-- only because the agent reported that id warm, never fabricated.
CREATE TABLE IF NOT EXISTS worker_model_state (
    worker_id      UUID NOT NULL,
    model_id       TEXT NOT NULL,
    last_seen_warm TIMESTAMPTZ DEFAULT now(),  -- last heartbeat that reported this model warm
    PRIMARY KEY (worker_id, model_id)
);
CREATE INDEX IF NOT EXISTS worker_model_state_model_idx
    ON worker_model_state (model_id, last_seen_warm DESC);  -- "which live workers have THIS model warm" (scheduler + quote)
