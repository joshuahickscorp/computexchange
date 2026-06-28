# Plane C - Compute Autopilot and Exchange Brain

> **Pre-canon, but written as a real plane.** Plane C intentionally folds two
> would-be planes into one: the buyer-facing **Compute Autopilot** and the
> operator/scheduler-facing **Exchange Brain**. They should not be split yet,
> because a quote is only honest if it is backed by live supply, benchmark
> history, memory risk, failure data, verification overhead, and budget controls.
> Plane C is the layer that makes Computexchange better than renting compute
> yourself.

## 0. Plane map

Plane A is the current business: job-level parallelism over independent tasks.
Buyers submit batch work, the control plane splits it, workers claim tasks, agents
run whole tasks on one machine, results are verified and merged.

Plane B is co-located high-memory execution: multiple Macs on the same physical
fabric register as one `apple_silicon_cluster` worker. It exists for models that
do not fit on one Mac. It is explicitly co-located because model sharding over
the random internet is latency physics, not product work.

Plane C is the intelligence and safety layer above both:

- **Compute Autopilot:** preflight, quote, plan, budget, submit, monitor,
  recover, explain.
- **Exchange Brain:** live supply intelligence, learned benchmarks, risk-aware
  routing, adaptive chunking, queue simulation, pricing intelligence, failure
  classification, and network-wide optimization.

This is two candidate planes deliberately baked into one. The reason is simple:
the buyer should experience one thing - "tell me what this will cost, how long it
will take, whether it will fit, and make sure it cannot silently burn money" -
but the system can only provide that experience by becoming much smarter
internally.

## 1. One-sentence thesis

Plane C turns Computexchange from "a verified compute marketplace" into "a
compute autopilot that predicts, caps, routes, retries, and explains batch AI
work better than a buyer could do by hand on a cloud GPU."

## 2. The product promise

The buyer should not need to know:

- which Mac class can run the job,
- how much memory the model plus KV cache will use,
- how to chunk the input,
- how many retries to expect,
- which tier to choose,
- whether the current fleet can clear the job in time,
- whether a task is likely to OOM,
- whether output will be huge,
- whether JSONL is malformed,
- whether redundancy overhead is worth it,
- why a job failed,
- whether to cancel, split smaller, raise budget, or wait.

Plane C should answer all of that before the buyer spends money, and continue
adjusting while the job runs.

The operator should not need to guess:

- which workers are genuinely profitable,
- which models are warming or cold,
- which suppliers are unstable,
- which queues are about to breach SLA,
- which workers should receive the next task,
- which failures are buyer input vs supplier fault vs infrastructure,
- which prices attract supply without overpaying,
- which jobs are underquoted,
- which workloads deserve a new runner,
- which clusters or hardware classes create real advantage.

Plane C should turn the exchange itself into a learning control system.

## 3. Why this is not an IDE

Plane C is not a full IDE and should not become one by accident. The goal is
project and input understanding, not code editing.

Allowed:

- scan a folder to detect likely workload shape,
- count files, records, tokens, images, audio minutes, and output size,
- detect a RAG embedding job, eval batch, transcription batch, rerank batch, JSON
  extraction batch, or synthetic generation batch,
- build a quote from a project or input file,
- recommend a job type and model,
- generate a submission manifest,
- warn that arbitrary code execution is out of scope,
- produce a runnable `cx submit` command,
- explain how the quote was derived.

Not allowed in Plane C:

- arbitrary buyer code execution,
- a full code editor,
- remote terminal execution,
- notebooks,
- untrusted build steps,
- plugin execution inside workers,
- "just upload your repo and we run it" without a closed job type.

The product can feel intelligent without becoming an attack surface.

## 4. Why this is the next plane

The core engine already proves the hard things locally: real Metal inference,
verification, Postgres queue, S3 payloads, hedging, throttling, status surface,
OpenAI batch shape, and the Plane B cluster seam. The remaining edge is not a
new framework. It is making the system:

- more predictive,
- harder to accidentally misuse,
- more efficient than manual GPU rental,
- more transparent about cost,
- safer against OOM and silent failure,
- better at routing than a human buyer,
- better at pricing than static tables,
- better at learning from every job.

RunPod-style pain is the target: "it OOMed, I did not find out, and money kept
draining." Plane C should make that failure mode structurally impossible.

## 5. Plane C as two fused subplanes

### C1. Compute Autopilot

The buyer-facing layer.

It takes a request, project, input file, or OpenAI Batch file and produces:

- validated job shape,
- estimated units,
- estimated memory,
- estimated output size,
- estimated verification overhead,
- eligible supply count,
- ETA range,
- cost range,
- confidence score,
- OOM risk,
- recommended model,
- recommended hardware class,
- recommended tier,
- recommended split size,
- budget cap,
- time cap,
- cancellation/refund behavior,
- exact submission manifest.

It continues during execution:

- watches progress,
- adjusts chunk size on retry,
- requeues immediately on classified failure,
- cancels before budget breach,
- escalates tier if configured,
- explains failures,
- emits buyer-visible events,
- produces a final invoice with estimate vs actual.

### C2. Exchange Brain

The internal control layer.

It maintains live intelligence:

- worker benchmark history,
- benchmark freshness,
- model cold/warm state,
- worker availability windows,
- worker failure rates,
- thermal degradation,
- memory headroom history,
- network transfer speed,
- object-store latency,
- queue depth by type/tier/model,
- verification mismatch rates,
- fraud risk,
- region/data residency capacity,
- supplier minimum payout curves,
- buyer cancellation patterns,
- underquote/overquote drift.

It uses that intelligence for:

- quote accuracy,
- routing decisions,
- dynamic pricing,
- dynamic chunking,
- hedging thresholds,
- retry policy,
- trust policy,
- worker tiering,
- new hardware-class evaluation,
- workload expansion decisions.

The two halves must share one truth store. If the quote system says a job will
fit and the scheduler later routes it to a worker that OOMs, Plane C failed.

## 6. Core artifacts to add

### API

Add:

```text
POST /v1/quote
POST /v1/jobs/{id}/budget
POST /v1/worker/task/{id}/fail
GET  /v1/jobs/{id}/events
GET  /admin/quotes
GET  /admin/capacity
GET  /admin/routing
GET  /admin/failures
```

Possible later:

```text
POST /v1/plan
POST /v1/preflight
GET  /v1/models/{id}/capacity
GET  /v1/supply
GET  /admin/pricing
GET  /admin/forecast
```

### CLI

Add:

```text
cx quote --model <id> --type <jobtype> --input <file|-> [flags]
cx plan  --project <dir> [--goal embeddings|eval|transcribe|extract|rerank]
cx doctor-input --type <jobtype> --input <file|->
cx failures <job_id>
cx events <job_id>
cx budget <job_id> --max-usd N
```

The first version can be entirely local plus API-backed:

- local file scan,
- local JSONL validation,
- local token/byte estimate,
- server quote call for supply and price,
- printed command for submission.

### Python SDK

Add:

```python
client.quote(...)
client.plan(...)
client.preflight(...)
client.events(job_id)
client.failures(job_id)
client.submit_with_budget(...)
```

The SDK should return structured reasons, not just strings.

### Data model

Add tables or views for:

- `quotes`
- `quote_inputs`
- `quote_supply_snapshot`
- `job_events`
- `task_failures`
- `worker_model_state`
- `worker_availability_windows`
- `worker_network_stats`
- `worker_memory_samples`
- `routing_decisions`
- `pricing_observations`
- `job_estimate_drift`
- `budget_controls`

The important rule: quote assumptions must be persisted. A later invoice should
be able to say what the system believed at quote time.

## 7. Quote object

Plane C's central object is a quote.

Sketch:

```json
{
  "quote_id": "uuid",
  "job_type": "embed",
  "model": "all-minilm-l6-v2",
  "tier": "batch",
  "input": {
    "records": 500000,
    "bytes": 180000000,
    "estimated_tokens": 45000000,
    "malformed_records": 0,
    "sampled_records": 512
  },
  "execution": {
    "recommended_split_size": 4096,
    "estimated_tasks": 123,
    "eligible_workers_now": 8,
    "eligible_workers_recent": 31,
    "cold_start_risk": "medium",
    "oom_risk": "low",
    "recommended_hw_classes": ["apple_silicon_pro", "apple_silicon_max"]
  },
  "cost": {
    "min_usd": 0.041,
    "expected_usd": 0.052,
    "max_usd": 0.068,
    "verification_overhead_usd": 0.004,
    "platform_take_usd": 0.006
  },
  "time": {
    "p50_secs": 600,
    "p90_secs": 1200,
    "worst_case_secs": 2400
  },
  "confidence": {
    "score": 0.78,
    "reasons": [
      "8 live workers have enough effective memory",
      "historical embed throughput exists for this model",
      "input token estimate is sampled, not exact"
    ]
  },
  "budget": {
    "suggested_max_usd": 0.08,
    "cancel_before_exceeding": true
  },
  "manifest": {}
}
```

