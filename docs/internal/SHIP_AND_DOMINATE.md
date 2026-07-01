<!-- Generated 2026-06-28 by a 27-agent audit+research workflow (6 codebase audits, 5 cited
frontier-research reports, 12 adversarial verifications). Hype discounted per the verdicts. -->

# Computexchange: Shippability + Dominance Roadmap

A lead synthesis of the parallel audits, frontier research, and adversarial verdicts. Hype is discounted per the verdicts: only confirmed/partly-true gains are carried forward (at their **revised** values), and refuted claims are named so you aren't misled.

---

## 1. Shippability verdict

**Honest call: the engine is done; the business around it is not.** The backend is functionally complete and proven (104/104 local matrix, live Metal inference, exact-money ledger, real verification spine, zero outstanding TODOs). What stands between you and charging real customers at scale is almost entirely **external + frontend + ops**, not core correctness.

The critical path to first real revenue is **not** the exo cluster (that's a separate flagship lane). It is:

1. **Stripe Connect live credentials + supplier tax-identity (W-9/W-8BEN) onboarding** — you literally cannot pay a supplier today.
2. **A buyer surface that can sign up, get a key, add a card, and see results** — none of this exists in shippable form.
3. **Production data durability** — no automated Postgres/MinIO backups for the money source-of-truth.
4. **Monitoring/alerting** — `/metrics` exists but nothing scrapes or alerts.

One-line readiness per subsystem:

| Subsystem | Readiness |
|---|---|
| **Control plane (Go)** | **Ship-ready.** Correct, idempotent, tested. Gaps are config/observability hardening (hours), not blockers. |
| **Agent (Rust)** | **Ship-ready on single-Mac path.** Honest, warm-pooled, no fabrication. Throughput left on table (mutex serialization, finished-seq waste) but correct. |
| **Data + money** | **Code-ready, operationally blocked.** Ledger is exact and structurally guarded; blocked on Stripe live creds + tax onboarding (legal). |
| **Ops / DR** | **NOT ready.** Single droplet SPOF, no backup automation, no replication, no alerting. Acceptable only for capped, hand-watched alpha. |
| **Frontend** | **NOT ready.** Test harness + uncompiled menubar app. No signup, no key lifecycle, no billing UI, no run detail, no docs. This is the real wall. |

---

## 2. Ship blockers (ranked, must-do before scaling real revenue)

| # | Blocker | Why it blocks | Effort | Files / area |
|---|---|---|---|---|
| 1 | **Stripe Connect live creds + supplier tax-identity (W-9/W-8BEN/T4A) onboarding** | Payout rail honestly refuses to mark money "released" without a real rail ref. No tax IDs = cannot legally transfer funds or issue 1099s. Today all payouts sit at `ready` forever. | **L** | `control/payment.go:150-226`, `control/store.go` (no supplier register endpoint), `db/schema.sql:17-27` |
| 2 | **Legal/compliance green-light** | FINTRAC/MSB question on buyer→rail→supplier flow, CRA Part XX digital-platform reporting, GST/HST, PIPEDA/Law 25 for job PII, ToS (contractor classification, disputes). All external, none code. | **XL** | external legal |
| 3 | **Buyer surface: signup → key → card → results** | No self-serve onboarding, no key issuance endpoint, no billing UI wired, no persistent job history. The product cannot acquire a buyer without you hand-seeding a key. | **L** (see §3) | `control/api.go` (no buyer-account/key endpoints), `web/demo.html` |
| 4 | **Production data durability (DR)** | Ledger is existential. No automated `pg_dump`/WAL archiving, no offsite MinIO backup; `scripts/backup.sh` is a manual one-shot doing a same-host `cp -R` (useless against host failure). | **L** | `docker-compose.prod.yml`, `scripts/backup.sh`, `docs/RUNBOOKS.md` |
| 5 | **Monitoring + alerting** | `/metrics` and `/healthz` exist but nothing scrapes them; container logs go nowhere; no alerts for pool exhaustion, payout failures, cert expiry, wedged background tickers. "Operationally unacceptable above ~10 concurrent jobs." | **M** | `control/metrics.go`, `control/main.go`, deploy |
| 6 | **Buyer `charge_status` exposed + charge-failure notification** | `chargeForJob` is best-effort and silently no-ops with no saved card → buyer sees `complete`, never charged, debt sits in ledger forever. `JobView` omits `charge_status`. (Note: submit-time **402 gate already exists** at `api.go:355`, which mitigates the worst case.) | **M** | `control/billing.go:236-255`, `control/store.go:1050-1068` |
| 7 | **Single-point-of-failure control plane** | One instance on one droplet is also the webhook handler + background-worker orchestrator. Droplet dies → everything stops. SKIP-LOCKED queue is multi-instance-ready but there's only one instance. | **L** | `docker-compose.prod.yml`, `control/workers.go` |
| 8 | **Supplier macOS app: Developer ID sign + notarize + auto-update** | Operator cannot download/run the agent. SwiftUI scaffold is complete and builds, but unsigned/unnotarized = undistributable. | **XL** (gated on Apple Developer ID + Sparkle) | `macapp/`, `scripts/install.sh` |
| 9 | **TLS cert renewal + secrets rotation** | Caddy assumed pre-wired; no documented config, no renewal monitoring, no secret rotation/revocation path. Cert expiry = total TLS outage with no alert. | **S–M** | `docker-compose.prod.yml`, `.env.example` |

**Lower-priority hardening (real, not blockers):** object-store circuit breaker is already landed (5-fail open, 10s cooldown) — verify only; demo.html XSS + live-poll 401 handling **audited as fixed** — verify deployed; background-ticker supervisor/liveness guard (a wedged payout ticker is silent); webhook dead-letter queue; per-API-key rate limiting (currently per-IP, gameable).

---

## 3. Frontend: the contention (your #1 worry)

**Honest gap:** today you have `web/demo.html` (a 646-line single-file **test harness** that boots into a hard-coded fake logged-in state — "Alex Rivera", "$1,284", seeded fake runs) and an **uncompiled** SwiftUI menubar app. Best-in-class dev-infra (Stripe, Modal, Replicate, Anthropic console) pairs the API with a real self-serve **console** where signup→first-call→billing→retention happens. You have none of that surface. **The aesthetic is not the problem — it's a moat. Keep the Geist-Mono / anodized / concrete-void doctrine.** The problem is missing product surfaces.

**Critical honesty correction (from verdicts):** several "just build the frontend" items are actually **backend projects**. There is **no buyers/accounts table, no key-issuance endpoint, no scopes** (only a single `is_admin` boolean at `schema.sql:131`), and **ledger spend is keyed by `buyer_id`, never by key**. And a new buyer hits a **hard 402 payment gate** (`api.go:355`) before any job — so "green light in 60 seconds" is impossible without a free-credit/sandbox lane that doesn't exist. Do not promise self-serve PLG as a pure UI swap.

**What to build, prioritized:**

1. **Copy-paste quickstart empty-state (ship now, days).** Replace the fake Runs list with an Informational/Guide empty state: one primary CTA + copy-clean curl / Python (the real `Client.embeddings`) / `cx` CLI snippets, **realistic values, the user's real key injected**, leading with the **OpenAI-Batch drop-in** (one-line `base_url` change). This is the genuinely high-leverage, buildable-today win. Surface it in SDK/CLI/docs too, since demo.html is explicitly a non-shipping harness.

2. **The verification receipt (highest-ROI differentiator, ~1-2 wk — but build the backend first).** This is your moat made visible, and **it does not exist anywhere in the code** — `JobStatus` carries zero verification fields. Add honeypot-passed / redundancy-matched / tiebreak-record / dispute-verdict to the job response and render it as the hero element of a run-detail page. Caveat (verdict): it will be **operator-attested, not cryptographically buyer-verifiable** — so market it as "verified multi-supplier cross-check on a real two-sided market," not "uncopyable proof." And it degrades to honeypot-only at low supply density, so gate the full "verified" tier on peer availability.

3. **API-key lifecycle panel.** *Tier 1 (small, real primitives exist — clone `CreateWorkerToken` at `store.go:616`):* named keys + reveal-once + revoke + `cx_live_`/`cx_test_` prefix. Needs 3 new endpoints + a `name` column. *Tier 2 (real backend epics, do NOT ship as UI):* **scopes** (replace `is_admin` boolean with a scope model + per-route enforcement) and **per-key spend** (stamp `api_key_id` onto `ledger_entries`). Do not render scopes/per-key spend before the schema supports them — that violates BLACKHOLE.

4. **Run-detail page: stage timeline + live logs + verification status.** Reuse the existing `.pipe/.stage` timeline as a retrospective per-job view (queue→running→verifying→complete) with durations and verbatim failures. Modal/Replicate are the reference; verification-as-a-visible-stage is where you beat both.

5. **Billing + usage surface.** Replace the static "•••• 4242" row with live spend (reuse the hand-rolled SVG sparkline engine at `demo.html:394`), MTD estimate, per-job line items, and a hard spend cap. Wire real Stripe Elements to the existing `/v1/billing/setup`.

6. **Results retrieval as a first-class screen + IA cleanup.** Typed artifact preview, real signed-URL download, re-run. Split the overloaded Settings grab-bag into Overview / Runs / Usage&Billing / API Keys / Docs / Settings, with a ⌘K command menu (cheap craft signal that fits the instrument doctrine).

**Supplier app:** add earnings sparkline, **payout proof** (last/next payout), a defensible **verification badge** ("honeypots passing" — never a badge you can't back), and a **first-run consent/onboarding** popover (what runs, resource limits, quiet hours, 90/10 split) before the agent accepts work. Trust made felt is what converts a skeptical Mac owner into liquidity.

---

## 4. Performance: be the fastest

### Inference (Apple Silicon) — discounted hard against the verdicts

The research's headline 2-4x/4.3x continuous-batching and 5.8x prefix-caching numbers are **interactive, 16-concurrent, 128GB-M4-Max benchmarks measured against serial decode** — a baseline CX doesn't run (CX already batches within a length bucket, on bandwidth-bound consumer Macs). Real CX-applicable wins:

| Win | Revised gain | Effort | Notes |
|---|---|---|---|
| **Active-set shrink on EOS** (stop stepping finished sequences in mixed batches) | **~1.1–1.5x** on high output-length-variance jobs, ~1.0x uniform | **S–M** | No architecture change. Do this FIRST, measure. (`runners.rs:1032-1048`) |
| **Bound the mask-cache OOM leak** | reliability, not speed | **M** | Long transcriptions/Llama loops OOM today. (`runners.rs:635-693`) |
| **Per-model semaphore + N independent backends** (kill the `Arc<Mutex>` serialization) | up to **~Nx** on concurrent same-model tasks (currently 1x) | **M** | The mutex makes "bounded concurrency" partly fiction today. (`pool.rs:86-121`) |
| **No-padding ragged batching** (let different-length prompts share a batch) | **~1.2–1.8x** on genuinely mixed jobs, near-0 when prompts cluster or batch is already bandwidth-bound | **L** | Needs per-sequence positions + ragged KV. Only invest after measuring prompt-length variance. |
| **Prefix caching for shared-instruction jobs** (classification/extraction ONLY) | **~2–4x** wall-clock on classification (max_tokens=12, prefill-dominated); **~1.5–2.5x** on extraction; **0x on rerank** | **M** | Rerank is an embedder, not generative — the research wrongly lumped it in. |
| **O(1) KV append** (replace `Tensor::cat` per step) | amortized, matters at 256+ output tokens | **L** | Prerequisite for efficient ragged batching. |
| **Quantization: keep Q4_K_M** | confirms current default is near-optimal (~99% quality) | **S** | Optional Q8 lane for quality buyers. |

**Refuted / wrong-lever — do NOT spend effort here:**
- **mistral.rs adoption "for 2-4x"** — *partly-true → mostly hype for CX.* It does NOT manufacture the cross-task concurrency the 2-4x needs; PagedAttention is **off-by-default on Metal** (no FlashAttention prefill assist there) and could pressure unified memory into your throttler. Only prefix caching is bankable, and that doesn't require leaving Candle. Porting off your vendored `quantized_llama` + re-proving verification determinism is an **L** cost, not a clean swap.
- **Speculative decoding** — *wrong lever for batch.* Goes net-negative once the GPU is compute-bound (your normal regime). Reserve only for a hypothetical low-latency single-request lane.
- **Quantized KV cache** — ~33% **slower** on Metal (no fused fast path).

### Control plane (Go/Rust hot paths)

| Win | Revised gain | Effort |
|---|---|---|
| `GOMEMLIMIT` as an OOM safety valve (absolute value, ~256–350MiB, on the cramped 1GB droplet) + add per-service `mem_limit`/`cpus` to compose | OOM hardening, **~0 p99 CPU gain** | **S** |
| Hoist `cheaper_class_online` EXISTS subquery + ORDER BY out of the in-transaction claim | constant-factor per-claim CPU saving | **M** |
| Autovacuum tuning on the churning `tasks` table | DB hygiene (prevents xmin-horizon dead-tuple bloat) | **S** |

**Refuted — do NOT do:**
- **"SKIP-LOCKED query optimization unlocks 30k jobs/s, highest-ROI backend change"** — *refuted.* The partial index **already exists** (`schema.sql:362-363`), isolation is **already READ COMMITTED** (pgx default). The 30k/s figure is a no-op-workflow benchmark on a different stack; CX tasks are ~45s of Metal compute with [2,4] concurrency behind a 25s long-poll, so you'd need ~11k–45k busy Macs to even approach 1k claims/s, and the pool caps ~100 jobs/s first. The queue is **not** the wall.
- **Migrate to fasthttp/gnet** — *confirmed: do NOT.* Every request is DB+S3-bound; HTTP framing is a rounding error. You'd lose HTTP/2 and stdlib correctness for an unmeasurable gain.
- **Replace `GOMAXPROCS` / add automaxprocs** — *partly-true → no.* Go 1.26 is already container-aware, and there's no CPU quota set to mismatch. Adding automaxprocs would be redundant dead code.
- **sonic/protobuf on the control wire** — not worth it; bulk data already bypasses it via presigned S3. Your hand-rolled binary embedding encoder is the correct pattern in the one place binary pays.
- **`pgx` → `database/sql`** — keep pgx; you already have the auto-prepare/binary-protocol ~3x win for free.

---

## 5. Self-reliance: hand-roll vs keep (the direct answer)

The honest headline: **you are already hand-rolled in every place that pays, and your kept dependencies are the treacherous ones you correctly should not own** (TLS, Postgres wire, S3 signing, crypto primitives, tokenization). BLACKHOLE is being applied correctly.

| Component | Verdict | Gain / risk |
|---|---|---|
| **Go HTTP (net/http + 1.22 routing)** | **ALREADY-HANDROLLED (keep)** | Zero framework; switching is negative ROI. |
| **Metrics (Prometheus text)** | **ALREADY-HANDROLLED** | ~50 lines, no telemetry SDK. Correct. |
| **Binary embedding encoder** | **ALREADY-HANDROLLED** | The right place for binary; extend only for new dense-tensor job types. |
| **Python SDK (urllib)** | **ALREADY-HANDROLLED** | Dep-free, elegant. |
| **Candle (Rust inference)** | **KEEP + hand-roll the wins on top** | The frontier wins (active-set shrink, ragged batch, prefix cache, O(1) KV) are **patches to your vendored `quantized_llama`**, not a runtime swap. This is the high-value hand-roll lane. |
| **pgx (Go + Rust)** | **KEEP** | Already gives the prepared-statement win; hand-rolling the wire is pure overhead. |
| **minio-go** | **KEEP** | Presigned-URL HMAC signing is treacherous; do not own it. |
| **Stripe SDK** | **KEEP** | Idempotency + webhook verification non-trivial; licensing is the blocker, not the code. |
| **tokio / candle deps (serde, reqwest, tokenizers, hf-hub, aes-gcm)** | **KEEP** | Async runtime + crypto + BPE tokenization are not worth hand-rolling. |
| **SwiftUI (menubar)** | **KEEP** | NSStatusBar/event-loop hand-roll is wasteful. |
| **Caddy / docker-compose** | **KEEP** | Operational choice, no code to maintain; do not hand-roll TLS/reverse-proxy. |
| **Frame parsing / `GOMAXPROCS` tuning libs** | **HAND-ROLL (marginal) → skip** | Premature on a single droplet running Go 1.26. |
| **exo / MLX-distributed (cluster substrate)** | **INTEGRATE, don't build** | Hand-rolling distributed collectives over TB5 is not viable; wire exo behind your already-clean `ClusterRunner` seam. |

**Net:** the only meaningful *new* hand-rolling worth doing is **inside the Candle inference path** (the §4 wins), where you control the kernel and the gain is real. Everywhere else, keep.

---

## 6. Market share: how to win

**The wedge (real but narrower than the research claimed):** *cheap, statistically-verified BATCH inference on idle consumer Apple Silicon, for embarrassingly-parallel small/mid-model jobs* — embeddings at scale, classification/tagging, document extraction, reranking, synthetic-data/eval generation. You already ship exactly these job types. **Do not** market tok/s or 70B chat: an H100 does ~2,300 tok/s on Llama-8B vs Apple's ~20-96 tok/s, and bandwidth (3.35 TB/s vs ~400 GB/s) crushes Apple on batched large-model decode.

**Beachhead:** teams already running **OpenAI Batch** for nightly embeddings/classification. You have an OpenAI-Batch-compatible API — make "your batch jobs, verified and cheaper, one line changed" the front door. Zero switching cost, highest-intent segment.

**Pricing + network effect:** price **per completed job** (per 1M docs embedded/classified), not per-GPU-hour — interruption-immune (verification + requeue absorbs it), which neither per-hour marketplaces nor per-token APIs match. Supply is idle Macs (zero CapEx, 2-4x better tokens/watt). Bootstrap the two-sided cold-start by **seeding demand with your own internal jobs** so early suppliers always get paid, then open the buyer side via the menubar "earn from your idle Mac" app.

**Honest disadvantages (from the verdicts — internalize these):**
- **The "verified" cell is contested, not empty.** Phala (GPU-TEE on OpenRouter), Cocoon, Spheron ship attested inference now; Hyperbolic ships Proof-of-Sampling at <1% overhead. Your verification is **statistical/replay, weaker than hardware attestation** — and currently **invisible to the buyer** (no receipt exists). Drop "uncopyable moat" framing. Your durable edge is *working multi-supplier cross-check on a real two-sided market*, plus data-residency/sovereignty.
- **You cannot win on cost-per-token.** "Throughput-per-device doesn't matter" is wrong: batch buyers buy on cost-per-token, which throughput/bandwidth + your verification redundancy (1.5-3x compute) + S3 round-trips + 90/10 split all inflate. Incumbents already sit at $0.001-0.01/1M. **Compete on verifiability + sovereignty + supplier economics, not price leadership.**
- **Model quality gap is real engineering, not positioning.** You ship only **all-MiniLM-L6-v2** for embeddings (well below BGE-M3/Qwen3 on MTEB). "Embeddings at scale" today means low-quality embeddings. **Upgrade the embedding/rerank models before marketing the wedge.**

**Flagship lane (separate bet, fund it):** the **exo/MLX-distributed Mac cluster** is your strongest frontier moat (*partly-true → real*). ~$50k of Macs serves 235B-1T models needing ~$780k of H100s. Sell the **single-stream giant-model capacity** (proven: 24-32 tok/s on 4× M3 Ultra) first; batch cost-per-token second (the 1.8x/3.2x figure is single-stream TP scaling, not batch aggregate — validate before banking it). Real blocker is **≥2 TB5-class Macs on macOS 26.2** + in-repo product work (widen cluster model markers beyond 405b/671b, multi-node billing, context limits), not merely "a 2nd Mac."

---

## 7. The prioritized top 12 (impact-to-effort)

| # | Action | Why | Effort |
|---|---|---|---|
| 1 | **Stripe Connect live creds + supplier tax-ID onboarding** | You cannot pay a supplier or charge legally without it — gates all revenue. | **L** |
| 2 | **Build the verification receipt + surface it in `JobStatus` and a run-detail page** | Converts your deepest technical investment from invisible to your #1 differentiator. ~70% built on backend, 0% on buyer surface. | **M** |
| 3 | **Copy-paste quickstart empty-state (curl/Python/CLI, OpenAI-Batch drop-in front door)** | Cheapest high-leverage buyer-activation win; today the path is cold-zero. | **S–M** |
| 4 | **Automated Postgres + offsite MinIO backups + WAL archiving + tested restore** | The ledger is existential; current backup is a useless same-host copy. | **L** |
| 5 | **Monitoring + alerting (scrape `/metrics`, ship logs, alert on payout/pool/cert/ticker)** | "Unacceptable above 10 concurrent jobs" without it. | **M** |
| 6 | **Buyer account + key-issuance endpoint + Tier-1 key lifecycle UI** | No self-serve buyer acquisition exists; clone `CreateWorkerToken`. | **M (backend) + M (UI)** |
| 7 | **Expose `charge_status` + charge-failure events/notification** | Silent uncollectable debt today; buyer sees "complete," never pays. | **M** |
| 8 | **Agent: per-model semaphore + N backends; active-set shrink on EOS; bound mask cache** | Real per-Mac throughput + reliability — the supply-cost lever, on the Candle path you own. | **M** |
| 9 | **Real billing UI (live spend, MTD, line items, hard cap) + Stripe Elements wiring** | Buyers won't scale volume on a platform that can't show the meter. | **L** |
| 10 | **Upgrade embedding/rerank models (BGE-M3 / real cross-encoder)** | "Embeddings at scale" on MiniLM is low-quality; required before marketing the wedge. | **M** |
| 11 | **macOS app: Developer ID sign + notarize + Sparkle auto-update + first-run consent** | Suppliers literally cannot run the agent today; supply liquidity gate. | **XL** (Apple-gated) |
| 12 | **Wire exo behind `ClusterRunner` on ≥2 TB5 Macs; sell single-stream giant-model capacity** | Flagship moat: $50k Macs = $780k of H100s. Budget as multi-week integration + hardware buy + batch validation. | **L** |

**Sequencing note:** 1-5 are the genuine ship gate (revenue + don't-lose-data + don't-fly-blind). 6-10 make it a product buyers adopt and a supply that's worth running. 11-12 are the durability and dominance bets. The exo cluster is your most exciting story but it is **not** on the critical path to first dollar — ship the batch wedge first.

---

**Files of record for the above:** payout/ledger `control/payment.go:150-226`, `control/billing.go:236-255`, `db/schema.sql:17-27,112-113,127-134,237,362-363`; queue/claim `control/scheduler.go:286,349-362`, `control/api.go:355,1817`; agent perf `agent/src/runners.rs:963-1059,1032-1048,1236,1343,1489-1530`, `agent/src/pool.rs:86-121`, `agent/src/quantized_llama_batched.rs:218-230,487-518`; cluster seam `agent/src/cluster.rs`, `agent/src/runners.rs:1609`; frontend `web/demo.html` (key rows :354-358, live path :593, sparkline :394), `macapp/`; ops `docker-compose.prod.yml`, `scripts/backup.sh`, `control/workers.go`, `control/metrics.go`.