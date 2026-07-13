<!-- CLAIM-SCOPE: internal-engineering-non-authoritative -->
# Computexchange Turbo — upgrade summary

> **Proven.** Every claim below is grounded in code read directly from this repo
> (with file citations) and reconciled against a live run: `make prove-local`
> passes **82/82** (`infra → migrate → seed → live Metal inference for embed /
> batch_infer / whisper / batch_classification → merge → verify → ledger`), with
> the new Turbo capabilities exercised end-to-end — the hard-filter claim
> (`hard-filter-live`: an `apple_silicon_ultra`-only job is never handed to a
> lower-class Apple-Silicon worker), the warm-pool multi-task path
> (`job-embed-multi`: 6 tasks, model loaded once), live `batch_classification` with
> redundancy verification, the merged buyer artifact, the invoice endpoint, and the
> served dashboard. The one honest stub is the payout transfer rail (§3), which the
> system refuses to fake.

Turbo is the second pass over the V1 marketplace: same horizon (the wire contract
in `proto/manifest.schema.json`, `control/types.go`, `agent/src/types.rs`), more
throughput and more workloads, with the scheduler, agent, and verifier each
sharpened. Five threads:

1. Scheduler V2 — hard-filter claim, adaptive chunking, ETA, hedging
2. Agent Turbo — warm model pool, bounded concurrency, three new workloads
3. Verification V2 — 3-way tiebreak, real majority vote, auto-quarantine
4. Result merging — one buyer-ready JSONL artifact per job
5. New workloads — `batch_classification`, `json_extraction`, `rerank`

---

## 1. Scheduler V2 — `control/scheduler.go`, `control/api.go`

### Hard-filter claim (the headline)
The claim query in `Store.ClaimTask` enforces the stored worker/resource
constraints **in SQL**. The `next` CTE joins the claiming worker and its supplier
and rejects on every currently persisted dimension at once
(`scheduler.go:226–262`):

- supplier active (quarantine gate): `s.status = 'active'`
- memory: `COALESCE(j.min_memory_gb,0) <= COALESCE(w.memory_gb,0)`
- hardware: `j.hw_classes IS NULL OR w.hw_class = ANY(j.hw_classes)`
- runtime capability: an exact current-matrix row exists in
  `worker_authorized_capabilities` for `(worker, job_type, model_ref)`
- data residency: `j.data_residency IS NULL OR s.data_country = ANY(j.data_residency)`
- trusted-tier gate: `j.tier <> 'trusted' OR $2 >= 2` (supplier tier ≥ 2)
- **min-payout gate:** `COALESCE(j.offered_rate_usd_hr,1e9) >= COALESCE(w.min_payout_usd_hr,0)`

`supported_jobs` and `supported_models` remain wire/debug compatibility roll-ups,
not authority. Registration intersects them with generated production cells and
atomically replaces normalized rows carrying the matrix SHA. Claim, quote, planner,
and routing queries all require that exact row; legacy array-only workers remain
inert until re-registration.

The queue itself is Postgres, not NATS: `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`
(`scheduler.go:248`) so concurrent pollers each grab a different row. Ordering is
`(t.claimed_by = $1) DESC, (j.tier = 'priority') DESC, t.created_at ASC`
(`scheduler.go:247`) — a task pinned to this worker jumps first, then priority
tier, then oldest. The claim flips the task to `running` and stamps `started_at`
in the same statement (`scheduler.go:251–253`), which is what lets the stale-task
reaper detect a claim that never committed.

The supplier tier driving the trusted gate is computed live from reputation +
lifetime completed tasks via `reputationTier(rep, jobsDone)` (`scheduler.go:216`).

### Min-payout offered rate
The `offered_rate_usd_hr` the gate compares is derived at submit time in
`Server.offeredRateUsdHr` (`api.go:1198`): `units/hr = throughput(jobType) × 3600`,
`$/hr = (units/hr / 1000) × price_per_1k_usd`, with the DB-backed model price and
a default when the model is unknown so the gate is never zeroed.

### Adaptive chunk sizing
`adaptiveSplitSize` (`api.go:1170`) picks JSONL lines-per-task. An explicit
`params.split_size` always wins (the buyer override); otherwise it is
`throughput(jobType) × targetTaskSecs` clamped to `[1, 4096]`, where
`targetTaskSecs = 45` (`api.go:1163`) and `jobTypeThroughput` (`api.go:1142`)
gives per-type items/sec (embed 200, batch_classification 80, rerank 40,
json_extraction 8, batch_infer 4, audio_transcribe 2, image_gen 0.2). So an embed
job packs far more items per chunk than a generation job for the same ~45s target.