The exact shape can evolve, but these categories should exist from the start.

## 8. Preflight scanners

### JSONL scanner

Required:

- count records,
- count blank lines,
- detect malformed JSON,
- track max line size,
- sample token length,
- infer field names (`text`, `prompt`, `query`, `docs`, `audio_b64`),
- detect impossible job shape,
- estimate output size,
- produce first bad line number,
- produce suggested fix.

### Embedding scanner

Detect:

- text fields,
- empty text ratio,
- duplicate text ratio,
- average characters,
- estimated tokens,
- expected vector count,
- expected result bytes as JSON,
- expected result bytes as binary,
- index preservation requirements.

Recommend:

- `embed`,
- MiniLM vs future larger embed model,
- split size,
- binary output if large,
- compression if result is huge.

### Batch inference scanner

Detect:

- prompt length distribution,
- requested max tokens,
- worst-case output tokens,
- context overflow risk,
- temperature/determinism risk,
- model memory class,
- generation cost.

Recommend:

- smaller max_tokens if the prompt does not need long output,
- priority tier only if queue and budget justify it,
- JSON extraction/classification if the prompt is actually structured.

### Classification scanner

Detect:

- label count,
- empty label set,
- duplicate labels,
- very similar labels,
- label length,
- text length,
- likely ambiguity.

Recommend:

- merge labels,
- change labels,
- use JSON extraction if labels are really structured fields,
- increase redundancy if labels are high-stakes.

### JSON extraction scanner

Detect:

- schema size,
- schema depth,
- required fields,
- enum complexity,
- input fields,
- prompt length,
- likely parse-failure risk.

Recommend:

- simpler schema,
- smaller chunks,
- higher redundancy,
- fallback behavior for `no_parseable_json`.

### Rerank scanner

Detect:

- docs per query,
- query length,
- doc length distribution,
- top_k vs doc count,
- expected embedding count,
- output size.

Recommend:

- top_k,
- split size by total docs,
- embedding cache strategy.

### Audio scanner

Detect:

- count files,
- total duration,
- sample rate,
- channels,
- invalid base64/WAV,
- huge single file,
- language hints.

Recommend:

- model size,
- split by file or chunk,
- transcription tier,
- expected cost per audio hour.

### Project scanner

Detect:

- RAG corpus,
- eval set,
- transcript folder,
- OpenAI Batch file,
- JSONL manifest,
- CSV needing conversion,
- unsupported arbitrary script,
- likely PII.

Output:

- suggested job type,
- conversion steps,
- quote command,
- submit command.

## 9. Memory model

Plane C needs an explicit memory estimator. It does not need to be perfect in V1,
but it must be conservative and explainable.

For each task:

```text
estimated_peak_memory =
  model_weights
  + runtime_overhead
  + tokenizer_buffers
  + input_batch_buffers
  + output_buffers
  + kv_cache
  + verification_temp
  + safety_margin
```

For embeddings:

- model weights are small,
- batch input tensor and hidden states matter,
- output vector buffer matters for huge batches,
- JSON serialization can dominate memory if not streamed.

For LLM generation:

- model weights dominate baseline,
- KV cache grows with context length and max output,
- batch size multiplies KV,
- long prompts are the common underquote cause.

For Whisper:

- mel spectrogram size matters,
- chunk duration matters,
- model size matters,
- one giant audio file should be split.

For Plane B clusters:

- summed memory matters,
- per-node margin matters,
- bottleneck interconnect matters,
- activations and KV must fit according to shard layout,
- cluster advertised memory is not raw member memory.

The quote should never use total worker memory alone. It should use effective
memory, headroom, historical pressure, and per-task estimated peak.

## 10. Failure taxonomy

Plane C needs first-class failure reasons.

Buyer/input failures:

- malformed JSONL,
- missing required field,
- invalid audio,
- prompt too long,
- schema invalid,
- labels invalid,
- output too large for requested mode,
- unsupported job type,
- unsupported model,
- data residency impossible.

Supplier/worker failures:

- OOM before model load,
- OOM during model load,
- OOM during inference,
- thermal throttle beyond allowed window,
- model cache missing and download failed,
- model file corrupt,
- backend unsupported,
- worker went offline,
- output upload failed,
- result malformed,
- result mismatch,
- honeypot fail,
- suspected fraud.

Infrastructure failures:

- object store unavailable,
- database unavailable,
- presigned URL expired,
- control plane timeout,
- webhook failure,
- payout rail failure.

Scheduler/planning failures:

