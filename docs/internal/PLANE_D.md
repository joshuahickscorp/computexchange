# Plane D - Local Advantage Engine

Plane D is the next frontier for Compute Exchange before RunPod, before CUDA
field tests, and before a second Apple device or Mac Studio.

It answers a sharper question:

> How much faster, safer, cheaper, and more self-improving can Compute Exchange
> become using only the local machine, the existing Go control plane, the Rust
> agent, Postgres, MinIO/S3, the CLI, the SDK, and the proof harness?

Plane D does not replace Plane C. Plane C is the intelligence and safety layer:
quote, preflight, budget, failure prevention, learned routing. Plane D is the
local advantage engine that turns those ideas into measurable speed and waste
reduction without waiting for external hardware.

## 0. Plane Map

Already landed:

- Plane A / Turbo: hardened queue, verification, scheduler, real workloads.
- Plane B: cluster seam, summed-memory routing, local proof boundary.
- Plane C first slice: Quote MVP, persisted assumptions, CLI and SDK quote.

Plane C carry-forward:

- immediate failure prevention,
- job events,
- task failures,
- budget enforcement,
- quote-to-submit binding,
- drift measurement.

Plane D adds:

- local queue acceleration,
- long-poll or wakeup-based dispatch,
- model warmth routing,
- agent memory telemetry,
- streaming and binary artifact paths,
- adaptive split feedback,
- local benchmark lab,
- event-driven observability,
- no-waste budget gates,
- proof-ledger expansion,
- operator/buyer surfaces that expose real system advantage.

## 1. One-Sentence Thesis

Plane D makes Compute Exchange locally better than raw cloud rental by removing
wasted waiting, wasted memory, wasted bytes, wasted retries, wasted claims, and
wasted buyer uncertainty.

## 2. Why This Plane Exists

The fail endpoint is mandatory. It fixes a severe failure mode.

But it is not enough.

If the next goal is only "build failure prevention," the project gets safer but
not dramatically more advantaged. Compute Exchange should not only avoid bad
cloud behavior. It should become better at running work than a buyer or provider
would be alone.

Plane D pushes the local system in every dimension that does not need external
hardware:

- faster job pickup,
- faster task completion,
- fewer stale windows,
- fewer bad assignments,
- better memory prediction,
- less JSON bloat,
- better warm model use,
- better split sizing,
- better quote accuracy,
- better queue depth accounting,
- better event trails,
- better proof discipline,
- better operator controls,
- better buyer caps.

## 3. Non-Negotiable Direction

Plane D must obey these rules:

- keep Postgres,
- keep the Go control plane,
- keep the Rust agent,
- do not rewrite frameworks for its own sake,
- do not create a full IDE,
- do not depend on RunPod,
- do not depend on the Mac Studio,
- do not depend on a second Apple device,
- do not fake distributed compute,
- do not make benchmark claims without proof rows,
- do not hide uncertainty from buyers,
- do not route jobs on soft hopes when hard filters can prove fit.

The system should get faster by becoming more precise, not by getting more
speculative.

## 4. What Local Means

Local does not mean small.

Local includes:

- local Postgres,
- local object storage,
- local control plane,
- local Rust agent,
- local Metal inference,
- local OpenAI-compatible batch path when configured,
- local CLI and SDK usage,
- local multi-agent simulation,
- local dashboard and app skeleton,
- local proof harness,
- local load tests,
- local backup/restore,
- local schema migration,
- local metrics.

Plane D only needs the current workspace and machine to make progress.

## 5. The Core Promise

Before external scale, Compute Exchange should be able to say:

> On one development machine, the exchange prevents bad spend, reports failures
> immediately, routes only feasible work, wakes workers efficiently, uses warm
> models when available, reduces payload overhead, learns from quote drift, and
> proves every claim in the ledger.

That is the local advantage engine.

## 6. D0 - Errata Closure

Plane D starts by closing Plane C errata.

Required:

- `POST /v1/worker/task/{id}/fail`,
- `task_failures`,
- `job_events`,
- typed agent failure reporting,
- immediate retryable requeue,
- terminal buyer-bad-input path,
- buyer-visible failure reason,
- stale reaper remains fallback,
- prove-local failure row.

