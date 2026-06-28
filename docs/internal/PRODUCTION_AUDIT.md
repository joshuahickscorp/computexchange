# Computexchange — Production Audit & Improvement Roadmap

_Authored 2026-06-27. A clear-eyed audit of where the codebase actually stands, what is
genuinely left, and the highest impact-to-effort improvements — grounded in the real code
(every claim has a `file` or a verified run behind it) and in current frontier practice
(every external claim is cited). Honest by construction, in the BLACKHOLE spirit: nothing
faked, every gap named._

> Companion to [`RELEASE_CANDIDATE.md`](../RELEASE_CANDIDATE.md) (local-vs-external line) and
> [`docs/ROADMAP_STATUS.md`](ROADMAP_STATUS.md) (plan status). This file adds (1) a fresh
> verified baseline, (2) frontier research, and (3) a single prioritized roadmap.

---

## 1. Verified baseline (this machine, this session)

Re-proven from scratch, not taken on faith. Both the deterministic matrix **and** the
**full live-Metal release-candidate proof** were run (the latter downloads the models and
drives the real Rust agent through live embed / Llama-infer / whisper / classification on
this M3 Pro):

| Check | Command | Result |
|---|---|---|
| Control plane builds | `go build ./...` | ✅ clean |
| Static analysis | `go vet ./...` | ✅ clean |
| Go unit tests | `go test ./...` | ✅ pass |
| Rust agent builds | `cargo check` | ✅ clean (exit 0) |
| **Full local proof** | `make prove-local SKIP_LIVE=1` | ✅ **104 pass · 0 skip · 0 fail** |
| Outstanding TODO/FIXME in non-test source | grep | **0** |

The integration matrix that passed covers auth (401/403), within-class redundancy +
honeypot fraud + 3-way tiebreak, idempotent commits, stale/failed requeue, payout
hold→ready (and transfer honestly **blocked** without a real rail), webhook exactly-once,
malformed-input 4xx, scheduler hard-filter (never dispatches incompatible/throttled work),
and metrics counters. **The backend is production-functional today.**

