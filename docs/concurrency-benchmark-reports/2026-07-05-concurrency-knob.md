# Agent concurrency & parallelism model 7→7.5 — the concurrency-knob benchmark

> Produced on real hardware: Apple M3 Pro (Mac15,7), 18 GB unified memory,
> macOS 26.6 (Darwin 25.6.0 arm64). Real MiniIM/Llama-3.2-1B weights loaded
> through the real `cx-agent` binary's `bench-concurrency` subcommand
> (`agent/src/main.rs::run_bench_concurrency`), driving the REAL
> `tokio::Semaphore` + `ModelPool` + `JobRunner::run` dispatch objects — the
> exact same object graph `run`'s main loop uses (`sem.clone().acquire_owned()`
> then spawn, mirroring `poll_and_spawn`) — not a stand-in or toy
> reimplementation. No control plane / network involved. Git commit `f8dc90d`
> (dirty — uncommitted session work present, including the P-embed-race fix
> this benchmark was run immediately after).

This answers docs/internal/CREED_AND_PATH_TO_TEN.md's "Agent concurrency &
parallelism model" facet, rung **7 → 7.5: Benchmark the concurrency knob
itself**: *"Run a synthetic N-task drive through the real semaphore-plus-pool
at permits 1, 2, and 4 for mixed embed-and-llama loads, replacing the
currently unvalidated `[2,4]` clamp with actual data."*

## Important context: this benchmark ran immediately after the P-embed-race fix

Before this benchmark was written, this same session found, reproduced, and
fixed a real data race in `agent/src/pool.rs`: concurrent `embed()` calls
against the shared Metal-backend embedder corrupted results (NaN embeddings).
The fix serializes embed access behind a `tokio::Mutex`, identical to the
llama/whisper backends (see the P-embed-race PATCH note in `pool.rs` and
`docs/internal/CREED_AND_PATH_TO_TEN.md`'s Implementation Log).

**This changes the rung's own prior expectation.** The parent section's text
(and this rung's own proof-artifact language) predicted: *"embed-heavy
workloads can safely run wider (the embedder is lock-free `&self`-concurrent)
while generative workloads see little benefit past two."* That was true of the
code as originally written, but the "lock-free `&self`-concurrent" embedder
was the source of the corruption bug — it was never actually safe to run
concurrently, so this benchmark measures the corrected, now-honestly-mutexed
world, not the one the rung's authors assumed when they wrote that prediction.

## Headline result: permits do not help either workload once corrected for the race fix

| workload | permits swept | tasks/s at permits=1 | tasks/s at best permit | speedup |
|---|---|---|---|---|
| mixed (8 embed + 8 batch_infer) | 1, 2, 4 | 10.55 | 10.75 (permits=2) | 1.02x |
| embed-only (64 tasks) | 1, 2, 4, 8 | 258.6 | 258.6 (permits=1, best) | ~1.00x (noise-level) |
| batch_infer-only (16 tasks) | 1, 2, 4 | 5.36 | 5.42 (permits=4) | 1.01x |

**Across all three workload shapes and every permit level tested, aggregate
throughput is flat within measurement noise (0.94x–1.02x).** Adding permits
buys essentially nothing today, for either workload type — not just llama
(as the doc already suspected), but embed too (which the doc's stale text did
not expect, because the embed mutex did not exist yet when that text was
written).

**Why:** both backends now serialize real compute behind a `tokio::Mutex` per
model (embed: the just-landed P-embed-race fix; llama: already `Arc<Mutex<
LlamaBackend>>` from the original warm-pool design, `&mut self` decode +
growing KV cache). With compute serialized, additional permits only let S3
GET/PUT and other non-compute I/O overlap — and this synthetic benchmark has
no real S3 I/O (`inputs`/`output` are empty/local), so there is nothing left
for extra permits to overlap. **The real production benefit of permits beyond
1 is specifically the S3/network overlap during a real job's I/O phases, not
compute parallelism** — this benchmark isolates compute-path concurrency
precisely because it has zero network I/O, and that isolation is what makes
the "permits don't help compute" result clean and attributable.

## Full data

### Mixed workload (8 embed + 8 batch_infer tasks, max_tokens=24)

| permits | wall_s | embed_wall_s (sum) | llama_wall_s (sum) | tasks/s | speedup vs permits=1 |
|---|---|---|---|---|---|
| 1 | 1.517 | 0.033 | 1.484 | 10.55 | 1.00x |
| 2 | 1.488 | 0.068 | 2.729 | 10.75 | 1.02x |
| 4 | 1.578 | 0.170 | 5.018 | 10.14 | 0.96x |

(`embed_wall_s`/`llama_wall_s` are the SUM of each task's own wall time, not
wall-clock — they grow with permits because more tasks are queued waiting on
the same mutex simultaneously, each counting its own wait-plus-run time. The
`wall_s` column is the real end-to-end wall-clock for the whole sweep point,
and that is what stays flat.)

### Embed-only (64 tasks, max_tokens irrelevant)

| permits | wall_s | tasks/s | speedup vs permits=1 |
|---|---|---|---|
| 1 | 0.247 | 258.6 | 1.00x |
| 2 | 0.262 | 244.3 | 0.94x |
| 4 | 0.256 | 249.8 | 0.97x |
| 8 | 0.256 | 250.2 | 0.97x |

### batch_infer-only (16 tasks, max_tokens=24)

| permits | wall_s | tasks/s | speedup vs permits=1 |
|---|---|---|---|
| 1 | 2.984 | 5.36 | 1.00x |
| 2 | 3.008 | 5.32 | 0.99x |
| 4 | 2.950 | 5.42 | 1.01x |

Repeated runs of the mixed 8/8 sweep (not all tabulated above) landed in the
same 0.96x–1.02x band every time — this is a stable, real result, not a
one-off measurement artifact.

## What this means for the `[2,4]` concurrency-default clamp

`config.rs::AgentConfig::concurrency` derives permits as
`(memory_gb / 8.0).clamp(2, 4)` when the operator hasn't set an explicit
value. This benchmark's finding does **not** argue for lowering that clamp to
1: real production dispatch has real S3 GET (input) and PUT (result/partial
checkpoint) I/O around every task's compute, which this synthetic benchmark
deliberately has none of (inputs/outputs are empty so the measurement isolates
compute-path concurrency). Permits 2-4 still buy real overlap of that I/O with
another task's compute — this benchmark just proves that overlap is *the
entire benefit*, not partial compute parallelism layered on top of it, for
both embed and batch_infer alike. The rung's proof-artifact language asked to
find "where added permits stop improving throughput for each workload mix" —
the measured answer is: immediately, at permits=1, for pure compute; the
`[2,4]` default's real value is exclusively about I/O overlap, a claim this
benchmark did not attempt to measure (it would need real S3 traffic, which is
Data Transfer & Artifact I/O's domain, not this facet's).

## Reproducing this

```
cd agent
cargo run --release --features metal -- bench-concurrency \
  --permits 1,2,4 --embed-tasks 8 --llama-tasks 8 --max-tokens 24

cargo run --release --features metal -- bench-concurrency \
  --permits 1,2,4,8 --embed-tasks 64 --llama-tasks 0 --max-tokens 24

cargo run --release --features metal -- bench-concurrency \
  --permits 1,2,4 --embed-tasks 0 --llama-tasks 16 --max-tokens 24
```

Prints a human table to stderr and the full JSON record (per-sweep-point
`wall_s`/`tasks/s`/`speedup_vs_permit_1`) to stdout — redirect stdout to
capture just the JSON, same convention as `bench-batch`/`bench-sustained`.