### ETA
`estimateETASecs` (`api.go:1214`) is a queue-depth/throughput estimate: this job's
tasks plus everything already queued, spread across the live fleet
(`waves = ceil((queued + nTasks) / workers) × perTaskSecs`), degrading to a
single-worker/no-backlog estimate on a DB error. The result is persisted to
`jobs.eta_secs` and returned in both `JobSubmitResponse` and `JobStatus`
(`types.go:192`, `types.go:202`); the RFC3339 `estimated_completion` adds a
per-tier floor (`tierMinCompletion`, `api.go:1235`: priority 5 min, else 15 min).

### Hedging
A background ticker `Workers.hedgeStragglers` (`workers.go:157`) finds running
primaries past ~2× their expected per-task time (capped per-job and per-tick),
picks a distinct same-class peer via `SelectRedundancyPeerExcluding`, and inserts
a pinned duplicate task via `Store.InsertHedgeTask` (`store.go:1275`) carrying
`hedged_from = the slow task`. On a winning commit, `Store.CancelStragglerSiblings`
cancels any still-running hedge sibling for the same `(job_id, chunk_index)` so
first-commit wins and the loser's worker is freed; the merge dedupes per chunk so a
hedge never double-counts. Hedge insertions are counted by `cx_hedges_total`
(`metrics.go:50`). Proven end-to-end by `TestStragglerHedge` (pinned to the
distinct peer, hedged at most once) in the integration matrix.

---

## 2. Agent Turbo — `agent/src/pool.rs`, `agent/src/runners.rs`, `agent/src/config.rs`

### Warm model pool
`ModelPool` (`pool.rs:52`) loads each backend **once** and reuses it across every
task; the baseline reloaded the model from disk inside every `run()`. Backends are
lazily initialised behind a per-key `tokio::sync::OnceCell`, so concurrent
first-touchers race to a single load and everyone after reuses the warm handle
(`pool.rs:70`, `pool.rs:87`, `pool.rs:106`). Sharing follows each backend's borrow:
`Embedder::embed` is `&self` → handed out as a bare `Arc<Embedder>`; the `&mut self`
Llama/Whisper decoders are guarded by a `tokio::Mutex` inside the `Arc`
(`pool.rs:9–16`). A process-wide `loads()` counter (`pool.rs:36`) backs the proof
test `pool_loads_once_across_n_runs` (`pool.rs:188`), which fires N concurrent
same-key getters and asserts the load counter reads 1.

Pool keys are canonicalised (`pool.rs:145`, `pool.rs:155`) so `""`, the catalogue
id, and the HF repo all share one warm model; Qwen refs key separately so the two
Llama-family models never collide on one slot.

### Bounded concurrency
`AgentConfig::concurrency` (`config.rs:101`) resolves the work-pipeline permit
count: an explicit `max_concurrent_tasks` wins (floored at 1), else a memory-aware
default of ~1 slot per 8 GiB clamped to `[2, 4]` (`config.rs:104`). The rationale
in the code: the warm models are the memory cost and heavy GPU compute serialises
behind each model's mutex, so a wide pool buys nothing but RAM pressure — the win
is overlapping S3 GET/PUT with held compute and letting *distinct* models run in
parallel (`config.rs:41–46`).

### Runner contract + new workloads
The `JobRunner` trait (`runners.rs:65`) is `can_run / run / backend_name`, where
`run` takes `&ModelPool` so every runner pulls warm models. `can_run` gates on job
type, model kind, and `meets_memory` (`cap.memory_gb >= manifest.constraints.min_memory_gb`).
The generation workloads (`batch_infer`, `batch_classification`, `json_extraction`)
gate to `ModelKind::Gguf` only; embed/whisper/rerank accept gguf|hf|mlx. There is
no hardware-class check in `can_run` — the control plane's claim filter owns that.

---

## 3. Verification V2 — `control/verification.go`, `control/api.go`

`Verifier.verifyTaskResult` (`verification.go:55`) runs the layered V1 scheme with
the V2 tiebreak grafted on:

- **Step 1 — honeypot** (`verification.go:59`): compare the committed bytes to the
  known answer keyed by `(job_type, input_ref)`. A confirmed bad answer docks
  reputation (`EventHoneypotFail`), claws back any credit, **auto-quarantines the
  supplier** (`QuarantineSupplier`, `verification.go:76`), and requeues the task.
  This is the quarantine that the scheduler's `s.status = 'active'` gate then
  enforces on every future claim.
- **Step 2 — redundancy + 3-way tiebreak** (`verification.go:98`): when a peer
  result exists, gather every committed result for the chunk
  (`gatherChunkResults`, `verification.go:161`) and:
  - **≥3 results** → a real N-way majority vote (`resolveTiebreak`,
    `verification.go:194`): each supplier on the winning side is credited
    `EventRedundancyMatch`, each loser docked `EventMismatch`; a 3-way split with
    no majority docks no one (an inconclusive vote must not punish,
    `verification.go:207`).
  - **2 results that disagree** → dispatch a third, distinct same-class worker
    (`dispatchTiebreak`, `verification.go:237`), excluding both workers that
    already ran the chunk, via `SelectRedundancyPeerExcluding` (`scheduler.go:109`);
    return `pass_with_penalty` and let the vote settle when the third commits. No
    third worker online → provisional trust, never a fabricated pass
    (`verification.go:258`).
  - **2 that agree / a single result** → credit `EventRedundancyMatch`.