**The Launch UI is already wired.** `web/demo.html` (served at `/demo`) has a real "live
mode": a buyer key (localStorage `cx_key`) turns the Launch flow into a real `POST
/v1/pipelines` + poll `/v1/pipelines/{id}`, with graceful fallback to the demo simulation
when no key is set; results download via `GET /v1/jobs/{id}/results`. So "wire it to the
backend" is, for the core buyer journey, **done**.

### What is genuinely left is **external** (cannot be done in a coding session)
Already tracked in `RELEASE_CANDIDATE.md`; restated so this audit doesn't pretend otherwise:
real **Stripe Connect/Trolley** credentials + supplier tax-identity onboarding (the
`StripePayout` code is implemented — it's a credentials task); production Postgres + object
storage with backups/TLS; **legal/compliance** opinions (FINTRAC/MSB, CRA Part XX, GST/HST,
ToS); **Apple Developer ID** signing + notarization + a Sparkle update channel; **multi-Mac
field testing** on real heterogeneous hardware; and **real users** (first paid buyer, first
real payout). None are code; all are flagged, not faked.

---

## 2. Frontier research (cited) — where the ceiling is

CX is "a task-priced, verified spot market for batch AI inference on Apple Silicon." Its two
structural moats are **supply economics** (cost per job on idle Macs) and **trust**
(verification without re-running). Frontier work maps cleanly onto both.

### 2.1 Supply / cost moat — Apple-Silicon inference SOTA
- CX runs **Candle** (Rust) on Metal. Current benchmarks put **MLX ~20–87% faster than
  llama.cpp for <14B models** (and they converge above ~27B where memory bandwidth
  dominates) — Candle is not even in the leading tier. ([Production-Grade Local LLM
  Inference on Apple Silicon, arXiv 2511.05502](https://arxiv.org/abs/2511.05502);
  [MLX vs llama.cpp](https://yage.ai/share/mlx-apple-silicon-en-20260331.html))
- **Continuous batching** is the single biggest per-machine throughput lever for a *batch*
  marketplace: **vLLM-MLX** (EuroMLSys '26) reaches up to **525 tok/s on an M4 Max** and
  **3.4–4.3× aggregate throughput at 5–16 concurrent requests**. ([same](https://yage.ai/share/mlx-apple-silicon-en-20260331.html))
- Where CX already is: the agent does **memory-aware bounded concurrency** (Semaphore,
  N in-flight tasks) and **batched prefill (bsz>1) with KV-cache** within a task
  (`agent/src/quantized_llama_batched.rs`, `agent/src/main.rs`). The frontier gap is
  **cross-task continuous batching** (merge prompts from independently-claimed tasks into one
  running batch with paged KV) — i.e., vLLM-style scheduling on the Apple-Silicon path.
- **Implication:** the highest-ceiling supply bet is an **MLX runner lane + continuous
  batching**. It directly lowers cost-per-job (the headline the cost model currently can't
  win on commodity small-model work — see `docs/COST_COMPARISON.md`). Effort: multi-week
  (new runtime lane, kept behind the existing job-type/verifier contract).

### 2.2 Capability frontier — distributed Mac clusters (CX's Plane B)
- **exo** + **MLX-distributed** + **Thunderbolt-5 RDMA** already shards a model's layers
  across Macs' unified memory (tensor parallelism), e.g. **4× M3 Ultra → a 2TB pool running
  DeepSeek-V3 671B**, RDMA cutting inter-device latency ~99% (<50µs). ([exo](https://github.com/exo-explore/exo))
- CX's `apple_silicon_cluster` horizon + `ClusterRunner` seam (`agent/src/cluster.rs`,
  `cx-agent cluster-plan`) is designed for exactly this substrate. **Implication:** integrate
  exo/MLX-distributed as the cluster execution backend rather than building a distributed
  forward pass from scratch — turns the "giant model" lane from a seam into a product.
  Effort: multi-week + needs ≥2 physical Macs (partly external).

### 2.3 Trust moat — cheaper verification
- CX verifies with within-class redundancy (re-run a fraction, compare cosine ≥ 0.999),
  honeypots, a 3-way tiebreak, and payout holds. The cost is **redundant recomputation**.
- Frontier: **optimistic verification** posts a result, opens a challenge window, and on
  dispute **recomputes only the divergent operator** (e.g. a single attention head) — Verde;
  Ora opML. Floating-point non-determinism across heterogeneous Macs (CX's exact problem) is
  addressed by **tolerance-aware optimistic verification** ([TAO, arXiv 2510.16028](https://arxiv.org/pdf/2510.16028);
  survey: [State of Verifiable Inference](https://equilibrium.co/writing/state-of-verifiable-inference)).
  TEE attestation (Atoma/evML; NVIDIA H100 confidential compute <7% overhead) is **less
  applicable** — Apple Silicon has no GPU TEE for inference, so redundancy+optimistic is
  CX's correct lane.
- **Implication:** an **optimistic-verification mode with operator-level bisection** cuts
  the redundancy tax → more margin per job, the documented Phase-6 item, now with concrete
  technique. Effort: multi-week; high research content.

### 2.4 Competitive positioning
- Vast.ai / RunPod / TensorDock / io.net dominate **NVIDIA idle-GPU** with bidding-driven
  spot pricing (RTX 4090 ~$0.25–0.30/hr). They do **per-hour rental**, not per-job-verified.
  ([Vast.ai vs RunPod 2026](https://medium.com/@velinxs/vast-ai-vs-runpod-pricing-in-2026-which-gpu-cloud-is-cheaper-bd4104aa591b);
  [GPU marketplace overview](https://aimultiple.com/gpu-marketplace))
- CX's wedge is the supply nobody serves — **idle Apple Silicon** (unified memory runs
  30–70B models cheaply, on-device privacy, air-gappable) — sold **per completed job, with
  verified output**. That's a real, defensible niche, not a cheaper-NVIDIA race. The binding
  constraint is **two-sided liquidity**, not technology.

---

## 3. Internal subsystem audit

_Filled from the parallel subsystem audit (scheduler/routing/pricing · verification/trust ·
money/ledger/payouts · agent inference perf · security/auth/API · reliability/observability ·
frontend/UX). See §4 for the unified prioritization._

Seven subsystems were audited in parallel (read-only, adversarial). Headline: **no
confirmed correctness bug in any production path; one latent bug in an untested fallback.**
The convergent theme across six of seven audits is *hardening for first real load + first
real users*, not new features.

| Subsystem | Health | Highest-signal findings |
|---|---|---|
| **scheduler / routing / pricing** | Architecturally sound; SKIP-LOCKED queue + exact budget-governor arithmetic; no correctness bugs. | Budget projection re-evaluates aggregate subqueries per candidate task in the claim hot path; tiebreak peer-selection does an extra `GetWorkerProfile` roundtrip; `CandidateWorkers` runs a correlated TPS subquery per worker (O(n)). All scale-only, fine at MVP load. |
| **verification / trust** | Sound moat (honeypots + within-class redundancy + 3-way tiebreak); deterministic sampling; real clawbacks. | **One real logic bug**: the empty-store fallback tiebreak path can double-dock one supplier (assigns both chunk votes the same supplier id) — masked in deployment, latent. Plus a cheap redundancy-cost reduction for elite-tier peers. |
| **money / ledger / payouts** | Solid, honest, idempotent; Stripe safeguards real. | Invariant "released ⟹ has rail ref" was enforced only in code, not structurally; charge state is log-only (not queryable); a couple of float-rounding nits in reconciliation/quote-vs-actual. |
| **agent inference perf** | Strong base (warm pool, batched prefill, quantization, Metal). | **Batched decode keeps computing finished sequences** (wasted GPU on mixed-length batches); **unbounded mask cache** = long-running OOM leak; per-model `Mutex` serializes Llama/Whisper so "bounded concurrency" is partly fiction; KV-cache concatenation is O(T)/step. |
| **security / auth / API** | Real credential hashing, SSRF-guarded webhooks, honest token sealing. | GitHub OAuth `access_token` stored plaintext (unlike hashed api/worker keys); OAuth CSRF state unsigned when `CX_STATE_SECRET` unset (prod is fatal-gated, dev/test isn't); no security headers on HTML; presigned-URL TTLs not tightened per stage. |
| **reliability / observability / ops** | Solid core; graceful shutdown, fatal-on-misconfig. | **DB pool unconfigured** (pgx default ≈ 4 conns — throttles everything under load); no retry/circuit-breaker around object storage; no request/correlation IDs or tracing; fixed (non-exponential) stale-task backoff; backup/restore not tested in prod compose. |
| **frontend / UX / wiring** | Launch live-mode genuinely wired; skeleton/dashboard escape correctly. | **XSS** via unescaped file name + unescaped server error string in `demo.html`; live poll **swallowed all errors** (dead polls on 401/network); silent 1 MB truncation; buyer key in `localStorage` (demo-only, already self-flagged). |

**Two audit findings were disproved on closer reading** (the codebase is even more mature than the audit credited — verify before "fixing"): GitHub OAuth tokens **are** encrypted at rest (`store.go` seals with `sealToken` on insert, `openToken` on read — the security agent only read `api.go`/`crypto.go`), and clawback **is** already atomic (`ClawbackTaskCredit` wraps the insert + original-credit update in one `Begin/Commit` tx). Neither was "fixed" because neither is broken.

---

## 4. Prioritized roadmap (impact-to-effort)

Ranked by impact-to-effort. Tier 0 was implemented + verified this session (§5).

### Tier 0 — landed this session (safe, verified at 104/104)
1. **Bounded DB connection pool** — pgx defaulted to ~4 conns; now `MaxConns=20` (env `DB_MAX_CONNS`) + lifetimes. *Single highest impact-to-effort fix in the audit.*
2. **demo.html XSS closed** — file name + server error string were raw-`innerHTML`; now escaped.
3. **Money invariant made structural** — `MarkPayout` fails loud + a scoped DB CHECK so a `supplier_credit` can never be `released` without a real rail ref.
4. **Security headers** on `/`, `/app`, `/demo` (nosniff, SAMEORIGIN, no-referrer).
5. **Live-poll failures surfaced** — 401/403 stops with a clear message; transient errors retry then fail; attempt cap; truncation warning.
6. **Ops nits** — metrics-query timeout widened (10s, named); shutdown drain 15→30s; `ledger_entries(kind)` audit index.
7. **Verification double-dock fixed** — the one genuine latent bug: the no-object-store fallback now docks the *real* peer supplier (threaded via `CommitTaskInfo.peerSupplierID`), not the committer twice.
8. **Queryable charge state** — `jobs.charge_status` (`not_attempted|charged|failed|no_payment_method`) set in `chargeForJob`, instead of log-only.
9. **Object-storage upload retry** — `PutObject` now retries with bounded ctx-aware backoff (idempotent PUT), so a transient store blip doesn't fail a job. *(Get intentionally fails fast on a missing object — verification must never treat absent as pass.)*
10. **Request/correlation IDs + access logging** — every request gets an `X-Request-ID` (propagated or generated) and one structured access-log line (method · path · status · latency · id); `/healthz`+`/metrics` skipped.
11. **Dead-code check** — `deadcode` run; the only non-test-used symbol is the idiomatic `httpError.Error`; removed a leftover unused `mDot` var in `demo.html`. (No real dead code — BLACKHOLE discipline holds.)

### Tier 1 — next, code-feasible (hours–days, low risk)
- **OpenTelemetry spans + exporter** over the request-id foundation now in place (submit→claim→commit→verify). A real multi-part feature (SDK + instrumentation + a collector in the deploy), so it's a deliberate next step, not a one-liner. *(I3/E3.)*
- **Scheduler hot-path trims** — precompute the budget projection once per `ClaimTask`; drop the extra tiebreak roundtrip; batch the per-worker TPS lookup. **Deliberately NOT done in-session**: the budget gate lives inside the SKIP-LOCKED money-critical claim query; the matrix proves correctness but not concurrency-safety of a rewrite, and the gain only matters past MVP scale (the team's own note). Do it with a load test + careful review. *(I2–3.)*
- **Dead-letter** for tasks requeued N times to the same broken supplier (exponential backoff already landed). *(I2/E2.)*

### Tier 2 — frontier bets (multi-week, high ceiling; some partly external)
- **Real bounded concurrency on the agent** — replace per-model `Arc<Mutex>` with a semaphore over N independent backends; **stop computing finished sequences** in batched decode and **bound the mask cache** (OOM leak). Directly multiplies idle-Mac throughput → the supply-side moat. *(I4/E2–4.)*
- **MLX runner lane + continuous batching** — CX runs Candle; MLX is 20–87% faster (<14B) and **vLLM-MLX continuous batching is 3.4–4.3× aggregate throughput** ([arXiv 2511.05502](https://arxiv.org/abs/2511.05502)). Add an MLX lane behind the existing job-type/verifier contract; merge same-model in-flight tasks into one running batch. *Biggest per-job-cost lever.* (§2.1)
- **Distributed-Mac cluster execution** via exo + MLX-distributed + Thunderbolt-5 RDMA as the `ClusterRunner` substrate — turns the 30–70B+ "giant model" lane from a seam into a product. Needs ≥2 Macs. ([exo](https://github.com/exo-explore/exo)) (§2.2)
- **Optimistic verification with operator-level bisection** (Verde/TAO, tolerance-aware for FP) — recompute only the divergent operator on dispute instead of full-task redundancy → lower verification tax → more margin. (§2.3)
- **Tested DB backup/restore + WAL archiving** for prod (`docker-compose.prod.yml` has a volume, no backup) — existential insurance for ledger/reputation/job state. *(I4/E4 — partly ops.)*
- **Per-job cost telemetry** (prefill/decode split, batch size, peak memory, KV size) flowing agent→control — unlocks routing/pricing intelligence (Plane C/D "Exchange Brain").

### Top recommendations (synthesis ranking)
| # | Move | Status |
|---|---|---|
| 1 | Configure the Postgres pool | ✅ done |
| 2 | Close demo.html XSS (name + error) | ✅ done |
| 3 | Agent: stop finished-seq compute + bound mask cache | Tier 2 — deferred: vendored/patched candle code + needs live-Metal verify; bounding adds a 2nd upstream divergence the team minimizes |
| 4 | Object-storage retry / circuit breaker | PutObject retry ✅ done · Get-retry + breaker Tier 1 |
| 5 | Ledger CHECK + queryable charge_status | ✅ both done |
| 6 | Fix verification double-dock fallback | ✅ done |
| 7 | Parallel per-model backend pool | Tier 2 |
| 8 | Tested DB backup/restore | Tier 2 |

> **Audit findings disproved (not bugs):** GitHub OAuth tokens are already sealed at rest
> (`store.go`); clawback is already atomic (single tx). Left unchanged.

> **External / deferred (unchanged, already tracked in `RELEASE_CANDIDATE.md`):** Stripe/Trolley
> creds + supplier tax onboarding, prod Postgres/S3 with TLS+backups, legal/compliance opinions,
> Apple Developer ID signing, multi-Mac field testing, first real buyer/payout. None are code.

---

## 4b. Frontier scaffolding (seams in place — execution is the remaining work)

Per the "build the seam, never fake the work" discipline, the frontier bets now have
**buildable, honest seams**; what remains is the execution behind each boundary.

| Frontier bet | Seam (in code) | Boundary surfaced | What completes it |
|---|---|---|---|
| **MLX serving lane** (vLLM-MLX continuous batching) | `inference_backend = "mlx"` (`agent/config.rs`) → `MlxRunner` (`agent/runners.rs`), inserted first for generative LLM jobs | `RunError::ExternalSubstrate` — "MLX runtime not wired (mlx-rs / Metal FFI)" | Wire the MLX runtime (mlx-rs / Metal FFI) behind `MlxRunner::run`; `can_run` already gates the lane. Default Candle path unchanged. Unit-tested (`mlx_runner_gates_llm_jobs_and_surfaces_boundary`). |
| **Distributed Mac cluster** (exo / Thunderbolt) | `ClusterRunner` (`agent/runners.rs`) + `cx-agent cluster-plan` + summed-memory routing (pre-existing) | `RunError::ExternalSubstrate` — "Exo / MLX-distributed / JACCL over Thunderbolt 5 not available" | The external substrate on **≥2 physical Macs** (the part needing a second Mac). Routing + shard plan already proven locally. |
| **Optimistic verification** (Verde bisection / TAO) | `disputes` table + `POST /v1/jobs/{id}/dispute` → `RecordDispute` (buyer-scoped) **+ baseline resolver `resolveDisputes` (`control/workers.go`)**: on dispute, dispatches an INDEPENDENT re-run to a distinct same-class supplier (`SelectRedundancyPeerExcluding` + `InsertTiebreakTask`); the existing verifier compares + clawbacks on mismatch; the dispute resolves off that objective verdict (open→reverifying→resolved/rejected), surfacing `no_peer` when no distinct supplier is free | dispute lifecycle status + `dispute_*` events; `no_peer` boundary when re-verification needs a second supplier | **Done (baseline, local):** full-rerun recompute + objective resolution, no new money logic. **Remaining:** the OPTIMIZED resolver (operator-level bisection, Verde/TAO) + the released-payout guarantee edge (clawback after payout already released). |

## 5. What this session changed

All verified: `gofmt`/`go vet`/`go build`/`go test` clean and **`make prove-local SKIP_LIVE=1`
→ 104 pass, 0 skip, 0 fail** after the changes (a first attempt's over-broad ledger CHECK was
caught by the proof and scoped to `supplier_credit`).

- `control/main.go` — bounded pgx pool (`NewWithConfig`: MaxConns 20 / `DB_MAX_CONNS`, 30m lifetime, 5m idle, startup log); shutdown drain 15→30s.
- `control/store.go` — `MarkPayout` refuses to mark `released` without a payout ref.
- `db/schema.sql` — `ledger_released_requires_ref` CHECK (scoped to `supplier_credit`); `ledger_kind_idx`.
- `control/api.go` — `secureHTMLHeaders` on `handleDashboard`/`handleApp`/`handleDemo`.
- `control/metrics.go` — metrics-query timeout as a named 10s (was a cryptic ns literal); `time` import.
- `web/demo.html` — `esc()` helper; escaped file name, run rows, and error messages (XSS); live-poll now surfaces 401/non-2xx/network failures + caps attempts; 1 MB truncation warning.
- `docs/PRODUCTION_AUDIT.md` — this document.

**Pass 2** (also verified at 104/104):
- `control/verification.go` + `store.go` + `api.go` — verification double-dock fixed (real peer supplier threaded via `CommitTaskInfo.peerSupplierID`).
- `control/billing.go` + `store.go` + `db/schema.sql` — `jobs.charge_status` set in `chargeForJob` (`SetChargeStatus`).
- `control/storage.go` — `PutObject` bounded ctx-aware retry.
- `control/api.go` — `observe` middleware: `X-Request-ID` correlation + structured access log (`statusRecorder` with `Unwrap`).
- `web/demo.html` — removed leftover unused `mDot` var.

**Pass 3** — security / testing / deployment (verified):
- **5 reachable stdlib CVEs fixed** — `govulncheck` flagged `net`/`net/http2`/`crypto/x509`/`mime`/`net/textproto` advisories reachable from the server, pgx, minio, Stripe. Bumped the toolchain `go 1.26` → **`go 1.26.4`** (`control/go.mod`, `cli/go.mod`); re-scan: **0 vulnerabilities**.
- **CI vuln gate** — `govulncheck` step added to `.github/workflows/ci.yml` (+ pinned `go-version: 1.26.4`) so new reachable CVEs fail the build.
- **Money-invariant regression test** — `TestMarkPayoutRefusesReleasedWithoutRef` proves a credit can't be marked `released` without a rail ref.
- **Deployment: container healthcheck** — distroless `control` had none (no shell/curl); added a `control healthcheck` self-probe subcommand (hits `/healthz`, needs no DB) + a Docker `healthcheck` in `docker-compose.prod.yml`.
- *(Investigated + deferred, with reason)*: the agent "semaphore-not-mutex" rec would **double model memory for ~no gain on a single Metal GPU** (kernels serialize anyway; fights the memory throttler) — the real win is continuous batching (the MLX lane). Left as Tier 2.

**Pass 4** — reliability + test coverage (verified at 105/105):
- `control/storage.go` — `GetObject` bounded retry on transient errors, **fail-fast on `NoSuchKey`** (never retry a genuinely-missing result; verification must not treat absent as pass).
- `control/workers.go` — stale-task requeue now uses **exponential backoff** by prior retries (1m→2m→4m→8m→16m capped) instead of a fixed 1m.
- `control/control_test.go` — `TestObserveMiddlewareRequestID` (correlation-id generated/propagated, status passed through).
- `control/storage.go` — extracted a single retry helper; `PutObject`/`GetObject` share one retry policy (removed the duplicated backoff/ctx loop — "eliminate duplication").

**Pass 5** — storage circuit breaker (verified):
- `control/storage.go` — added a `storeBreaker` (opens after 5 consecutive fully-failed calls, 10s cooldown) folded into the shared `withRetry`, so a **sustained** store outage fails fast instead of every caller grinding through retries and saturating the bounded DB pool. A call the store *answers* (success or NoSuchKey) keeps it closed, so normal 404s never trip it.
- `control/control_test.go` — `TestStoreBreaker` exercises the full open→cooldown→close state machine with an injected clock (no infra).

**Pass 6** — full live proof + fix:
- Ran the **full live-Metal release-candidate proof** (not just the matrix); it surfaced 3 failures the matrix can't. Confirmed none were caused by this session's work (zero `agent/`/`macapp/` changes). **Fixed `macapp-build`** — a stale Swift module cache pointing at an old `computeexchange` path from a directory rename (`rm -rf macapp/.build`; `swift build` now clean). The remaining `status-file`/`memory-telemetry` are the agent-telemetry-timing checks `ROADMAP_STATUS` documents as environmental/flaky.

**Pass 7** — frontier scaffolding (seams, verified; see §4b):
- **MLX serving lane** — `agent/config.rs` `InferenceBackend` (candle|mlx) + `agent/runners.rs` `MlxRunner` (honest `ExternalSubstrate` boundary) + `agent/main.rs` opt-in wiring + unit test. cargo build/clippy/test green; default Candle path unchanged.
- **Optimistic verification / buyer-dispute** — `db/schema.sql` `disputes` table + `control/store.go` `RecordDispute` (buyer-scoped) + `control/api.go` `POST /v1/jobs/{id}/dispute` (`handleFileDispute`, emits `dispute_filed`, surfaces the recompute boundary).
- **exo cluster** — confirmed the pre-existing `ClusterRunner` seam is exemplary; the only missing piece is a second physical Mac.

**Pass 8** — adversarial review of the new seams (4 parallel reviewers + per-finding verification) caught and fixed a **real high-severity bug it introduced**, proving the review's worth:
- `MlxRunner` was inserted FIRST and `can_run` ignored `hw_class`, so on a cluster worker a giant (405B/671B) model would hit the MLX boundary instead of `ClusterRunner`'s — the *wrong* boundary (a BLACKHOLE violation the unit test + matrix missed). **Fixed**: `MlxRunner.can_run` now yields cluster models (`!is_cluster_model`), and it's inserted *after* `ClusterRunner` (defense-in-depth); strengthened the unit test (asserts a 405B model and Rerank are NOT claimed).
- **Dropped `Rerank`** from the MLX lane — it's MiniLM-embedding-based, not generative-LLM; it stays on Candle (would otherwise have been blocked when MLX is enabled).
- Added `TestFileDispute` (integration): owner files a dispute → 202 + recorded; a non-owned job → 404 + nothing recorded (buyer-scoping proven end-to-end). Added `disputes` to the test-reset truncation.
- Review verdict on the rest: security-abuse and gating-regression dimensions **clean** (dispute scoping correct; MLX truly default-off).

**Pass 9** — completed the one frontier seam whose execution is genuinely local-doable: the **optimistic-verification baseline resolver** (`control/workers.go` `resolveDisputes`, 20s tick). On a buyer dispute it dispatches an INDEPENDENT re-run of the disputed job's primary result to a *distinct* same-class supplier (reusing the proven `SelectRedundancyPeerExcluding` + `InsertTiebreakTask` redundancy path); the existing verifier compares + clawbacks on a real mismatch; the dispute resolves off that OBJECTIVE verdict — `open`→`reverifying`→`resolved` (upheld, original clawed back) / `rejected` (re-run agreed) — and surfaces `no_peer` (retried) when no distinct supplier is free. Blast radius is contained: `disputes` is only populated by the opt-in endpoint, the resolver adds **no new money logic** (clawback/refund flow only through the existing verifier), and existing flows are untouched. Store: `ActiveDisputes`/`ReverifyTarget`/`SetDisputeReverifying`/`SetDisputeStatus`/`JobHasPendingTasks`/`TaskHasClawback`; schema: `disputes.reverify_task_id`. Verified: `TestDisputeResolverNoPeer` (the boundary) + matrix **110/110 PASS**. Remaining frontier: the OPTIMIZED resolver (operator-level bisection, Verde/TAO) + MLX runtime FFI + exo on a 2nd Mac — all genuinely multi-week/hardware-bound.

Net: the system was already release-candidate (104/104, Launch wired). Across the session this
work closed the two demo XSS holes, the top load-bottleneck (DB pool), a money-integrity gap +
charge observability, the one latent verification bug, storage upload resilience, and added
request-level observability — while disproving two audit "bugs" (OAuth sealing, atomic
clawback) rather than churning correct code. The remaining frontier (MLX + continuous batching,
exo cluster, optimistic verification, real agent concurrency) is multi-week and scoped above.
</content>