- no eligible supply,
- supply exists but all throttled,
- supply exists but reservation price too high,
- quote stale,
- budget cap too low,
- max duration impossible,
- verification peers unavailable.

Each failure should carry:

- class,
- retryable bool,
- buyer_fault bool,
- supplier_fault bool,
- infra_fault bool,
- suggested action,
- whether buyer is charged,
- whether supplier is credited/docked,
- whether chunk size should shrink,
- whether model/tier should change.

## 11. Immediate fail endpoint

Add `POST /v1/worker/task/{id}/fail`.

The agent should call it when it knows a task cannot complete. Waiting for the
stale reaper is too slow and too cloud-like.

Example body:

```json
{
  "reason": "oom_during_inference",
  "message": "next token allocation exceeded effective memory",
  "retryable": true,
  "duration_ms": 1273,
  "memory": {
    "total_gb": 64,
    "available_gb": 2.1,
    "effective_gb": 0,
    "reserved_headroom_gb": 8
  },
  "backend": "batch_infer",
  "model": "llama-3.2-1b-instruct-q4"
}
```

Control-plane behavior:

- record `task_failures`,
- update `job_events`,
- requeue immediately if retryable,
- shrink split size if memory/input related,
- fail job if buyer input is invalid,
- refund or avoid charge according to failure class,
- dock only when supplier fault/fraud is clear,
- update quote model drift.

## 12. Budget controls

Plane C should support hard buyer caps.

Controls:

- `max_usd`,
- `max_tasks`,
- `max_retries`,
- `max_wall_time_secs`,
- `max_verification_overhead_frac`,
- `cancel_before_exceeding`,
- `ask_before_escalating_tier`,
- `allow_partial_results`.

Budget states:

- `tracking`,
- `near_limit`,
- `paused_for_budget`,
- `cancelled_by_budget`,
- `complete_under_budget`,
- `complete_over_estimate_but_under_cap`.

Rules:

- Never dispatch a new primary if the projected charge would breach `max_usd`.
- Never add redundancy beyond the buyer's verification budget unless platform
  absorbs it.
- If retries are eating budget, pause and explain.
- If output is partial, mark it partial; never pretend complete.
- If a job is cancelled by budget, merge completed safe chunks if requested.

## 13. Adaptive chunking

Current adaptive chunking is static by job type. Plane C should make it live.

Inputs:

- quote memory estimate,
- worker effective memory,
- historical duration per chunk,
- failure rate,
- output size,
- model warm/cold state,
- verification overhead,
- queue depth,
- tier target.

Actions:

- initial split size,
- shrink on OOM,
- shrink on timeout,
- grow on fast stable chunks,
- keep chunk sizes homogeneous enough for merge simplicity,
- choose small chunks for high-risk jobs,
- choose larger chunks when dispatch overhead dominates.

Chunking modes:

- line-count split,
- token-count split,
- byte-count split,
- audio-duration split,
- docs-per-query split,
- max-output-token split.

The planner should know when line count is the wrong unit.

## 14. Routing intelligence

The scheduler's hard filter remains sacred: no worker should claim unsafe work.
Plane C sits above it and ranks choices better.

Signals:

- reputation,
- benchmark throughput,
- p99 latency,
- model warm state,
- last-seen freshness,
- availability window,
- recent failure rate,
- memory pressure,
- network speed to object store,
- thermal stability,
- minimum payout,
- data residency,
- verification peer availability,
- queue fairness,
- buyer tier,
- estimated task shape.

Routing policies:

- cheapest eligible,
- fastest eligible,
- lowest OOM risk,
- warm-model preferred,
- same-class redundancy preferred,
- reserve high-memory workers for high-memory jobs,
- avoid clusters for small jobs,
- avoid flaky workers for long chunks,
- keep new suppliers on small/probe tasks,
- route honeypots deliberately across risk classes.

Routing should write a row explaining why a worker got a task. That is how the
system learns and how the operator debugs.

## 15. Pricing intelligence

Plane C should not replace the simple catalogue immediately, but it should start
collecting the data needed for dynamic pricing.

Static inputs:

- model catalogue price,
- tier multiplier,
- platform take,
- verification overhead,
- expected units.

Dynamic inputs:

- live supply count,
- queue depth,
- supplier reservation prices,
- recent completion time,
- failure/retry overhead,
- cold-start probability,
- data residency scarcity,
- hardware class scarcity,
- cluster scarcity.

Outputs:

- quote range,
- floor price to attract supply,
- suggested buyer price,
- expected supplier credit,
- platform margin,
- underquote risk.

