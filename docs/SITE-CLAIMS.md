# SITE-CLAIMS · the receipts ledger for the public page

Every factual sentence on `web/index.html` traces to a row in this file, and every row carries
`path:line` receipts with a short quote of the evidencing code, test, or doc. Anything that could
not be evidenced was not softened · it was killed, and the kill is recorded here so it stays dead.

Audit: 2026-07-01, 26 agents · 12 claim finders, each followed by an adversarial receipt checker
that re-opened every cited line and tried to refute the sentence, plus rig / download / routes
auditors. 10 claims confirmed as corrected below · 1 contested (claim 1, parser flaw found in the
proof tooling itself) · 1 killed (claim 9, replaced by a narrower true sentence).

Transcription note: the repo dash gate bans em and en dashes in every file, so where a quoted
source line contains one it is transcribed as `·`. Line numbers are exact as of commit `32bf80d`.

## Summary

| # | claim | status | one-line version |
|---|-------|--------|------------------|
| 1 | prove-local count | corrected · number re-pinned in the site shift | the proof ledger tally, printed with its real count |
| 2 | Postgres is the queue | verified | FOR UPDATE SKIP LOCKED, no broker in the tree |
| 3 | two binaries at the core | corrected | agent + control plane · CLI and menu bar app are optional |
| 4 | real inference on Metal | corrected | Candle in pure Rust · MiniLM, bge-small, Whisper, Llama 3.2 1B |
| 5 | 45s chunks + hard-filter scheduler | corrected | five hard filters at claim time · quiet hours enforced on-device |
| 6 | verification stack | verified | honeypots, redundancy, majority vote, 0.25 audit floor, quarantine, clawback |
| 7 | quote before spend | corrected | /v1/quote + /v1/quote/pipeline · max_usd binds at dispatch |
| 8 | payout lifecycle | corrected | held then released against a real ref, or ready when owed |
| 9 | device is the identity | KILLED · replaced | see 9b |
| 9b | no sign-in screen | verified (replacement) | silent device-local provisioning · payment method is the hand-connected credential |
| 10 | Batch API + SDK | verified | OpenAI-compatible /v1/files + /v1/batches · stdlib-only Python SDK |
| 11 | payout rail is a stub | KILLED as stale · corrected | real Stripe Connect transfers when configured |
| 12 | pricing constants | verified | $0.001 per 1k embeddings · the page's one serif number |

## 1 · prove-local-count · corrected, number pending the parser fix

**Line the page prints:** `make prove-local · N pass · 0 skip · 0 fail` with N taken from a fresh
run made after the parser fix below, in the same shift that ships the page.

**Receipts:**
- `scripts/prove-local.sh:92` · `printf '%s\t%s\t%s\n' "$status" "$cap" "$*" >>"$LEDGER_FILE"` · every check appends one ledger line
- `scripts/prove-local.sh:198` · `if (cd control && go test -tags integration -count=1 -v ./... )` · the integration matrix feed
- `scripts/prove-local.sh:1017` · `pass=$(grep -c '^PASS' "$LEDGER_FILE" || true)` · the tally is a grep over the ledger
- `scripts/prove-local.sh:1030` · `die "$failc capability check(s) FAILED · not release-candidate clean"` · any FAIL aborts
- `scripts/prove-local.sh:209` · `grep -E '^--- (PASS|FAIL): Test[A-Za-z]+ '` · THE FLAW: `[A-Za-z]+` drops digit-named tests

**Notes:** the adversarial checker refuted the naive count. The matrix parser regex silently drops
any top-level test with a digit in its name: `TestPerTaskSecsFromP90FallbackAndConversion` ran and
passed in the 2026-07-01 run but produced zero ledger lines, so the printed `167 pass` was one
short of the 168 checks that actually passed (162 matrix + 6 capability). The regex is fixed to
`Test[A-Za-z0-9_]+` in the site shift and the page prints the post-fix number from a fresh run.
The count is ledger capability lines, not raw test cases (the whole Go unit suite and the whole
cargo suite are one line each), and a `SKIP_LIVE=1` run is matrix-only · the page says which.

## 2 · queue-postgres · verified

**Line:** The job queue is a plain Postgres table claimed with FOR UPDATE SKIP LOCKED · there is
no Kafka, RabbitMQ, NATS, or Redis anywhere in the dependency tree.

