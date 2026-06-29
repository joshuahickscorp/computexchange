# Computexchange

A task-priced, verified spot market for batch AI inference on idle Apple Silicon Macs. Buyers pay per job completed rather than per GPU-hour held; suppliers earn on idle Macs, including ones with the unified memory to run 30B to 70B models; output is verified (honeypots, within-class redundancy, and payout holds) so results can be trusted without re-running. Two binaries: a **Rust supplier agent** (`agent/`) and a **Go control plane** (`control/`), over **Postgres and S3**.

## Architecture

```
                 ┌──────────────────────────── Control plane (Go, control/) ────────────────────────────┐
  Buyer  ──────▶ │  REST API → Scheduler/Matcher → Verification → Reputation → Payment/Ledger → Benchmark│
  (api_key)      └───────┬───────────────────────────────────┬──────────────────────────────────────────┘
                         │ enqueue tasks                      │ claim: SELECT ... FOR UPDATE SKIP LOCKED
                         ▼                                    ▼          (the queue IS Postgres, no NATS)
                   ┌───────────────┐                  ┌──────────────────────────────────────┐
                   │  S3 / MinIO   │  presigned       │              Postgres                 │
                   │  job in/out   │◀───── URLs ─────▶│  tasks · jobs · workers · ledger · …  │
                   └───────┬───────┘                  └──────────────────────────────────────┘
                           │ GET input / PUT result            ▲ register · poll · commit · heartbeat (HTTP)
                           ▼                                    │
              ┌──────────────────────── Supplier agents (Rust, agent/) ─────────────────────────┐
              │   Mac M4 Max          Mac M4 Pro          Mac Studio …   (poll → run job → commit)│
              └─────────────────────────────────────────────────────────────────────────────────┘
```

The control plane is a single Go binary; the supplier agent a single Rust binary. The job queue is a **Postgres table** (`tasks`), claimed with `FOR UPDATE SKIP LOCKED` gated on `(status, visible_at)`, retries just push `visible_at` forward. No message broker.