Important: early alpha can keep prices simple. The learning data should be
captured now so pricing can become dynamic later.

## 16. Events and explainability

Every job should have an event stream.

Events:

- `quote_created`
- `job_created`
- `input_validated`
- `split_planned`
- `task_created`
- `task_claimed`
- `task_started`
- `task_failed`
- `task_requeued`
- `task_completed`
- `verification_started`
- `verification_mismatch`
- `tiebreak_dispatched`
- `hedge_dispatched`
- `budget_near_limit`
- `budget_paused`
- `job_cancelled`
- `results_merged`
- `job_completed`
- `invoice_settled`

Buyer-facing event text should be clear:

- "Chunk 18 OOMed on a 48 GB worker; retrying with smaller chunks on 64 GB
  workers."
- "No eligible workers are currently online for this memory requirement."
- "This job is paused before exceeding your $2.00 budget cap."
- "Verification found a disagreement; a third same-class worker is checking the
  chunk."

Operator-facing details can include raw IDs, SQL state, worker IDs, and failure
class.

## 17. Admin surfaces

Plane C needs operator views for:

- active quotes,
- stale quotes,
- quote-to-actual drift,
- underquoted jobs,
- overquoted jobs,
- jobs paused by budget,
- jobs paused by no supply,
- worker capacity by model,
- worker memory pressure history,
- routing decisions,
- task failures by class,
- failure hotspots by model,
- verification mismatch heatmap,
- supplier failure rate,
- queue forecast,
- price pressure.

These can be plain tables first. No heavy frontend needed.

## 18. Buyer surfaces

Buyer should see:

- quote summary,
- quote details,
- cost range,
- ETA range,
- risk warnings,
- recommended flags,
- budget cap,
- current spend,
- progress,
- event stream,
- failure reasons,
- partial result availability,
- final estimate vs actual.

CLI output should be compact but useful:

```text
Quote q_123
  Workload: embed, all-minilm-l6-v2
  Input: 500000 records, ~45M tokens, 180 MB
  Plan: 123 tasks, split_size=4096, batch tier
  Supply now: 8 eligible, 3 warm
  Cost: $0.041-$0.068 expected $0.052
  ETA: p50 10m, p90 20m
  Risk: low OOM, medium cold-start
  Suggested cap: --max-usd 0.08
  Submit: cx submit ...
```

## 19. What makes this defensible

The moat is not the quote endpoint by itself. The moat is the dataset and control
loop:

- real worker benchmarks,
- real completion times,
- real memory failures,
- real model warm/cold behavior,
- real buyer cancellation behavior,
- real supplier reservation prices,
- real verification outcomes,
- real queue/supply curves,
- real output-size distributions,
- real field availability data.

A cloud GPU provider has hardware telemetry, but not this exact network: idle
Apple Silicon supply, task-priced jobs, verification outcomes, and per-workload
cost curves. Plane C makes that data compound.

## 20. Workload expansion through Plane C

Plane C should be the gate for adding workloads. A new workload is not accepted
because it is interesting; it is accepted when Plane C can quote, bound, route,
verify, and explain it.

Candidate workloads:

- OCR,
- document parsing,
- vision embeddings,
- image classification,
- batch translation,
- batch summarization,
- synthetic data generation,
- eval batches,
- larger Whisper transcription,
- diarization,
- LoRA fine-tune,
- checkpoint evaluation,
- image generation,
- codebase embedding,
- codebase summarization,
- retrieval corpus cleanup.

For each workload, require:

- input scanner,
- unit estimator,
- memory estimator,
- output estimator,
- runner,
- verifier or trust policy,
- merge format,
- failure taxonomy,
- quote support,
- budget behavior.

If any one of those is missing, the workload is not ready.

## 21. Plane C and Plane B

Plane C should understand Plane B clusters, but not depend on them.

For clusters, Plane C quotes:

- summed usable memory,
- per-node margin,
- bottleneck link,
- shard plan,
- model fit,
- expected interconnect penalty,
- cluster warm/cold state,
- cluster failure/offline risk,
- same-class cluster redundancy availability.

Plane C should also protect clusters from bad routing:

- do not send small jobs to cluster workers,
- reserve clusters for jobs that need summed memory,
- charge cluster scarcity appropriately,
- require explicit buyer acknowledgement for expensive cluster jobs,
- surface that cluster execution is a reserved/high-memory tier.

Plane B provides capability. Plane C decides when using that capability is wise.

## 22. Plane C and verification

Verification should become quote-aware.

Quote should estimate:

- redundancy task count,
- honeypot task count,
- expected verification cost,
- expected verification delay,
- tiebreak probability from historical mismatch rate,
- same-class peer availability,
- payout hold time.

Runtime should adapt:

- increase redundancy for suspicious suppliers,
- lower redundancy for trusted repetitive low-risk work,
- never punish on inconclusive votes,
- surface mismatch events,
- track verification overhead as platform cost vs buyer cost.

Plane C does not invent a new verification primitive. It makes the existing trust
spine cost-aware and explainable.

## 23. Plane C and failure economics

The system needs to decide who pays for failure.

Buyer pays when:

- valid work completed,
- buyer cancels after completed chunks,
- buyer requested high redundancy,
- buyer input produced partial usable output by policy.

Buyer should not pay when:

- task OOMs before useful result,
- worker disappears before commit,
- model load fails,
- infrastructure fails,
- supplier fraud is detected,
- job never had eligible supply and is cancelled.

Supplier is credited when:

- task result passes verification,
- task was cancelled after successful commit due to unrelated downstream issue,
- platform chose extra verification and supplier did valid work.

Supplier is not credited or may be docked when:

- honeypot fail,
- confirmed mismatch after tiebreak,
- malicious malformed result,
- repeated unsupported backend claim,
- spoofed capability.

Platform absorbs or explicitly prices:

- hedging overhead,
- tiebreak overhead,
- quote error,
- infrastructure retries,
- early alpha manual interventions.

These rules should be explicit before real money volume.

## 24. Local-first build order

### Phase C0 - document and schema-only planning

- Land this document.
- Define quote response type in Go/Rust/Python docs.
- Decide failure taxonomy enum.
- Decide event enum.
- Decide budget state enum.
- No runtime behavior yet.

### Phase C1 - preflight and quote MVP

- Add `POST /v1/quote`.
- Add `cx quote`.
- Validate JSONL.
- Count records/bytes.
- Sample token-ish size with byte/token heuristic.
- Estimate cost with existing model catalogue.
- Estimate task count with current adaptive split.
- Use active worker count and queue depth.
- Return cost/ETA/risk as a conservative band.
- Persist quote assumptions.
- Tests for malformed input and stable quote shape.

### Phase C2 - immediate failure reporting

- Add `POST /v1/worker/task/{id}/fail`.
- Add `task_failures`.
- Agent reports typed failures instead of waiting for stale timeout.
- Control plane requeues/fails/refunds based on class.
- Buyer sees failure event.
- Tests for OOM-like retry and bad-input non-retry.

### Phase C3 - budget caps

- Add `max_usd` to job or budget table.
- Add projected spend check before dispatching new work.
- Pause/cancel before breach.
- Add job events.
- Add CLI/SDK budget support.
- Tests for cap enforcement.

### Phase C4 - adaptive chunk feedback

- Record per-task duration, tokens, bytes, failure reason.
- Shrink retry chunks on OOM/timeout.
- Grow future chunks for stable repetitive jobs.
- Add quote-to-actual drift.
- Tests for split adjustment.

### Phase C5 - Exchange Brain v1

- Record worker model state: warm/cold/failed.
- Record routing decision reasons.
- Add capacity admin endpoint.
- Prefer warm eligible workers.
- Add benchmark freshness.
- Add availability windows.
- Add queue forecast beyond active worker count.

### Phase C6 - product surface

- Buyer quote panel in the existing skeleton.
- Admin quote/routing/failure tables.
- Human-readable event stream.
- Invoice estimate-vs-actual explanation.

### Phase C7 - learned pricing and routing

- Use historical drift to adjust quotes.
- Use supplier reservation prices to estimate clearing price.
- Add scarcity-aware recommendations.
- Add SLA confidence bands.
- Add tier recommendation.
- Keep dynamic pricing manual-gated until real buyer data exists.

## 25. Tests and proof ledger

Plane C must be proven in the same style as the rest of Computexchange.

Unit tests:

- JSONL scanner,
- quote math,
- memory estimator,
- failure classifier,
- budget state machine,
- event rendering,
- split planner,
- route scorer.

Integration tests:

- quote endpoint with live Postgres,
- quote persists assumptions,
- job created from quote,
- budget cap pauses dispatch,
- fail endpoint requeues retryable task,
- fail endpoint fails buyer-bad-input task,
- quote-to-actual drift recorded,
- event stream ordered.

Live/local proof:

- submit a quoted embed job,
- verify estimate vs actual,
- force a synthetic task failure and immediate requeue,
- prove no 30-minute stale wait is required,
- prove budget cap stops new dispatch,
- prove results remain correct.