Already applied as part of the errata:

- `tasks_ready_unclaimed_idx` partial expression index for the common ready-task
  claim path.

Why D0 matters:

- speed is not only throughput,
- speed is also time-to-knowledge,
- an OOM known in two seconds is better than an OOM discovered after a stale
  timeout,
- a buyer-bad-input failure should not consume supplier time,
- a retryable provider/system failure should not strand the task.

## 7. D1 - Queue Wakeups

Current shape:

- workers poll,
- scheduler claims via SKIP LOCKED,
- stale reaper catches old running tasks,
- tasks become visible based on status and `visible_at`.

Plane D should reduce idle delay.

Options:

- long-poll worker task endpoint,
- Postgres `LISTEN/NOTIFY` on new task or requeue,
- server-side wait with timeout,
- adaptive poll interval,
- queue-depth-aware poll pacing,
- worker backoff when no eligible task exists,
- immediate wake when a job is created.

First implementation:

- keep HTTP,
- add optional `?wait_ms=25000` to worker poll,
- control waits until work is likely available or timeout,
- worker uses long poll when supported,
- fallback to existing poll loop,
- prove that idle pickup time drops.

Important:

- do not add NATS,
- do not add Redis,
- do not replace Postgres,
- do not make local dev harder.

Proof rows:

- `long-poll` below a tight threshold,
- `long-poll-timeout` returns cleanly,
- old agent poll still works,
- new agent uses long poll,
- load test does not deadlock.

## 8. D2 - Queue Index And Claim Mechanics

The claim query is the hot path.

Already added:

- `tasks_ready_unclaimed_idx`

Next local improvements:

- explain-analyze the claim query in proof mode,
- record claim query latency histogram,
- cache eligible-worker metadata per heartbeat,
- cache worker benchmark summary,
- avoid recalculating expensive ranking signals on every claim,
- split "find candidate tasks" from "score task" only if measured,
- add a tiny query benchmark with 1k, 10k, 100k synthetic tasks.

Candidate indexes:

- ready unclaimed tasks,
- pinned tasks by `claimed_by`,
- job status/tier for scheduler ordering,
- benchmark latest by worker/job/model,
- worker capability arrays if they become large enough to matter.

Do not add every index up front. Plane D should add indexes that match observed
queries.

## 9. D3 - Model Warmth And Cache Intelligence

The fastest task is often the one that avoids loading a model.

Current shape:

- agent has a model pool,
- quote does not know warm state,
- scheduler does not prefer warm workers,
- quote cold-start risk is conservative.

Add:

- worker heartbeat includes loaded model ids,
- worker heartbeat includes model load timestamps,
- worker heartbeat includes approximate model memory footprint,
- control stores `worker_model_state`,
- quote reports warm eligible workers,
- scheduler gives warm workers a small preference,
- agent reports evictions and load durations.

Rules:

- warm preference must never override hard fit,
- warm preference must never route to throttled workers,
- warm preference must not starve other workers indefinitely,
- warm preference should be measurable.

Proof:

- warm worker chosen over cold worker when otherwise equal,
- cold worker chosen if warm worker is throttled,
- quote confidence improves when warm eligible supply exists,
- load duration appears in metrics/events.

## 10. D4 - Memory Telemetry And Peak Tracking

OOM prevention needs observed memory, not only catalogue memory.

Add agent telemetry:

- memory before model load,
- memory after model load,
- memory before task run,
- peak memory during task run if observable,
- memory after task run,
- model load delta,
- task delta,
- throttle reason at time of claim,
- failure memory snapshot.

Add control behavior:

- persist memory observations,
- expose memory fields in admin view,
- feed quote risk,
- feed per-task memory estimator,
- dock or pause workers that repeatedly fail memory-fit tasks,
- distinguish model-load OOM from input-size OOM.

Proof:

- fake memory report persists,
- OOM failure includes memory snapshot,
- quote risk changes when model floor plus observed task memory exceeds supply,
- agent tests cover typed memory failures.

## 11. D5 - Binary And Streaming Artifact Path

JSON is fine for control messages. It is not ideal for large embeddings.

Plane D should reduce artifact overhead without replacing the whole API.

Targets:

- embeddings output,
- large JSONL splits,
- merge path,
- result fetch path.

