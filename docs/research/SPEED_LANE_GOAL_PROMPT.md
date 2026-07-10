# Goal prompt — the Speed Lane iterative build loop (paste into a fresh chat)

> **SUPERSEDED 2026-07-06 — do NOT paste this into a fresh chat.** The "beat an A100 on
> wall-clock" thesis this prompt executes was REFUTED by real measurement: a real
> A100-SXM4-80GB under vLLM serves 44,269 tok/s (~19× the Candle-bench figure used here);
> honest break-even is ~318 M3-Pro-class nodes, not ~18. The salvaged, honest routing rule and
> current state of play: `docs/speed-lane-reports/A100_REFERENCE_MEASURED.md`,
> `A100_CAPABILITY_SWEEP.md`, `docs/research/SPEED_LANE_AUDIT_2_AND_HANDOFF.md`. Kept unedited
> below for the receipt trail.

> **Before running this, read `docs/research/SPEED_LANE_HANDOFF.md` first** — it carries the full
> session state, tells you to keep planning and *sharpen this prompt* before executing, and flags
> open decisions (build order, the moat-vs-dispatch sequencing, commit?, re-verify research?).
> This prompt is the execution artifact; the handoff is the entry point that improves it.

*Copy everything below the line into a new session in this repo. It is self-contained: it
carries the target, the methodology proven in the prior session, the discipline, and the
context pointers. It reproduces the scope → parallel-waves → real-infra-proof loop that landed
85 verified climbs and a live production deploy.*

---

You are working in the real git tree at `/Users/scammermike/Downloads/computexchange` — a
compute marketplace that dispatches LLM inference jobs to a heterogeneous fleet of strangers'
machines (Apple Silicon Macs + assorted GPUs). A job may run on one node or be split across many
and run in parallel. **Your mission: make this the fastest way to run a batch inference job —
win on WALL-CLOCK TIME, not on cost.** The buyer should get their result back sooner than if
they'd rented a single A100/H100, because we fan their batch across many machines in parallel.
Quantization/compression (saving money) is NOT the goal; saving TIME is.

## Read first (context that already exists — do not redo it)
- `docs/research/SPEED_LANE_RESEARCH.md` — the cited frontier report (fastest runtimes + measured
  tok/s; quality-neutral speed techniques; disaggregation; the distributed-fleet frontier).
- `docs/research/SPEED_LANE_GRADING.md` — our two-tier grade (vs frontier, vs the beyond-frontier
  marketplace potential) with a ranked build sequence. **This is your backlog.**
- `docs/research/SPEED_LANE_CURRENT_STATE.md` — our real measured numbers + what's built vs not.
- `docs/HAWKING_PORT_PLAN.md` — the continuous-batch lane status (model correctness + churn
  proven on real Metal through Week 5; dispatch wiring is Week 6, the top item).
- `docs/internal/CREED_AND_PATH_TO_TEN.md` Implementation Log (entries 1–85) — how the prior
  session logged every proven climb; follow the same format and continue numbering.

## The target sequence (pressure-tested 2026-07-06 — see "Sequencing decisions" below)
1. **Wire continuous-batch dispatch** — ✅ **LANDED (wave 1A, CREED entry 86) — and the
   "biggest per-node throughput unlock" hypothesis was REFUTED by measurement:** the wired
   lane is correct (byte-identical to the Candle runner on the gate set, all 9 Metal gates
   green) but measured **0.67x vs candle batched** at dispatch level (88.3 vs 132.1 tok/s).
   Opt-in only; the levers if it is ever to win are bulk prefill, PrefixIndex reuse, and true
   cross-task continuous arrival (Week 6b). See
   docs/batching-efficiency-reports/2026-07-06-m3pro-hawking-dispatch.md.
