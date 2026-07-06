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
    status       TEXT DEFAULT 'pending',  -- pending|active|suspended|banned
    -- Maintained running count of this supplier's lifetime completed tasks
    -- (Control Plane Hot Path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md):
    -- incremented once per real commit (CommitTask) instead of ClaimTask
    -- re-scanning `tasks` with a `count(*)` on every single claim.
    completed_tasks BIGINT NOT NULL DEFAULT 0
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
    p99_latency_ms REAL,
    load_ms        BIGINT DEFAULT 0 -- cold-load wall-clock ms the agent measured (0 = pre-load_ms agent build)
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
    claimed_by      UUID,                     -- worker currently holding the task
    claimed_at      TIMESTAMPTZ,              -- when the claim was taken
    visible_at      TIMESTAMPTZ DEFAULT now(),-- task is claimable only once now() >= visible_at
    -- Verification-requeue worker exclusion (Scheduling & Matching Engine 8->9,
    -- docs/internal/CREED_AND_PATH_TO_TEN.md "add backoff plus worker-exclusion to
    -- verification-requeue so a chunk that just failed verification doesn't
    -- immediately return to the same worker with no delay"). When a task is requeued
    -- after a failed honeypot, RequeueTask records the worker that just failed it
    -- here and until excluded_until; the claim query skips that worker for the
    -- window (a DIFFERENT worker gets first crack), then the exclusion expires so a
    -- thin/single-worker fleet is never permanently starved of the retry.
    excluded_worker UUID,                     -- worker to skip on the next claim (the one that just failed it)
    excluded_until  TIMESTAMPTZ               -- exclusion is only in force while now() < excluded_until (NULL = no exclusion)
);
-- input_ref added after the fact for already-created tables (idempotent ALTER).
-- A task is a split of its job; when its own input_ref is null the dispatch uses
-- the parent job's input_ref. This column lets a job fan out into per-chunk
-- inputs without a second table.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS input_ref TEXT;
-- Verification-requeue worker exclusion (Scheduling & Matching Engine 8->9) —
-- idempotent ALTERs for already-created tables. See the column comments above.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS excluded_worker UUID;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS excluded_until  TIMESTAMPTZ;

-- E3 · autovacuum tuning for the hottest table. `tasks` churns constantly: every
-- claim/start/commit/fail/requeue is an UPDATE, so dead tuples (and the visibility
-- bloat that slows the FOR UPDATE SKIP LOCKED claim scan) accumulate fast. The DB
-- defaults (scale_factor 0.2 = vacuum/analyze only after 20% of the table turns
-- over) are tuned for cold tables and let bloat ride on a queue. Drop to small
-- scale factors with flat thresholds so autovacuum fires on absolute churn, and
-- raise the cost limit so a vacuum keeps pace instead of falling behind under load.
-- Idempotent (ALTER ... SET is a no-op replay) and table-scoped · no global GUC change.
ALTER TABLE tasks SET (
    autovacuum_vacuum_scale_factor  = 0.02,
    autovacuum_vacuum_threshold     = 50,
    autovacuum_analyze_scale_factor = 0.02,
    autovacuum_analyze_threshold    = 50,
    autovacuum_vacuum_cost_limit    = 1000
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ DEFAULT now(),
    kind          TEXT NOT NULL,  -- 'buyer_charge'|'supplier_credit'|'platform_take'|'clawback'|'stripe_fee'
                                  -- 'stripe_fee': the REAL Stripe processing fee of one successful PaymentIntent
                                  -- (latest_charge.balance_transaction.fee, fetched — never estimated), stored
                                  -- negative with payout_ref = the PaymentIntent id. One row per PI, enforced by
                                  -- ledger_stripe_fee_ref_uniq below, so a retried fee fetch can never double-count.
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

-- Self-serve buyer accounts. Until now a buyer_id was a free-floating UUID minted
-- by the seed / referenced by api_keys; there was no way to sign UP. This is the
-- account of record: a UNIQUE email + a bcrypt password_hash (cost >= 12, set in
-- control/accounts.go · NEVER a plaintext or a fast hash). free_credit_usd is the
-- sandbox grant a new buyer gets so they can run jobs before adding a card; the 402
-- submit gate exempts spend up to this, then requires a card honestly. The id is the
-- buyer_id every existing buyer-scoped table already keys on, so accounts slot in
-- without a data migration. password_hash is nullable so a seeded / API-key-only
-- buyer (no password) is representable; login requires a non-null hash.
CREATE TABLE IF NOT EXISTS buyers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT,                      -- bcrypt; NULL = no password set (seed / API-key-only)
    free_credit_usd NUMERIC(12,6) NOT NULL DEFAULT 0,  -- sandbox grant; 402 gate exempts spend up to this
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Opaque session tokens for the web/app login flow, hashed at rest exactly like
-- api_keys/worker_tokens (only the SHA-256 hash is stored; the raw token is shown
-- once at login and never recoverable). authBuyer accepts a cx_sess_ bearer as well
-- as an api key. expires_at bounds the session; revoked supports explicit logout.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,               -- SHA-256 of the raw cx_sess_ token
    buyer_id   UUID NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked    BOOLEAN DEFAULT false
);
CREATE INDEX IF NOT EXISTS sessions_buyer_idx ON sessions (buyer_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id   UUID,
    key_hash   TEXT UNIQUE NOT NULL,      -- store a hash, never the raw key
    is_admin   BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    revoked    BOOLEAN DEFAULT false
);
-- Buyer-managed key lifecycle (POST/GET/DELETE /v1/keys). `name` is a human label;
-- `masked` is a NON-secret display hint captured at mint (prefix + last4) so the list
-- view can show which key is which WITHOUT ever reconstructing the raw secret (only
-- key_hash is stored · the raw value is revealed once and is unrecoverable). Idempotent
-- ALTERs so older DBs created before the lifecycle columns upgrade cleanly.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS name   TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS masked TEXT;