**Receipts:**
- `control/scheduler.go:521` · `FOR UPDATE OF t SKIP LOCKED`
- `control/scheduler.go:454` · `FROM tasks t`
- `control/scheduler.go:526` · `status = 'running', started_at = now()`
- `db/schema.sql:8` · `-- The job queue is Postgres, not NATS (BLACKHOLE compression): workers claim work`
- `control/go.mod:8` · `github.com/jackc/pgx/v5 v5.10.0` · five direct deps total, no broker
- `agent/Cargo.toml:12` · `[dependencies]` · inspected in full, no broker client

## 3 · two-binaries · corrected

**Line:** The core is two binaries · a Rust supplier agent and a Go control plane · a supplier
runs only the agent, a buyer needs only the REST API, and an optional buyer CLI and macOS menu
bar app sit on top.

**Receipts:**
- `README.md:3` · `Two binaries: a **Rust supplier agent** (agent/) and a **Go control plane** (control/)`
- `agent/Cargo.toml:9` · `name = "cx-agent"`
- `Dockerfile.control:46` · `ENTRYPOINT ["/control"]` · the control plane is operator infrastructure
- `cli/README.md:3` · `A single stdlib-only Go binary for the buyer REST API` · optional third binary
- `macapp/README.md:3` · `A SwiftUI MenuBarExtra app that is the operator's face for the Rust cx-agent`
- `sdk/python/README.md:3` · dependency-free library, not a binary

**Notes:** the raw candidate ("the whole product is two binaries, a user runs nothing else") was
false both ways: the repo builds four executables, and no single user runs both core binaries.
The corrected line states who runs what.

## 4 · models-metal · corrected

**Line:** Inference runs through Candle in pure Rust · Metal by default on Apple Silicon, CUDA as
a build flag · shipping all-MiniLM-L6-v2 and bge-small-en-v1.5 embeddings, Whisper tiny and base
transcription, and quantized Llama 3.2 1B Instruct.

**Receipts:**
- `agent/src/runners.rs:1` · `//! Job runners · the closed job-type contract, with REAL Candle inference.`
- `agent/src/models.rs:101` · `repo: "sentence-transformers/all-MiniLM-L6-v2"`
- `agent/src/models.rs:111` · `repo: "BAAI/bge-small-en-v1.5"`
- `agent/src/models.rs:142` · `"openai/whisper-tiny"` · and `:140` whisper-base
- `agent/src/models.rs:195` · `repo: "unsloth/Llama-3.2-1B-Instruct-GGUF"` · `:196` Q4_K_M file
- `agent/Cargo.toml:47` · `default = ["metal"]` · `:53` `cuda = [...]` opt-in feature
- `agent/src/models.rs:36` · `match Device::new_metal(0)` · Metal tried first

**Notes:** Qwen 0.5B/7B refs exist in models.rs but are excluded from the page: the 7B GGUF URL
404s on HuggingFace today and `models.rs:176` itself marks Qwen output parity UNPROVEN. The four
listed model families' weight URLs were live-probed and resolve.

## 5 · chunking-scheduler · corrected

**Line:** Jobs are split into tasks sized for a 45 second runtime target, and the scheduler
hard-filters every assignment on memory fit, model availability, hardware class, memory-pressure
throttle, and the job's budget cap · quiet hours are enforced by the agent on the device itself.

**Receipts:**
- `control/api.go:2034` · `const targetTaskSecs = 45`
- `control/api.go:2050` · `n := int(effectiveThroughput(jobType, avgLineBytes) * targetTaskSecs)`
- `control/scheduler.go:346` · `// Scheduler V2 hard filter (the #1 goal: a worker can NEVER claim a task it` (cannot run)
- `control/scheduler.go:468` · memory fit · `:470` throttle · `:471` hw class · `:473` model availability
- `control/scheduler.go:498-506` · budget governor gate against `j.max_usd`
- `agent/src/config.rs:328` · `pub fn is_eligible_to_run(&self, now_hour: u8, on_battery: bool) -> bool` · quiet hours on-device

**Notes:** the candidate bundled quiet hours into the scheduler; in truth `throttled` covers only
memory pressure and quiet hours live agent-side. The corrected line attributes each enforcement
point. More hard filters exist than the five named (supplier active, job-type support, residency,
reputation floor, private pools) · the page understates rather than overstates.

