# The Speed Lane — graded against the frontier, then against the potential

*2026-07-06. Grades computexchange's inference-SPEED lane in two tiers, per the owner's ask:
first vs. the researched frontier (SPEED_LANE_RESEARCH.md), then vs. the beyond-frontier
potential (what a heterogeneous marketplace can do that no single-GPU vendor can). Same
receipts-not-hopes discipline as CREED_AND_PATH_TO_TEN.md: a rung is "reached" only when a
real measured artifact exists. Grounding: SPEED_LANE_CURRENT_STATE.md.*

**One-line verdict:** our per-node speed is mid-frontier and bandwidth-capped; our *distributed*
speed — the only thing that can beat an A100 on wall-clock and the only thing a vendor can't copy
— is **barely started**. The thesis lives or dies on Tier 2, and Tier 2 is wide open.

---

## TIER 1 — computexchange vs. the current frontier (per-node + known techniques)

Scale: 0 = absent, 10 = at or beyond published SOTA, on real hardware, proven.

| # | Dimension | Grade | Where we are | Frontier | Next rung |
|---|---|---|---|---|---|
| 1 | **Batched decode (Apple)** | **6** | 1.34× real / 1.67× best @ batch32, byte-exact | MLX 2.6–3.7× @16-way; bandwidth-capped for all | near-length bucketing landed → wire continuous-batch dispatch to lift real-traffic 1.34→~2× |
| 2 | **Continuous batching (wired to dispatch)** | **3** | scheduler + kernel PROVEN (churn-safe) but `HawkingRunner::run` still an honest boundary — **not driving real jobs** | vLLM/SGLang in-flight batching is the throughput lever | wire HawkingRunner to real dispatch through ModelPool (Week 6) — the single biggest per-node unlock we already 90% built |
| 3 | **Speculative decoding** | **0** | none | EAGLE/EAGLE-3 ~1.6–2× at batch-1, **lossless**, bandwidth-friendly on Apple | build a draft+verify path (EAGLE-style) — highest-leverage per-node LATENCY win, dead-on "speed not quality" |
| 4 | **FlashAttention / FlashDecoding** | **2** | multi-seq Metal KV kernel (real), but no flash-style tiling/softmax-fusion | FA3 1.6–1.8× on H100; FlashDecoding for long context | a flash-decoding-style Metal kernel for the decode attention (long-context wins) |
| 5 | **PagedAttention** | **3** | slot-strided KV (real), not paged | vLLM PagedAttention → higher batch density | page the KV cache to lift max batch before OOM |
| 6 | **CUDA speed kernels (FP8/Marlin/graphs)** | **1** | CUDA lane = inert vLLM seam (NotImplemented); VRAM-bw now advertised correctly (entry 85) | FP8 ~1.6×, CUDA graphs kill host tax, TensorRT-LLM SOTA | run the RunPod soak → light the vLLM lane with FP8 + graphs (owner-gated on GPU $) |
| 7 | **Host/scheduler tax** | **7** | claim path O(queue×fleet)→O(job) this session (~1.4s→13ms), LISTEN/NOTIFY, maintained counters | vLLM cut 62% host overhead via scheduler rewrite | keep driving dispatch overhead down; measure end-to-end host ms as a first-class metric |
| 8 | **Per-node benchmarking honesty** | **8** | real tok/s per node, mixed-vs-identical curves, byte-exact gates, sustained-vs-peak | many vendors quote peak-only | already strong; add spec-dec + long-context to the sweep |

**Tier 1 average ≈ 3.75.** Reading: the *measurement* and *host-path* discipline is genuinely
strong (7–8); the *decode-acceleration* techniques the frontier is built on — continuous-batch
dispatch, speculative decoding, flash/paged kernels — are 0–3. The good news: #2 is ~90% built
(proven, just not wired), and #3 (spec-dec) is greenfield with the highest latency payoff.

---

## TIER 2 — computexchange vs. the BEYOND-frontier potential (the marketplace moat)

These are the moves a single A100/H100 rental **structurally cannot make**. This is where the
"save the buyer TIME" thesis actually wins. Grades are vs. *what's possible for us*, not vs. a
vendor (a vendor scores 0 here by construction).