-- Admin passkey (WebAuthn) credentials — the operator's own device authenticators
-- (Touch ID / a security key) for the /admin panel. Single-operator: every row is the
-- one operator's. None of these fields is a secret: a credential id + COSE public key
-- gate nothing on their own — only a device holding the matching PRIVATE key can
-- produce a valid assertion — so they are stored plainly. sign_count is the standard
-- clone-detection counter (a decrease signals a cloned authenticator).
CREATE TABLE IF NOT EXISTS admin_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credential_id BYTEA NOT NULL UNIQUE,      -- the WebAuthn credential id (lookup + exclusion)
    credential    JSONB NOT NULL,             -- the full go-webauthn Credential (public key, sign_count, aaguid, …)
    label         TEXT,                       -- human label, e.g. "Josh MacBook Touch ID"
    created_at    TIMESTAMPTZ DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);

-- Admin sessions minted after a successful passkey login. Kept SEPARATE from the
-- buyer `sessions` table by design (a buyer session is never admin). Only the SHA-256
-- hash of the opaque cx_admin_ token is stored; the raw token lives in an httpOnly
-- Secure cookie in the operator's browser and is unrecoverable from the DB.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
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
-- The verification class the known_answer was produced under, as "engine|build_hash"
-- (or '' = unknown). For a BYTE-EXACT job type the verifier auto-quarantines on a
-- honeypot byte mismatch ONLY when the committing worker shares this class — a
-- class-blind ('' ) or cross-class byte honeypot is NOT grounds to quarantine an
-- honest worker whose engine/build legitimately produces different bytes (the audit's
-- "Candle-seeded answer would byte-fail a correct vLLM result" hazard). Tolerant job
-- types (embed/classification/json/rerank) compare semantics and ignore this column.
-- DEFAULT '' so existing/seed honeypots (class-blind) keep working — they simply stop
-- auto-quarantining cross-class byte mismatches, which is the safe behavior. Full
-- hw_class-aware honeypot seeding is the Wave-2 prerequisite (docs/DETERMINISM_CLASS.md).
ALTER TABLE honeypots ADD COLUMN IF NOT EXISTS answer_class TEXT NOT NULL DEFAULT '';

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
    ('bge-small-en-v1.5', 'bge',   NULL,   'embed',   384,  'embed',            0.00100000, NULL,        2, 'BAAI/bge-small-en-v1.5'),
    ('llama-3.2-1b-instruct-q4', 'llama', 'q4_k_m', 'gguf', NULL, 'batch_infer', 0.00200000, NULL,        4, 'unsloth/Llama-3.2-1B-Instruct-GGUF'),
    ('qwen2.5-7b-instruct-q4', 'qwen', 'q4_k_m', 'gguf', NULL, 'batch_infer',    0.00800000, NULL,       40, 'bartowski/Qwen2.5-7B-Instruct-GGUF'),
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
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_status      TEXT NOT NULL DEFAULT 'not_attempted'; -- not_attempted|charged|failed|no_payment_method|deferred (queryable charge state, not log-only).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS results_merged_at  TIMESTAMPTZ;      -- watermark: set when the buyer-ready artifact was last successfully merged, so GET /v1/jobs/{id}/results only re-merges once since completion instead of on every poll
-- 'deferred': the job settled BELOW the CX_CHARGE_MIN_USD batching threshold, so it is
-- deliberately not charged alone (a ~30¢ Stripe fixed fee on a sub-$5 charge is fee
-- bleed); the charge-collect sweep groups deferred jobs per buyer into charge_batches
-- and bills once the buyer's deferred sum crosses the threshold or the oldest deferred
-- job turns 24h old. The money stays honestly owed in the ledger the whole time.
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

