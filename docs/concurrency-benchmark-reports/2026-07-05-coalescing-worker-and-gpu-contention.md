# Agent concurrency & parallelism model 7.5→8 and 8→9

> Produced on real hardware: Apple M3 Pro (Mac15,7), 18 GB unified memory,
> macOS 26.6 (Darwin 25.6.0 arm64). Real MiniLM/Llama-3.2-1B-Instruct-Q4_K_M
> weights loaded through real `cargo test --release --features metal`
> invocations of the tests named below, driving the REAL `ModelPool` +
> `EmbedRunner`/`BatchInferRunner`/`LlamaCoalescer` objects — the exact
> production dispatch path, not a stand-in. No control plane / network
> involved beyond the initial model download (cached after first run).

This answers two rungs of docs/internal/CREED_AND_PATH_TO_TEN.md's "Agent
concurrency & parallelism model" facet.

## Rung 7.5 → 8: "Interim cross-task batching via a coalescing worker"

**Built. Correctness-tested. NOT wired into production, because the real,
order-controlled timing measurement does not show the benefit the rung
assumes.**

`agent/src/coalesce.rs` adds `LlamaCoalescer`: a channel-fed worker task per
canonical llama model id that drains all currently-waiting `generate_batch`
requests (grouped by `max_tokens`, since `generate_batch` has one `max_tokens`
budget per call) and runs each group through exactly one `generate_batch`
call, so two concurrent same-model `batch_infer` submissions can share one
larger forward pass instead of serializing on the raw per-model mutex. No new
kernel — it calls the existing, already-proven `LlamaBackend::generate_batch`
unmodified, so no new byte-equality gate is needed beyond the ones that
function already carries (see the module's own doc comment for the full
byte-exact-equivalence argument).

### The measurement

`runners::tests::coalescer_concurrent_vs_serial_measured` (real weights,
`#[ignore]`d, run with `cargo test --release --features metal
coalescer_concurrent_vs_serial_measured -- --ignored --nocapture`) compares:

- **SERIAL arm:** submit batch A, await its full reply, then submit batch B.
- **CONCURRENT arm:** submit both batches at the same instant (`tokio::join!`)
  against a fresh coalescer, so the worker drains and merges them into one
  `generate_batch` call (bsz doubles — confirmed via the worker's own debug
  trace on every run, `CX_COALESCE_DEBUG=1`).

Both arms run the same total prompt count and `max_tokens`, so total compute
budget is identical — only the scheduling differs.

**Critical methodology note:** this machine measurably throttles under
sustained real-inference load. A same-process, same-warm-backend control
probe (`runners::tests::probe_ground_truth_bsz_scaling_same_process`) caught
the identical workload shape running ~3.5x slower in the first half of a
sustained run than the second half. A naive single-ordering test (serial arm
always measured first, concurrent arm always second) has a systematic thermal
bias against whichever arm runs second. So the real test runs BOTH orderings
(serial-then-concurrent and concurrent-then-serial) and requires the result to
hold in both.

### Result

| batch width (rows merged) | ordering 1 (serial first) | ordering 2 (concurrent first) |
|---|---|---|
| 16→32 | 0.96x-0.98x | 0.96x-0.98x |
| 32→64 | 0.96x | 0.96x |

Repeated runs (10+ across both batch widths) landed consistently in a tight
**0.96x-0.98x** band in BOTH orderings — i.e. concurrent submission through
the coalescing worker was slightly SLOWER than strict serial dispatch, not
faster, once thermal-order bias is controlled for. This directly contradicts
the rung's assumed benefit ("two concurrent same-model generative tasks
measurably complete faster together").

**Root cause, isolated with a dedicated same-process control
(`probe_ground_truth_bsz_scaling_same_process`, and a coalescer-overhead
isolation probe `probe_coalescer_round_trip_overhead`):**

- The coalescing worker's own per-call round-trip overhead (channel send +
  oneshot await + task wake/dispatch) is negligible in isolation:
  0.995x-1.001x for a single (non-merged) submission.
- The raw kernel's batch-width scaling on this hardware, once thermal drift is
  controlled for by testing both orderings, is close to parity too (~0.83x-1.5x
  swings that average out near 1.0x across orderings) — i.e. this Apple M3
  Pro / Q4_K_M-quantized Llama-3.2-1B combination is close to compute-bound
  already at the tested batch widths (16-64 rows), leaving little
  memory-bandwidth headroom for coalescing to exploit.
- With minimal headroom to gain and small-but-real worker scheduling overhead
  once merging actually happens, the net measured result is a small,
  consistent regression, not a win.

### Disposition