Options:

- JSONL for small jobs,
- binary float32 for large embeddings,
- newline-delimited metadata sidecar,
- compressed result objects,
- streaming upload of split chunks,
- streaming merge to avoid loading all results into RAM.

Rules:

- preserve simple JSON for small jobs and debugging,
- add binary only where it saves real bytes or memory,
- SDK should hide the format,
- proof should compare output shape and size.

Proof:

- binary embedding artifact is smaller than JSON for N rows,
- merge works without buffering all rows,
- SDK can read binary output,
- existing JSON output remains supported.

## 12. D6 - Adaptive Split Feedback

Current split sizing is heuristic.

Plane D should make it adaptive from actuals.

Inputs:

- quote scan,
- records,
- bytes,
- max line size,
- estimated tokens,
- job type,
- model,
- worker class,
- observed duration,
- observed memory,
- failure type,
- retry count,
- verification overhead.

Outputs:

- next recommended split size,
- split risk,
- memory risk,
- timeout risk,
- retry expectation.

First local version:

- keep current split as baseline,
- record actual task duration per split,
- compute basic p50/p90 duration by job type/model/split size,
- quote uses historical p90 when available,
- scheduler does not change until quote is stable.

Proof:

- historical duration table updates,
- quote ETA uses observed data when present,
- fallback works with no history,
- malformed or failed tasks do not poison the estimate.

## 13. D7 - Quote To Submit Contract

Quote is useful. Quote-to-submit is stronger.

Add:

- `quote_id` optional on job submit,
- quote expiration,
- quote input hash,
- quote model/tier/job type check,
- job stores quote reference,
- invoice reports quote vs actual.

Buyer value:

- "Here is what you were told before spending."
- "Here is why the actual changed."
- "Here is what the system prevented."

Proof:

- submit with valid quote succeeds,
- expired quote rejected or marked stale,
- changed model rejected,
- changed input rejected or forces requote,
- invoice includes quote delta.

## 14. D8 - Budget Governor

Quote suggests a cap. Plane D should enforce it.

Add:

- `max_usd` on job submit,
- projected remaining cost check before issuing task,
- budget warning event,
- budget stop event,
- cancelled-before-exceeding terminal state,
- invoice distinguishes completed spend from prevented spend.

Rules:

- never exceed buyer cap knowingly,
- verification and retries must count toward projected exposure,
- if remaining budget cannot cover the next task safely, stop before claim,
- admin can see cap decisions.

Proof:

- job stops before cap,
- completed partial work is billed correctly,
- no supplier payout is faked,
- events explain stop.

## 15. D9 - Event-Driven Buyer Trust

Status alone is not enough.

Add:

- `GET /v1/jobs/{id}/events`,
- CLI `cx events <job_id>`,
- SDK `job_events(job_id)`,
- dashboard event timeline,
- event types for quote, queue, claim, start, fail, retry, verify, complete, invoice.

Rules:

- events are append-only,
- events should be safe to show buyers,
- sensitive internal errors should be summarized,
- event payloads can have redacted details.

Proof:

- event row for quote-created or job-created,
- event row for task failure,
- event row for budget stop,
- CLI displays event timeline.

## 16. D10 - Local Benchmark Lab

The project needs a repeatable local benchmark harness separate from correctness
proof.

Add:

- `make bench-local`,
- synthetic JSONL generator,
- warm vs cold model benchmark,
- claim latency benchmark,
- split size benchmark,
- embed JSON vs binary size benchmark,
- quote latency benchmark,
- event write overhead benchmark,
- DB explain capture.

Output:

- markdown report,
- JSON artifact,
- timestamp,
- git commit if available,
- machine class,
- model cache state,
- p50/p90/p99.

Rules:

- never replace prove-local,
- benchmark should be optional,
- no external services required,
- results should be comparable over time.

## 17. D11 - Scheduler Explanation

The scheduler should be able to explain why work was or was not claimed.

Add:

- `/admin/scheduler/explain?worker_id=...`,
- reason counts for rejected work,
- no eligible task reason,
- memory mismatch reason,
- model mismatch reason,
- residency mismatch reason,
- throttled reason,
- tier trust reason,
- payout floor reason.