2. **Speed-optimal data-parallel batch fan-out — THE MOAT.** ✅ **LANDED (wave 1B, CREED entry
   87):** planner + adaptive-N + endgame racing + speed-ordered peers, measured 2.7x wall-clock
   cut on the real control plane (scheduling layer); fleet-vs-A100 curve MODELED (break-even 18
   Macs, 2.95x at 50); L3 real-fleet run remains owner-gated (runbook:
   docs/speed-lane-reports/FANOUT_PLANNER_WAVE1B.md §6). Original scope kept below for the
   record: The scheduler already split a batch
   job into chunks across nodes, but not *speed-optimally*: chunks were sized once at submit
   from the FLEET-AVERAGE rate (api.go targetTaskSecs=45s), claiming is pull-based (which gives
   only coarse implicit speed-weighting), and hedging is purely time-based (90s / 15s throttled).
   Build the wall-clock planner: per-node-rate-aware shard sizing, adaptive-N (fan wide only when
   the job's shape amortizes N cold loads + per-chunk overhead), and ENDGAME RACING (when claimed
   work outnumbers idle workers near the end, duplicate the slowest running chunks immediately
   instead of waiting out the 90s hedge — the last chunk's straggler IS the buyer's wall-clock).
   (control/scheduler.go, control/api.go split path, control/workers.go hedge path, a new
   planner file) — this is the thing that literally beats an A100.
3. **Wall-clock speed-SLA quote** — extend the firm-quote tier into a *guaranteed completion
   time* backed by the fan-out planner's predicted wall-clock ("your 10k-prompt batch in 4 min,
   guaranteed"). (control/quote.go, control/api.go)
4. Later waves: phase-aware prefill/decode routing across heterogeneous nodes; cross-fleet
   KV-prefix reuse (+ the Week-6 on-node PrefixIndex KV-copy); CUDA-lane FP8/graphs (owner-gated
   on the RunPod soak).

**Speculative decoding is DEMOTED to spike-gated — do not build it in this loop.** The original
sequence ranked it #3 from frontier paper numbers (EAGLE ~1.6–2× at batch-1). But our OWN
measured evidence says otherwise on the actual target hardware:
`docs/internal/PERF_AND_CAPABILITY_AUDIT.md:47,88` — Hawking's benches measured the trained
EAGLE head **net-negative (0.40x–0.21x)** on the tested Mac, n-gram τ=1.43 best but still
sub-gate, and a viable path needs a batched multi-query Metal kernel that does not exist
("not wiring an existing seam"). `docs/HAWKING_PORT_PLAN.md` lists spec decode under "Do NOT
pull in (per the audit)". It is also off-thesis for the batch lane: spec-dec's win is batch-1
latency and fades at batch ([VERIFIED] 1.21× at batch 128) — our thesis is BATCH wall-clock via
fan-out. Local measurement on the target hardware beats a frontier paper number every time.
Revisit only as a cheap measured spike (prompt-lookup/n-gram draft on real Metal, ship gate
>1.3× real mixed-traffic) if the interactive single-request lane ever becomes the priority.

## Sequencing decisions (resolved 2026-07-06, do not re-litigate)
- **Items 1 and 2 run in PARALLEL as wave 1** — the "which first" question dissolves: item 1 is
  Rust (`agent/src/`), item 2 is Go (`control/`), disjoint file sets, and neither's proof
  depends on the other's landing (the planner takes per-node rates as INPUT data, whatever they
  are; the dispatch wiring lifts those rates but doesn't change the planner's math).
- **Item 3 (SLA quote) is wave 2** — it consumes the planner's predicted-wall-clock function and
  shares `api.go`/`quote.go` surface with wave-1B's edits; sequencing it avoids hot-file churn.
- **The hawking cross-worker determinism re-gate** (seed `(apple_silicon, hawking, build_hash)`
  honeypots + golden baseline, control-side) is wave 2 — required before the wired lane carries
  byte-exact money work, but control-side seeding files are disjoint from the wave-1 Rust work,
  and the agent-side wiring can land + be proven locally without it (the runner stays opt-in via
  `inference_backend = "hawking"`).