-- Control Plane Hot Path 8->9 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Get
-- result-commit off the S3 critical path"): the worker-reported SHA-256 (hex) of
-- its own committed result bytes, persisted at CommitTask. A later commit's
-- redundancy/honeypot comparison trusts a hash-to-hash match for byte-exact job
-- types instead of a second synchronous S3 GetObject inside the commit
-- transaction. NULL for an older agent that omits it (or a pre-migration row) —
-- the commit handler always falls back to a real GetObject when a hash is
-- missing, so correctness never depends on this column being populated.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS result_sha256 TEXT;

-- Worker capability, queryable (the agent already advertises these on register).
ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_jobs     TEXT[];        -- job_type tags this worker can run
ALTER TABLE workers ADD COLUMN IF NOT EXISTS supported_models   TEXT[];        -- model ids resident/runnable locally
ALTER TABLE workers ADD COLUMN IF NOT EXISTS min_payout_usd_hr  REAL DEFAULT 0;-- operator reservation price ($/hr)
ALTER TABLE workers ADD COLUMN IF NOT EXISTS thermal_ok         BOOLEAN DEFAULT true;

-- The on-device inference ENGINE this worker runs (candle|mlx|vllm|hawking). It is
-- the SECOND axis of the verification class alongside hw_class: byte-exact redundancy
-- peers and honeypots are drawn from the same (hw_class, engine), because two engines'
-- FP kernels differ even on identical hardware, so a future mlx/vllm/hawking worker is
-- never byte-compared against a Candle one. DEFAULT 'candle' so every existing worker
-- row (and an older agent that does not advertise the field) keeps today's behavior —
-- a single-engine Candle fleet's (hw_class, engine) class collapses back to hw_class.
ALTER TABLE workers ADD COLUMN IF NOT EXISTS engine             TEXT NOT NULL DEFAULT 'candle';

-- The FINER axis of the verification class BELOW (hw_class, engine): a stable hash of
-- the byte-output-determining BUILD inputs (engine + agent build + device backend +
-- catalogue quant — agent hardware::engine_build_hash). Two workers in the same
-- hw_class + engine but on different agent builds (a kernel/codegen change between
-- releases) can emit different bytes even on identical hardware, so BYTE-EXACT
-- redundancy peers + honeypots are pinned to the same (hw_class, engine, build_hash);
-- a cross-build byte mismatch is NOT an auto-dock — it falls back to provisional trust
-- (the missing-third-worker pattern). DEFAULT '' = "unknown build": an older agent that
-- does not advertise it is never drawn as a byte-exact peer and never auto-docked, so a
-- single-build fleet that all reports the same hash collapses the class to today's
-- behavior. See docs/DETERMINISM_CLASS.md.
ALTER TABLE workers ADD COLUMN IF NOT EXISTS build_hash         TEXT NOT NULL DEFAULT '';

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

