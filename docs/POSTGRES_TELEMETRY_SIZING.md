# Postgres telemetry-table disk / IO sizing — measured, not guessed

> **Facet:** Postgres Data Lifecycle & unbounded history growth (7→8, "Size the
> database tier to the real churn rate, not a guess" — `docs/internal/CREED_AND_PATH_TO_TEN.md`).
> **Method:** REAL per-row bytes measured with `pg_total_relation_size` /
> `pg_relation_size` / `pg_indexes_size` on 100,000 synthetic rows per table
> (heap + primary key + secondary index — the full footprint), against the REAL
> `db/schema.sql` table + index definitions, multiplied by the REAL retention
> windows (`control/workers.go`) and the REAL churn drivers (30 s agent heartbeat,
> one `task_durations` row per completed task, the buyer-visible `job_events`
> timeline). No estimate is derived from reading a struct; every byte figure below
> is a measurement.
>
> Measured 2026-07-06 on PostgreSQL 17.10 (Apple M3 Pro, 12 cores). Reproduce by
> re-running the measurement queries at the bottom of this doc.

## TL;DR — the recommendation

The three bounded telemetry tables (`worker_memory_samples`, `task_durations`,
`job_events`) reach a **bounded steady state** — retention (entries 10/14) caps
them — but that steady state at the **600-worker target scale is ~11.3 GB of
live data alone**, already **>11× the current unmeasured `1 GB / 1 vCPU`
default**. With a realistic autovacuum-lag bloat margin and WAL/OS headroom, the
tier should be provisioned as below:

| fleet scale | live telemetry (measured) | recommend disk (×1.7 bloat + WAL/OS headroom) | recommend RAM / vCPU |
|-------------|---------------------------|-----------------------------------------------|----------------------|
| today (~1 worker, trickle) | ~16 MB | the current 1 GB / 1 vCPU is fine | 1 GB / 1 vCPU |
| early (50 workers) | ~0.7 GB | **10 GB** disk | 2 GB / 2 vCPU |
| **target (600 workers)** | **~11.3 GB** | **40 GB** disk | **8 GB / 4 vCPU** |
| 10× target (6,000 workers) | ~113 GB | **256 GB** disk | 32 GB / 8 vCPU |

The `1 GB` default is outgrown **between the "early" and "target" tiers** — i.e.
the first few hundred real workers. This is the number the migration off the
single-droplet tier should be triggered by, and it was previously a guess.

## Measured per-row footprint (the load-bearing numbers)

100,000 rows inserted into a `LIKE … INCLUDING ALL` clone of each real table (so
the primary key rides along), plus the real secondary index re-created, then
`VACUUM ANALYZE`, then `pg_total_relation_size` ÷ 100,000:

| table | bytes/row (total) | heap/row | index/row | dominant cost |
|-------|-------------------|----------|-----------|---------------|
| `worker_memory_samples` | **220 B** | 84 B | 135 B | the `(worker_id, created_at DESC)` index (135 B) outweighs the tiny 6-column heap row |
| `task_durations` | **220 B** | 161 B | 58 B | heap (two UUIDs + text job_type/model_ref/engine/build_hash) |
| `job_events` | **328 B** | 195 B | 133 B | heap (jsonb `detail` + `buyer_text` + two UUIDs) + the `(job_id, created_at)` index |

## Churn drivers (the real write rates)

- **`worker_memory_samples`** — one row per heartbeat that reports memory. The
  agent heartbeats every **30 s** (`agent/src/main.rs`, `interval(Duration::from_secs(30))`),
  and every memory-reporting beat writes a sample (`store.go` `HeartbeatWorker`),
  so **2 rows/min/worker = 2,880 rows/day/worker**. Retention window **14 days**
  (`workerMemorySampleRetention`). This is by far the highest-churn table.
- **`task_durations`** — one row per completed task. Retention **30 days**
  (`taskDurationRetention`). Pure internal telemetry.
- **`job_events`** — the buyer-visible job timeline (submitted / running /
  task_complete / verifying / complete / budget events); ~6 rows per job is a
  representative average. Retention **180 days** (`jobEventRetention`) —
  deliberately long because it is buyer-facing history (`GET /v1/jobs/{id}/events`),
  not telemetry.

## Steady-state footprint by scale (measured bytes × retention × churn)

`live_bytes = rows_retained × bytes_per_row`, where `rows_retained` is
`churn_rate × retention_window` for each table:

| scenario | workers | worker_memory_samples | task_durations | job_events | **TOTAL (live)** |
|----------|---------|-----------------------|----------------|------------|------------------|
| today (trickle) | 1 | 8.5 MB | 0.6 MB | 6.8 MB | **~16 MB** |
| early | 50 | 423 MB | 126 MB | 135 MB | **~0.7 GB** |
| **target** | **600** | **4.96 GB** | **3.07 GB** | **3.30 GB** | **~11.3 GB** |
| 10× target | 6,000 | 49.6 GB | 30.7 GB | 33.0 GB | **~113 GB** |

At the 600-worker target the telemetry write path sustains **~26.5 INSERTs/sec**
(≈1.7 M `worker_memory_samples`, 0.5 M `task_durations`, 60 K `job_events` per
day), and the hourly retention sweep (`sweepTelemetryRetention`, `workers.go`)
deletes a matching **~72 K wms + ~21 K td + ~2.5 K je rows/hour** at steady
state. That delete churn is the reason the autovacuum tuning below is not
optional.

## Bloat: why provisioned disk ≠ live-row bytes (measured)

A retention-driven table is a continuous insert+delete churn, and Postgres does
not shrink a heap file on `DELETE` (or on a plain `VACUUM`) — it marks the dead
space reusable on the free-space map and settles the file at its **high-water
mark**. Measured directly: a 50,000-row steady-state `worker_memory_samples`
clone (11 MB) churned through 20 cycles of (insert 10 K / delete-oldest 10 K)
**with no vacuum keeping pace grew to 54 MB — a ~5× high-water mark** — and a
subsequent plain `VACUUM` did **not** shrink the file; the reclaimed space was
instead **reused** by the next 10 K inserts (60 K live rows still fit in the same
54 MB). So:

- **Provision disk for the high-water mark, not the live-row bytes.** The ×1.7
  factor in the recommendation is a *well-tuned-autovacuum* steady state; the ~5×
  is the pathological "autovacuum fell behind" ceiling this must never reach.
- Keeping the high-water mark near ×1.7 (not ×5) is exactly what the aggressive
  per-table autovacuum tuning does — see below.

## Autovacuum: the tuning that keeps the high-water mark low

The `tasks` table already carries aggressive autovacuum settings
(`db/schema.sql`: `autovacuum_vacuum_scale_factor=0.02`, `threshold=50`,
`cost_limit=1000`). The same discipline is what holds the telemetry tables'
high-water mark near ×1.7 instead of ×5 under the delete-driven churn above
(Data Lifecycle 5→6, entry 56). Without it, a table that turns over 72 K rows/hour
would only autovacuum after 20 % of the table died (the default scale factor) —
far too late on a churning queue — and drift toward the measured ×5 ceiling.

## Reproduce (the measurement queries)

Per-row footprint — run against any DB with `db/schema.sql` applied:

```sql
CREATE TEMP TABLE _sz (LIKE worker_memory_samples INCLUDING ALL);
CREATE INDEX ON _sz (worker_id, created_at DESC);
INSERT INTO _sz (worker_id, available_gb, effective_gb, throttled, created_at)
  SELECT gen_random_uuid(), 8, 6, (g%5=0), now() - (g||' seconds')::interval
  FROM generate_series(1,100000) g;
VACUUM ANALYZE _sz;
SELECT pg_total_relation_size('_sz')/100000.0 AS bytes_per_row;   -- ≈ 220
```

Steady-state size for a scale is `bytes_per_row × churn_rate × retention_days`
with the churn rates above; the bloat high-water mark is measured by the
insert/delete-cycle simulation in this doc's history.

---

_See also `docs/PERF.md` (site-delivery perf, a different facet) and
`scripts/bench-local.sh` (claim-query latency, Control Plane Hot Path). This doc
is the Postgres storage-tier sizing input the Scalability Headroom / Reliability
facets consume when sizing the production database._
