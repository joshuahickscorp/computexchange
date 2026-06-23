# Plane C Errata - What The Quote MVP Did Not Finish

This file is the correction ledger for Plane C.

Plane C was intentionally broad: Compute Autopilot plus Exchange Brain. The
Quote MVP landed the first buyer-trust surface, but it did not finish the plane.
Calling the next step only "build the failure endpoint" is accurate but too
small. The endpoint is a required fix, not the whole frontier.

The purpose of this errata is to keep the missed pieces visible while Plane D
pushes the project forward.

## 1. Errata Summary

Plane C currently has:

- `POST /v1/quote`
- `cx quote`
- SDK `quote()`
- JSONL preflight scan
- persisted `quotes`
- eligible-supply count
- cost/ETA/risk/confidence band
- prove-local coverage

Plane C does not yet have:

- immediate task failure reporting,
- typed agent failure categories,
- `task_failures`,
- `job_events`,
- buyer-visible failure history,
- enforced budget caps,
- quote-to-submit binding,
- quote expiration,
- quote-to-actual drift tracking,
- warm/cold model state,
- learned routing decisions,
- a real per-task memory estimator,
- streaming/binary artifact paths,
- queue-wait calibration from actuals,
- supply forecasting,
- live claim-path observability,
- operator quote/failure/drift tables.

So the correction is:

> Plane C is canon-ready as a direction, but not complete as a system. The Quote
> MVP is the first landed slice. The failure-prevention slice remains mandatory,
> and the larger speed/efficiency frontier belongs in Plane D.

## 2. Fix Applied In This Errata

The Plane C audit called out a missing low-risk speed fix: the hot Postgres
claim path needed a partial index for ready, unclaimed work.

That fix is now applied in `db/schema.sql`:

```sql
CREATE INDEX IF NOT EXISTS tasks_ready_unclaimed_idx
ON tasks (status, (COALESCE(visible_at, created_at)), created_at)
WHERE claimed_by IS NULL AND status IN ('queued','retrying');
```

Why this matters:

- The scheduler's core claim query filters queued/retrying tasks.
- It only wants tasks whose `visible_at` window is open.
- The common case is unclaimed work.
- The query orders by oldest created task after tier logic.
- Workers can poll concurrently through `FOR UPDATE SKIP LOCKED`.

The old `tasks_status_visible_idx` remains for compatibility and simpler query
shapes. The new partial expression index is more exact for the normal ready-work
path and keeps the index smaller than a full tasks index.

This does not replace the need for runtime metrics. It is just a cheap, safe,
local speed improvement.

## 3. Required Plane C Carry-Forward

Plane C cannot be considered operationally complete until these are built.

### C-Errata-1: Immediate Fail Endpoint

Add:

```text
POST /v1/worker/task/{id}/fail
```

Required behavior:

- worker-authenticated,
- only the claiming worker can fail the task,
- accepts structured failure type,
- records a `task_failures` row,
- records a `job_events` row,
- retryable failures requeue immediately,
- non-retryable buyer-input failures fail cleanly,
- stale reaper remains fallback only,
- buyer can see what happened without reading logs.

This is the structural fix for silent OOM and money drain.

### C-Errata-2: Failure Taxonomy

The system needs one shared vocabulary across agent, control, CLI, SDK, and docs.

Initial categories:

- `oom`
- `model_load_failed`
- `unsupported_model`
- `unsupported_job_type`
- `bad_input`
- `bad_jsonl`
- `timeout`
- `cancelled`
- `worker_shutdown`
- `thermal_throttle`
- `transient_io`
- `object_store_error`
- `internal_error`
- `verification_failed`

Each category must declare:

- retryable or terminal,
- buyer fault or system/provider fault,
- chargeable or non-chargeable,
- reputation effect,
- event visibility,
- whether input should be redacted.

### C-Errata-3: Job Events

Plane C needs a timeline.

Minimum event types:

- `quote_created`
- `job_created`
- `task_queued`
- `task_claimed`
- `task_started`
- `task_failed`
- `task_requeued`
- `task_completed`
- `verification_started`
- `verification_failed`
- `job_failed`
- `job_completed`
- `budget_warning`
- `budget_stopped`
- `invoice_created`

The buyer should not need to infer state from status fields alone.

### C-Errata-4: Quote-To-Submit Binding

The quote endpoint currently creates an advisory quote. That is useful, but not
complete.

Add:

- `quote_id` accepted by `POST /v1/jobs`,
- quote expiration,
- quote model/tier/job-type compatibility check,
- quote input hash or object reference check,
- job row stores `quote_id`,
- invoice can compare quoted vs actual.

The product promise is stronger when a buyer can say:

> I accepted this quote, this is what changed, and this is why the invoice
> differs.

### C-Errata-5: Budget Enforcement

The Quote MVP suggests a `max_usd`; it does not enforce one.

Required next state:

- buyer submits `max_usd`,
- scheduler estimates projected remaining cost before issuing more tasks,
- job pauses or fails safe before exceeding cap,
- budget stop is visible in `job_events`,
- invoice distinguishes completed spend from prevented spend.

The user-facing difference is huge: raw cloud can keep draining; Compute Exchange
should stop.

### C-Errata-6: Drift Measurement

Persisting quote assumptions only matters if actuals are compared later.

Add:

- quoted records vs completed records,
- quoted cost vs actual cost,
- quoted ETA vs actual duration,
- quoted eligible supply vs claimable supply at runtime,
- OOM risk prediction vs failure reality,
- malformed input warnings vs actual bad-input failures.

This is how the Exchange Brain starts learning.

### C-Errata-7: Warm/Cold State

The quote honestly reports cold-start risk as medium because warm state is not
tracked. That honesty is good, but it is not enough.

Add:

- worker-model warm cache state,
- last loaded model,
- load time,
- estimated memory occupied by model,
- eviction reason,
- warm-model preference in routing when safe,
- quote confidence improvement when warm supply exists.

This is a local-only speed win. It does not require another Mac.

### C-Errata-8: Memory Estimator

The quote uses catalogue model memory floor and effective worker memory. That is
conservative, but it does not model input-driven memory pressure.

Add:

- per-job-type memory formula,
- line-size risk,
- prompt-size risk,
- batch-size risk,
- audio duration risk,
- redundancy multiplier,
- model-load plus activation memory estimate,
- observed peak memory feedback from agent.

The point is not perfect prediction. The point is to fail before spend when a job
is obviously impossible.

## 4. What Moves To Plane D

Plane D is not "more docs because we ran out of work."

Plane D is the local frontier:

- make the current system faster,
- waste less work,
- reduce idle time,
- tighten queue mechanics,
- reduce payload overhead,
- make every run teach the next run,
- improve buyer trust without needing RunPod,
- improve provider efficiency without needing the Mac Studio.

Plane D should absorb this errata as its first phase, then keep going.

## 5. Acceptance For Closing This Errata

This errata can be closed when:

- the partial claim index is applied by schema migration,
- `POST /v1/worker/task/{id}/fail` exists,
- `task_failures` exists,
- `job_events` exists,
- agent reports typed failures,
- retryable failures requeue immediately,
- buyer-bad-input fails immediately,
- job status/events make failure understandable,
- prove-local includes at least one immediate-fail row,
- Plane D has started with a local-only speed slice beyond failure prevention.

Until then, Plane C is directionally canonized but operationally incomplete.

## 6. Status — D0 failure-prevention slice LANDED

The §5 closing criteria are met by the Plane D D0 slice (docs/PLANE_D.md §6),
proven locally:

- [x] partial claim index applied (`tasks_ready_unclaimed_idx`) — **applied + valid**,
      asserted deterministically by the `claim-index` proof row; `make bench-local`
      shows the planner choosing it for the ready-task claim under a synthetic queue.
- [x] `POST /v1/worker/task/{id}/fail` — `control/failure.go` `handleWorkerFail`;
      worker-authed, only the claiming worker, idempotent.
- [x] `task_failures` table (+ `task_failures_job_idx`).
- [x] `job_events` table (+ `job_events_job_idx`) + `GET /v1/jobs/{id}/events`
      (buyer-scoped) + `cx events` + SDK `events()`. Events emitted: `job_created`,
      `task_failed`, `task_requeued`, `job_failed`, `job_completed`.
- [x] agent reports typed failures — `agent/src/failure.rs` maps `RunError` → the
      shared taxonomy + a REAL memory snapshot; `protocol.fail_task`; wired in the
      execute-task error path.
- [x] retryable failures requeue immediately (~5 s backoff, not the 30-min reaper);
      buyer-bad-input fails terminally + refunds — both in one transaction.
- [x] failure understandable to the buyer — `cx failures` / SDK `failures()` +
      the event timeline; `task_failures.memory` carries the OOM snapshot.
- [x] prove-local rows: `matrix:TestFailEndpointRequeuesImmediately`,
      `matrix:TestFailEndpointBadInputTerminal`, `matrix:TestFailEndpointOnlyClaimingWorker`,
      live `job-events`, `claim-index`.
- [x] Plane D started beyond failure prevention — the `claim-index` speed
      proof; the rest of the sequence (long-poll, warm routing, memory telemetry,
      budget governor, quote binding, binary embeddings, bench-local) is the
      documented D1–D15 backlog in docs/PLANE_D.md.

The shared taxonomy is the same vocabulary on both sides: `control/failure.go`
`failureClasses` ⇄ `agent/src/failure.rs` `classify` (a unit test on each side
guards it). The stale reaper remains the fallback for workers that die without
reporting.

**Errata closed.** Plane C is now operationally complete for the failure-prevention
spine; the larger speed/efficiency frontier continues in Plane D.