**Inference is real.** The agent runs models on-device with [Candle](https://github.com/huggingface/candle): sentence embeddings (all-MiniLM-L6-v2, 384-dim), Whisper transcription, and quantized Llama generation, on **Metal** on Apple Silicon (CPU fallback elsewhere). Weights are pulled from HuggingFace on first use and cached (never re-fetched, never deleted).

**The result flow is presigned S3.** The control plane mints a presigned **GET** for each task's input and a presigned **PUT** for its result, signed against `S3_PUBLIC_ENDPOINT` (the address the *agent* reaches), while control-side reads/writes go through the internal `S3_ENDPOINT`. The agent uses the URLs verbatim, no S3 credentials ever leave the control plane.

## Repo layout

```
agent/             Rust, supplier agent binary (warm model pool + concurrency)
  src/{main,hardware,runners,pool,models,protocol,status,config,types}.rs
control/           Go, control plane, single binary (one flat package main)
  {main,api,openai,scheduler,store,storage,verification,reputation,payment,
   benchmark,workers,metrics,seed,types}.go
db/schema.sql      Single authoritative PostgreSQL schema (applied via `make migrate`)
proto/manifest.schema.json   Canonical wire contract (JSON Schema, draft 2020-12)
cli/               `cx`, standalone Go CLI (submit / status / results / cancel / estimate)
                   control/openai.go, OpenAI-compatible Batch API (files + batches)
sdk/python/        dep-free Python client (urllib) + OpenAI-shaped `embeddings()`
web/dashboard.html Operator dashboard (served at `/`, same-origin)
macapp/            SwiftUI menu-bar app, `swift build` via Package.swift (signing external)
docs/TURBO.md · docs/RUNBOOKS.md · docs/ALPHA_READINESS.md
                   Turbo scheduler pass · operator runbooks · alpha readiness (proven vs skeleton vs external)
scripts/prove-local.sh       the one-command local proof (native PG+MinIO, live Metal)
scripts/{install,uninstall,backup}.sh   one-command agent install/uninstall · DB + object-store backup
.github/workflows/ci.yml     CI: Go build/vet/fmt/unit+integration, Rust fmt/clippy/build/test, schema apply
RELEASE_CANDIDATE.md         what's proven locally vs what's left (external only)
Dockerfile.control · docker-compose.yml · Makefile · .env.example · .gitignore
```

**Turbo** (second pass): a hard-filter scheduler (a worker can never claim a task
it can't run), warm model pool + bounded concurrency, three new workloads
(`batch_classification` / `json_extraction` / `rerank`), 3-way verification
tiebreak, auto-quarantine, straggler hedging, one merged buyer-ready artifact per
job, an **OpenAI-compatible Batch API**, and a menu-bar **status surface**, all
proven by `make prove-local` (**82/82**). See [docs/TURBO.md](docs/TURBO.md).

**Apple Silicon only.** Metal on Apple Silicon is the one supported supply target
(a CPU fallback exists for Linux CI but is never advertised as a supply class).
There is **no CUDA or external-cloud rail**, the whole exchange runs on one Mac, a
LAN, offline, or air-gapped, with zero mandatory third-party SaaS.

**Closed-alpha pass:** **dynamic supplier throttling** (the agent reads real
available memory and pauses claiming work before it would breach the operator's
reserved headroom or swap the box), a **scheduler safety contract** (the claim
filter refuses to dispatch to a throttled worker or one whose *effective* memory
is below the job's need), and a **minimal role-based app skeleton** (`/app`:
Supplier / Buyer / Admin / Workflows). Frontend is **skeleton-only by decision**; final design and the `Workflows`/IDE idea are deferred pending product input. See
[docs/ALPHA_READINESS.md](docs/ALPHA_READINESS.md) (what's proven vs skeleton vs
external) and [docs/PRODUCT_SHAPE.md](docs/PRODUCT_SHAPE.md) (app topology).
Configure headroom with `memory_headroom_gb` / `max_memory_pct` in `agent.toml`.

There are no `utils/` or `helpers/` junk drawers; every file is load-bearing.

`proto/manifest.schema.json` is the single source of truth for the wire shape (the "horizon"). `agent/src/types.rs` (serde) and `control/types.go` mirror it exactly: snake_case fields, snake_case string enums, internally-tagged `JobType`. Keep all three in lockstep.

## Quickstart

Requires Docker (compose v2), the Postgres client (`psql`), Rust (`cargo`), and Go 1.26.

```bash
cp .env.example .env                      # defaults already match the dev stack
cp agent/agent.example.toml agent/agent.toml   # agent config (gitignored; holds the worker token)
make dev-up                   # start Postgres + MinIO + create the cx-jobs bucket (detached)
make migrate                  # apply db/schema.sql  (idempotent)
make seed                     # mint a demo api_key + worker_token (prints both)
#   → put the printed worker_token in agent/agent.toml (or export CX_WORKER_TOKEN)
make control                  # run the Go control plane on :8080 (foreground)
make agent-bench              # (other shell) benchmark local hardware
make agent-run                # (other shell) start the supplier polling loop
```

> **Verified live.** This full loop has been run end-to-end on Apple Silicon (M3 Pro, Metal): a 3-line embed job submitted over the REST API splits into a task, the Rust agent claims it, runs **real all-MiniLM-L6-v2 inference** (384-d, L2-normalized), uploads the result to MinIO via a presigned URL, commits, and the control plane verifies it, marks the job complete, writes the 90/10 ledger split, and the payout-release worker moves the held credit to `ready`. The only Phase-3 stub is the actual Stripe/Trolley transfer.

Auth is **not bypassable**: `make seed` inserts a *hashed* api_key into `api_keys` and a token into `worker_tokens`. The api_key is the buyer's `Authorization: Bearer` for `POST /v1/jobs`; the worker_token is the agent's `CX_WORKER_TOKEN`. Without seeded rows, every request is rejected.

## Run the full stack

The control plane is containerized; the agent is not (Metal is unavailable in Linux containers, see "Why no agent image"). `make up` builds and starts Postgres, MinIO, the bucket + schema one-shots, and the control plane, in dependency order:

```bash
make up                       # docker compose up -d --build  (control plane on :8080)
make seed                     # mint api_key + worker_token against the running stack
make down                     # tear the stack down  (add `-v` via compose to wipe volumes)
```

Then run the agent natively against it: `CX_CONTROL_URL=http://localhost:8080 CX_WORKER_TOKEN=<seed token> make agent-run`.

## Seed

`make seed` (or `cd control && go run . seed`) runs the control plane's `seed` subcommand: idempotent (stable demo UUIDs + `ON CONFLICT DO NOTHING`), it prints a buyer `api_key` and a `worker_token` and exits without starting the server. Re-running is a no-op.

## Prove everything locally

`make prove-local` (→ `scripts/prove-local.sh`) is the one command that proves the whole stack. It provisions a **throwaway native Postgres + MinIO** (no Docker image pulls, reliable; `USE_DOCKER=1` to use compose instead), applies the schema, runs the **deterministic proof matrix** (the Go `-tags integration` suite, auth, verification, honeypot/fraud, idempotency, requeue, payout hold→ready, webhooks, malformed input, metrics), then drives the **real** Rust agent through **live Metal inference** for all three job types, and prints a **PROOF LEDGER**. It fails loudly and exits non-zero on any gap; artifacts land in `.artifacts/prove-local/`.

```bash
make prove-local            # ~3–5 min; last run: 82/82 pass
#   SKIP_LIVE=1   matrix only (no model run)     KEEP=1   leave the stack up to inspect
#   PROVE_WHISPER=1   make whisper a required check (default best-effort)
```

A passing run proves, among the 82 checks: infra boot · migrate · seed · `/healthz` · MinIO object flow · worker registration · **live embed (dim=384)** · **live Llama-3.2-1B infer** · **live whisper transcription** · redundancy + honeypot verification · mismatch/fraud clawback · duplicate-commit idempotency · stale + failed-job requeue · webhook retries · payout hold→ready · payout transfer **honestly blocked** · invalid-auth 401/403 · malformed-manifest 4xx · metrics counters · **menu-bar status file** · **OpenAI batch API** · **admin panel** · **multi-supplier (2 agents, one job)** · **manual-export payout** · **load test** · **disaster-recovery** · **install + menu-bar build** · structured logs · `go test` + `cargo test`. See **[RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md)** for the full local-vs-external breakdown.

This is a macOS step (the agent needs Metal); the models (MiniLM, Llama-3.2-1B, whisper-tiny) are cached after first use. Needs `go`, `cargo`, `psql`, native `postgres`/`minio` (or `USE_DOCKER=1`), `curl`, `python3` on PATH.

## Observability

- **`GET /healthz`**, liveness; the control plane fatals at startup if Postgres or the object store is unreachable, so a 200 here means the deps are wired.
- **`GET /metrics`**, Prometheus exposition (`make metrics` to scrape). Job/task counters, queue depth, payout state.
- **Background workers** run for the life of the process: **payout-release** (held credits → released once `release_at` passes), **stale-task requeue** (claims that outlive their deadline get `visible_at` pushed forward and are re-dispatched), and **webhook delivery / job sweep**.

## Deliberate simplifications

Two deliberate simplifications, documented here so they can be reverted if you disagree.

1. **No Python, Go + Rust only.** The plan's standalone `bench/bench.py` is folded into the Rust agent as a `bench` subcommand (`make agent-bench` → `cargo run -- bench`). The agent already detects and benchmarks hardware on startup, so a separate Python tool and a third language toolchain are pure overhead. *Revert:* re-add `bench/bench.py` and have it write `benchmark_results` rows directly.
2. **No NATS, Postgres queue.** The plan names NATS for the job queue; we use a Postgres queue instead (`SELECT ... FOR UPDATE SKIP LOCKED` on `tasks`, with `claimed_by`/`claimed_at`/`visible_at` columns and a `(status, visible_at)` index). This removes a dependency and a dev container, the dev stack is just Postgres + MinIO. At V1 scale Postgres is more than enough and keeps everything in one transactional store. *Revert:* introduce NATS, move dispatch off the `tasks` table, drop the queue columns/index.

## Why no agent image

There is deliberately **no Dockerfile for the agent**. It uses Candle + **Metal** for on-device inference, and Metal is unavailable inside Linux containers. The agent must run natively on macOS; only the control plane is containerized (`Dockerfile.control`, distroless static binary). `docker-compose.yml` and CI reflect this, the stack runs the control plane in a container and you point the native agent at it via `CX_CONTROL_URL`.

## Status

**V1 is real, not scaffolding.** Execution, verification, storage, the queue, and the background workers all work end to end (`make prove-local` proves it, 82/82 local checks, including live Metal inference, a two-agent multi-supplier run, a load burst, and a real backup→restore). The one Phase-3 stub is the *licensed* payout rail (the alpha manual-export rail is built).

**Real now**
- **Execution**, Candle inference on Metal: MiniLM embeddings (384-dim), Whisper transcription, quantized Llama generation.
- **Verification**, honeypots + within-class redundancy + payout holds; the hold→release state machine is real.
- **Storage**, real presigned S3 GET/PUT (internal vs public endpoint split), bucket auto-created at startup.
- **Queue**, the Postgres `tasks` queue (`FOR UPDATE SKIP LOCKED`, retry visibility).
- **Workers**, payout-release, stale-task requeue, webhook delivery, all running.
- **Observability**, `/healthz`, `/metrics`, structured startup that fatals on missing config.
- `db/schema.sql`, complete schema (domain, queue columns/index, auth/honeypot, webhooks, models catalogue).
- `proto/manifest.schema.json`, the full wire contract, matching `agent/src/types.rs` and `control/types.go`.

**Phase-3 stub (explicit, not faked)**
- **Payout transfer rail**, Stripe Connect / Trolley money movement. The ledger and the hold→release lifecycle are real; only the final external transfer call is stubbed (`stubPayout`). It surfaces an explicit boundary, never a fake success.

## Build / verify

```bash
make build    # cargo build (agent) + go build ./... (control)
make test     # cargo test + go test ./...   (agent model-download tests are #[ignore]d)
make fmt      # cargo fmt + gofmt -w
make loc      # line counts vs the BLACKHOLE targets
make docker-build   # build the control-plane image standalone (cx-control)
```

CI (`.github/workflows/ci.yml`) gates every push/PR: the **control** job builds, vets, gofmt-checks, and runs both the unit tests **and the full integration matrix** (`go test -tags integration`) against live Postgres + MinIO service containers; the **agent** job runs fmt/clippy/build/test on macOS (for Candle/Metal); the **schema** job applies `db/schema.sql` into Postgres 17 and asserts a clean, re-runnable load. The deeper `make prove-local` (live Metal inference) is the local release-candidate gate, it needs Apple Silicon + cached models, so it runs on a developer machine, not cheap CI.

## Roadmap

Computexchange is a task-priced, verified spot market for batch AI inference on
idle Apple Silicon Macs. The core engine (queue, scheduling, on-device
inference, verification, ledger) works end to end today. The work ahead is the
buyer-facing product, real multi-machine supply, and the licensed payout rail.

### Now (works today)
- The full job lifecycle: submit, queue (Postgres FOR UPDATE SKIP LOCKED),
  claim, run real on-device inference on Metal, verify, and settle the ledger.
- Verification: honeypots, within-class redundancy, 3-way majority vote,
  reputation, and payout holds.
- An OpenAI-compatible Batch API, a CLI, a Python SDK, and a menu-bar agent.

### Next
- A real buyer console and the Compute Autopilot pipeline builder (the backend
  seams exist; the visual product is a build).
- A routing-intelligence dashboard that surfaces the per-job-type timing data
  the control plane already collects.
- Cross-machine validation on a real Mac Studio and a second physical Mac
  (byte-identical output rates, sustained thermal load).

### Later
- The licensed payout transfer rail (Stripe Connect or Trolley). The ledger and
  the hold-to-release lifecycle are real today; only the final transfer call is
  stubbed.
- A co-located Mac Studio cluster for large-model inference, offered as a
  reserved tier.
- The enterprise and privacy tier (private-pool routing already exists; the
  compliance attestations are external work).