No Plane C claim should rely on a dashboard screenshot alone.

## 26. Non-goals

Plane C does not:

- run arbitrary code,
- replace the hard-filter scheduler,
- remove Postgres,
- become a full IDE,
- hide quote uncertainty,
- promise exact cost when sampling,
- fake memory telemetry,
- fake worker availability,
- auto-charge beyond budget,
- silently retry forever,
- silently drop failed chunks,
- mark partial output complete,
- dynamically price real buyers before the policy is reviewed.

## 27. Kill criteria

Plane C is not worth widening if:

- quotes are not materially more accurate than the current static estimate,
- buyers do not care about preflight or budget caps,
- most failures remain unclassifiable,
- OOM still waits for stale timeout,
- quote overhead makes small jobs annoying,
- the admin/operator burden grows faster than reliability improves,
- adding Plane C encourages arbitrary code execution.

If that happens, keep only the fail endpoint, budget cap, and quote MVP.

## 28. Open decisions

- Should quotes expire after 5 minutes, 15 minutes, or when supply changes
  materially?
- Should a buyer be able to reserve supply after quote?
- Should `max_usd` be required for all alpha jobs?
- Should verification overhead be buyer-visible or platform-internal at first?
- Should binary embedding output be a separate model/output mode or automatic
  above a size threshold?
- Should quotes be free, rate-limited, or counted against account limits?
- Should project scanning live in CLI only, or also server-side upload?
- Should token estimation use a real tokenizer per model or a byte heuristic at
  first?
- Should cold-start model downloads be included in ETA?
- Should a worker advertise warm model state on heartbeat?
- Should supplier reservation price affect public quote ranges?
- Should dynamic pricing wait until first real buyer data?
- Should budget cancellation produce partial results by default?

## 29. Long backlog

Buyer preflight:

- `cx quote`,
- `cx plan`,
- `cx doctor-input`,
- JSONL validation,
- CSV-to-JSONL hints,
- OpenAI Batch file validation,
- token estimate,
- output size estimate,
- model recommendation,
- tier recommendation,
- memory recommendation,
- verification recommendation,
- data residency warning,
- PII warning,
- malformed-line report,
- sample preview,
- submission manifest preview.

Budget:

- max spend,
- max retries,
- max wall time,
- max verification overhead,
- partial result policy,
- budget pause,
- budget cancel,
- spend events,
- invoice drift.

Failure:

- fail endpoint,
- failure enum,
- failure table,
- retry classifier,
- buyer-visible reasons,
- worker-visible reasons,
- operator dashboard,
- OOM immediate requeue,
- bad-input immediate fail,
- infra retry,
- fraud separation,
- no-supply pause.

Scheduler:

- long-poll wakeups,
- remove redundant start call,
- route scoring,
- warm-model preference,
- benchmark freshness,
- availability windows,
- model-state heartbeat,
- partial indexes,
- routing audit rows,
- queue forecast.

Storage:

- streaming split,
- streaming merge,
- binary embeddings,
- compression,
- result size caps,
- input size caps,
- presign TTL tuning,
- upload retry,
- object-store latency metrics.

Agent:

- typed failure reporting,
- memory-at-failure report,
- model warm state,
- model preload hints,
- local network test,
- cache size report,
- backend choice benchmark,
- MLX/llama.cpp comparison,
- continuous batching,
- per-model concurrency limits.

Admin:

- quote list,
- quote drift,
- capacity view,
- routing view,
- failures view,
- budget-paused jobs,
- no-supply jobs,
- worker model state,
- queue forecast,
- underquote report,
- supplier failure report.

Pricing:

- quote bands,
- clearing-price estimate,
- supplier reservation curves,
- tier recommendation,
- scarcity surcharge,
- cluster scarcity price,
- verification overhead accounting,
- platform margin report.

Plane B integration:

- cluster quote support,
- cluster scarcity,
- cluster warm/cold state,
- shard-plan surfaced in quote,
- interconnect bottleneck in ETA,
- cluster-only workload guard,
- avoid routing small jobs to cluster.

## 30. The first concrete build slice

The first build slice should be small and sharp:

1. Add `POST /v1/quote` with a conservative quote response.
2. Add `cx quote`.
3. Add JSONL scanner and record/byte/token-ish estimates.
4. Persist quote assumptions.
5. Add `POST /v1/worker/task/{id}/fail`.
6. Add `task_failures` and `job_events`.
7. Make the agent report typed execution failure.
8. Requeue retryable failure immediately.
9. Fail buyer-bad-input immediately.
10. Add tests and one proof-ledger row.

