# The Speed/CUDA/Fan-out frontier — interpretation, a critical audit (3.5/10), the
# multi-node proof plan, the road to 10, and a handoff that DEMANDS a second audit

*2026-07-06, written after the first real A100 measurements. The owner asked for this to
be brutally critical — "we want to be the best." So the tone here is adversarial toward
our own work on purpose. Nothing is graded generously; every soft spot is named. This is
audit #1. The handoff (Part 6) exists to force audit #2 next session — a genuine DOUBLE
audit, each independent, so nothing self-congratulatory survives.*

---

## Part 1 — What the real A100 numbers tell us about our CUDA process

We rented a real A100-SXM4-80GB and measured it honestly (vLLM, `docs/speed-lane-reports/
A100_REFERENCE_MEASURED.md` + `A100_CAPABILITY_SWEEP.md`). Four things it exposed about
*our* CUDA process — none of them flattering:

1. **Our own CUDA path leaves ~95% of the GPU on the floor.** The A100 number baked into
   our modeled fan-out curve (2,345 tok/s) was *our Candle batched bench at batch 64*. A
   real A100 running vLLM at saturation does **44,269 tok/s** — ~19× more. On the SAME
   hardware. Our Candle CUDA lane is not a serious way to serve a datacenter GPU. **The
   product implication is decisive: on CUDA we must BROKER vLLM/TensorRT-LLM, not run our
   own kernel.** The `VllmRunner` seam (agent/src/runners.rs) is the correct architecture;
   it is just not lit.

2. **We had been benchmarking against a strawman of ourselves.** Every "the fleet is
   competitive with an A100" statement rested on our own weak A100 number, never a real
   vLLM A100. That is a methodology failure the reference caught. Rule going forward,
   non-negotiable: **when we claim to beat or match X, X must be measured at ITS best**
   (vLLM/TRT-LLM on the GPU), never at our implementation of X.

3. **All the A100 throughput lives in batching, and it saturates late.** The sweep: ~110×
   from batch=1 to the ceiling for ≤14B, saturating only around batch 512. This tells us
   the CUDA lane's value is *exclusively* high-throughput batch work — and that our own
   continuous-batching instinct (Hawking on Apple) is directionally right but pointless to
   rebuild on CUDA, where vLLM already does it far better.

4. **CUDA is a distinct, non-byte-deterministic verification class.** vLLM's reduction
   order is not bit-stable across runs/SKUs (our own `docs/VLLM_LANE.md` says so). So a
   CUDA supplier can never be byte-compared to an Apple supplier — different class,
   tolerant comparison. Our engine-tag + build_hash system already models this; the
   `runpod-vllm-soak.sh` soak (still unrun) is what characterizes how stable the pin is
   before CUDA carries byte-exact money work.

**Net interpretation:** our CUDA "process" today is a correct architectural seam sitting
on top of an inadequate default engine, benchmarked against a strawman, and unlit. The
A100 numbers don't just refute the fan-out headline — they tell us our CUDA serving story
has to be "we route to vLLM on the supplier's GPU," and our job is the *routing and the
verification*, not the kernel.

---

## Part 2 — The critical audit: where this frontier really is, 1–10

**Frontier definition (what 10 means):** computexchange is the easiest AND fastest way to
run a batch inference job — one click, the system reads the job's shape, quotes a
*guaranteed* wall-clock, auto-routes to the optimal substrate (fleet for latency/low-batch,
a lit GPU lane for throughput), delivers, and hands back a receipt proving it hit the SLA
and beat the do-it-yourself alternative on the axis that mattered — every lane
verification-gated. Grade against THAT, not against "we have some scheduling code."

| # | Dimension | Grade | Honest reason (critical) |
|---|---|---|---|
| 1 | **CUDA throughput lane (lit, serving)** | **2** | `VllmRunner` is a seam carrying zero traffic; our Candle CUDA default is ~19× below vLLM. We can't actually serve a throughput buyer on a GPU today. |
| 2 | **Apple per-node speed** | **5** | Competent, bandwidth-capped. Hawking continuous-batch wired but measured **0.67×** vs our own Candle batched — a real loss, not a win. |
| 3 | **Fan-out scheduler (the moat)** | **4** | Planner + endgame racing landed, measured 2.7× — but ONLY on fake-GPU workers on one box. Zero real distinct-node runs. |
| 4 | **Substrate-routing intelligence** | **2** | We now *know* the routing rule (fleet<→GPU crossover ~batch 8–64) but the planner does not READ job shape and choose a substrate. Insight exists; code doesn't act on it. |
| 5 | **Wall-clock SLA / one-click UX** | **3** | SLA quote + refund mechanism is built, but backed by the REFUTED throughput model and never recalibrated to real A100 competition. No actual one-click buyer path. |
| 6 | **Multi-node proof on real hardware** | **1** | Nothing. One M3 Pro can't host distinct nodes; the 2-RunPod / 2nd-Mac plan (Part 3) is unstarted. |
| 7 | **Measurement / methodology honesty** | **8** | Genuinely strong: measured the A100 at its best, refuted our own optimistic model, cleaned up, logged it. The one thing clearly at frontier. |
| 8 | **Verification across substrates** | **6** | Engine-tag/build_hash classes are real and the Apple-hawking honeypot is seeded (entry 89); CUDA class is modeled but unproven (soak unrun). |

