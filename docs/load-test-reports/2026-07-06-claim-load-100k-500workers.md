# Scheduling & Matching 6→6.5 — claim-load report (100k-task queue, 500 workers)

> **Facet:** Scheduling & Matching Engine 6→6.5 ("Load-prove the real claim path"
> — `docs/internal/CREED_AND_PATH_TO_TEN.md`). A real claims/sec + p50/p90/p99
> report at the rung's stated scale: a **100,000-task queue** behind **500
> registered workers**, driven by **concurrent pollers** hitting the EXACT shipped
> `Store.ClaimTask` (the `/v1/worker/poll` hot path), against a REAL Postgres.
>
> This supersedes the 50k-task report (`2026-07-05-claim-load-50k.md`) at a larger
> scale AND after the Control Plane Hot Path 8→9 fix (per-job hoisting of
> `cheaper_class_online` / `worker_tps` / `warm_for_task` / `job_dispatched_count`).
> Measured 2026-07-06, PostgreSQL 17.10, Apple M3 Pro (12 cores).

## Harness (real, committed, reproducible)

`control/hotpath_wave_test.go` · `TestClaimLoad100kConcurrent`, gated behind
`CX_CLAIM_LOAD=1`. It:

1. Seeds **50 active suppliers, 500 workers** (varied `hw_class` across all cost
   ranks so `cheaper_class_online` has real candidates), a `worker_tps_cache` +
   `worker_model_state` row for a realistic fraction, and **100,000 queued tasks**
   spread across ~2,000 fifty-task jobs (a third capped, so the budget-governor
   subquery runs for real; not one giant job, so fairness/`job_dispatched_count`
   has per-job variety).
2. Drives **50 concurrent poller goroutines**, each a DISTINCT registered worker
   identity, each calling the real `Store.ClaimTask` in a tight loop for a fixed
   **60 s** window — genuine fleet-wide contention, not one worker in a loop.
3. Records per-claim wall time (the same measurement `cx_claim_duration` captures
   in production) and computes fleet-wide claims/sec + p50/p90/p99.

Reproduce:

```
CX_CLAIM_LOAD=1 CX_CLAIM_POLLERS=50 CX_CLAIM_WINDOW=60 \
  go test -tags integration -run TestClaimLoad100kConcurrent -v -timeout 300s ./control
```

## Headline result

| queue depth | workers | pollers | window | claims | claims/sec | claim p50 | p90 | p99 |
|-------------|---------|---------|--------|--------|-----------|-----------|-----|-----|
| 100,000 | 500 | 50 | 60 s | 1,509 | **25.1/s** | 1,526 ms | 3,326 ms | 3,890 ms |
| _(50k report, pre-fix)_ | 50,000 | 100 | 90 s | 209 | _1.95/s_ | _23,678 ms_ | _25,068 ms_ | _29,798 ms_ |

**The Control Plane Hot Path 8→9 fix moved this materially.** At a 2× larger
queue (100k vs 50k) the fleet-wide throughput is **~13× higher** (25/s vs 1.95/s)
and the p99 claim latency is **~7.7× lower** (3.9 s vs 29.8 s), with **zero
client timeouts** (the 50k run timed out 48% of its 100-poller requests at 30 s).
The specific mechanism the 50k report root-caused — `cheaper_class_online`
sequentially scanning `workers` once per candidate row, widening the `ORDER BY`
sort and spilling it to disk — is gone: all four per-job ordering signals now
compute once per candidate job (`eligible_jobs AS MATERIALIZED`), so the sort is
narrower and the per-claim fleet rescan is eliminated (proven flat in
`docs/bench-local-reports/2026-07-06-claim-flatness-cheaper-class-online-per-job.md`:
loaded:near-empty ratio 190× → 8.95×).

## The honest residual: the LIMIT-1 sort over the candidate set

The remaining per-claim cost — p50 ~1.5 s under 50-way concurrency at 100k depth
— is the `ORDER BY … LIMIT 1` sort over the candidate task set. To pick the
single best task, Postgres materializes and sorts every claimable task of every
eligible job; at 100k rows that sort exceeds the default `work_mem` (4 MB) and
spills, and 50 concurrent sessions contend for that bounded `work_mem` and the
integration pool's connections (pgx default ~12). This is a fundamentally cheaper
cost class than the removed O(queue × fleet) fleet-rescan — an O(n log n) sort,
not a per-row workers-table scan — but it is still O(queue), and it is the next
thing to attack if 100k-deep single-tenant backlogs become a real production
condition. Two known, un-taken levers (named, not silently ignored):

- **`work_mem`**: raising it from the 4 MB default keeps the sort in memory. This
  is an operational tuning knob (the 50k report already flagged the 4 MB spill),
  not a code change, and was deliberately left at the default here so the number
  is honest about the shipped configuration.
- **Rank jobs, then pick the oldest task of the winning job**: since every
  ordering signal except `t.created_at` is now per-job, the optimal task is the
  oldest task of the best-ranked job — a rewrite that would replace the 100k-row
  sort with a (small) job-rank sort + an indexed oldest-task lookup. This is a
  larger change with its own equivalence-proof burden and is scoped as a separate
  follow-up, not claimed here.

## Environment (for an adversarial re-run)

- PostgreSQL 17.10, `work_mem=4MB` (default), `max_connections=100`.
- Integration connection pool: pgx default `MaxConns` (~12 on a 12-core box) —
  production sets `MaxConns=20` (`main.go`); a larger pool would raise the
  concurrency ceiling before pool-queuing dominates.
- Apple M3 Pro, 12 cores, macOS. Throwaway native Postgres + MinIO.
- The exact seeded fleet/queue shape and poller loop are in
  `control/hotpath_wave_test.go` (`TestClaimLoad100kConcurrent`), committed — not
  throwaway `.artifacts/` harness code, so this is re-runnable verbatim.