## Classify-pass findings (2026-07-06 scouts — items the grading missed, with receipts)
The fresh code scan over the speed lane surfaced these concrete wall-clock losses beyond the
headline backlog. Fold them into the waves (several fit naturally inside item 2's bundle):
- **Per-node measured rates exist but are IGNORED everywhere that matters:** `worker_tps_cache`
  (store.go ~1070) and `benchmark_results.tps` are read for claim ORDERING only
  (scheduler.go:603-605); chunk sizing uses a STATIC throughput map (api.go:2857-2926), ETA uses
  blunt `queued/workers × perTaskSecs` aggregates (api.go:2971-2994), and hedge peer selection
  takes the first eligible peer, never the fastest (workers.go:548). The planner fixes all four.
- **ETA has no cold-load term and no heterogeneity term** — `benchmark_results.load_ms` is
  persisted (benchmark.go:114) but never read at claim/quote time; a mixed fleet's tail is
  systematically underestimated.
- **The endgame tail is reactive-only:** hedge at 90s (workers.go:99-115), no-peer watchdog at
  5min (latency_watchdog.go:52), stale reaper at 30min. Nothing races the last running chunks
  onto idle workers the moment spare capacity appears — the endgame-racing gap.
- **Result merge blocks `results_url`:** mergeJobResults (api.go:1288-1357) buffers ALL chunks
  in control-plane memory, no streaming; merge latency lands directly on the buyer's wall-clock
  after the last chunk finishes. (Defer unless cheap — but name it in the ledger.)
- **No pre-warm/prefetch signaling:** a claim for a cold model just blocks inside the task
  (2-3min GGUF load); the 60s `last_seen_warm` window (scheduler.go:612-616) also mis-classes a
  model loaded 61s ago as cold. Dispatch-early model-load signaling is a later-wave item.
- **The hawking determinism re-gate is SMALLER than planned:** verification.go's class gating
  (sameVerificationClass:392-399, byteHoneypotComparable:433-438) already handles a hawking
  class correctly; the actual blocker is that byte-exact `batch_infer` honeypots were
  deliberately never seeded with an `answer_class` (seed.go:149-157). The fix is seeding: a
  real hawking-produced known answer + `answer_class=classKey(engine,build_hash)` honeypot, plus
  the golden-baseline row — control-side file set is essentially seed.go (+ docs), NOT a
  verification.go rewrite.
- **SLA scaffolding already half-exists:** `slaMinEligibleWorkers=5` + an `SLAEligible` flag in
  quote.go:361, `deadline_secs` on jobs with a watchdog escalation ladder (workers.go:573-626),
  and the firm-quote price cap (`jobs.firm_quote_max_usd`, api.go:673-707). The time-SLA extends
  these; the miss-remedy needs collect.go/store.go refund hooks.

## The methodology (proven last session — reproduce it exactly)
1. **Scope first.** Run a classify pass over the speed-lane backlog above (and re-scan the code
   for anything the grading missed): for each item decide done / code-doable-now / blocked, and
   group the code-doable ones into DISJOINT-FILE bundles so parallel agents never touch the same
   file. Flag the hot files (`agent/src/runners.rs`, `quantized_llama_batched.rs`,
   `control/scheduler.go`, `control/api.go`, `control/quote.go`, `control/store.go`) — bundles
   touching the same hot file must be SEQUENCED into one agent, never run in parallel (the prior
   session hit real merge churn when this was violated).
2. **Fan out implementation waves** via the Workflow tool (opt-in multi-agent orchestration).
   Each bundle is one agent owning a disjoint file set, with the full discipline below in its
   prompt. Sequence the `runners.rs` lane (it has multiple items). Use distinct Postgres/MinIO
   ports per Go bundle to avoid collisions.
3. **After each wave, independently RE-VERIFY the risky claims yourself** — don't trust the
   sub-agent reports. Re-run the determinism gates on real Metal, re-run the full integration
   suite on a fresh stack, fix any regression or new clippy warning yourself, check CREED
   numbering for collisions, clean up orphaned processes.
4. **Log each proven climb** in the CREED Implementation Log (continue from entry 85) with a real
   proof artifact. Do NOT self-bump the scorecard grades (Commitments 1 & 2: grades move only via
   the adversarial re-audit).

## The discipline (non-negotiable — this is why the work is trustworthy)
- **Prove everything against REAL infrastructure** — real Postgres + MinIO stacks stood up
  per-task (initdb `-U cx --auth=trust -E UTF8`, `LC_ALL=C` to dodge the multithreaded-postmaster
  bug; MinIO + `mc mb`), real Metal inference on the M3 Pro, real HTTP against a running control
  plane. NEVER assert from a diff. Add real tests. Run the full suite before AND after.
- **Determinism is sacred.** The whole verification/trust system depends on batched==serial
  byte-equality. Any change near the quantized-llama path needs a real `#[ignore]`d real-Metal
  byte-exact gate, and the existing gates
  (`hawking_real_gguf_decode_matches_serial_and_is_coherent`,
  `batch_padded_bucket_equals_serial_mixed_lengths`,
  `batch_active_shrink_equals_serial_mixed_lengths`, `batch_shared_prefix_equals_serial`,
  `batch_width_split_matches_unsplit_batch`) must still pass. runners.rs's test module has a
  `METAL_HARDWARE_TEST_LOCK` every real-model test must acquire. Speculative decoding is
  *lossless* — its output must match the non-speculative path to the documented tolerance, gated.
- **Baselines:** Rust `cargo build`/`clippy` clean on BOTH `--features metal` and
  `--no-default-features` at exactly 4 pre-existing hardware.rs doc warnings; Go `go build`/`vet`
  clean, `gofmt -l control/ | grep -v webauthn.go` empty. If you see more warnings than baseline,
  one is yours — fix it.
- **A fully-proven subset beats a rushed whole.** If an item can't be proven safely in one pass
  (e.g. the Hawking dispatch wiring, or a distributed run needing nodes you don't have), land the
  largest genuinely-proven increment and name the precise remaining blocker honestly. Never claim
  a speedup you did not measure on real hardware.
- **Measure the win, not just correctness.** Every speed item's proof artifact is a real
  before/after wall-clock or tok/s number on real hardware, committed under `docs/` (follow the
  `docs/batching-efficiency-reports/`, `docs/load-test-reports/` precedent). "It's correct" is
  necessary; "it's N× faster, measured" is the deliverable.
- **The fan-out moat (item 2) proof splits into three explicit layers — be crisp about which
  layer each claim lives at, and never let a lower layer masquerade as a higher one:**
  1. *Planner math, proven locally (this loop, mandatory):* unit tests on shard-sizing /
     adaptive-N / endgame-racing invariants, plus a SIMULATION harness calibrated with our REAL
     measured rates (M3 Pro 139 tok/s real-traffic, A100 spike 2345 tok/s @ batch 64, real
     model-load times) showing the planner's assignment minimizes modeled wall-clock vs the
     current fleet-average/fixed-chunk baseline, and reporting the modeled fleet-vs-A100 curve
     (break-even N, the 50-node margin). Committed as a real report under `docs/`.
  2. *Control-plane behavior, proven locally (this loop, mandatory):* a real-Postgres
     integration test where N concurrent fake workers with DECLARED different rates actually
     claim/commit through the real scheduler + planner + endgame-racing code paths, and the
     measured end-to-end wall-clock through the real control plane beats the pre-planner
     baseline for the same synthetic job. This exercises the real code, not a model of it —
     but its workers fake the GPU, so it proves SCHEDULING, never tok/s.
  3. *Real multi-node wall-clock vs a real A100 (owner-run, scoped by this loop):* the
     demonstration needs real distinct machines (one M3 Pro cannot host independent Metal
     nodes — they'd share one bandwidth-bound GPU and prove nothing). Deliver the runbook +
     harness script (same pattern as the RunPod soak) and name it honestly as the remaining
     step in the ledger.

## Standing constraints
- **Never attribute AI in git** — no `Co-Authored-By: Claude`, no "Generated with" footers, ever.
- **Commit/deploy only when the user explicitly asks.** Work stays in the tree otherwise.
- Digital Ocean prod IS reachable via `~/.ssh/tailor_droplet` over IPv4 (the prior session
  deployed to it, health-gated, with a backup + rollback image). Treat any prod action as
  hard-to-reverse: survey read-only first, back up, confirm the specific action.

## Deliverable each turn
Keep the user in the loop: after each wave, a tight summary of what landed with its real measured
speedup, what you re-verified yourself, and what deferred + why. When the code-doable speed
frontier is swept, give the honest ledger of what's proven vs what needs real multi-node hardware
or the RunPod GPU to confirm — and, per the thesis, whether we can now demonstrably beat a single
A100 on a real batch's wall-clock.