This helps local development because many "slow" states are actually "nothing
eligible."

Proof:

- worker with incompatible hardware gets clear reason,
- throttled worker gets clear reason,
- quote zero-supply reason matches scheduler explanation.

## 18. D12 - Supplier Efficiency

Provider advantage matters too.

Add:

- local idle time metric,
- useful work time metric,
- model load time metric,
- bytes downloaded/uploaded,
- thermal throttle time,
- memory throttle time,
- tasks declined by fit,
- tasks failed by category,
- payout per active hour estimate.

Supplier view should answer:

- Am I useful right now?
- Why am I idle?
- What model should I keep warm?
- What job types pay best for my machine?
- What is hurting my throughput?

No second Mac is needed to build this.

## 19. D13 - Control Plane Throughput

Targets:

- lower claim latency,
- lower quote latency,
- lower queue count overhead,
- fewer full scans,
- fewer repeated benchmark reads,
- more predictable p90.

Potential local changes:

- partial indexes,
- query latency logging,
- prepared statements only if measured,
- queue depth gauge updated on task transitions,
- cached worker capability snapshot,
- cached model catalogue in memory with invalidation,
- reduced JSON encoding allocations for hot endpoints,
- long poll.

Do not:

- replace Postgres,
- add a separate queue,
- add a framework,
- introduce a distributed coordinator.

## 20. D14 - Agent Loop Throughput

Targets:

- less startup delay,
- fewer model reloads,
- less memory churn,
- better typed errors,
- better cancellation checks,
- fewer object store round trips.

Local changes:

- long poll,
- warm model heartbeat,
- typed fail report,
- task cancellation check before expensive run,
- peak memory tracking,
- result streaming,
- chunk download streaming,
- graceful shutdown reports retryable failure for in-flight task.

Proof:

- cancellation before run avoids model work,
- graceful shutdown requeues immediately,
- typed OOM surfaces in job events,
- warm model route reduces second-task latency.

## 21. D15 - Data Shape And Payload Economy

Compute Exchange should avoid making the buyer pay for wasteful data shape.

Targets:

- reduce repeated metadata,
- reduce huge JSON arrays,
- compress artifacts where useful,
- keep exact row ordering,
- preserve debuggability.

Artifacts:

- embedding rows,
- classification rows,
- rerank rows,
- transcription outputs,
- JSON extraction outputs,
- invoice and event payloads.

First target:

- embeddings binary sidecar for large outputs.

Why first:

- embeddings can be very large,
- shape is regular,
- binary float32 is obvious,
- SDK can decode,
- JSON fallback can remain.

## 22. D16 - Local Reliability Drills

Add proof rows that break things on purpose:

- object store unavailable during result commit,
- worker exits mid-task,
- model load failure,
- malformed JSONL,
- budget cap exceeded,
- quote expired,
- bad quote id,
- DB restore after quote/failure/events,
- concurrent workers racing same queue,
- stale reaper after fail endpoint fallback.

These drills make Compute Exchange more credible than raw cloud because failures
are rehearsed.

## 23. D17 - Operator Surface

Minimal local UI additions:

- quote list,
- failure list,
- job event timeline,
- queue depth panel,
- claim latency panel,
- worker warmth panel,
- model load time panel,
- budget stopped jobs,
- quote drift table.

This does not need to be pretty first. It needs to be honest and useful.

## 24. D18 - Buyer Surface

Buyer-facing additions:

- quote detail,
- quote warnings,
- accepted quote,
- budget cap,
- event timeline,
- failure reason,
- retry reason,
- invoice delta,
- estimated vs actual.

The buyer should never wonder:

- Did my job start?
- Why is it waiting?
- What failed?
- Am I still being charged?
- Did the system stop before wasting more?

## 25. D19 - CLI Surface

Commands to add or expand:

- `cx quote`
- `cx submit --quote-id`
- `cx events <job_id>`
- `cx failures <job_id>`
- `cx bench-local`
- `cx explain-scheduler`
- `cx invoice`
- `cx tail <job_id>`

The CLI is important because it proves the product can be used headlessly when
the project goes to GitHub and another machine pulls it down.

## 26. D20 - SDK Surface

SDK additions:

- `quote()`
- `submit(quote_id=..., max_usd=...)`
- `events(job_id)`
- `invoice(job_id)`
- `failures(job_id)`
- binary embedding reader,
- typed exceptions for buyer-bad-input and budget-stopped jobs.

The SDK should make the smarter path easier than the raw path.

## 27. D21 - Metrics

Add metrics for:

- quote count,
- quote latency,
- quote malformed rate,
- quote zero-supply rate,
- quote-to-submit rate,
- claim latency,
- claim misses,
- long-poll timeouts,
- worker idle time,
- model load time,
- task failure count by category,
- immediate requeue count,
- budget stopped count,
- event write count,
- binary artifact bytes saved.

Every Plane D claim should become observable.

## 28. D22 - Proof Ledger Expansion

Add proof rows:

- `fail-endpoint-requeue`
- `fail-endpoint-bad-input`
- `job-events`
- `quote-submit-binding`
- `budget-cap`
- `long-poll`
- `warm-routing`
- `memory-telemetry`
- `binary-embeddings`
- `claim-index`
- `bench-local-smoke`

The proof ledger is part of the product. It is how the project resists vibes.

## 29. D23 - What To Avoid

Avoid:

- replacing Postgres before measuring,
- adding Kafka/NATS/Redis before long poll and indexes are exhausted,
- making a full IDE,
- building a huge UI before the event data is correct,
- adding dynamic pricing before quote drift exists,
- making binary protocol changes for tiny control messages,
- routing based on optimistic speed instead of hard fit,
- claiming cluster performance before the second-machine proof,
- hiding uncertainty to make the quote look cleaner.

## 30. D24 - Local Workload Expansion

Plane D can still expand workloads locally if each workload passes the gate.

Gate:

- quoteable,
- splittable,
- verifiable,
- memory bounded,
- failure typed,
- evented,
- budgetable,
- proofed.

Candidate local workloads:

- OCR,
- image embeddings,
- captioning,
- eval scoring,
- synthetic data,
- batch moderation,
- text dedupe,
- local rerank at larger scale.

Do not add workloads just because they are interesting. Add them when they deepen
the exchange advantage.

## 31. D25 - First Build Sequence

The first Plane D sequence should be:

1. Close Plane C errata with immediate fail endpoint.
2. Add `task_failures`.
3. Add `job_events`.
4. Add buyer-visible events endpoint.
5. Add agent typed fail reporting.
6. Add prove-local fail rows.
7. Add quote-to-submit binding.
8. Add budget cap enforcement.
9. Add long-poll worker task endpoint.
10. Add warm model heartbeat.
11. Add scheduler warm preference.
12. Add memory telemetry.
13. Add quote drift capture.
14. Add bench-local smoke.
15. Add binary embeddings for large outputs.

That is the local frontier. None of it requires RunPod or another Apple device.

## 32. D26 - Stretch Backlog

Long backlog:

- claim query explain capture,
- synthetic queue generator,
- scheduler no-eligible explanation,
- queue-depth transition gauge,
- worker idle reason tracking,
- worker useful-work ratio,
- task event timeline,
- event tailing CLI,
- event stream endpoint,
- quote expiration policy,
- quote input hash,
- quote acceptance state,
- quote drift rollup,
- quote confidence calibration,
- quote warning taxonomy,
- budget warning threshold,
- budget hard stop,
- partial invoice for stopped jobs,
- retry budget,
- task timeout policy by job type,
- model-load timeout policy,
- per-job-type memory formula,
- per-model load memory observation,
- per-worker peak memory report,
- thermal slowdown report,
- warm model cache state,
- warm model route bonus,
- warm model quote confidence,
- model eviction events,
- graceful shutdown fail report,
- cancellation before model load,
- cancellation during long job,
- object store retry classification,
- object store streaming upload,
- object store streaming download,
- embedding binary format,
- embedding binary SDK reader,
- embedding JSON fallback,
- streaming merge,
- merge memory benchmark,
- result size metric,
- CLI `cx tail`,
- CLI `cx events`,
- CLI `cx failures`,
- SDK event iterator,
- dashboard event timeline,
- admin failure table,
- admin quote drift table,
- admin worker warmth table,
- admin queue latency panel,
- local benchmark report,
- benchmark history artifacts,
- proof ledger trend,
- failure drill matrix,
- backup/restore with events,
- restore quote history,
- restore task failures,
- restore binary artifacts,
- long-poll compatibility test,
- long-poll timeout metric,
- worker protocol version negotiation,
- SDK typed errors,
- invoice quote delta,
- invoice budget stop,
- supplier payout per active hour,
- supplier idle reason,
- supplier recommended warm model,
- buyer prevented-spend summary,
- buyer bad-input preflight hints,
- model catalogue memory audit,
- model catalogue throughput audit,
- stale reaper fallback proof,
- retry storm prevention,
- idempotent fail endpoint,
- idempotent event writes,
- task failure dedupe,
- claim fairness audit,
- priority starvation guard,
- no-supply alert,
- high-OOM-risk alert,
- quote zero-supply history,
- local-only release checklist.

