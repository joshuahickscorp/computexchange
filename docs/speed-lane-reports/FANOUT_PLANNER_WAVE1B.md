# Speed Lane wave 1B — the speed-optimal data-parallel fan-out planner

*2026-07-06. Target item 2 of `docs/research/SPEED_LANE_GOAL_PROMPT.md` ("THE
MOAT"): minimize a batch job's buyer-visible WALL-CLOCK by exploiting per-node
measured rates, adaptive fan-out width, and endgame straggler racing.*

Every claim below is tagged with its proof layer, per the goal prompt's
three-layer split. **A lower layer never masquerades as a higher one:**

- **L1 (planner math + calibrated simulation)** — deterministic unit tests +
  a seeded simulation calibrated with this project's REAL measured rates. All
  L1 numbers are **MODELED**.
- **L2 (real control plane, measured)** — real Postgres + real MinIO + real
  HTTP submit→split→claim→commit→merge, concurrent fake workers with DECLARED
  rates. L2 numbers are **REAL MEASURED WALL-CLOCK of the scheduling plane**;
  the workers fake the GPU, so L2 proves scheduling, never tok/s.
- **L3 (real multi-node fleet vs a real A100)** — NOT run in this wave; the
  runbook is §6 below and it remains the honest boundary.

## 1. What landed (code)

| Piece | Where | What it does |
|---|---|---|
| Pure planner | `control/planner.go` `PlanFanout` | Divisible-load makespan minimization with per-worker start costs: completion(w) = chunkOverhead + coldLoad(if !warm) + items/rate. Water-fill via monotone bisection + largest-remainder integer rounding. Adaptive-N falls out of the math: a worker whose cold load + overhead ≥ the fleet's achievable finish time is EXCLUDED (the plan refuses to fan wider when that would raise wall-clock). Deterministic; outputs p50 + conservative band (rates degraded to 75%, grounded in the measured 91–111 tok/s serial spread + thermal sustained-vs-peak) + modeled comparison vs the best single node. |
| Fleet snapshot | `control/benchmark.go` `FleetRateSnapshot` | The planner's input: live eligible workers (same hard-filter axes as the claim path) with `worker_tps_cache` rate, warm bit (`worker_model_state`, 60s window), and the **measured** `benchmark_results.load_ms` (persisted since the warm-pool facet, never read at planning time before this wave; 120s documented default only when no measurement exists). |
| Live chunk sizing | `control/api.go` `adaptiveSplitSizeLive` | Chunk = targetTaskSecs of work for the MEDIAN live measured rate (tok/s ÷ modeled tokens/item), replacing the static `jobTypeThroughput` map when ≥3 live workers have real rates for the job type; **plus a width floor**: chunk count ≥ the planner's recommended width when the exact record count is known, so the fan-out width is actually achievable. Static map remains the honest fallback (thin cache, non-generative types, streamed inputs past the 1 MiB sample, DB error). |
| Planner ETA | `control/api.go` `estimateETASecs` → `plannerETASecs` | Same signature (quote.go untouched); when the fleet has ≥3 measured rates, the ETA is the planner's modeled makespan over HETEROGENEOUS relative rates (anchored at the drift-fed p90/static per-task time) **plus a cold-load term** — the two effects the old `ceil(queue/workers)×perTaskSecs` cannot represent. Falls back to the old formula otherwise. |
| Endgame racing | `control/workers.go` `raceEndgameTails` (+ `EndgameTailTasks`, store.go) | New 5s sweep: when a job has ZERO unclaimed tasks and ≥1 running task, duplicate the slowest running chunks (≥10s old) onto the FASTEST IDLE WARM same-class independent peer immediately, instead of waiting the 90s hedge. Reuses the hedge machinery verbatim (InsertHedgeTask pinning, per-chunk one-hedge guard, hedgeMaxInFlight cap, merge dedupe). Cold-model suppression respected on the straggler side (isColdModelStraggler) and enforced on the peer side (no cold-racing). |
| First-commit-wins both ways | `control/store.go` `CancelStragglerSiblings` | Pre-existing gap this wave's win depended on: when a hedge/race duplicate committed FIRST, the hedged ORIGINAL kept running and `JobAllTasksDone` still waited it out — nullifying the wall-clock point of duplicating. The original is now cancelled **only** by its own winning duplicate's commit (redundancy/tiebreak commits can never cancel a primary). |
| Speed-ordered peers | `control/planner.go` `rankPeersBySpeed`, applied in `scheduler.go` `SelectRedundancyPeerExcluding` + `SelectEndgameRacePeer` | Hedge/race/tiebreak dispatch now picks warm-first-then-fastest-tps among eligible same-class peers (Match's reputation-weighted order was a TRUST ordering; the residual tie-break keeps it). Fleets with no measured rates behave exactly as before (stable sort). |
| Kill switch / A-B | `control/planner.go` `fanoutPlannerEnabled` | `CX_DISABLE_FANOUT_PLANNER=1` reverts every wave path to pre-wave behavior — the L2 proof's A/B switch and the operator escape hatch. |
| Observability | structured log lines | `planner: split …`, `planner: eta …`, `workers: endgame race: …` — every planner decision logs fleet size, width, modeled p50/conservative band, single-node comparison, all tagged `[MODELED]`. Races also increment the existing `cx hedges` counter (an endgame race IS a hedge dispatch). A dedicated metrics counter is a follow-up (metrics.go was outside this bundle's file set). |

## 2. L1 — planner invariants (unit, deterministic; `control/planner_test.go`)

All green (`go test -run 'TestPlanFanout|TestRankPeers|TestTokensPerItem'`):

- Rate-weighted assignment strictly beats the uniform (fleet-average) split on
  modeled makespan for a heterogeneous fleet; the 4×-faster node gets ~4× the
  items.
- Adaptive-N: small job + one warm fast node + cold peers → width 1, all items
  on the warm node.
- The cold-load penalty flips the SAME fleet wide→narrow as the job shrinks
  (100k items → width 8; 60 items → width 1), and the wide plan beats the best
  single node on the big job (modeled).
- A worker whose start cost alone exceeds the fleet's achievable finish time is
  excluded even when items are plentiful (integer rounding cannot leak work
  onto excluded workers).
- Deterministic: identical fleet in any input order ⇒ byte-identical Plan.
- Conservation (Σ assigned = items), throttled/rate-less workers never planned
  onto, empty fleet/job ⇒ Width 0 (callers fall back).
- Conservative band ≥ p50; modeled speedup-vs-single-node populated.
- `rankPeersBySpeed`: warm beats faster-cold, tps orders warm peers, stable on
  ties, input not mutated.

## 3. L1 — the MODELED fleet-vs-A100 curve (calibrated simulation)

> **⚠️ REFUTED BY MEASUREMENT (2026-07-06). Do not cite this curve's break-even.** The
> A100 baseline below (2345 tok/s) was our own Candle batch-64 bench, not vLLM. A real
> A100 SXM running vLLM on the full 10k-prompt batch measured **44,269 tok/s — ~19× this
> assumption** (`A100_REFERENCE_MEASURED.md`, CREED entry 90). Real break-even is ~318
> M3-Pro nodes, not 18. The "dozens of Macs beat an A100 on wall-clock" headline does
> not survive a real vLLM A100 for small models. The curve is kept below only as the
> record of what the model predicted before it was tested against reality.

**Everything in this table is MODELED** — computed by `PlanFanout` in
`TestPlanFanoutModeledFleetVsA100Curve` (seed 42), calibrated with this
project's real measurements (docs/GPU_CAPABILITY.md / SPEED_LANE_CURRENT_STATE):
M3 Pro Llama-3.2-1B Q4_K_M **139 tok/s** real-traffic batched (serial spread
91–111 → ±10% seeded node jitter), rented A100 same model **2345 tok/s** @
batch 64. Job: 10,000 prompts × 256 completion tokens; both sides warm, same
2s dispatch overhead.

| N (M3-Pro-class Macs) | modeled fleet wall-clock | vs A100 (1093.7s modeled) |
|---:|---:|---:|
| 1 | 18899.1 s | 0.06× |
| 5 | 3872.2 s | 0.28× |
| 10 | 1885.9 s | 0.58× |
| 15 | 1255.7 s | 0.87× |
| **18 (break-even)** | **1038.5 s** | **1.05×** |
| 20 | 925.7 s | 1.18× |
| 30 | 624.3 s | 1.75× |
| 40 | 463.8 s | 2.36× |
| 50 | 370.5 s | 2.95× |

Modeled break-even under jitter: **18 Macs** (nominal rate ratio 2345/139 =
16.9 → 17; the seeded −10% draws push it to 18 — the test pins the band
[15,19]). Modeled 50-node margin: **2.95×** (test band [2.5, 3.4]). The test
also asserts the planner's makespan stays within 2% of the ideal
aggregate-rate bound at every N (no capacity left idle) and that the curve is
monotone.

**These are not measurements of real inference.** They are the planner's own
model evaluated at really-measured rates. The real-fleet demonstration is L3.

## 4. L2 — REAL measured control-plane wall-clock (`control/planner_integration_test.go`)

Stack: real Postgres 17 (native, port 55491, `initdb -U cx --auth=trust`,
LC_ALL=C) + real MinIO (port 19100) + the real control plane over HTTP
(httptest server from the shared integration TestMain). Fleet: three
concurrent fake workers on independent suppliers, REAL registration path
(benchmarks → `worker_tps_cache`, `load_ms`) and REAL heartbeats (warm model
state): fastA 200 tps / 0.5s per chunk, fastB 180 tps / 0.5s per chunk, slow
12 tps / **30s** per chunk. Job: 6-record `batch_infer` on the seeded 1B
model. A/B via `fanoutPlannerEnabled` on ONE identical harness.

Measured on the M3 Pro dev machine, 2026-07-06 (test log
`TestFanoutPlannerLiveSizingETAAndEndgameRace`):

| Act | planner OFF (pre-wave) | planner ON (this wave) |
|---|---|---|
| Adaptive split (6 generative records, no explicit split_size) | 1 chunk (static map: 4 items/s × 45s) | 2 chunks — the planner width floor makes fan-out achievable at all |
| Submit ETA | 45 s (blunt `ceil(queue/workers)×45s`) | 47 s (heterogeneous rates: the 12-tps node is modeled as nearly useless; two indivisible tasks land on the two fast nodes) |
| **End-to-end wall-clock, 6 pinned chunks, slow worker claims one** | **30.24 s** (job waits out the slow worker's whole 30s chunk; 90s hedge never fires) | **11.23 s** (endgame race fires at ~10.5s: minRun floor 10s + sweep; duplicate commits in ~0.7s; first-commit-wins cancels the straggler) |

**Measured result: 30.24s → 11.23s = 2.69× wall-clock cut** on the same job,
same fleet, same harness — attributable to the endgame race alone (chunk count
was pinned equal in both timed runs; sizing/ETA were proven separately in the
probes above). Also asserted by the test, not just observed:

- the race actually fired **through the real machinery** (a `hedged_from` task
  row exists on the raced job only; zero on the baseline job),
- it was pinned to the **fastest idle warm peer** (fastA, 200 tps — the
  `rankPeersBySpeed` contract),
- **first-commit-wins in both directions**: the duplicate committed
  (`complete`), the raced straggler was cancelled (`failed`),
- the **merge deduped** to exactly 6 result lines and the artifact is readable.

Honesty notes for L2: the sweep was driven at a 500ms test cadence (production
registers the same function on a 5s ticker; worst-case production adds ≤5s to
the raced number, still ≈2.2× on this shape). The measured 11.23s is dominated
by the deliberate `endgameRaceMinRun` 10s arming floor — the win GROWS with
straggler size (a real 3-minute straggler would be cut ~16×, but that number
is extrapolation, not measurement, so it is not claimed). Fake workers sleep
instead of inferring: this measures the scheduling plane, not GPUs.

## 5. Regression status

- Unit suite (`go test ./control`): **120 → 130 PASS, 0 FAIL** (10 new L1
  tests), before and after.
- Full integration suite (`go test -tags integration`, real PG+MinIO, PG
  :55491 / MinIO :19100): green before the change (exit 0, 25.5s); after:
  **241 PASS, 0 FAIL, 1 SKIP** (the pre-existing `CX_CLAIM_LOAD`-gated 100k
  load test), 65.2s, exit 0 — including the new L2 test. Two independent runs
  of the L2 measurement: 30.24s→11.23s and 30.22s→11.30s (stable ≈2.7×).
- `go build ./...`, `go vet ./...` (both tags): clean. `gofmt -l control/`
  (minus webauthn.go): empty.
- `db/schema.sql`: untouched (no new index needed — `FleetRateSnapshot` runs
  at submit/quote cadence, `EndgameTailTasks` every 5s prefiltered to
  running-with-empty-queue jobs; both bounded, neither on the claim hot path).
- `quote.go` untouched; `estimateETASecs`' signature unchanged (quote's p50
  transparently gains the heterogeneity/cold-load terms when the fleet has
  measured rates).

## 6. L3 runbook — real multi-node fleet vs a real A100 (owner-run; NOT done)

This is the remaining honest boundary: one M3 Pro cannot host independent
Metal nodes (they would share one bandwidth-bound GPU and prove nothing).
Mirrors `scripts/runpod-vllm-soak.sh`'s pattern:

1. **Reference side (rented A100 or H100, ~20-30 min) — SCRIPTED
   (`scripts/runpod-a100-reference.sh`, added 2026-07-06).** One command
   provisions the box, stands up pinned vLLM, runs the EXACT competitive batch
   through vLLM offline continuous batching, times it end-to-end on the pod,
   pulls back `T_ref` + aggregate tok/s, and tears the pod down on any exit:
   ```
   export RUNPOD_API_KEY=...            # RunPod → Settings → API Keys
   export HF_TOKEN=hf_...               # accept the Llama-3.2-1B license on HF first
   bash scripts/runpod-a100-reference.sh                       # A100, 10k×256
   GPU_TYPE="NVIDIA H100 80GB PCIe" bash scripts/runpod-a100-reference.sh   # H100
   ```
   **Two honest A100 baselines, don't conflate them:** (a) this script measures
   the A100/H100 at its STRONGEST — vLLM fp16, the engine a buyer actually rents
   an A100 to run — so "the fleet beats an A100" can't be dismissed as
   crippling the A100; (b) the modeled curve in §3 (2345 tok/s) is OUR Candle
   **Q4** bench (`scripts/runpod-spike.sh`), which sits on the same axis as the
   fleet's Q4 rate. The fleet (Q4 Macs) vs the A100 (fp16 vLLM) is the buyer's
   real choice; the fleet vs A100-Q4 is the internal-consistency check. Report
   both, labeled. Default batch is `IGNORE_EOS=1` → exactly 10k×256 fixed-work
   token-gens, the clean apples-to-apples vs the fleet.
2. **Fleet side (N real Macs, the more the better; ≥5 is already probative):**
   on each Mac: install the agent (`cx-agent`), point at the control plane
   (`compute.exchange` or a LAN control plane via `scripts/prove-local.sh`
   Phase 3 with SKIP_LIVE=0), let the startup benchmark report real tps +
   load_ms, heartbeat warm. Verify `worker_tps_cache` has one row per Mac
   (`SELECT worker_id, tps FROM worker_tps_cache WHERE job_type='batch_infer'`).
3. **Submit the SAME 10k-prompt job** via `POST /v1/jobs` (no explicit
   split_size — let `adaptiveSplitSizeLive` size it; check the `planner: split`
   log line for the width decision). Record wall-clock submit→`results_url`
   ready = T_fleet.
4. **A/B the wave itself on the fleet:** re-run once with
   `CX_DISABLE_FANOUT_PLANNER=1` on the control plane — the delta isolates
   this wave's contribution on real hardware (expect the endgame race to show
   up in the last chunks; watch for `workers: endgame race:` lines).
5. **Report:** T_fleet vs T_ref at the real N; compare against §3's modeled
   curve at that N (the model's honesty check). Break-even claim ("~18 Macs
   beat an A100") may only be promoted from MODELED to MEASURED by this run.

Pass criteria: T_fleet(planner ON) < T_fleet(planner OFF), and the measured
points sit within the modeled curve's conservative band. Anything else is a
finding to fix, not to explain away.

**Sequencing note (2026-07-06):** the reference side (step 1) needs only the
RunPod box and can run NOW; the fleet side (steps 2-4) needs the second Mac.
Running step 1 first banks `T_ref` (and, via `scripts/runpod-spike.sh`,
refreshes the CUDA-lane correctness gate + the 2345 figure) so that the moment
the Macs are online the comparison is one fleet-side run away.

## 7. Follow-ups noted (outside this bundle's file set)

- `metrics.go`: dedicated `cx_endgame_races` / planner-decision counters
  (currently: structured logs + the shared hedges counter + task rows).
- `quote.go` (wave 2 per the goal prompt): surface the planner's conservative
  band as the speed-SLA quote.
- Streamed inputs larger than the 1 MiB look-ahead sample get median-rate
  sizing but no width floor (record count unknown at split time) — a
  two-pass or manifest-count approach could close it.
- The quote-path `adaptiveSplitSize` call (quote.go:531) still uses the static
  map (quote.go untouched by design this wave), so a quote's task-count
  estimate can differ from the submit's live-sized reality; reconcile in wave 2.
