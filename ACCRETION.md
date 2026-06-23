# ACCRETION — The Grand Plan

*BLACKHOLE.md is how the code stays dense. ACCRETION.md is how the black hole feeds:
how a proven, honest engine greedily pulls in supply, demand, and margin until it
is the default settlement layer for batch AI compute. Written 2026-06-23, grounded
in a full subsystem audit + live market data (citations inline).*

---

## 0. TL;DR — the thesis, sharpened

Computexchange today is a **genuinely real, honest, well-built two-sided compute
marketplace** that is *over*-built on engineering and *under*-built on reach. The
audit found ~13.7k lines of load-bearing Go + a real Rust inference agent with the
"never fake" doctrine honored throughout — far more real than typical pre-launch
code. The gaps are not "is it real" — they're **four money/security P0s, a
single-architecture supply ceiling, and no product skin.**

The market has moved while the wedge was being polished. Two facts reprice
everything:

1. **The H100 collapsed.** Median H100 rental is now **~$2.99/GPU-hr, down 64–75%
   from $7–10 in early 2024**, with spot as low as **$1.03/hr**.
   ([SemiAnalysis](https://semianalysis.com/gpu-pricing-index/),
   [Spheron](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/))
2. **Commodity embeddings are ~free.** OpenAI batch `text-embedding-3-small` is
   **$0.02 per *million* tokens**.
   ([TokenMix](https://tokenmix.ai/blog/openai-batch-api-pricing))

The original plan targeted embeddings at **$1–3/M tokens** ("70% under OpenAI").
Against $0.02/M that is **15–150× too expensive.** Embeddings are a loss-leader
commodity, not a wedge. **We stop pretending otherwise.**

**The greedy reframe:** the defensible, high-margin territory is **large-model
batch inference (30B–70B+) priced per-output, on whatever silicon is cheapest to
produce that output** — Apple unified memory where it wins (huge models, privacy,
power), NVIDIA where the volume already is. Verification is the moat that turns
unreliable consumer supply into trusted output. We build the **settlement and
verification layer for batch AI**, lane-agnostic, and we feed on both.

---

## 1. Where the project is AT (audit scorecard)

| Subsystem | Maturity | Verdict |
|---|---|---|
| Control plane API / auth wiring | **4/5** | Real, stdlib, every route authed |
| Job lifecycle (submit→run→merge→bill) | **3.5/5** | End-to-end real; merge-then-mark is honest |
| Scheduling (`FOR UPDATE SKIP LOCKED` + budget governor) | **3.5/5** | Strong, race-free claim path |
| Data model (Postgres) | **4/5** | Comprehensive, indexed; missing ledger uniqueness |
| Verification / fraud (honeypot + redundancy) | **3/5** | Real, but leaks honeypot flags to workers |
| Billing / payout correctness | **2.5/5** | Ledger real; collection + payout rail soft |
| Agent — Metal lane | **4/5** | Real, CI-tested, the only production lane |
| Agent — CUDA lane | **2/5** | Validated spike, **not** a productized lane |
| `cli/` (Go) + `sdk/` (Python) | **5/5** | Shipping-quality, stdlib-only |
| `web/` front-end | **2.5/5** | Wired skeleton + faked demo, **no product** |
| `macapp/` (Swift menu-bar) | **4/5** | Real, compiles; unsigned (needs Apple Dev ID) |
| `proto/` wire contract | **4.5/5** | Authoritative but hand-maintained, already lagging |

### The four P0s that block real money (fix before any paid traffic)
1. **Worker tokens are stored & compared in plaintext** (`store.go:415`, `seed.go`) — asymmetric with hashed API keys. A DB read leaks live supplier credentials. → **Hash them.**
2. **No `UNIQUE(task_id, kind)` on `ledger_entries`** (`schema.sql:93`) — nothing at the DB level prevents a double-charge/double-credit; safety rests on one status guard. → **Add the constraint + `ON CONFLICT DO NOTHING`.**
3. **Honeypots ship their `is_honeypot` flag + expected answer to the worker** (`api.go:1133`) — a hostile agent aces every probe and cheats everything else. Fraud detection is theater against an adversary. → **Verify server-side only; never tell the worker.**
4. **Cardless buyers' completed jobs are "owed" forever** (`billing.go:236`) — no collection, no dunning. → **Require a card at submit, or build real AR.**

### The CUDA verdict (your direct question)
**No — the CUDA lane is not up to par. Metal is 4/5; CUDA is ~2/5.** The inference
*path* is real and **proven on real hardware** — a RunPod A100 spike on 2026-06-23
built `--features cuda` in 1m25s and hit **3,213 emb/s, 170.8 tok/s**
(`docs/RUNPOD_SPIKE.md`). But as a *lane* — a way for NVIDIA supply to join as a
properly-classed, schedulable, verifiable worker — it's missing everything around
the inference: **no NVIDIA hardware class (every NVIDIA box mislabels as `cpu`), no
`nvidia-smi`/VRAM detection, no CI coverage, no `Dockerfile.agent`, no
cross-architecture verification.** It is a validated spike, not a shipping lane.
The repo docs even contradict themselves — `COMPUTE_EXCHANGE_HARDENED_ACTION_PLAN.md`
says "Apple Silicon only, no CUDA," while the code and the dated spike prove
otherwise. **You asked for both lanes to excel. That is the single highest-leverage
TAM unlock in this plan — see §3.2.**

---

## 2. The market (why now, grounded)

- **Decentralized compute is real and growing.** io.net aggregates 30k+ GPUs
  (claims 1M+); Akash hit a record **$5M compute spend in Q1 2026**. Vast.ai
  consistently undercuts RunPod (e.g. L40 $0.31 vs $0.69/hr).
  ([aimultiple](https://aimultiple.com/gpu-marketplace),
  [Medium](https://medium.com/@velinxs/vast-ai-vs-runpod-pricing-in-2026-which-gpu-cloud-is-cheaper-bd4104aa591b))
- **Capital is flowing to decentralized AI.** Prime Intellect ~$70M (Founders
  Fund), Gensyn ~$50M (a16z), **Nous Research $65M at a $1B valuation** (Paradigm).
  ([Sacra](https://sacra.com/c/prime-intellect/),
  [DeSpread](https://research.despread.io/ai-infra-projects/)) The category is
  funded and legitimized — but they chase *training*. **Batch inference settlement
  is open.**
- **Apple Silicon's edge is structural, not marketing.** M4 Max runs Llama-3.3-70B
  Q4 at **12–20 tok/s**; M3 Ultra's **800 GB/s + 192–256 GB unified memory** runs
  DeepSeek-R1 **671B** entirely in memory — *no single consumer NVIDIA card can*.
  ([ModelPiper](https://modelpiper.com/blog/local-llm-benchmarks-apple-silicon),
  [Sean Kim](https://blog.imseankim.com/apple-m4-max-macbook-pro-ai-inference-benchmarks/))
  At ~40–65W vs a 4090's ~350W, the **power-cost-per-token on big models is the
  one number nobody can copy.**
- **The gravity (be honest):** the H100 price collapse compresses any "cheaper
  GPU-hour" pitch. **Competing on $/hour is a losing game.** Competing on
  **$/verified-output for models that are expensive-per-token via API, on the
  cheapest silicon to produce them** — that's the game that's still open.

---

## 3. The grandiose, greedy thesis — what we become

> **The Stripe of batch AI compute: a verification-and-settlement layer that makes
> any idle silicon a trustworthy supplier and any batch job a single priced API
> call — lane-agnostic, output-priced, and impossible to leave once your pipeline
> runs on it.**

Three accretion engines, each pulling mass the others can't:

### 3.1 Demand engine — sell *output*, not *hours*, where the API tax is highest
- Kill the embeddings-price fantasy. **Lead with large-model batch** (30B–70B
  classification, extraction, synthetic data, eval, transcription) where OpenAI/
  Anthropic batch is **$1.25–$7.50/M tokens** and an open 70B on owned silicon can
  undercut **at positive margin** — the opposite of the embeddings trap.
- **Privacy/sovereignty tier:** "your data never touches a hyperscaler, runs
  on-shore / air-gappable." The codebase *already* supports fully-offline, single-
  box, LAN operation — that is a sellable enterprise SKU, not a footnote.
- **Compute Autopilot (Plane C)** becomes the front door: paste a job, it quotes,
  preflights for OOM/cost, routes, and prevents failure before it bills. Buyers
  never see "GPUs." They see *answers with a price tag.*

### 3.2 Supply engine — DUAL LANE (this is the TAM unlock you asked for)
Keep **Apple Silicon as the differentiated wedge** (70B+, privacy, power-efficiency
— the moat). Add **NVIDIA/CUDA as the liquidity lane** (meet the 30k+ GPU DePIN
supply and the bulk of rentable hardware where it already lives). "We also run
anywhere" stops being a footnote and becomes **half the network**:
- Productize the proven spike: NVIDIA hardware classes, `nvidia-smi`/VRAM
  detection, VRAM-gated throttling, `--features cuda` in CI, a `Dockerfile.agent`
  for Linux+NVIDIA, within-class verification. (Roadmap in §4.)
- Result: the marketplace prices a job and routes it to **whichever lane produces
  the verified output cheapest** — Apple for the 70B privacy job, a spot A100 for
  the throughput job. Buyers don't care which; **we capture the spread on both.**

### 3.3 Moat engine — verification + lock-in
- **Verification 2.0** (server-side honeypots, mandatory within-class redundancy,
  reputation-weighted check rates, TOPLOC when cross-arch tolerance is proven) is
  the thing Salad/Vast/io.net structurally lack. It's what lets us sell *trusted*
  output at a premium. **It is the moat — invest here disproportionately.**
- **Lock-in by integration, not contract:** the OpenAI-shaped SDK (`embeddings()`
  already exists) means "change one base URL." Once a buyer's nightly pipeline and
  a supplier's idle-Mac income both run through us, leaving means rewriting both
  sides. The SDK *is* the lock-in.

---

## 4. Codebase amelioration roadmap (the technical "how")

Ordered by leverage. Each item is concrete and traces to an audit finding.

### Wave 1 — "Real money safe" (P0; ~1–2 weeks)
- [x] Hash worker tokens (mirror API-key SHA-256 path) + `CreateWorkerToken` mint helper. `store.go`, `seed.go`, `schema.sql`, `crypto.go`
- [x] `UNIQUE(task_id, kind)` on `ledger_entries` + `ON CONFLICT DO NOTHING` (charge + clawback). `schema.sql`, `store.go`
- [x] Stop shipping `is_honeypot`/`honeypot_ans` to workers; verify server-side only (was already server-side). `api.go`, `types.go`, `agent/src/types.rs`, `proto/`
- [x] Require a saved card at submit when billing is configured (402 otherwise) — no more uncollectable debt. `api.go`, `billing.go`
- [x] Surface missing `CX_TOKEN_KEY`/`CX_STATE_SECRET` loudly at startup (warn, not fatal — local/test run without them by design; prod sets both in .env). `main.go`
- [x] Rate limiting (stdlib token buckets, no new dep): per-IP across the surface + per-credential; loopback + health/metrics exempt. `ratelimit.go`, `api.go`
- [x] Payout rail already live — real Stripe Connect selected when `STRIPE_SECRET_KEY` set (prod has it). End-to-end held→released needs a real supplier (RunPod). `main.go`, `payment.go`

### Wave 2 — "The NVIDIA lane ships" (TAM unlock; ~2–4 weeks)
- [x] `nvidia_*` VRAM-tiered hardware classes (24g/48g/80g/180g) across all three contract files — distinct family from Apple so verification never crosses architectures. `proto/`, `agent/src/types.rs`, `control/types.go`
- [x] NVIDIA detection in `agent/src/hardware.rs`: `nvidia-smi` → name + VRAM → VRAM-tiered class, advertised as the gating `memory_gb` so the scheduler routes on VRAM (was mislabeling every NVIDIA box as `cpu`). + unit test.
- [x] `--features cuda` build gate in CI so the lane can't silently rot. `.github/workflows/ci.yml`
- [x] `Dockerfile.agent` (multi-stage CUDA devel→runtime) + `Dockerfile.agent.dockerignore` — the one containerizable lane.
- [~] Cross-architecture verification: Apple↔NVIDIA never compared now (distinct class families). Remaining: within-NVIDIA arch grouping + a `prove-cuda.sh` — verify on RunPod.
- [ ] Per-poll VRAM throttle + real GPU telemetry both lanes (replace host-RAM throttle / `gpu_pct: 0.0`). `agent/src/main.rs`, `config.rs` — verify on RunPod.

### Wave 3 — "The product has a face" (revenue gate; ~3–5 weeks)
- [ ] Merge `web/skeleton.html` (real wiring) + `web/demo.html` (real design) into one product. Delete the empty `web/online`.
- [ ] Wire the demo's **mocked** billing/GitHub/intake flows to the **real** endpoints that already exist (`/v1/billing/*`, `/v1/connect/github`, `/v1/intake`, `/v1/deliver`)
- [ ] Real auth/login + account/billing UI. Decide: keep vanilla (BLACKHOLE) or adopt a minimal framework — **recommend a thin SvelteKit/Astro layer**, isolated, no bloat
- [ ] Sign + notarize the macOS app (needs Apple Developer ID — external)

### Wave 4 — "Defensible & observable" (scale gate)
- [ ] Verification 2.0: reputation-weighted check rates, mandatory redundancy coverage, penalize unresolved mismatches
- [ ] Ledger↔Stripe reconciliation job + payout retry/alerting
- [ ] Webhook HMAC signing + SSRF guard. `workers.go`
- [ ] `proto/` drift: regenerate types from schema (or CI-validate); add the 3 missing job types + result docs
- [ ] OS sandbox for the agent (seccomp/Landlock on Linux, sandbox-profile on macOS) — defense-in-depth even though jobs carry no code
- [ ] Observability: Prometheus is wired; add Grafana, alerting, tracing on the claim/merge/bill path

### Wave 5 — "Singularity" (the overengineered dream)
- [ ] Plane B: real Thunderbolt-fabric Mac clusters as one `apple_silicon_cluster` worker (sum memory → frontier models on a closet of Mac Minis)
- [ ] Plane C "Exchange Brain": learned routing + dynamic pricing from the duration/memory telemetry already being collected
- [ ] Multi-region settlement, supplier futures/forward pricing, a real spot/priority/trusted tier market with order-book mechanics

---

## 5. The greedy money model (refined from the data)

- **Take rate:** the action plan's own math shows **10% take + 10% redundancy =
  negative margin**. The sustainable number is **15% take with a reputation-driven
  ~5% check rate → ~$0.18 gross margin per buyer dollar.** Start at **5–8% to buy
  liquidity**, ratchet to **15%** as reputation filters cut verification cost.
  **Recommendation: set `CX_PLATFORM_TAKE_PCT` to the launch number now** (your call
  — I'd seed at 8%).
- **Margin expansion levers:** reputation reduces check overhead; the NVIDIA lane
  adds volume to amortize fixed cost; the privacy tier and priority tier carry
  premium take; Autopilot upsells preflight/SLA.
- **Network effects:** more verified suppliers → more reliable output → more
  buyers → more job density → better routing/pricing data (Plane C) → cheaper
  output → more buyers. The flywheel is the asset.
- **Realistic ceiling (honest):** the docs peg an unfunded outcome at **$100M–$500M**.
  The dual-lane + settlement-layer framing is what raises that ceiling — it makes
  the story "the clearing house for batch AI," not "Salad with Macs."

---

## 6. Immediate next actions

1. **Wave-1 P0s** — I can start these now; they're well-scoped and gate real money.
2. **Pick the launch take rate** → set `CX_PLATFORM_TAKE_PCT` (I suggest 8%).
3. **Cloudflare hardening** — flip to proxied (DDoS/WAF + hide origin IP); needs a
   scoped CF API token (Zone:DNS:Edit) so Caddy can renew via DNS-01.
4. **Then the front end** — merge skeleton + demo, wire the real endpoints.

*Mass is conserved. Only volume collapses. Now we make it pull.*