-- Stripe Connect payout readiness, flipped by the account.updated webhook
-- (control/suppliers.go). A supplier can only be PAID once Stripe says its
-- connected account can receive transfers; this column is the cached view the
-- status endpoint reads without a live Stripe call. Default false (not yet able).
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS payouts_enabled BOOLEAN DEFAULT false;

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
--
-- job_events is a declarative RANGE-partitioned table (Postgres Data Lifecycle 6->7) —
-- its parent + partitions are created together with the other two telemetry tables by
-- cx_partition_telemetry() further down (all three must be created after their column
-- shapes are declared, so the definition lives there, not here).

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
-- latest-benchmark lookup for the claim's throughput tiebreak (worker × job_type, newest first)
CREATE INDEX IF NOT EXISTS benchmark_worker_type_time_idx ON benchmark_results (worker_id, job_type, measured_at DESC);
CREATE INDEX IF NOT EXISTS workers_class_engine_seen_idx ON workers (hw_class, engine, build_hash, last_seen_at);  -- (hw_class, engine, build_hash) redundancy-peer class lookups
CREATE INDEX IF NOT EXISTS webhooks_job_idx          ON webhooks (job_id);
CREATE INDEX IF NOT EXISTS models_job_type_idx       ON models (job_type);
CREATE INDEX IF NOT EXISTS tasks_job_chunk_idx        ON tasks (job_id, chunk_index);  -- ordered merge
CREATE INDEX IF NOT EXISTS workers_supplier_idx       ON workers (supplier_id);
CREATE INDEX IF NOT EXISTS quotes_buyer_created_idx   ON quotes (buyer_id, created_at DESC);  -- buyer quote history + admin drift
-- job_events_job_idx is created by cx_partition_telemetry() alongside the partitioned
-- job_events parent (it cannot be created here — job_events does not exist yet at this
-- point in the file on a fresh DB, and the partitioned parent propagates the index to
-- every leaf).

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
    kind        TEXT NOT NULL,  -- honeypot_pass|honeypot_fail|redundancy_match|redundancy_mismatch|tiebreak_win|tiebreak_loss|redundancy_cross_class|tiebreak_cross_class|redundancy_same_supplier
                                -- *_cross_class: a byte-exact comparison was skipped because the peer was in a DIFFERENT
                                -- verification class (engine/build_hash) — recorded for forensics, NOT counted as "checked".
                                -- redundancy_same_supplier: the only agreeing peer shared the committing supplier, so the
                                -- match was NOT counted as independent (no redundancy_match credit). The task still
                                -- succeeds; it is simply not independently verified (backlog P0 items 6-7).
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
-- Postgres Data Lifecycle 6->7 (docs/internal/CREED_AND_PATH_TO_TEN.md): task_durations,
-- worker_memory_samples and job_events are declarative RANGE-PARTITIONED tables (monthly
-- partitions on created_at) so expired history is dropped by an O(1) DROP PARTITION
-- instead of an O(rows) DELETE competing with autovacuum. All three are created together
-- by cx_partition_telemetry() below (after all three column shapes are declared), because
-- a partitioned parent needs its DEFAULT + month partitions created in the same breath to
-- be insertable. See that function for the full rationale (composite PK, NOT NULL
-- created_at, leaf-level autovacuum, fresh-vs-existing-DB convergence with the Go
-- migration control/partition.go).

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
-- number, never faked. Retention is enforced by monthly partitioning (6->7 below) plus
-- the hourly DELETE sweep that trims the sub-month tail.
--
-- ─────────────────────────────────────────────────────────────────────────────
-- Postgres Data Lifecycle 6->7 (docs/internal/CREED_AND_PATH_TO_TEN.md) — the three
-- telemetry tables, created as declarative RANGE-partitioned tables.
-- ─────────────────────────────────────────────────────────────────────────────
-- cx_partition_telemetry(table, columns, index_name, index_body, autovacuum) creates one
-- partitioned parent with a composite (created_at, id) PK, its secondary index, a DEFAULT
-- catch-all partition, and month partitions spanning [now()-1 month, now()+2 months] — all
-- with leaf-level autovacuum params (a partitioned PARENT cannot carry storage params, so
-- they go on every leaf). It is a COMPLETE NO-OP when a relation of that name already
-- exists in ANY form (plain or partitioned): a fresh DB is born partitioned here; an
-- existing plain-table DB is left untouched for the Go migration
-- (control/partition.go MigrateTelemetryPartitions) to convert IN PLACE, preserving every
-- row. Both paths converge to the identical shape, and the rotation job
-- (control/workers.go rotateTelemetryPartitions) keeps months current thereafter.
--
-- The composite PK (created_at, id) is mandatory (a partition-key column must be in every
-- unique constraint); no reader looks these rows up by id and no FK references them, so
-- gen_random_uuid()'s collision-freeness preserves effective global id-uniqueness.
-- created_at is NOT NULL because a RANGE partition key cannot be NULL — every existing row
-- already has a non-NULL created_at (it has defaulted to now() since creation).
CREATE OR REPLACE FUNCTION cx_partition_telemetry(
    p_table text, p_columns text, p_index_name text, p_index_body text, p_autovacuum text
) RETURNS void LANGUAGE plpgsql AS $cx$
DECLARE
    m date; lo date; hi date; part text; nextm date;
BEGIN
    -- No-op if the relation already exists in any form (idempotent + non-destructive).
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = p_table AND relnamespace = 'public'::regnamespace) THEN
        RETURN;
    END IF;
    EXECUTE format('CREATE TABLE %I (%s, PRIMARY KEY (created_at, id)) PARTITION BY RANGE (created_at)', p_table, p_columns);
    EXECUTE format('CREATE INDEX %I ON %I %s', p_index_name, p_table, p_index_body);
    EXECUTE format('CREATE TABLE %I PARTITION OF %I DEFAULT WITH (%s)', p_table || '_default', p_table, p_autovacuum);
    lo := date_trunc('month', (now() - interval '1 month'))::date;   -- one back so a just-inserted row near a month edge has a home
    hi := date_trunc('month', (now() + interval '2 months'))::date;  -- create-ahead headroom (matches partitionCreateAheadMonths)
    m := lo;
    WHILE m <= hi LOOP
        nextm := (m + interval '1 month')::date;
        part := p_table || '_p' || to_char(m, 'YYYY_MM');
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L) WITH (%s)',
                       part, p_table, m::timestamptz, nextm::timestamptz, p_autovacuum);
        m := nextm;
    END LOOP;
END;
$cx$;

