# Coalescer re-measurement at larger batch widths / more submitters

**Facet:** Agent Concurrency & Parallelism Model 7.5→8 — "Interim cross-task
batching via a coalescing worker."
**Date:** 2026-07-06. **Hardware:** Apple M3 Pro (fanned), Metal, engine `candle`.
**Model:** `llama-3.2-1b-instruct-q4` (Q4_K_M). **Decode:** 48 tokens/request.

## Why this re-measurement exists

Implementation Log entry 67 built and correctness-tested `agent/src/coalesce.rs`'s
`LlamaCoalescer` and measured it at **2 submitters × 16-32 rows each** (merged widths
32-64), finding **no win** (0.96x-0.98x — concurrent submission via coalescing slightly
SLOWER than strict serial). It root-caused this to the M3 Pro being close to
compute-bound at those widths, and left an explicit open question: *does a win regime
exist at LARGER widths or MORE submitters?* This pass answers that with real timed data.
Per the bundle discipline, the deliverable is measured data either way — a genuine win
would be wired in; a confirmed no-win is reported honestly and the coalescer stays
unwired.

## Method

Test: `agent/src/coalesce.rs`, `coalescer_width_sweep_remeasured` (`#[ignore]`, real
weights, real timing). For each `(submitters, per-submitter width)` config:

- **SERIAL arm:** submit each submitter's batch, await its full reply, then the next.
  A `LlamaCoalescer` fed one request at a time never coalesces — a plain pass-through to
  N sequential `generate_batch` calls (identical to holding the raw mutex directly).
- **CONCURRENT arm:** every submitter is its own `tokio` task (via `JoinSet`, no extra
  dep) racing into the SAME coalescer channel against a FRESH warm pool, so the worker
  loop's drain can merge them into one wide `generate_batch` call.

**Thermal-order control (mandatory on this hardware).** This M3 Pro measurably
throttles under sustained load (see the Thermal facet and
`probe_ground_truth_bsz_scaling_same_process`). Each config runs in BOTH orderings
(serial-first and concurrent-first); a real win must appear in BOTH, not just the one
that happens to favor it thermally. `speedup = serial_wall / concurrent_wall` (>1 = win).

Reproduce:

```
cargo test --release --features metal coalescer_width_sweep_remeasured -- --ignored --nocapture
```

## Results — two independent runs

Raw logs: `2026-07-06-coalescer-width-sweep-run1.log`, `…-run2.log` (same directory).

| config (subm × width) | merged width | run 1 spd (s-first / c-first) | run 2 spd (s-first / c-first) |
|-----------------------|-------------:|:-----------------------------:|:-----------------------------:|
| 2 × 32  |  64 | 0.87x / 0.89x | 0.97x / 0.91x |
| 2 × 64  | 128 | 0.83x / 0.80x | 0.89x / 0.82x |
| 2 × 128 | 256 | 0.78x / 0.73x | 0.62x / 0.74x |
| 4 × 32  | 128 | 0.88x / 0.76x | 0.48x / 0.31x |
| 4 × 64  | 256 | 0.75x / 0.81x | 0.77x / 0.83x |
| 8 × 16  | 128 | 0.92x / **1.09x** | 0.93x / 0.94x |

## Finding: NO win regime at any tested width or submitter count

Across every config (merged widths **64 → 256**) and every ordering, in two independent
runs, concurrent submission through the coalescer measured **at or below serial** — the
only value above 1.0x anywhere in the sweep was a single ordering of one config
(8×16 concurrent-first, run 1, 1.09x), which came back at 0.93x/0.94x in run 2 and whose
own other ordering in run 1 was 0.92x. It fails the both-orderings-robust bar and is
noise, not a win — precisely why that bar exists.

If anything the result is **worse at scale**: the widest merged calls (2×128 and 4×64,
merged 256) sit at 0.62x-0.83x, i.e. concurrent submission is 17-38% *slower* than strict
serial. This directly contradicts the coalescer's original theory (that decode would be
memory-bandwidth-bound at large widths, leaving headroom for merging to reclaim).

**Mechanism, confirmed from the debug trace (`CX_COALESCE_DEBUG=1`):**

- At 2× configs the worker frequently drained `1 request` at a time — the first
  submitter's `generate_batch` finished (or was already mid-flight and holding the
  mutex) before the second submitter's request landed, so no merge happened and the
  concurrent arm was just serial-plus-channel-overhead.
- At 4× and 8× configs the worker DID merge (`drained batch of 3 request(s)`,
  `drained batch of 7 request(s)`), proving the mechanism works — but the resulting
  wider `generate_batch` call cost *more* wall time than the equivalent sequential calls.
  This is the compute-bound signature: on this M3 Pro, one bsz=128 call costs about the
  same as (or more than) four bsz=32 calls, so there is no aggregate throughput to gain
  by widening, and the worker's real scheduling overhead tips the net slightly negative.

## Disposition

**Rung 7.5→8 remains NOT claimed.** The re-measurement extends entry 67's evidence from
merged widths 32-64 to 64-256 and from 2 submitters to 8, and the conclusion is
unchanged and stronger: coalescing does not win on this hardware at any realistic width.

`BatchInferRunner` continues to lock the warm model's mutex directly (`pool.llama(...)`);
the coalescer stays in the tree, correct and correctness-tested, carrying
`#[allow(dead_code)]` (the same precedent as `continuous_batch.rs`'s unwired Hawking
skeleton). Wiring it in would add real complexity and a measured regression for zero
measured benefit — a violation of this repo's "a rung is not claimed on code existing
alone" discipline. The regime detector (`coalescer_width_sweep_remeasured`) is kept as a
permanent `#[ignore]`d test that FAILS on a both-ordering ≥1.15x win, so if future
hardware (a higher-bandwidth Mac, or a fanned desktop) does unlock a real win, it will be
surfaced loudly rather than silently missed.