## 6 · verification-stack · verified

**Line:** Results are verified with honeypot known-answer tasks, within-class redundancy, N-way
majority voting, and reputation-weighted audits with a spot-check floor of 0.25 · a failed
honeypot docks reputation, claws back the task credit, and auto-quarantines the supplier.

**Receipts:**
- `db/schema.sql:236` · `CREATE TABLE IF NOT EXISTS honeypots (`
- `control/verification.go:98` · `if info.IsHoneypot && v.checkSampled(info.TaskID, checkProb())`
- `control/verification.go:20` · `Within-class redundancy is the trust spine.`
- `control/verification.go:484` · `resolveTiebreak runs the real N-way majority vote over all committed results`
- `control/verification.go:284` · `verifyCheckProbFloor = 0.25` · `:281` `even the most trusted worker is audited ~1 task in 4`
- `control/verification.go:125` · `ClawbackTaskCredit` · `:128` `QuarantineSupplier`
- `control/store.go:1590` · `const quarantineRepFloor = 0.2`
- `db/schema.sql:112` · ledger kinds include `'clawback'`

**Notes:** 0.25 is the floor reached at reputation 1.0; at or below the 0.90 trust floor the audit
probability is 1.0 and it ramps linearly between. Cross-class byte mismatches never dock and a
no-majority vote never punishes.

## 7 · quote-before-spend · corrected

**Line:** Buyers get a price quote before any spend via POST /v1/quote and /v1/quote/pipeline,
and a max_usd cap set at job submission is enforced at dispatch by a budget governor that pauses
the job before the cap is breached.

**Receipts:**
- `control/api.go:117` · `mux.Handle("POST /v1/quote", ...)` · `:118` `/v1/quote/pipeline`
- `control/api.go:363` · `// MaxUSD is the optional buyer hard spend cap (Budget Governor, ...)`
- `control/scheduler.go:487` · `-- spend cap, NEVER dispatch a new task whose projected charge would breach`
- `control/scheduler.go:498-506` · the projected-charge gate in the claim SQL
- `control/scheduler.go:819` · `SET budget_state = 'paused_for_budget'`
- `control/integration_test.go:1322` · `func TestBudgetCapPausesDispatch(t *testing.T)` · proves the pause and the resume