SELECT cx_partition_telemetry(
    'worker_memory_samples',
    $c$id           UUID NOT NULL DEFAULT gen_random_uuid(),
       worker_id    UUID,
       available_gb REAL,
       effective_gb REAL,
       throttled    BOOLEAN,
       created_at   TIMESTAMPTZ NOT NULL DEFAULT now()$c$,
    'worker_memory_samples_worker_time_idx', '(worker_id, created_at DESC)',
    'autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=200, autovacuum_analyze_scale_factor=0.02, autovacuum_analyze_threshold=200, autovacuum_vacuum_cost_limit=1000'
);
SELECT cx_partition_telemetry(
    'task_durations',
    $c$id          UUID NOT NULL DEFAULT gen_random_uuid(),
       created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
       job_id      UUID,
       job_type    TEXT,
       model_ref   TEXT,
       split_size  INT,
       duration_ms BIGINT,
       worker_id   UUID,
       engine      TEXT,
       build_hash  TEXT$c$,
    'task_durations_type_model_idx', '(job_type, model_ref)',
    'autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_threshold=100, autovacuum_analyze_scale_factor=0.05, autovacuum_analyze_threshold=100, autovacuum_vacuum_cost_limit=500'
);
SELECT cx_partition_telemetry(
    'job_events',
    $c$id          UUID NOT NULL DEFAULT gen_random_uuid(),
       created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
       job_id      UUID NOT NULL,
       task_id     UUID,
       event       TEXT NOT NULL,
       buyer_text  TEXT,
       detail      JSONB$c$,
    'job_events_job_idx', '(job_id, created_at)',
    'autovacuum_vacuum_scale_factor=0.1, autovacuum_vacuum_threshold=100, autovacuum_analyze_scale_factor=0.1, autovacuum_analyze_threshold=100, autovacuum_vacuum_cost_limit=500'
);

-- Postgres Data Lifecycle 5→6 (docs/internal/CREED_AND_PATH_TO_TEN.md): autovacuum
-- tuning for the telemetry tables sweepTelemetryRetention (control/workers.go
-- telemetryTables) now DELETEs from on an hourly ticker. Bounding the tables (4→5)
-- fixed unbounded growth but introduced a NEW churn shape the DB defaults were never
-- tuned for: instead of a slow trickle of dead tuples from scattered UPDATEs (what
-- the 0.2 default scale factor assumes), each table now takes one large DELETE burst
-- per hour, all at once. On a big table 20%-of-table-since-last-vacuum can be a huge
-- absolute number of dead tuples sitting unvacuumed between the hourly bursts,
-- bloating both the table and worker_memory_samples_worker_time_idx /
-- task_durations_type_model_idx / job_events_job_idx (which every real read of these
-- tables goes through — ListWorkers' recent-N-per-worker LATERAL, the p90/drift
-- rollup, and the buyer-facing GET /v1/jobs/{id}/events timeline). Same discipline as
-- the `tasks` table's own E3 tuning above: small scale factors with flat thresholds
-- so autovacuum fires on absolute churn instead of waiting for a fraction of the
-- table, sized per table to its actual insert-vs-delete pattern:
--
--   worker_memory_samples — the hottest of the three. One row per worker per
--   heartbeat-with-memory-reporting (control/store.go InsertWorkerMemorySample) is
--   the highest insert rate of any telemetry table (~2,880 rows/worker/day, per the
--   facet's own sizing note above), and the 14-day retention window is the
--   shortest, so the hourly sweep's delete burst is proportionally the largest
--   fraction of the table each cycle. Tuned as aggressively as `tasks` itself.
--
--   task_durations — one row per COMMITTED task (not per heartbeat), so a lower
--   insert rate than worker_memory_samples, but the 30-day window means the hourly
--   delete burst still removes a meaningful slice of the table each cycle at any
--   real task volume. Tuned between `tasks` and job_events: still low, not as low
--   as the heartbeat table.
--
--   job_events — a handful of rows per job/task (creation, failures, requeues), the
--   lowest insert rate of the three, AND the 180-day retention window means any
--   single hourly delete burst is a much smaller fraction of total table size than
--   the other two. Still tuned below the 0.2 default (it is still hourly-delete
--   churn, not cold storage), but the least aggressive of the three.
--
-- Idempotent and table-scoped · no global GUC change. Since 6->7 these tables are
-- PARTITIONED, and a partitioned PARENT rejects storage parameters (Postgres requires
-- them on the leaves), so the tuning is applied per-leaf by cx_partition_telemetry()
-- and the rotation job at partition-creation time — NOT on the parent here. This block
-- therefore applies the params ONLY when the table is still a PLAIN table (relkind 'r'):
-- on a fresh partitioned DB it is a no-op (relkind 'p'); on an older DB whose telemetry
-- tables are still plain (pre-6->7, awaiting the Go in-place conversion) it keeps them
-- tuned exactly as before, and the Go migration then carries the same params onto every
-- leaf when it converts. cx_apply_plain_autovacuum() encapsulates that relkind guard.
CREATE OR REPLACE FUNCTION cx_apply_plain_autovacuum(p_table text, p_params text)
RETURNS void LANGUAGE plpgsql AS $cx$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = p_table
                 AND relnamespace = 'public'::regnamespace AND relkind = 'r') THEN
        EXECUTE format('ALTER TABLE %I SET (%s)', p_table, p_params);
    END IF;
END;
$cx$;
SELECT cx_apply_plain_autovacuum('worker_memory_samples',
    'autovacuum_vacuum_scale_factor=0.02, autovacuum_vacuum_threshold=200, autovacuum_analyze_scale_factor=0.02, autovacuum_analyze_threshold=200, autovacuum_vacuum_cost_limit=1000');