Per this bundle's own discipline ("a rung is not claimed on code existing
alone — its own proof artifact must show a real win") and its explicit
guidance ("if a coalescing-worker rewrite turns out to be a larger, riskier
change than a focused pass can safely verify, it is better to report the real
measured baseline... than to ship a half-tested concurrency rewrite"):
`BatchInferRunner` keeps locking the warm model's mutex directly, exactly as
before this pass (`pool.llama(...)`, not `pool.llama_coalescer(...)`). The
coalescer mechanism is kept in the tree (`#[allow(dead_code)]`, matching the
precedent already set by `continuous_batch.rs`'s unwired Hawking-port
skeleton) because it is real, correct, and may pay off on different hardware
or at larger real batch widths — wiring it in is a one-line change
(`BatchInferRunner::run_with_checkpoints`, swap `pool.llama(...)` for
`pool.llama_coalescer(...)`) if a future measurement shows a real win.
**Rung 7.5→8 is NOT claimed complete** — the mechanism exists but its own
proof artifact came back negative on the hardware tested.

## Rung 8 → 9: "Add real GPU-level scheduling awareness"

**Measured. Finding: real, substantial (~1.8x), but highly predictable and
repeatable GPU-queue contention. No explicit priority/queuing added, because
the measurement shows the existing implicit Candle-level serialization is
predictable, not an emergent accident — matching the rung's own stated
condition for NOT adding speculative queuing machinery.**

### The measurement

`runners::tests::mixed_model_contention_is_predictable_not_emergent` (real
weights, `#[ignore]`d, run with `cargo test --release --features metal
mixed_model_contention_is_predictable_not_emergent -- --ignored --nocapture`)
drives the real `EmbedRunner`/`BatchInferRunner` dispatch objects: one embed
task (8 short sentences, MiniLM) and one batch_infer task (12 prompts,
Llama-3.2-1B, `max_tokens=48`) — distinct models, distinct per-model mutexes
(P-embed-race already separated these), truly concurrent via `tokio::join!`,
funneling into the one shared Metal command queue. Measured across 3 repeats:
solo embed alone, solo llama alone, and both running simultaneously.

### Result

| | mean wall time |
|---|---|
| solo embed | 0.005s |
| solo llama | 1.78s |
| concurrent wall (embed + llama together) | 3.25s |
| llama's own leg while embed ran concurrently | 3.25s |

**llama's own decode time increased ~1.83x when embed ran concurrently**
(1.78s solo → 3.25s concurrent) — real, substantial GPU-level contention, not
free "parallelism." But this result was **extremely stable**: run-to-run
spread was 0.1%-0.2% of the mean across every repeat and every independent
re-run of the test (multiple full re-runs all landed at 1.83x within
±0.02x). Embed's own leg stayed cheap throughout (tens of milliseconds).

**Isolating the cause** (`runners::tests::
probe_llama_slowdown_is_gpu_specific_not_cpu_scheduling`): a control
experiment ran llama concurrently with a pure CPU integer busy-loop (zero
Metal/GPU calls, same rough wall-clock duration) instead of a real embed
task. Result: **0.99x — essentially zero slowdown.** This cleanly isolates
the ~1.83x effect to genuine Metal-GPU-queue contention specifically, not
general CPU scheduling, thread-pool contention, or thermal/power response to
higher overall system utilization.

### Is this "predictable, not an emergent accident"?

Yes, by the rung's own operational criteria:

- **Low run-to-run variance** (0.1%-0.2% spread on both the overall wall time
  and llama's own leg) — an emergent/accidental contention pattern (priority
  inversion, queue thrashing, starvation) would show large, inconsistent
  swings run to run; this shows a stable, repeatable number every time.
- **Bounded magnitude** (~1.83x, well under a 3x sanity ceiling that would
  indicate something pathological like thrashing rather than ordinary GPU
  time-slicing).
- **Isolated mechanism** (confirmed GPU-specific via the CPU-control probe,
  not a mysterious multi-factor interaction).

This is the honest, measured cost of two independent real GPU workloads
sharing one physical Metal queue — Candle's implicit queue handling does not
hide it, but it also does not produce anything worse than a predictable,
bounded slowdown. Per this task's own explicit instruction ("if the
measurement shows current behavior is already predictable/acceptable, it is
fully valid to report that finding and NOT add speculative new queuing
machinery for a problem that measurement shows does not exist"): **no
explicit priority/queuing was added.** The measurement is the deliverable for
this rung.

### What would change this conclusion

If a future measurement (different hardware, larger concurrent workload mix,
more than two simultaneous distinct-model tasks) showed the slowdown factor
growing unboundedly with load, or showed high run-to-run variance under
otherwise-identical conditions, that would be the "emergent accident" pattern
this rung warns about, and would justify adding explicit priority/queuing (a
real scheduling layer above Candle's implicit command-queue serialization).
Today's two-task, two-model measurement does not show that pattern.

## Reproducing this

```
cd agent
cargo test --release --features metal coalescer_concurrent_vs_serial_measured -- --ignored --nocapture
cargo test --release --features metal probe_ground_truth_bsz_scaling_same_process -- --ignored --nocapture
cargo test --release --features metal probe_coalescer_round_trip_overhead -- --ignored --nocapture
cargo test --release --features metal mixed_model_contention_is_predictable_not_emergent -- --ignored --nocapture
cargo test --release --features metal probe_llama_slowdown_is_gpu_specific_not_cpu_scheduling -- --ignored --nocapture
```

Each test prints its own real timing data to stdout/stderr (`--nocapture`).
Run each test individually (not as a batch) — running several real-model
timed tests together in one process reintroduces exactly the thermal/
scheduling contention this report characterizes, contaminating each other's
measurements (confirmed while producing this report: batching two of these
tests together inflated one arm's wall time by ~2.7x relative to running it
alone).