## 33. D27 - Success Criteria

Plane D is successful when all of these are true:

- a failed task reports in seconds,
- a buyer can see why a job failed,
- retryable failures requeue without waiting for stale timeout,
- buyer-bad-input failures stop cleanly,
- quote assumptions bind to job submission,
- budget caps are enforced,
- worker pickup latency improves locally,
- warm model routing improves second-task latency,
- memory telemetry feeds quote risk,
- large embedding artifacts have a lower-overhead path,
- proof ledger grows beyond 100 meaningful rows,
- no external hardware is required for the improvements.

## 34. Canonization Test

Plane D should be canonized if this sentence feels true:

> Compute Exchange is not waiting for bigger hardware to become better; it is
> becoming smarter, faster, and safer on the hardware it already has.

If that is the direction, Plane D is the right next plane.

## 35. Status — D0 landed (errata closed)

The first Plane D sequence (§31) is underway. **D0 — errata closure — is landed and
proven locally**, closing the Plane C errata (docs/PLANE_C_ERRATA.md §6):

- **Immediate typed fail endpoint** — `POST /v1/worker/task/{id}/fail`
  (`control/failure.go`): worker-authed, only the claiming worker, **idempotent**.
  Retryable provider/system failures requeue in ~5 s (not the 30-min reaper);
  buyer-bad-input fails terminally + refunds — both in one transaction so the
  timeline and the requeue can never disagree. The stale reaper stays the fallback.
- **Shared failure taxonomy** — one vocabulary across `control/failure.go`
  `failureClasses` ⇄ `agent/src/failure.rs` `classify` (a unit test guards each
  side); `retryable` + `buyer_fault` drive requeue-vs-terminal-refund.
- **`task_failures`** (with the real memory snapshot at failure) + **`job_events`**
  (append-only buyer timeline) tables + indexes.
- **Buyer-visible failure** — `GET /v1/jobs/{id}/events` + `GET /v1/jobs/{id}/failures`,
  `cx events` / `cx failures`, SDK `events()` / `failures()`. Events emitted:
  `job_created`, `task_failed`, `task_requeued`, `job_failed`, `job_completed`.
- **Agent typed reporting** — `agent/src/failure.rs` maps each `RunError` to the
  taxonomy (S3 errors → `object_store_error`, low-memory inference → `oom`) and
  attaches a real memory snapshot; reported on the execute-task error path.
- **D2 claim-path index, proven** — the errata's `tasks_ready_unclaimed_idx` (partial
  expression index on the hot ready-task claim predicate) is applied + valid, asserted
  deterministically by the `claim-index` proof row; `make bench-local` shows the planner
  choosing it under a synthetic queue (plan choice on a tiny live table is stats-
  dependent, so the proof gate asserts validity, not a one-off plan pick).
- **Proof rows**: `matrix:TestFailEndpointRequeuesImmediately`,
  `matrix:TestFailEndpointBadInputTerminal`, `matrix:TestFailEndpointOnlyClaimingWorker`,
  `matrix:TestFailureTaxonomyClassification`, agent `failure::tests::*`, live
  `job-events` + `claim-index`.

## 36. Status — D1–D15 LANDED (the local frontier, proven)

The remaining first-build sequence (§31) is now landed and proven by `make
prove-local`, each with its own proof row:

- **Budget governor (D8)** — a hard `max_usd` cap that PREVENTS dispatch before a
  job breaches it (`control/scheduler.go` claim gate); projected spend counts
  already-charged + IN-FLIGHT (running, uncommitted) tasks + the candidate, so the
  cap holds under the agent's bounded concurrency. Emits `budget_warning` /
  `budget_stopped`; the cap never charges or refunds. Proof: `budget-cap`,
  `matrix:TestBudgetCapPausesDispatch`, `matrix:TestBudgetCapCountsInflightTasks`.
- **Quote-to-submit binding (D7)** — `POST /v1/jobs` accepts a `quote_id` (expiry +
  input-hash + model/tier/job-type checked, else 409); the invoice carries
  quoted-vs-actual. Proof: `quote-submit-binding`, `matrix:TestQuoteBindingMatchAndExpiry`.
- **Quote-to-actual drift (D6)** — committed task durations persist (`task_durations`);
  the quote's ETA leans on the observed p90 when history exists; `GET /admin/drift`.
  Proof: `quote-drift`, `matrix:TestTaskDurationRecorded`.
- **Warm-model routing (D3)** — the agent reports warm model ids on the heartbeat
  (`loaded_models`), control persists `worker_model_state`, the scheduler gives a
  small warm re-rank bonus (the hard filter is UNCHANGED), and the quote reports
  `warm_eligible_workers` + drops cold-start risk. Proof: `warm-routing`,
  `matrix:TestWorkerModelStateUpsert`.
- **Long-poll dispatch (D1)** — `GET /v1/worker/poll?wait_ms=N` parks server-side
  (each attempt its own claim, context-cancellable, no held tx) and the agent
  requests it; old no-wait polling is unchanged. Proof: `long-poll`,
  `matrix:TestLongPoll{ReturnsOnNewTask,TimesOutCleanly}`, `matrix:TestPollNoWaitUnchanged`.
- **Memory telemetry (D4)** — heartbeats append `worker_memory_samples`;
  `GET /admin/workers` surfaces avg available memory; the quote's OOM risk reads the
  median effective memory of eligible workers. Proof: `memory-telemetry`,
  `matrix:TestMemorySampleRecorded`.
- **Scheduler explain (D11)** — `GET /admin/scheduler/explain?worker_id=` attributes
  why a worker is (not) getting work, per hard-filter reason. Proof: `scheduler-explain`,
  `matrix:TestSchedulerExplain`.
- **Binary embeddings (D5/D15)** — the agent emits a compact `CXEM` float32 artifact
  for large embed jobs (JSON stays the default + fallback); the SDK decodes it.
  Proof: `binary-embeddings`.
- **CLI/SDK surfaces (D19/D20)** — `cx invoice` / `cx explain-scheduler` /
  `cx submit --quote-id --max-usd`; SDK `invoice()`, `submit(quote_id=, max_usd=)`,
  typed `BudgetStoppedError`/`BadInputError`, binary-embedding reader. Proof:
  `cli-surfaces`.
- **Metrics (D21)** — `cx_quotes_total`, `cx_budget_stops_total`,
  `cx_long_poll_timeouts_total`. Proof: `metrics-planed`.
- **Bench-local lab (D10)** — `make bench-local` (quote latency, claim-query plan,
  JSON-vs-binary embed size) writing `.artifacts/bench-local/report.md`. SEPARATE
  from `prove-local`, never replaces it.

This wave was built by a sequential agent pipeline and then integration-hardened: the
adversarial review + the live proof gate caught and fixed a budget concurrency
under-count, a claim-path no-work regression, a quote-binding test bug, and two
atomicity gaps before anything was claimed proven. The proof ledger grew past
**100 → ~129 rows** (§33 milestone). The §33 success criteria are now met on one
machine: failures report in seconds, the buyer sees why, retryable work requeues
without the stale wait, bad input stops cleanly, quotes bind to submission, budget
caps are enforced, warm routing improves second-task latency, memory telemetry
feeds quote risk, and large embeddings have a lower-overhead path — no external
hardware required.

**Beyond D15 (stretch backlog, §32):** the longer D16–D27 list (reliability drills,
operator/buyer dashboards, learned pricing, more workloads) remains the documented
forward path, each gated on its own proof rows.