SELECT cx_apply_plain_autovacuum('task_durations',
    'autovacuum_vacuum_scale_factor=0.05, autovacuum_vacuum_threshold=100, autovacuum_analyze_scale_factor=0.05, autovacuum_analyze_threshold=100, autovacuum_vacuum_cost_limit=500');
SELECT cx_apply_plain_autovacuum('job_events',
    'autovacuum_vacuum_scale_factor=0.1, autovacuum_vacuum_threshold=100, autovacuum_analyze_scale_factor=0.1, autovacuum_analyze_threshold=100, autovacuum_vacuum_cost_limit=500');

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

-- worker_tps_cache maintains ClaimTask's throughput tiebreak (Control Plane Hot
-- Path 7->8, docs/internal/CREED_AND_PATH_TO_TEN.md "Get the correlated-subquery
-- cost out of the transactional hot path"): the claim CTE used to run a fresh
-- correlated subquery (SELECT br.tps FROM benchmark_results ... ORDER BY
-- measured_at DESC LIMIT 1) for EVERY eligible candidate row, on EVERY single
-- claim — an O(candidate rows) cost paid on the hottest transactional path for a
-- number that only actually changes once per real worker state change (a fresh
-- benchmark report). This table is upserted once, in UpsertWorker's own
-- transaction, exactly when a new benchmark_results row lands (mirrors
-- worker_model_state's "maintained on write, read as O(1) on the claim path"
-- shape). ClaimTaskSQL now LEFT JOINs this table per (worker, job_type) instead
-- of running the correlated subquery — a plain indexed lookup, not a per-row scan.
CREATE TABLE IF NOT EXISTS worker_tps_cache (
    worker_id  UUID NOT NULL,
    job_type   TEXT NOT NULL,
    tps        REAL NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (worker_id, job_type)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Stuck-run watchdog V2 (control/workers.go reapStuckJobs) — escalation ladder,
-- buyer deadline policy, and the ETA calibration loop.
-- ─────────────────────────────────────────────────────────────────────────────
-- watchdog_strikes is the escalation state: 0 = never judged stuck; the first
-- stuck verdict RESCUES (unfinished tasks requeued to a different machine) and
-- sets it to 1; a second verdict KILLS (checkpoint + cancel + settle). Guarded
-- transitions (WHERE status='running' AND watchdog_strikes=0) keep concurrent
-- sweeps idempotent.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS watchdog_strikes INT NOT NULL DEFAULT 0;
-- deadline_secs is the buyer's watchdog policy knob (POST /v1/jobs):
--   NULL / 0 → default behavior (ETA-derived deadline with floor + 24h cap),
--   -1       → opt OUT of the watchdog entirely (run to completion),
--   60..604800 → an explicit wall-clock deadline (1 minute to 7 days).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deadline_secs INT;
-- eta_calibration is the feedback loop that tunes the watchdog's ETA factor:
-- one row per finalized job with an ETA prediction, pairing what was PREDICTED
-- at submit (jobs.eta_secs) with what was REALIZED (seconds from created_at to
-- finalize). Real observations only — a job with no prediction inserts nothing.
CREATE TABLE IF NOT EXISTS eta_calibration (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id         UUID,
    job_type       TEXT,
    tier           TEXT,
    predicted_secs INT,
    realized_secs  INT,
    created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS eta_calibration_type_idx ON eta_calibration (job_type, tier, created_at DESC);
-- One calibration row per job, enforced structurally: the two finalize sites (the
-- commit-path finalize and the webhook sweep) can race, and INSERT..WHERE NOT EXISTS
-- is not atomic under READ COMMITTED — without this index both could insert,
-- duplicating rows and double-counting the near-miss metric.
CREATE UNIQUE INDEX IF NOT EXISTS eta_calibration_job_uniq ON eta_calibration (job_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Charge batching + Stripe fee truth (control/collect.go, control/billing.go).
-- ─────────────────────────────────────────────────────────────────────────────
-- A charge batch is ONE PaymentIntent covering many small ('deferred') jobs of one
-- buyer, formed by the charge-collect sweep once the buyer's deferred sum crosses
-- CX_CHARGE_MIN_USD or the oldest deferred job turns 24h old. amount_usd is FROZEN
-- at formation: every retry re-sends exactly this amount under the same Stripe
-- idempotency key ("cxbatch-"+id), so an ambiguous prior attempt (e.g. a network
-- timeout after the PaymentIntent may or may not have been created) can never
-- double-charge — Stripe replays the first outcome. status: 'attempting' (formed,
-- not yet confirmed charged; retried every sweep) | 'charged' (stripe_pi + charged_at
-- set, member jobs flipped to charge_status='charged').
CREATE TABLE IF NOT EXISTS charge_batches (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id   UUID NOT NULL,
    amount_usd NUMERIC(10,6) NOT NULL,       -- FROZEN sum of the member jobs at formation
    status     TEXT NOT NULL DEFAULT 'attempting',  -- attempting|charged
    stripe_pi  TEXT,                          -- the PaymentIntent id, once confirmed
    created_at TIMESTAMPTZ DEFAULT now(),
    charged_at TIMESTAMPTZ
);
-- Money-truth hardening (adversarial-review fixes):
--   deferred_at        · when the job entered 'deferred'; the 24h batching age counts
--                        from here, not from job creation (long-queued jobs keep their
--                        full accumulation window).
--   charge_attempt_usd · the FROZEN amount of the first single-job charge attempt;
--                        every retry replays the same (idempotency key, amount) pair,
--                        immune to a later actual_usd re-settle.
--   charge_batches.attempts/next_at · failed-batch backoff (30min x attempts <= 6h),
--                        so a hard-declined card is not retried once a minute forever.
--   charge_batches.amount_usd widened to NUMERIC(12,6): an unbounded re-armed debt
--                        must overflow at $999,999, not at $9,999 (formation is also
--                        capped at 500 member jobs per batch).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deferred_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_attempt_usd NUMERIC(10,6);
ALTER TABLE charge_batches ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE charge_batches ADD COLUMN IF NOT EXISTS next_at TIMESTAMPTZ;
ALTER TABLE charge_batches ALTER COLUMN amount_usd TYPE NUMERIC(12,6);

CREATE INDEX IF NOT EXISTS charge_batches_status_idx ON charge_batches (status, created_at);
-- charge_batch_id stamps a deferred job into exactly one batch (stamped WHERE
-- charge_status='deferred' AND charge_batch_id IS NULL, so a concurrent sweep can
-- never double-batch a job). charge_attempts / charge_next_at back off the retry of
-- FAILED single-job charges (30min × attempts, capped at 6h) so a dead card is not
-- hammered every sweep; the amount stays owed in the ledger regardless.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_batch_id UUID;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS charge_next_at  TIMESTAMPTZ;
-- The PaymentIntent id of a successfully charged SINGLE job (batches carry theirs on
-- charge_batches.stripe_pi). Needed by the stripe_fee backfill scan: a charge whose
-- fee fetch failed is found by "charged with a pi, no stripe_fee ledger row" and the
-- real fee is fetched again next sweep. NULL for jobs charged before this column
-- existed (their fee is honestly unknown, never estimated).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stripe_pi TEXT;
CREATE INDEX IF NOT EXISTS jobs_charge_status_idx ON jobs (charge_status);
-- One stripe_fee row per PaymentIntent, structurally: the fee recorder is
-- INSERT-if-absent by payout_ref, and this partial unique index makes a racing
-- double-insert impossible rather than merely unlikely.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_stripe_fee_ref_uniq ON ledger_entries (payout_ref) WHERE kind = 'stripe_fee';

-- ─────────────────────────────────────────────────────────────────────────────
-- Public-site alpha-access capture (docs/CREED_AND_PATH_TO_TEN.md, "Public site
-- & conversion" 4→5). The site's release beat previously said "ask for alpha
-- access" with no mechanism to ask through — this is that mechanism: a real,
-- unauthenticated, rate-limited capture endpoint (POST /v1/alpha-request) so a
-- prospective buyer or supplier can actually leave contact info instead of the
-- funnel dead-ending at the sentence.
CREATE TABLE IF NOT EXISTS alpha_requests (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT now(),
    email      TEXT NOT NULL,
    role       TEXT,   -- 'buyer' | 'supplier' | '' (unspecified) — whichever CTA was clicked
    note       TEXT,   -- optional free-text ("what would you run", etc.)
    source_ip  TEXT    -- for the same per-IP abuse-rate reasoning as signupLimiter
);
CREATE INDEX IF NOT EXISTS alpha_requests_created_at_idx ON alpha_requests (created_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- Buyer Advantage & Pricing Edge 4.5→5 (docs/internal/CREED_AND_PATH_TO_TEN.md,
-- "Reprice from real supplier economics, not hand-seeded constants"). Price
-- provenance: a price is either 'seed' (the original hand-typed launch constant)
-- or 'measured_supplier_economics' (derived by control/pricing.go's
-- repriceFromSupplierEconomics from a real docs/GPU_CAPABILITY.md throughput
-- figure, the real control/payment.go supplier-share rate, and an electricity-cost
-- floor — the same inverse arithmetic scripts/supplier_earnings_calculator.py does
-- by hand for one supplier, now applied to reprice the catalogue itself).
-- price_formula records the exact figures so any repriced number is traceable back
-- to a real measurement, never a re-guessed constant.
ALTER TABLE models ADD COLUMN IF NOT EXISTS price_source  TEXT DEFAULT 'seed';
ALTER TABLE models ADD COLUMN IF NOT EXISTS price_formula TEXT;

-- Project Detection & Quotation 7→8 ("Ship a firm-quote tier: a real commitment,
-- not just an estimate"). An opt-in per-job flag: when set, the buyer's charge is
-- capped at firm_quote_max_usd (the quote's stated maximum) regardless of actual
-- cost — any overage is absorbed by the platform, never passed through. billed_usd
-- is what the buyer was actually charged (== actual_usd unless capped), so the
-- invoice/ledger can show the cap took effect.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS firm_quote          BOOLEAN DEFAULT false;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS firm_quote_max_usd  NUMERIC(12,6);

-- ─────────────────────────────────────────────────────────────────────────────
-- Speed Lane wave 2A (docs/speed-lane-reports/SLA_QUOTE_WAVE2A.md) — the
-- wall-clock speed-SLA quote. A quote whose fleet is SLA-eligible AND whose ETA
-- was planner-backed (real measured per-worker rates, control/planner.go) may
-- carry a TIME GUARANTEE derived from the planner's CONSERVATIVE band plus an
-- explicit safety margin and a merge/collect allowance (control/quote.go
-- slaGuaranteedSecs — every term documented there). The guarantee is priced: a
-- documented premium (sla_premium_usd) that is refunded automatically on a miss.
--
--   quotes.sla_guaranteed_secs / sla_premium_usd — the OFFER persisted with the
--     quote's other assumptions (NULL = no guarantee was offerable: thin fleet,
--     planner disabled, or no measured rates — honest degradation, never a guess).
--   jobs.sla_guarantee_secs / sla_premium_usd — the BINDING, stamped at submit
--     when firm_quote binds an SLA-bearing quote. The guarantee clock is the
--     buyer-visible span: jobs.created_at → jobs.results_merged_at.
--   jobs.sla_met — the OUTCOME: NULL until decided (or no SLA), true = met,
--     false = missed (an sla_refund ledger row + job event were recorded).
--     Deliberately NOT wired into deadline_secs: the deadline drives the stuck-run
--     watchdog's rescue/KILL ladder, while a missed SLA must complete-and-REFUND —
--     killing a late job would destroy the buyer's results to punish lateness.
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS sla_guaranteed_secs INT;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS sla_premium_usd     NUMERIC(12,6);
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS sla_guarantee_secs  INT;
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS sla_premium_usd     NUMERIC(12,6);
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS sla_met             BOOLEAN;
-- Exactly ONE sla_refund ledger row per job, structurally: the refund insert is
-- INSERT-if-absent by payout_ref ('sla-<job_id>'), and this partial unique index
-- makes a racing double-insert (two finalize sites + the collect sweep can all
-- observe the same miss) impossible rather than merely unlikely — the same
-- pattern as ledger_stripe_fee_ref_uniq above.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_sla_refund_ref_uniq ON ledger_entries (payout_ref) WHERE kind = 'sla_refund';

-- ─────────────────────────────────────────────────────────────────────────────
-- Public Site & Conversion 6→7 (docs/internal/CREED_AND_PATH_TO_TEN.md, "Make the
-- funnel observable"). A minimal, self-hosted, cookie-free beacon: pageview,
-- scroll-depth per narrative beat, receipts-panel opens, and CTA clicks. No
-- tracking pixel, no third-party script, no cookie — see control/beacon.go for
-- the endpoint and web/index.html's inline beacon script for the client side.
--
-- Cookie-free by construction: page_id is generated client-side in memory only
-- (crypto.randomUUID(), never written to a cookie or localStorage) purely to
-- group the handful of events one single pageview emits — it dies with the tab
-- and is never sent back to the same visitor on a later visit, so it is not a
-- persistent client identifier. source_ip is retained only as long as every
-- other IP already stored in this database (alpha_requests, signup) for the
-- same abuse-rate reasoning, not to build a cross-session profile.
CREATE TABLE IF NOT EXISTS site_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    page_id     UUID    NOT NULL, -- in-memory-only per-pageview id (see above) — groups events from one page load, nothing more
    event_type  TEXT    NOT NULL CHECK (event_type IN ('pageview', 'scroll_depth', 'receipts_open', 'cta_click')),
    beat        SMALLINT,         -- narrative beat index (0-6, see web/index.html data-beat) — NULL when not beat-scoped
    detail      TEXT,             -- e.g. which CTA ('alpha-request', 'demo') — free text, capped, never PII
    path        TEXT,             -- request path the beacon fired from
    referrer_host TEXT,           -- host-only referrer (no query string / path — never the full referring URL)
    source_ip   TEXT              -- same per-IP abuse-rate reasoning as alpha_requests.source_ip
);
CREATE INDEX IF NOT EXISTS site_events_created_at_idx ON site_events (created_at);
CREATE INDEX IF NOT EXISTS site_events_type_idx ON site_events (event_type, created_at);
CREATE INDEX IF NOT EXISTS site_events_page_id_idx ON site_events (page_id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS billed_usd          NUMERIC(12,6);