That slice alone makes Computexchange materially better than cloud rental: it
quotes before spend and stops waiting for stale timeout when a task cannot run.

## 31. Canonization test

Plane C should be canonized if this sentence feels true:

> Computexchange is not merely a place to send batch AI work; it is the system
> that tells you what the work will cost, whether it will fit, how it should be
> split, where it should run, when to stop, and why anything failed.

If that is the product direction, Plane C is the right next plane.

## 32. Status — first slice landed (Quote MVP)

Plane C is **canon-ready**: the engine already produces every signal a quote needs
(price catalogue, adaptive split, queue depth, live eligible supply with effective
memory, throttle state), so the autopilot is an assembly + persistence layer, not a
new framework. Phase C0 (this document) and the buyer-trust half of the §30 first
slice are now **landed and proven locally**:

- **`POST /v1/quote`** (`control/quote.go`) — scans the input and returns a
  conservative cost/ETA/supply/risk band WITHOUT spending or creating a job. It
  reuses the SAME estimators the real submission uses (`estimateJobUSD`,
  `adaptiveSplitSize`, `estimateETASecs`) so a quote and the eventual job agree.
- **JSONL preflight scanner** (`scanJSONL`, PLANE_C §8) — real record/byte counts,
  malformed detection + first-bad-line, max line size, a byte→token heuristic
  (surfaced as an estimate, never exact), and field-name inference. Pure + unit-tested.
- **Honest supply** — `eligible_workers_now` is a real count of workers that would
  pass the claim hard filter for THIS job (`EligibleWorkerCount`, same predicate as
  `ClaimTask`: supported job/model, effective memory ≥ need, not throttled, active).
- **Risk + confidence** — OOM risk from real eligible supply, cold-start honestly
  reported `medium` (warm-state untracked yet), a confidence score with structured
  reasons; malformed input and no-supply both surface buyer-visible warnings.
- **Persisted assumptions** (`quotes` table) — what the system believed at quote
  time, so a later invoice/drift view can compare (PLANE_C §6 rule).
- **`cx quote`** (compact band per §18) + **SDK `client.quote(...)`** (returns
  structured reasons, §5/§265).
- **Proven**: `TestScanJSONL*`, `TestAssessRisk*` (unit) ·
  `TestQuoteEndpointPersistsAssumptions` (integration, live Postgres) · a `quote`
  row in `make prove-local`.

Honest MVP boundaries (not faked): token count is a byte heuristic (§28 open
decision — a real per-model tokenizer is later); the per-task **memory estimator**
(§9) is not built yet (the quote surfaces the model's catalogue memory floor +
filters supply on effective memory, which is conservative); warm/cold model state
is untracked (Exchange Brain C5). The normalized `quote_inputs` /
`quote_supply_snapshot` tables (§6) are folded into `quotes.quote_json` for the MVP.

**Immediate next slice (the failure-prevention half of §30):** `POST
/v1/worker/task/{id}/fail` + `task_failures` + `job_events` + agent typed-failure
reporting + immediate retryable requeue (no 30-min stale wait) + buyer-bad-input
immediate fail. This is the structural fix for the RunPod "silent OOM, money kept
draining" failure mode (§4, §11) and is sequenced second only because it touches
the agent's live execution path (the quote slice is pure additive control-plane).

**Recommended doc refinement (from the audit):** §29's "partial indexes" should
name the highest-value one explicitly — a partial index on
`tasks(status, visible_at) WHERE status IN ('queued','retrying') AND claimed_by IS
NULL` directly accelerates the SKIP-LOCKED claim (the hottest query). It is cheap,
safe, and independent of the rest of Plane C.

## 33. Errata And Next Plane

Plane C now has an explicit errata ledger: [`docs/PLANE_C_ERRATA.md`](PLANE_C_ERRATA.md).
The errata keeps the unfinished safety debt visible: immediate failure reporting,
`task_failures`, `job_events`, quote-to-submit binding, budget enforcement, drift
measurement, warm/cold model state, and memory telemetry.

The queue-claim index called out above has been applied in `db/schema.sql` as
`tasks_ready_unclaimed_idx`. That is the first concrete local speed fix from the
errata.

The next frontier is Plane D: [`docs/PLANE_D.md`](PLANE_D.md). Plane D does not
replace Plane C; it stretches beyond the failure-prevention slice into local
queue speed, warm routing, memory telemetry, streaming/binary artifacts, budget
governance, proof-ledger expansion, and every advantage that can be built before
RunPod or a second Apple device is available.