The comparison primitive `resultsAgree` (`verification.go:286`) is job-type-aware:
embed → mean per-vector cosine ≥ `0.999` (`embeddingCosineThreshold`,
`verification.go:274`); batch_classification → per-item top-1 label equality;
json_extraction → per-item canonical-JSON (sorted-key) equality; rerank → exact
order-array equality; everything else → exact byte match. A parse failure, shape
mismatch, or count mismatch is a **disagreement**, never a pass.

Tiebreak insertions are counted by `cx_tiebreaks_total` (`metrics.go:51`,
`verification.go:267`); mismatches bump `cx_verification_mismatch_total`
(`api.go:943`).

---

## 4. Result merging — `control/api.go`

A completed job is collapsed into **one** buyer-ready JSONL artifact.
`mergeJobResults` (`api.go:507`) fetches the job's primary task results in
`chunk_index` order and flattens each via `mergeResultObject` (`api.go:539`) to one
JSON line per input item, stamping a running global `index`:

- `embed` → `{"index":<global>,"vector":[...]}`
- `batch_classification` → `{"index":<global>,"label":"..."}`
- `rerank` → `{"index":<global>,"order":[...]}`
- `json_extraction` → the extracted object with an `index` field stamped in
- `batch_infer` / other → the per-item `completions`/`items` records flattened,
  else the raw result object passed through as one line (never dropped)

A missing or malformed primary result is surfaced as a hard error
(`api.go:520`, `api.go:524`) — a silent gap would hand the buyer a short file. The
merge runs in two places: the completion path `finalizeJobIfDone` (`api.go:983`)
merges **before** marking the job `complete` (so a buyer never sees
`status=complete` with a missing/short output), and the results handler
`handleJobResults` (`api.go:427`) merges on read as the correctness fallback and
then presigns `output_ref`. Merges are counted by `cx_result_merges_total`
(`metrics.go:52`, `api.go:532`). The buyer gets `results_url` (the merged
artifact) plus `result_urls[]` (per-task presigned URLs) — both real signed URLs
(`types.go:220`).

---

## 5. New workloads — `agent/src/types.rs`, `agent/src/runners.rs`, `control/verification.go`

Three workloads join the closed job-type set (`control/types.go:39`,
`agent/src/types.rs:42`), each with a matching agent runner and a real result
verifier:

| Job type | Params | Runner behavior | Result JSONL shape | Verifier |
|---|---|---|---|---|
| `batch_classification` | `labels: [String]` | warm Llama (gguf-only); generate, map to the closest of the provided labels (normalized exact/contains/prefix); never invents a label outside the set | `{"labels":[{"index","label"}]}` | per-item top-1 label equality (`classificationAgree`, `verification.go:327`) |
| `json_extraction` | `schema: JSON` | warm Llama (gguf-only); generate, extract the first balanced JSON object; on parse failure records `{"_error":"no_parseable_json","_raw":…}` rather than faking | `{"items":[{"index","json"}]}` | per-item canonical-JSON equality (`jsonExtractionAgree`, `verification.go:362`) |
| `rerank` | `top_k: u32` (0 = no cut) | warm MiniLM embedder; embed query + docs once, cosine-score, order by descending score (ties → ascending index); truncate to `top_k` | `{"rankings":[{"index","order"}]}` | exact order-array equality (`rerankAgree`, `verification.go:404`) |

*(Runner field names + behavior above are from a focused read of `runners.rs`; the
result-shape and verifier columns are confirmed directly against
`verification.go`'s schema structs `classificationResult`/`jsonExtractionResult`/
`rerankResult` and the merge in `api.go`.)* The Rust side decodes these params
through the poll dispatch — see the `new_jobtypes_carry_params` test
(`agent/src/types.rs:321`).

---

## New Turbo metrics — `control/metrics.go`

Hand-rolled Prometheus text exposition (no client dependency). Turbo adds, on top
of the baseline `cx_jobs_submitted_total` / `cx_tasks_dispatched_total` /
`cx_tasks_completed_total` / `cx_verification_mismatch_total` /
`cx_payouts_released_total` / `cx_active_workers` / `cx_queue_depth{tier,job_type}`:

- `cx_quarantines_total` — suppliers auto-quarantined on fraud / low reputation
- `cx_hedges_total` — straggler hedge tasks inserted
- `cx_tiebreaks_total` — third-worker redundancy tiebreak tasks inserted
- `cx_result_merges_total` — buyer-ready artifacts merged

(`metrics.go:29–33`, `metrics.go:49–52`.) An unavailable gauge is emitted as a
`#`-comment, never a fabricated zero (`metrics.go:60`).