**Overall: ~3.5 / 10.** Deliberately harsh, as asked. The average of the rows is ~3.9, but
I'm docking to 3.5 because the two dimensions that MOST define the frontier — a lit CUDA
lane (#1) and substrate-routing (#4) — are the weakest, and because the headline thesis
("dozens of Macs beat an A100 on wall-clock") was **refuted** this session. That refutation
is *progress* (we now know the truth and have a defensible re-scoped position), but it
lowers the ceiling of what we can currently claim, and honesty demands the number reflect
that. What keeps it from being a 2: the measurement discipline (#7) is real, the scheduler
(#3) is real code with a real number, and we now have the exact routing rule to build #4 on.

**The single most important critique:** we have been building the MOAT (fan-out scheduling)
and the KERNEL (Hawking) while the actual product — *read the job, pick the right
substrate, quote it, deliver it in one click* — barely exists. The A100 sweep just handed
us the routing rule on a plate. The gap between "we know the rule" (#4 grade 2) and "the
planner applies it" is where the next real points are.

---

## Part 3 — The multi-node proof plan (start planning now, per the owner)

The fan-out scheduler (entry 87) is proven only on fake-GPU workers on one box. To promote
it to real hardware we need real DISTINCT nodes. Two independent tracks, neither blocking
the other:

### Track A — CUDA fan-out via TWO RunPods (doable NOW, no 2nd Mac needed)
**Yes, CUDA parallelizes exactly like Apple Silicon** for our workload, because the pattern
is *data-parallel* (split independent prompts; each node runs the whole model; only results
transfer — zero per-token network dependency). Model-parallel (splitting one model's layers
across WAN boxes) does NOT work and was never our pattern. So:

- Open **2 RunPods** (cheapest GPUs that run vLLM cleanly — do NOT use 2×A100; each A100
  finishes a small batch so fast that scheduling overhead dominates and the fan-out signal
  vanishes. Use 2× a modest GPU, e.g. A10/L4/4090-class, so each node's rate sits in a
  regime where the scheduler's shard-sizing + endgame-racing are actually exercised. Or use
  a big enough batch that even 2×A100 stays busy long enough to measure).
- Register both as real workers against a control plane (local via `prove-local.sh` Phase 3,
  or the LAN control plane), each reporting real vLLM tps.
- Submit ONE batch; prove the planner splits it across two REAL distinct machines, that
  endgame racing fires across them, and that measured wall-clock beats single-node.
- This is the **L2→L3 bridge on real hardware** — it proves the *scheduler* on distinct
  nodes without waiting for the Mac. It does NOT prove "beat an A100" (2 rented GPUs isn't
  the thesis); it proves the distributed machinery is real.
- Budget/ops: reuse the money-safety pattern in `scripts/runpod-all-cuda.sh` (watchdog +
  auto-teardown). Two pods bill simultaneously — cap accordingly.

### Track B — Apple fan-out via the 2nd Apple Silicon device (when it arrives)
- Register both Macs as workers; submit the same batch; measure real Apple-lane fleet
  wall-clock with the planner ON vs OFF (`CX_DISABLE_FANOUT_PLANNER`).
- This is the real fleet substrate — the numbers that back the RE-SCOPED thesis (fleet wins
  on latency/low-batch, per the sweep's routing rule), not the refuted throughput headline.

### The honest framing for both
Neither track resurrects "beat an A100 on a big batch." Track A proves the scheduler is
real on distinct nodes; Track B proves the Apple fleet's real competitive lane. The product
claim they support is the ROUTING one: *we send your job to the substrate that runs it
fastest, and tell you exactly when it'll finish.*

---

## Part 4 — What happens when we hit 10

At 10, on this frontier specifically:

- A buyer pastes a batch (or points at a bucket) and **clicks once**. No infra choices.
- The planner **reads the job shape** (prompt count, model, latency sensitivity, deadline)
  and instantly returns a **guaranteed wall-clock** with a price — and a plain-English
  "we're running this on the fleet / on a GPU because <reason>."
- It **auto-routes to the optimal substrate**: latency-sensitive / low-concurrency → the
  Apple fleet (where a few nodes match an A100, proven by Track B); big throughput batch →
  a lit vLLM GPU-supplier lane (Track A machinery), or an honest "rent a GPU, here's the
  one-click path" if no GPU supplier is online.
- It **delivers and proves it**: a receipt showing the SLA was met and that this beat the
  DIY alternative (rent-it-yourself) on the axis that mattered for THIS job.
- Every lane is **verification-gated** (honeypots + redundancy per class), so the buyer
  trusts the result without knowing or caring which stranger's machine ran it.
- The whole thing is one API call / one button. "You never know what you'll get" has become
  "you always get it at the optimal speed for your job, guaranteed, at one click."

Concretely, 10 requires closing the audit's weak rows: lit CUDA lane (#1: 2→9),
substrate-routing wired into the planner (#4: 2→9), real multi-node proof (#6: 1→8),
one-click SLA UX recalibrated to real competition (#5: 3→9).

---

## Part 5 — Frontiers BEYOND 10 (where this expands next)

Once the routing marketplace is real, the frontier keeps going — these are the expansions
that matter for "the easiest way to run compute, at the click of a button":

1. **Cross-substrate disaggregation** — prefill on a GPU (compute-bound), decode on the
   fleet (bandwidth-bound), one KV handoff. Splitwise/DistServe showed +54% throughput /
   −64% TTFT from exactly this; a heterogeneous marketplace is its natural home.
2. **Distributed speculative decoding** — cheap draft on a fast fleet node, verify on a GPU;
   a latency play only a fleet can run (per-node spec-dec was measured net-negative on Mac,
   but the *distributed* version is a different animal).
3. **Fleet-wide KV-prefix cache** — shared system prompts / few-shot prefixes served warm
   from whichever node already holds them; skip prefill for repeat prefixes across the fleet.
4. **Elastic auto-scaling to an SLA** — the marketplace spins fleet capacity up/down to HIT
   a promised deadline, turning the SLA into a control loop, not an estimate.
5. **Beyond inference** — fine-tuning / LoRA jobs, embeddings-at-scale, and multimodal
   (whisper/vision) as first-class one-click job types.
6. **A compute-futures layer** — reserve capacity ahead of time, price discovery, a spot vs
   reserved market. The "NASDAQ of idle compute."
7. **The one-line SDK** — the literal click-of-a-button / one API call that hides every bit
   of the above. This is the ultimate product surface and the thing the owner keeps naming:
   the easiest way to run this, period.

---

## Part 6 — HANDOFF: your first job next session is a SECOND, independent audit

**Read this, then do NOT trust Part 2. Re-audit it adversarially and even more critically.**
The owner explicitly wants a DOUBLE audit — two independent critical passes so nothing
self-serving survives. You are audit #2. Your mandate:

1. **Re-grade the frontier yourself, 1–10, per dimension, from the evidence — not from my
   3.5.** Pull the real artifacts (`docs/speed-lane-reports/A100_REFERENCE_MEASURED.md`,
   `A100_CAPABILITY_SWEEP.md`, `FANOUT_PLANNER_WAVE1B.md`, CREED entries 86–91) and the code
   (`agent/src/runners.rs` VllmRunner/HawkingRunner, `control/planner.go`, `control/quote.go`
   SLA). Verify each of my grades against what the code/measurements actually show. Where I
   was too generous OR too harsh, say so with evidence and move the number.
2. **Hunt for what audit #1 MISSED.** Candidates I may have under-weighted: is the SLA quote
   now actively DANGEROUS (it guarantees a wall-clock backed by a refuted model — could it
   promise a buyer a deadline we can't hit and auto-refund into a loss)? Is the planner's
   live chunk-sizing (`adaptiveSplitSizeLive`) making bad splits now that we know the real
   rate curve is non-linear in batch? Does anything in the tree still cite the dead 2345 /
   "18 Macs" numbers as if live? Grep for stale claims.
3. **Adversarially verify the one thing that would most embarrass us** — that we'd promise a
   buyer "beat an A100" anywhere in the product/site/quote path. If a buyer could be quoted a
   guarantee we now know is false, that's a P0 finding, not a grade.
4. **Then, and only then, plan the work** to move the weak rows — but sequence it so the
   PRODUCT (read-job → pick-substrate → quote → deliver, one click) advances, not just more
   scheduling internals. The critique from audit #1 is that we build moat/kernel while the
   product barely exists; audit #2 should confirm or refute that and act on it.
5. **Multi-node:** if a RunPod key is available, execute Track A (2 modest GPUs) to promote
   the fan-out scheduler to real-distinct-node proof; otherwise scope it and wait for the
   owner. The scripts (`runpod-all-cuda.sh`, `runpod-a100-reference.sh`) and the money-safety
   pattern are ready.

Be harder on us than I was. The goal is to be the best, and you can't get there by grading
your predecessor kindly. Two independent critical audits, then build toward the one-click
routing product that hits 10.

---

## Part 7 — AUDIT #2 (2026-07-06, the second independent pass — executed)

*Fresh session, fresh eyes, mandate from Part 6 executed in full: every grade re-derived
from the artifacts and the code, the P0 adversarially verified against the LIVE product
surface, and audit #1's own reasoning attacked. Where audit #1 was wrong it is named in
both directions.*

### 7.1 The P0 verification — verdict: NO P0, with receipts

The one thing that would embarrass us — a buyer promised "beat an A100" or a wall-clock
backed by the refuted model — **does not exist on any live surface**:

- **Production site** (https://computexchange.net, fetched live this session, HTTP 200,
  19 KB): zero occurrences of "A100", "batched", "tok/s", or any speed comparison. The
  deployed page makes no throughput claim at all.
- **Built-not-yet-deployed site** (`web/index.html:357`): one claim — "batched decode
  about 1.5x on an m3 pro, up to 9.6x on an a100" — a batching MULTIPLIER of our own
  backend (serial→batched), really measured (`docs/GPU_CAPABILITY.md`), not an A100-vs-fleet
  comparison and not a wall-clock promise. Honest as written; ledger wording tightened
  (see 7.3).
- **The quote path is provably independent of the refuted model.** The speed-SLA
  guarantee is built ONLY from the fleet's own measured per-worker rates: planner
  conservative band (rates × 0.75, `control/planner.go`) × 1.25 + 60s
  (`control/quote.go` deriveQuoteSLA), gated on ≥5 eligible workers AND ≥3 real measured
  rates AND planner enabled AND premium > $0 — otherwise no offer, advisory only. The
  2345/44269 A100 numbers appear NOWHERE in the quote/SLA computation. Audit #1's fear
  ("the SLA guarantees a wall-clock backed by the REFUTED throughput model") was
  **wrong**: the refuted model was the fleet-vs-A100 *comparison curve*; the SLA never
  consumed it.
- **A miss cannot refund into a loss.** The remedy is exactly the 15% premium,
  `min(premium, chargeable)`, netted off the bill, floored at $0, exactly-once by
  partial unique index (CREED entry 88's forced-miss integration proof). Bounded by
  construction.

### 7.2 The re-grade — each dimension from evidence, not from audit #1's 3.5

| # | Dimension | #1 | **#2** | Evidence (receipts) |
|---|---|---|---|---|
| 1 | CUDA throughput lane | 2 | **2** | `VllmRunner` stubbed behind CX_VLLM_BASE_URL + CX_VLLM_SOAK_MODE, returns NotImplemented (`agent/src/runners.rs:4228-4264`); soak never run (`.artifacts/vllm-soak/` empty); job-type mapping typed and real → not a 1. |
| 2 | Apple per-node speed | 5 | **5** | 139 tok/s real-traffic M3 Pro; Hawking dispatch 0.67× Candle batched, 88.3 vs 132.1 tok/s (`runners.rs:3826-3834`, report 2026-07-06); batched ceiling 1.52×. |
| 3 | Fan-out scheduler | 4 | **4** | Planner pure math + L2 real-control-plane 2.7× (entry 87); endgame racing live on a 5s ticker (`control/workers.go:342`), planner-gated. Zero real distinct nodes. |
| 4 | Substrate-routing intelligence | 2 | **2** | Verified ABSENT: no substrate/crossover/job-shape routing anywhere in `control/` (adversarial search this session). The measured rule exists only in `A100_CAPABILITY_SWEEP.md`. |
| 5 | Wall-clock SLA / one-click UX | 3 | **3.5** | UP: the "dangerous SLA" premise was false (7.1) — the mechanism is honest, planner-backed, exactly-once, loss-bounded, L2-proven (entry 88). Still no one-click buyer path; calibration vs real fleet variance owner-gated. |
| 6 | Multi-node proof | 1 | **1** | Nothing on real distinct nodes. RUNPOD_API_KEY absent this session → Track A owner-gated (scripts ready: `runpod-all-cuda.sh` watchdog + teardown verified). |
| 7 | Measurement / methodology honesty | 8 | **7** | DOWN: live-stale residue survives in the tree — `control/planner_test.go:290` still asserts a curve calibrated on "A100 = 2345 tok/s // rented A100 spike, measured" (the exact strawman audit #1 banned) in CI; `docs/SITE-CLAIMS.md:292` attributes 2345 to "a RunPod A100 80GB" with no engine caveat; CREED scorecard prose (~751, ~918) presents 2345 as the A100; 4 research docs (`SPEED_LANE_HANDOFF/GOAL_PROMPT/GRADING/CURRENT_STATE.md`) still carry "beat an A100" as the live thesis with no superseded banner. A hostile grep finds all of these. |
| 8 | Verification across substrates | 6 | **6** | Class machinery enforced (`control/verification.go` classKey/byteHoneypotComparable); hawking byte-exact honeypot seeded from a real engine capture with membership-stability machine-proof (entry 89, `control/seed.go`); CUDA class modeled, unseeded, soak unrun; candle byte-exact honeypot missing; injection-time param/model guard still open. |

**Overall (product-weighted, rows 1/4/5 ×2): ≈ 3.5/10 — audit #1's headline CONFIRMED,
but two of its row judgments were wrong in opposite directions** (it invented a danger in
#5 and overlooked residue in #7). The central critique — moat/kernel built while the
read-job→pick-substrate→quote→deliver product barely exists — **CONFIRMED** by the
verified absence of any routing code (#4) and the gated seam (#1).

### 7.3 What audit #1 missed (the hunt, findings M1–M4)

- **M1 (audit #1 wrong, good direction):** the SLA-danger hypothesis is false — see 7.1.
  Audit #1 raised the scare without checking the code; the check took one file read.
- **M2 (audit #1 wrong, bad direction):** `control/planner_test.go` keeps the refuted
  strawman ALIVE IN CI — a green test asserting break-even 15–19 Macs from
  `a100TokPerS = 2345.0 // rented A100 spike, measured`. Rule #2 of Part 1 ("X must be
  measured at ITS best") is violated by our own test suite. Fix: relabel the constant as
  what it is (our Candle bench) AND add a companion curve test calibrated on the REAL
  vLLM 44,269 reference asserting the honest break-even (~318 M3-Pro-class).
- **M3 (structural, nobody flagged it):** `PlannerWorker.ItemsPerSec` is a SCALAR, and
  the entire planner/SLA stack assumes rate is linear in assigned items. The sweep just
  proved GPU throughput is ~110× NON-linear in batch. Harmless today (Apple-only fleet,
  1.5× max batching effect) — but the moment a vLLM worker registers a cached tps, chunk
  sizing, ETA, and the SLA band are garbage for it. **Lighting the CUDA lane (#1) without
  a rate-vs-batch model poisons #3 and #5.** This constraint must ride along with any
  dimension-1 work.
- **M4 (process hazard):** `docs/research/SPEED_LANE_HANDOFF.md` — still named as the
  session entry point in older notes — opens with "beats an A100 AND can't be copied.
  The moat is wide open." A fresh agent pointed there resurrects the refuted thesis.
  Superseded banners are required on all four pre-refutation research docs.

### 7.4 The amended plan (sequence confirmed, one amendment)

The Part 6 / ROAD_TO_TEN backlog order stands: (1) purge + ledger hygiene, (2) wire
substrate routing into the quote (highest product leverage), (3) light the CUDA lane by
brokering vLLM — **amended to include a rate-vs-batch worker model (M3) as a
precondition**, (4) Track A two-pod proof (owner-gated on RUNPOD_API_KEY), (5) one-click
buyer path, (6) Track B (owner-gated on the Studio). Grades move only via the next
adversarial re-audit.

---

## Part 8 — Iteration 1 RE-AUDIT (2026-07-06, grades move only here)

*After the iteration-1 wave (climb A: purge the stale claims; climb B: wire substrate
routing into the quote), a fresh adversarial agent re-graded the two touched dimensions
from evidence — not from the implementers' self-reports. This is the pass that moves the
grades. Its findings, verbatim in substance:*

### Grade movements (evidence-based, adversarial)

- **Dimension 4 (substrate-routing intelligence): 2 → 5.** `DecideSubstrate`
  (`control/routing.go:208`) genuinely reads job shape and branches on the measured
  crossover; the GPU competition curve is transcribed EXACTLY (all 10 sweep points
  verified, e.g. 1b@8=2954, 7b@64=5355); every GPU number is `[MODELED]` and conservative
  (linear-between-concave overstates the GPU; provisioning excluded); wired live in
  `buildQuote` and persisted in `quote_json`. Honest ceiling is **5, not higher**: it is
  ADVISORY-only — no dispatch path acts on the decision, and `litGPULaneWorkers` is
  `const 0`, so `gpu_lane` is unreachable and every GPU decision resolves to
  `gpu_recommend` (a suggestion the fleet then still runs). Reads + picks + explains
  against the measured curve = real movement from 2; not an executor = not yet 7+.
- **Dimension 5 (SLA/one-click): 3 → 3.5** (carried from audit #2; the "dangerous SLA"
  premise stays disproven — the re-audit re-confirmed the SLA is built only from the
  fleet's measured band, refunds bounded, no A100 number in the path).
- **Dimension 7 (methodology honesty): stays 7.** The re-audit confirmed the purge was
  thorough (renamed constant with a hard-floored companion test asserting break-even 325
  in [312,338]; five SUPERSEDED banners; correction boxes in the internal docs) — an 8 IF
  the tree were clean — but it CAPPED the grade at 7 on one surviving live-stale claim it
  caught that this session's own triage had waved through: `web/index.html:357`
  ("up to 9.6x on an a100", uncaveated, buyer-facing), plus an overclaim in CREED entry 92
  ("fixed every instance" / "zero speed claims") that conflated the deployed binary with
  the repo tree. **Both were then FIXED in a re-audit follow-up** (the line reworded to
  "our candle backend's batched-vs-serial decode …", the ledger reconciled, entry 92
  corrected). The uplift 7 → 8 is DEFERRED to the next adversarial pass — the fix was made
  by the working session, and the discipline forbids self-awarding a grade for one's own
  fix. Next re-audit on a clean tree should confirm 8.

### P0 status: CLEAR (re-confirmed)

No buyer-facing surface promises "beat an A100" or a wall-clock backed by the refuted
model. The `web/index.html` residue was marketing ambiguity (a batching multiplier), never
a quotable guarantee — now reworded regardless. The deployed production site carries zero
speed claims.

### Process-health check: PASS on all three

- Every climb has a real committed measured artifact (`SUBSTRATE_ROUTING_WAVE3.md` on real
  PG+MinIO re-run on two stacks; this audit doc Part 7).
- The product advanced (dimension 4 is a product row — `POST /v1/quote` now returns a
  routing block a buyer sees), not just internals.
- The blocker ledger is honest (quote-side-only, the scalar-rate model, the flaky
  `TestAdversarialGameabilityBounds` — all named, none hidden).

### The single highest-leverage next move (per the re-audit)

Light the CUDA/vLLM lane so `litGPULaneWorkers > 0` turns `gpu_recommend` into a real
`gpu_lane` route — the one thing between dimension 4's **5** and a **7+**, and the same
move that lifts dimension 1 (CUDA throughput lane, still **2**). It is owner-gated on a
`RUNPOD_API_KEY` for the determinism soak (`scripts/runpod-vllm-soak.sh`, unrun).

### Current honest grade (post-iteration-1, re-audited)

| # | Dimension | audit #2 | **iter-1 re-audit** |
|---|---|---|---|
| 1 | CUDA throughput lane | 2 | 2 |
| 2 | Apple per-node speed | 5 | 5 |
| 3 | Fan-out scheduler | 4 | 4 |
| 4 | **Substrate-routing** | 2 | **5** |
| 5 | Wall-clock SLA / one-click | 3.5 | 3.5 |
| 6 | Multi-node proof | 1 | 1 |
| 7 | Measurement honesty | 7 | 7 (→8 next pass; cap fixed) |
| 8 | Verification across substrates | 6 | 6 |

**Overall ≈ 3.9/10** (product-weighted; up from 3.5 — dimension 4, a double-weighted
product row, moved 2→5). The frontier moved honestly: the highest-leverage product gap
went from insight-in-a-report to a measured, tested, live routing decision.

---

## Part 9 — Iteration 2 RE-AUDIT + the honest exit read (2026-07-06)

*Climb 3 (routing surfaced on the job submission response, the persisted job row, the
ClearingReceipt, and a `routed` timeline event) was re-graded by a fresh adversarial agent
that reproduced every measured number on its own PG+MinIO stack (schema idempotent 2×,
3-rec→fleet/47s, 500-rec→gpu_recommend/182s, embed→NULL + zero routed events, eta
unchanged) and ran the tests green. Grades move here, not by self-report.*

### Grade movement (evidence-based)

- **Dimension 5 (wall-clock SLA / one-click UX): 3.5 → 4.5.** The product's honest half —
  "we ran it on X because Y" — now lands on the three artifacts a buyer actually receives
  (submit response `JobSubmitResponse.Routing`, `ClearingReceipt.Routing` via the pure
  `receiptRouting`, and the `routed` timeline event), behind the same generative+records>0
  honesty boundary, every GPU number `[MODELED]`, zero regression, reproduced number-for-
  number. **Held below 5** on three adversarial counts the re-audit confirmed: (a) still
  ADVISORY — `litGPULaneWorkers=0`, nothing in `scheduler.go`/`workers.go`/`pipeline.go`
  reads the substrate, every big-batch job says `gpu_recommend` but runs on the fleet;
  (b) no delivery loop that PROVES the SLA was met (metadata on responses, not a
  submit→deliver→proof cycle); (c) the SLA number is still fleet-band-only, not backed by
  real measured competition. New-honesty-risk check CLEAR: the `gpu_recommend` reason does
  not mislead, and the disclosed quote-vs-submit ETA mismatch is real, code-confirmed
  PRE-EXISTING (createJob never applied `sustainedBatchETASecs`), and honestly filed — not
  introduced by this climb (`routing.fleet_eta_secs == eta_secs` within every response).
- **Cleanups (post-re-audit, by the working session):** the re-audit found a dead inner
  `totalRecords > 0` re-check (removed) and the now-uncalled `estimateETASecs` (its doc
  corrected to stop implying `createJob` as a caller and to state it's a retained p50
  accessor). Build/vet/gofmt/unit re-verified clean.

### Current honest grade (post-iteration-2, re-audited)

| # | Dimension | audit #2 | iter-1 | **iter-2** |
|---|---|---|--:|--:|
| 1 | CUDA throughput lane | 2 | 2 | 2 |
| 2 | Apple per-node speed | 5 | 5 | 5 |
| 3 | Fan-out scheduler | 4 | 4 | 4 |
| 4 | **Substrate-routing** | 2 | 5 | 5 |
| 5 | **Wall-clock SLA / one-click** | 3.5 | 3.5 | **4.5** |
| 6 | Multi-node proof | 1 | 1 | 1 |
| 7 | Measurement honesty | 7 | 7 | 7 (→8 next pass; cap fixed) |
| 8 | Verification across substrates | 6 | 6 | 6 |

**Overall ≈ 4.2/10** (product-weighted; 3.5 → 3.9 → 4.2 across the two iterations — both
moved a product row, per the loop's own health test).

### The honest exit read — the code-doable product frontier is swept; the rest is owner-gated

Every remaining HIGH-leverage move on this frontier is owner-gated, confirmed by the
re-audit ("the single highest-leverage next move is owner-gated, not code-doable"):

- **Light the vLLM GPU lane** (flips `litGPULaneWorkers` > 0 so `gpu_recommend` becomes a
  real `gpu_lane` auto-route, AND lets the SLA be backed by measured competition instead of
  the fleet's own band) — the ONE change that unlocks dimensions **1** (2), **4** (5→7+),
  and **5** (4.5→…). Gated on a `RUNPOD_API_KEY` + `HF_TOKEN` for the determinism soak
  (`scripts/runpod-vllm-soak.sh`, unrun) — the money-safety scripts are staged and ready.
- **Real multi-node proof** (dimension 6, still 1) — Track A (2 RunPods) gated on the key;
  Track B (2nd Apple Silicon) gated on the Studio.
- **SLA calibration vs real fleet variance** (dimension 5's last mile) — gated on real
  distinct nodes.

Code-doable but LOWER-leverage (polish / internals, not product rows — deferred so the loop
doesn't "polish the moat while the button waits"): unify the quote-vs-submit ETA (filed),
the flaky `TestAdversarialGameabilityBounds` (filed), metrics counters (`cx_endgame_races`,
`cx_sla_misses`), and the injection-time honeypot param/model guard (dimension 8 — required
before production-scale byte-exact seeding, which itself needs more supply).

**Honest distance to 10:** the keyboard has taken this frontier from 3.5 to ~4.2 by lighting
the routing INTELLIGENCE and surfacing it to the buyer end-to-end. The next climb is not
more code — it is the owner putting a real GPU supplier online (a `RUNPOD_API_KEY`) so the
lane can be lit and byte-verified, at which point dimensions 1/4/5 all move together.

---

## Part 10 — vLLM-lane RE-AUDIT (2026-07-06 late session, real A100s; grades move only here)

*After the owner provided a RunPod key + GPUs: the within-`nvidia_*` byte-stability soak was
run on real A100s, the production `VllmRunner` exercised against a live pinned vLLM in
soak-mode, and `litGPULaneWorkers` wired from `const 0` to a live supply count (CREED 96). A
fresh adversarial agent re-graded dimension 1 from the evidence + reproduced the test.*

### Grade movement (evidence-based)

- **Dimension 1 (CUDA throughput lane): 2 → 3.** The rubric's 10 needs three things; the
  re-audit scored each: **(A) carries real byte-verified PRODUCTION traffic — NOT met**
  (`VllmRunner::run` is still double-gated behind `CX_VLLM_BASE_URL` + `CX_VLLM_SOAK_MODE`,
  `agent/src/runners.rs:4228,4252`; only an opt-in `#[ignore]`d soak-mode test drove it —
  the shell-out `vllm_completions` IS fully implemented, not a stub, but "soak-mode against a
  live server" ≠ production dispatch). **(B) soak-proven byte-stability — MOSTLY met** (real
  hashes: within-run `c930c65e…`, across-restart, cross-pod same-SKU corpus + golden
  `bd745e7a…`; honestly labeled same-SKU, not cross-SKU; serial-request caveat stated).
  **(C) CUDA verification class seeded — NOT met** (only the hawking/Apple class is seeded;
  the `(nvidia_*, vllm, build_hash)` class is unseeded — asserted tolerant-by-construction,
  not wired). One of three met, one gated, one absent → **+1, honestly 3, not higher.**
- **Dimension 4 stays 5, dimension 5 stays 4.5** — the live supply count makes `gpu_lane`
  reachable (re-audit reproduced the `gpu_recommend`→`gpu_lane` flip on a real vLLM-worker
  registration), but the block is still advisory and nothing dispatches on it.

### The one buyer-facing honesty gap it caught — FIXED

The `gpu_lane` reason said "the gpu lane is the faster substrate … eligible to run this job",
which — since the claim path (`scheduler.go`) filters on job/model/memory but NOT engine — a
buyer could read as "a GPU WILL run my job." It won't deterministically; a fleet worker can
claim it. **Fixed** (`control/routing.go`): the reason now states plainly "routing is advisory
today — the platform does not pin your job to the vllm lane, so it may still run on the fleet
at the quoted eta." CREED entry 96's "self-lighting" / "carries real traffic" / "supply half
is now real" overclaims were softened to match the code (soak-mode not production; a count of
self-declared vLLM workers, not verified supply; the tolerant class named as a remaining step).

### Current honest grade (post-vLLM-lane, re-audited)

| # | Dimension | iter-2 | **now** |
|---|---|--:|--:|
| 1 | **CUDA throughput lane** | 2 | **3** |
| 2 | Apple per-node speed | 5 | 5 |
| 3 | Fan-out scheduler | 4 | 4 |
| 4 | Substrate-routing | 5 | 5 |
| 5 | Wall-clock SLA / one-click | 4.5 | 4.5 |
| 6 | Multi-node proof | 1 | 1 |
| 7 | Measurement honesty | 7 | 7 (→8 next pass) |
| 8 | Verification across substrates | 6 | 6 |

**Overall ≈ 4.4/10** (3.5 → 3.9 → 4.2 → 4.4). Dimension 1 moved on real-hardware evidence.

### The single highest-leverage next step (owner-gated)

Stand up a pinned PRODUCTION vLLM supplier (a GPU running the agent with
`inference_backend=vllm`, `CX_VLLM_BASE_URL` + `CX_VLLM_SOAK_MODE=1`) and either (i) add an
`engine` predicate to the claim path so a `gpu_lane` job pins to vLLM supply, or (ii) seed the
tolerant `(nvidia_*, vllm, build_hash)` verification class. Both are code-doable NOW but only
matter with a real vLLM supplier online — i.e. owner GPU spend (the RunPod key + budget the
owner is weighing). That single step is what takes dimension 1 from 3 toward 8+.

---

*Standing constraints unchanged: never attribute AI in git; commit/deploy only when the
owner asks; measure the win on real hardware and label modeled numbers as modeled; a
fully-proven subset beats a rushed whole; don't self-bump scorecard grades — grades move
via the adversarial re-audit, which this handoff is explicitly commissioning.*