**Notes:** correction · the pipeline quote itself is advisory (`control/quote.go:564` "it persists
nothing and binds nothing"); what binds is `max_usd` on POST /v1/jobs. The page says the cap
binds, not the quote.

## 8 · payout-lifecycle · corrected

**Line:** Supplier credits are written held until a hold window expires, then either released
against a real transfer reference or marked ready when owed with no payout rail · settlement is
idempotent via a UNIQUE(task_id, kind) ledger index · a read-only reconciliation pass audits
released credits against actual Stripe transfers every 15 minutes and never moves money.

**Receipts:**
- `control/payment.go:112` · `PayoutStatus: PayoutHeld, // held until the hold window expires`
- `control/workers.go:302` · ready when the rail is unconfigured · `:308` released with ref
- `db/schema.sql:133` · `CHECK (kind <> 'supplier_credit' OR payout_status <> 'released' OR payout_ref IS NOT NULL)`
- `db/schema.sql:140` · `CREATE UNIQUE INDEX ... ledger_task_kind_uniq ON ledger_entries (task_id, kind)`
- `control/store.go:1890` · `ON CONFLICT (task_id, kind) DO NOTHING`
- `control/reconcile.go:18` · `It NEVER moves money, marks a row, or "fixes" a` (drift) · `:26` every 15 minutes

**Notes:** corrected from a linear held to ready to released pipeline · ready and released are
alternative outcomes of the release sweep, not stages. Found in passing: `DuePayouts` selects only
`payout_status = 'held'` (`control/store.go:2191`) so a `ready` row is not re-picked despite the
comment at `workers.go:301`, and `ready` is missing from the schema enum comment at
`schema.sql:121`. Flagged as follow-up work, not a page claim.

## 9 · device-identity · KILLED

The candidate ("no account creation flow exists; device + payment method is the only identity")
is architecturally false and was killed. POST /v1/signup (`control/api.go:92`) accepts email +
password, stores a bcrypt hash (`control/accounts.go:192`, `db/schema.sql:159`), and POST
/v1/login (`control/api.go:93`) is a full credentialed login. The device-is-the-account behavior
is real but is a client layer: the console silently generates and stores credentials per browser
(`web/demo.html:834`). The page must never say no account exists, because one demonstrably does.

## 9b · no-sign-in-screen · verified replacement

**Line:** There is no sign-in screen · on first use the console silently provisions a
device-local account, and the only credential a person ever connects by hand is a payment method.

**Receipts:**
- `web/demo.html:823` · `// No sign-in, no password. On first use we silently provision a device-local buyer`
- `web/demo.html:834` · `c={email:'dev-'+rnd()+rnd()+'@device.computexchange.net',password:...}` · generated, never typed
- `web/demo.html:459` · `Connect a payment method to run real jobs. It is the only thing we verify · no account, no password.`
- `control/accounts.go:26` · `// POST /v1/signup {"email","password"} -> 201 {...}` · the silent client calls this
- `db/schema.sql:159` · `password_hash TEXT, -- bcrypt; NULL = no password set (seed / API-key-only)`

## 10 · batch-api-sdk · verified

**Line:** The control plane serves an OpenAI-compatible Batch API at /v1/files and /v1/batches,
and the Python SDK is a single file that imports only the Python standard library.

**Receipts:**
- `control/openai.go:3` · `// openai.go · an OpenAI-compatible Batch API mapped onto the native job pipeline.`
- `control/api.go:143` · `POST /v1/files` · `:145` `POST /v1/batches` · `:146` `GET /v1/batches/{id}`
- `control/integration_test.go:2866` · posts to `/v1/batches`
- `sdk/python/computeexchange/__init__.py:20-25` · imports are json, struct, time, urllib only

**Notes:** OpenAI-compatible in shape for the Batch API surface specifically, not a general
OpenAI clone · the page keeps that scope.

## 11 · money-rail-stub · KILLED as stale, corrected

**Line:** Supplier payouts are real Stripe Connect transfers when the Stripe key is configured ·
an unconfigured deployment keeps credits marked owed and never fakes a paid transfer.

**Receipts:**
- `control/payment.go:165` · `// StripePayout is the REAL money rail: a Stripe Connect transfer to the supplier's` (account)
- `control/payment.go:208` · `"https://api.stripe.com/v1/transfers"` · with per-payout Idempotency-Key
- `control/main.go:141-142` · rail selected when `STRIPE_SECRET_KEY` is set
- `control/main.go:148` · `payout rail: none configured · credits reach 'ready' (owed), never 'released'`
- `control/connect.go:13` · supplier Express onboarding with Stripe-hosted KYC

**Notes:** the handoff expected "the licensed money-transfer rail is the stub" to be the page's
honest limitation. That is stale: a real transfer rail exists behind the key gate (two old
comments at `payment.go:21` and `:155` predate it and fed the myth). The page's honest limitation
is therefore the download state instead · see the download audit: closed alpha, no public
artifact, unsigned local build (`macapp/assemble-app.sh:12`, `GO_LIVE.md:3`,
`RELEASE_CANDIDATE.md:124`).

## 12 · pricing-constant · verified

**Line:** The seeded catalogue prices embeddings at $0.001 per 1,000 units · Llama 3.2 1B batch
inference at $0.002 per 1,000 units · Whisper transcription from $0.004 per audio minute · the
platform take defaults to 3%.

**Receipts:**
- `db/schema.sql:299` · `('all-minilm-l6-v2', ..., 0.00100000, ...)` · USD per 1,000 units per `:287`
- `db/schema.sql:301` · `('llama-3.2-1b-instruct-q4', ..., 0.00200000, ...)`
- `db/schema.sql:303` · `('whisper-tiny', ..., 0.004...)` · per audio-minute per `:277`
- `control/seed.go:97` · the Go seed mirrors the embedding price
- `control/payment.go:63` · `const def, lo, hi = 3.0, 1.0, 5.0` · take clamped to the 1% to 5% band

**Notes:** prices are served from the models table, not a Go constant (`control/api.go:1145`).
The one serif monument on the page is $0.001 per 1,000 embeddings (`db/schema.sql:299`).
