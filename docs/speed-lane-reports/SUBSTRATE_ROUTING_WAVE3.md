# Substrate-routing wired into the quote — the first real piece of the product (MEASURED)

*2026-07-06. Road-to-ten iteration 1, rubric dimension 4 ("the planner READS job shape
and picks fleet / GPU-lane / recommend"). Audit #2
(`docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md` Part 7) verified that NOTHING in
`control/` read a job's shape to choose a substrate — the measured routing rule existed
only in a report. This wave makes `POST /v1/quote` act on it. Landed alongside the
stale-claim purge (same wave); this report covers the routing climb.*

## What shipped

`control/routing.go` — a pure, deterministic decision (no DB, no clock, no randomness,
the same discipline as `planner.go`) that encodes the measured 2026-07-06 A100-SXM4-80GB
vLLM sweep as the GPU competition curve and reads a job's shape to pick a substrate:

- `gpuCompetitionCurve` transcribes the sweep's two classes our catalogue serves
  (`docs/speed-lane-reports/A100_CAPABILITY_SWEEP.md`, raw
  `artifacts/a100-sxm-capability-sweep-2026-07-06.jsonl`): 1b `{1:387, 8:2954, 64:19864,
  512:43570, 2048:44852}`, 7b `{1:100, 8:784, 64:5355, 512:11116, 2048:11310}` tok/s.
- `interpolatedAggTokS` — piecewise-linear between measured points, clamped at the
  batch-2048 ceiling (never extrapolated). Linear between concave points OVERSTATES the
  GPU — the honest direction when we're recommending the competition.
- `gpuModeledSecs` — `records × tokensPerItem / interpolatedAggTokS(min(records, 2048))`,
  labeled `[MODELED]` everywhere and EXCLUDING rental/provisioning/queue time, so the
  figure structurally favors the GPU (we understate our own case when pointing elsewhere).
- `DecideSubstrate` — the measured rule, grounded point by point in the sweep:
  - `records < 8` (below the measured crossover) → **fleet**, unconditionally: at batch 1
    a single A100 is ordinary (1–3 fleet nodes) and it's offline, the fleet is online.
  - `records 8..64` (the crossover band) → compare the fleet ETA against the modeled GPU
    wall-clock, preferring **fleet** on ties, on a non-planner-backed (blunt) ETA, and on
    a priority tier (latency is the fleet's measured lane).
  - `records > 64` → the GPU's batching advantage compounds → **gpu_lane** if a lit lane
    exists, else **gpu_recommend**, UNLESS the planner-backed fleet ETA actually models
    faster (then fleet, both numbers stated).
- `gpu_recommend` is a RECOMMENDATION, never a refusal: its reason names both numbers, the
  `[modeled]` label, that no lit GPU lane is online yet, and that the fleet still runs the
  job at the quoted ETA if submitted. `litGPULaneWorkers` is a package const 0 (the vLLM
  lane is gated behind its determinism soak, `agent/src/runners.rs`), passed as a
  PARAMETER so lighting the lane needs no signature change.

`control/quote.go` — minimal integration on the hot file: a `Routing *QuoteRouting`
field, attached in `buildQuote` only for generative jobs with `records > 0` (the sweep
measured generative decode only — the honesty boundary), using the quote's own
sustained-adjusted p50 + conservative band + `plannerBacked` from the existing
`etaBandSecs`. No change to SLA, pricing, or any existing field; the block persists with
the quote via `quote_json`.

## The measured proof (real infra, independently re-verified)

Real native Postgres 17 + real MinIO, `db/schema.sql` applied, real HTTP `POST /v1/quote`,
a real 5-worker fleet registered through the production register/heartbeat paths (feeding
`worker_tps_cache` at 200 tps so the ETA is the planner's real modeled makespan). Run by
the build agent (PG :55497 / MinIO :19700) AND independently re-run by the orchestrating
session on a fresh stack (PG :55499 / MinIO :19710) — identical results both times:

| job shape | substrate | fleet ETA (p50) | GPU modeled | why |
|---|---|--:|--:|---|
| 3-record `batch_infer` (1b) | **fleet** | 47 s | 0.70 s `[MODELED]` | below the measured crossover; GPU figure excludes provisioning, fleet is online |
| 500-record `batch_infer` (1b) | **gpu_recommend** | 217 s | 3.04 s `[MODELED]` | past the crossover, no lit lane; honest comparison, still runs on the fleet if submitted |
| 20-record `embed` | **(no routing block)** | — | — | non-generative shape; the sweep didn't measure it — honesty boundary |

The `gpu_recommend` block is persisted and read back from `quotes.quote_json->'routing'
->>'substrate'` = `gpu_recommend`. Every GPU number is `[MODELED]` from the measured sweep,
never presented as a measurement of the job. Note the 500-record GPU model (3.04 s) is
~70× the fleet's 217 s — that is the honest reveal of the measured data (an A100 at batch
500 is genuinely that much faster on raw compute); the reason states it plainly and still
declines to promise the fleet wins there.

## Gates (independently re-verified by the orchestrating session)

- `cd control && go build ./...` clean; `go vet ./...` clean; `gofmt -l .` lists only
  `webauthn.go` (the pre-existing baseline exception).
- Unit: `go test ./...` green; the new `routing_test.go` (interpolation exactness against
  all 10 measured points, monotonicity, the full decision matrix over
  `records × class × tier`, fleet-wins-below-crossover, quote-voice reasons, determinism)
  passes uncached.
- Integration: `TestQuoteSubstrateRouting` PASS on both stacks; **zero regression on the
  shared hot file `quote.go`** — `TestFanoutPlannerLiveSizingETAAndEndgameRace` (41.5 s),
  `TestSLAQuoteHonestDegradation`, `TestSLAQuoteFirmSubmitGuaranteeMet`,
  `TestSLAForcedMissRefundsExactlyOnce` all PASS.

## Honest scope / blockers (named, not hidden)

- **Quote-side only by design.** Nothing in the dispatch path acts on the decision yet —
  `gpu_recommend` has no lit lane to route to. This is the routing INTELLIGENCE surfaced
  to the buyer, not yet an auto-route executor. Lighting the CUDA lane (rubric #1) is what
  turns `gpu_recommend` into `gpu_lane`.
- **Scalar-rate caveat carried forward (audit #2 finding M3).** The planner/ETA the fleet
  side of this comparison rests on assumes rate is linear in assigned items; the sweep
  proved GPU throughput is ~110× non-linear in batch. Harmless while the fleet is
  Apple-only (≤1.5× batching), but a rate-vs-batch worker model is a PRECONDITION before a
  vLLM worker's cached tps feeds these paths.
- **Full integration suite is not claimed green.** `TestAdversarialGameabilityBounds`
  (a probabilistic fraud-quarantine bounds test, untouched by this wave and on a disjoint
  code path — it submits via `POST /v1/jobs`, never `/v1/quote`) is flaky under full-suite
  concurrency: it passes 3/3 in isolation with N inside its published bounds, but the whole
  suite run at once can push a garbage/replay worker past `maxGarbageN`/`maxReplayN`. Filed
  as a separate fix; it does not gate this wave (proven disjoint from the routing/quote
  code path).
- **14B/32B curve rows** are intentionally not encoded — no catalogue model maps to them;
  transcribe from the same sweep artifact if such models are ever added.