| # | Structural move | Grade | Where we are | The potential | Next rung |
|---|---|---|---|---|---|
| A | **Speed-optimal data-parallel batch fan-out** (50 Macs beat 1 A100) | **4** | chunk splitter + warm-routing + dispatch fairness exist; NOT node-speed-weighted, no straggler hedging, fixed chunking | node-speed-weighted shard sizing + straggler re-race + adaptive-N → provably beat 1 A100 on a big batch (break-even ~17 Macs; ~3× at 50) | build the speed-optimal scheduler: size each shard by the node's live tok/s, hedge the slowest, pick N to minimize wall-clock. **The core moat.** |
| B | **Wall-clock speed SLA / guaranteed-ETA quote** | **2** | quotes give a cost/ETA band from live supply; not a *guarantee*, not speed-routed | "your 10k-prompt batch in 4 min, guaranteed" — route-to-hit-the-deadline; a product no rental offers | firm-quote tier exists (entry 49) → extend it to a *time* SLA backed by the fan-out scheduler |
| C | **Distributed speculative decoding** | **0** | none | draft on a fast node, verify on a bigger node — a fleet-only latency play for single big jobs | after per-node spec-dec (Tier 1 #3), split draft/verify across nodes |
| D | **Phase-aware heterogeneous routing** (prefill→compute node, decode→bandwidth node) | **1** | hw_class filters exist; no prefill/decode disaggregation | Splitwise/DistServe show +54% throughput / −64% TTFT from exactly this; heterogeneous fleet is the ideal substrate | disaggregate prefill/decode across two matched nodes with a one-shot KV handoff (WAN-tolerant) |
| E | **Adaptive job-shape router** (1 node vs 200, latency vs throughput) | **3** | scheduler dispatches; doesn't *choose the topology* to minimize the buyer's wall-clock | turn "you never know what you'll get" into "we always pick the fastest shape for your job" | a planner that reads job shape (batch size, prompt lengths, model) → picks single-node / data-parallel-N / disaggregated |
| F | **Cross-job KV-prefix reuse across the fleet** | **1** | per-task prefix sharing exists on-node | shared system-prompt / few-shot prefixes cached fleet-wide → skip prefill for repeat prefixes | prefix-hash index → route repeat-prefix jobs to nodes that already hold that KV |

**Tier 2 average ≈ 1.8.** This is the real story: **the moat is almost entirely unbuilt.** But it
is unbuilt *code*, not blocked business — every one of A–F is implementable and verifiable from
the keyboard + the M3 Pro + a couple of real nodes, exactly like this session's work. A and B are
the fastest paths to a demonstrable, defensible "we beat an A100 on your batch, and we told you
exactly when it'd finish."

---

## The sequence (what to build, in order, and why)

1. **Wire continuous-batch dispatch** (Tier 1 #2) — 90% built, proven; lights up per-node
   throughput immediately. *Small–medium, high certainty.*
2. **Speed-optimal data-parallel fan-out** (Tier 2 A) — the moat; the thing that literally beats
   an A100 on a big batch. Node-speed-weighted shards + straggler hedging + adaptive-N.
   *Medium; the highest-value item in the whole plan.*
3. **Per-node speculative decoding** (Tier 1 #3) — the biggest single-machine latency win,
   lossless, Apple-bandwidth-friendly. *Medium–large; greenfield.*
4. **Wall-clock speed-SLA quote** (Tier 2 B) — productizes 1–2 into a guarantee no vendor offers.
   *Small–medium, once the fan-out scheduler exists.*
5. **Phase-aware routing + distributed spec-dec + prefix reuse** (Tier 2 C/D/F) — the exotic
   frontier, each a marketplace-scale version of a published technique. *Large; later waves.*
6. **CUDA lane FP8/graphs** (Tier 1 #6) — owner-gated on the RunPod soak; big datacenter-node win.

**The honest headline for the next loop:** per-node, we're a competent mid-frontier engine with
a hard Apple bandwidth ceiling — chasing per-node speed alone tops out fast. The *win condition*
is Tier 2: **the distributed, speed-optimal, deadline-guaranteed batch is the only thing that
both beats a single A100 on wall-clock AND can't be copied by anyone renting one.** Build the
fan-out scheduler and the speed SLA, and "you never know what you'll get" becomes "you always get
it faster, and we told you exactly when."

---

## Post-wave-1 addendum (2026-07-06 — measured results; grades NOT self-bumped)

Wave 1 landed and was independently re-verified the same day. Two measured facts this grading
must carry (the table rows above are left as graded; the re-audit moves numbers):

- **Tier 1 #2 (continuous batching wired to dispatch): wired, but the "single biggest per-node
  unlock" framing is REFUTED at dispatch level.** `HawkingRunner::run` is real, opt-in, and
  byte-identical to the Candle runner on the gate set — but measured **0.67x vs candle batched**
  (88.3 vs 132.1 tok/s, M3 Pro, mixed 24-prompt regime). The lane's original 5.0x was aggregate
  vs SINGLE-STREAM in the upstream engine; nothing ever measured it against candle's per-task
  BATCHED path until now. Levers if it is to win: bulk prefill, PrefixIndex reuse, true
  cross-task continuous arrival. Receipts: CREED entry 86,
  docs/batching-efficiency-reports/2026-07-06-m3pro-hawking-dispatch.md.
- **Tier 2 A (speed-optimal fan-out): the planner + endgame racing EXIST now.** Rate-weighted
  divisible-load planner + adaptive-N + endgame straggler racing + speed-ordered peers, measured
  **2.7x wall-clock cut on the real control plane** (scheduling layer, fake-GPU workers) and a
  MODELED fleet-vs-A100 curve (break-even 18 Macs, 2.95x at 50) calibrated on real rates. L3
  (real multi-node vs real A100) remains owner-run — the runbook is
  docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md §6. Receipts: CREED entry 87.
