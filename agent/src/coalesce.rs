//! coalesce.rs — the interim cross-task batching bridge (docs/internal/
//! CREED_AND_PATH_TO_TEN.md, "Agent concurrency & parallelism model" 7.5 → 8:
//! "Interim cross-task batching via a coalescing worker").
//!
//! **STATUS: built, correctness-tested, and NOT wired into production.** Real,
//! order-controlled timing on this facet's own reference hardware (Apple M3
//! Pro) found NO reliable wall-clock benefit from merging concurrent
//! `batch_infer` tasks' prompts into one `generate_batch` call, at ANY tested
//! width. Two independent measurement passes agree. The original pass
//! (Implementation Log entry 67,
//! `runners::tests::coalescer_concurrent_vs_serial_measured`) tested 2
//! submitters × 16-32 rows (merged widths 32-64) and landed at 0.96x-0.98x.
//! The width-sweep re-measurement (this module's
//! `tests::coalescer_width_sweep_remeasured`) extended that to merged widths
//! 64-256 and up to 8 concurrent submitters, and found the result unchanged
//! and if anything WORSE at scale: every config in both orderings measured at
//! or below serial (0.62x-0.97x; the widest merged-256 calls the slowest
//! relative to serial), directly contradicting the theory that large widths
//! would be memory-bandwidth-bound with headroom for merging to reclaim.
//! See `docs/concurrency-benchmark-reports/2026-07-06-coalescer-width-sweep.md`
//! for the full sweep data and root-cause (debug-trace-confirmed: at large
//! widths the worker DOES merge, but a wider `generate_batch` call costs
//! proportionally more wall time on this compute-bound kernel, so there is no
//! aggregate throughput to gain), and
//! `runners::BatchInferRunner::run_with_checkpoints`'s own doc note for why
//! production still calls `pool.llama(...)` + the raw mutex directly rather
//! than this module's `pool.llama_coalescer(...)`. This module is kept
//! (unwired, like `continuous_batch.rs`'s Hawking-port skeleton) because the
//! mechanism itself is real, correct, and may pay off on DIFFERENT hardware (a
//! higher-bandwidth Mac, or a fanned desktop) — the wiring is a one-line change
//! if a future measurement shows a real win, and `coalescer_width_sweep_
//! remeasured` is the permanent regime detector that will surface one loudly.
//!
//! SAFE (see the classification rule this file follows, matching the discipline
//! already established for `quantized_llama_batched.rs`/`runners.rs`): this module
//! calls the EXISTING, already-proven `LlamaBackend::generate_batch` — it does not
//! touch the vendored/patched candle kernel at all. It only changes WHO ends up
//! inside one `generate_batch` call: instead of two concurrent `batch_infer` tasks
//! each locking the per-model mutex and calling `generate_batch` with their OWN
//! prompt set (strict serialization — task B's `generate_batch` call cannot start
//! until task A's finishes), both tasks' prompts are concatenated into ONE
//! `generate_batch` call, so task B's prompts share A's forward passes as soon
//! as A is already mid-flight when B arrives. The THEORY was that wall time
//! would not scale linearly with bsz because decode would be memory-bandwidth-
//! bound at these small batch widths; the MEASUREMENT (see STATUS above) found
//! this specific hardware/kernel combination close enough to compute-bound at
//! the tested widths (16-64 rows) that the theoretical win did not reliably
//! materialize in wall-clock terms.
//!
//! Why this preserves byte-exact determinism trivially, with NO new equivalence
//! gate needed: `generate_batch` is already proven byte-identical whether its
//! prompts arrive as one bucket or several sub-batches (see
//! `runners::tests::batch_split_across_two_backends_matches_one_bucket` and
//! `generate_batch_shared_prefix` equivalence tests) — batching is bucketed by
//! EXACT token length internally and processes strictly WITHIN one length-bucket,
//! with per-row EOS driving an active-set shrink that leaves surviving rows'
//! positions and KV entries bitwise unchanged (see `generate_batch`'s own doc
//! comment). This module's only new behavior is WHICH caller's prompts land in
//! the `prompts` slice passed to one `generate_batch` call — it is still one
//! `&mut self` call into the same unmodified function. Concatenating two callers'
//! prompt vectors and passing the result to `generate_batch` produces EXACTLY
//! the same per-row output as if each half had been run through `generate_batch`
//! on its own (proven token-for-token by the existing
//! `batch_split_across_two_backends_matches_one_bucket` test on a fresh backend
//! instance; unaffected by which backend instance holds the KV cache since each
//! `generate_batch` call owns its own transient per-call KV state). This module
//! ships NO kernel change, so it needs no new byte-equality gate beyond the ones
//! `generate_batch` already carries.
//!
//! GATED aspect (must be respected, not re-derived): coalescing is restricted to
//! requests sharing the SAME `max_tokens`. `generate_batch`'s decode loop runs
//! `for step in 0..max_tokens` for the WHOLE batch — it has no per-row max_tokens
//! budget, only per-row EOS-triggered early exit. Two callers with DIFFERENT
//! max_tokens cannot be merged into one `generate_batch` call without either (a)
//! truncating the caller that wanted more tokens (using the smaller max_tokens for
//! the merged call — a wrong, silently-lossy answer for that caller) or (b)
//! forcing the caller that wanted fewer tokens to keep decoding past its own
//! budget (a wrong, wasteful answer, and a behavior change to a value the caller
//! explicitly bounded). Neither is a Coalescer decision to make silently, and
//! implementing correct per-row max_tokens support is exactly the kind of kernel
//! -level change this bundle's safety rule forbids shipping half-verified. So this
//! module coalesces WITHIN a `max_tokens` group only — requests with distinct
//! max_tokens never merge, and simply run their own `generate_batch` call, which
//! is IDENTICAL to today's behavior for that group (a group of one is exactly
//! today's per-task call).
//!
//! Concretely: a single long-lived `tokio::task` per canonical llama model id
//! owns the `Arc<Mutex<LlamaBackend>>` (reusing the pool's existing warm handle)
//! and drains a `tokio::sync::mpsc` channel. On each wake it takes the first
//! request, then immediately drains every OTHER request already buffered in the
//! channel via `try_recv()` (never waiting for more to arrive — that would add
//! latency for the sole-requester case, which is still the common case today).
//! Buffered requests are grouped by `max_tokens`; each group's prompts are
//! concatenated (in FIFO arrival order across requests) and run through exactly
//! one `generate_batch` call; each request gets back the disjoint slice of the
//! result that corresponds to the prompts it submitted, in its OWN original
//! order (never reordered across requests).

#![allow(dead_code)] // unwired (see STATUS above): exercised by its own tests and
                     // by pool.rs::llama_coalescer, not by any production runner.

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::{mpsc, oneshot, Mutex};

use crate::runners::{LlamaBackend, RunError};

/// One caller's coalescable unit of work: a full prompt set plus a shared
/// `max_tokens` for the whole set (the caller's own EXISTING per-task chunk,
/// unchanged from today's `generate_batch(&chunk_prompts, max_tokens)` call
/// shape — see `runners::BatchInferRunner::run_with_checkpoints`), plus the
/// `oneshot` the coalescing worker replies on.
struct Request {
    prompts: Vec<String>,
    max_tokens: u32,
    reply: oneshot::Sender<Result<Vec<(String, usize)>, RunError>>,
}

/// A handle to a running coalescing worker for one canonical llama model id.
/// Cloneable (cheap `Arc`/`mpsc::Sender` bumps) so every task that wants this
/// model gets its own submission handle to the same shared worker loop.
#[derive(Clone)]
pub struct LlamaCoalescer {
    tx: mpsc::UnboundedSender<Request>,
}

impl LlamaCoalescer {
    /// Spawn the worker loop that owns `backend` (the pool's existing warm
    /// `Arc<Mutex<LlamaBackend>>` handle — NOT a new instance; this wraps the
    /// same warm-loaded backend the mutex-direct path used, so no extra model
    /// load is introduced) and returns a submission handle. The loop runs for
    /// the lifetime of the process (mirrors every other warm-pool resource:
    /// never explicitly torn down, dropped only on process exit or, in
    /// principle, if every `LlamaCoalescer` clone and the map entry holding it
    /// were dropped — which the pool never does today, matching its existing
    /// no-eviction-of-in-flight-structures discipline elsewhere).
    pub fn spawn(backend: Arc<Mutex<LlamaBackend>>) -> Self {
        let (tx, rx) = mpsc::unbounded_channel::<Request>();
        tokio::spawn(coalescing_worker_loop(backend, rx));
        Self { tx }
    }

    /// Submit one caller's prompt set and await its own reply. Never blocks the
    /// caller on anyone else's request beyond ordinary `generate_batch` compute
    /// time — the coalescing happens transparently inside the worker loop.
    pub async fn generate_batch(
        &self,
        prompts: Vec<String>,
        max_tokens: u32,
    ) -> Result<Vec<(String, usize)>, RunError> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(Request {
                prompts,
                max_tokens,
                reply,
            })
            .map_err(|_| RunError::Inference {
                backend: "batch_infer",
                msg: "coalescing worker channel closed".to_string(),
            })?;
        rx.await.map_err(|_| RunError::Inference {
            backend: "batch_infer",
            msg: "coalescing worker dropped the reply channel".to_string(),
        })?
    }
}

/// The worker loop body. Runs until the channel closes (every `LlamaCoalescer`
/// clone dropped) or the process exits — whichever first, exactly like every
/// other long-lived warm-pool resource.
async fn coalescing_worker_loop(
    backend: Arc<Mutex<LlamaBackend>>,
    mut rx: mpsc::UnboundedReceiver<Request>,
) {
    while let Some(first) = rx.recv().await {
        let mut batch: Vec<Request> = vec![first];
        // Drain everything ALREADY waiting — this is the coalescing step. Never
        // await for more to arrive: a lone request must not pay extra latency
        // waiting on a caller that has not shown up yet, so we only ever merge
        // requests that were genuinely concurrent (already queued) at the
        // instant this worker woke up.
        while let Ok(req) = rx.try_recv() {
            batch.push(req);
        }
        // Opt-in trace (never on by default — this is a hot path): confirms
        // whether a real drain actually merged >1 request, independent of the
        // wall-clock timing question of whether merging HELPS (see
        // runners::tests::coalescer_concurrent_vs_serial_measured and
        // tests::coalescer_width_sweep_remeasured — merging was confirmed to
        // happen at 4+ concurrent submitters on every real-hardware run, but did
        // NOT translate into a wall-clock win at ANY tested width on the M3 Pro).
        if std::env::var("CX_COALESCE_DEBUG").is_ok() {
            eprintln!("[coalesce] drained batch of {} request(s)", batch.len());
        }

        // Group by max_tokens (see the module doc's GATED note: generate_batch
        // has one max_tokens for the whole call, so only equal-max_tokens
        // requests may share one call). Groups preserve FIFO arrival order
        // within themselves; a singleton group is exactly today's per-task
        // call, byte-for-byte.
        let mut groups: HashMap<u32, Vec<Request>> = HashMap::new();
        for req in batch {
            groups.entry(req.max_tokens).or_default().push(req);
        }

        for (max_tokens, group) in groups {
            // Concatenate every request's prompts, remembering each request's
            // own disjoint [start, end) slice so results are scattered back to
            // the RIGHT caller, in that caller's own original order.
            let mut all_prompts: Vec<String> = Vec::new();
            let mut spans: Vec<(usize, usize)> = Vec::with_capacity(group.len());
            for req in &group {
                let start = all_prompts.len();
                all_prompts.extend(req.prompts.iter().cloned());
                spans.push((start, all_prompts.len()));
            }

            let backend = backend.clone();
            let result = tokio::task::spawn_blocking(move || -> Result<_, RunError> {
                let mut backend = backend.blocking_lock();
                // The one call every coalesced caller's prompts now share —
                // unmodified `generate_batch`, see the module doc for the
                // byte-exact-equivalence argument.
                backend.generate_batch(&all_prompts, max_tokens)
            })
            .await
            .map_err(|e| RunError::Inference {
                backend: "batch_infer",
                msg: format!("coalescing worker thread failed: {e}"),
            })
            .and_then(|inner| inner);

            match result {
                Ok(all_results) => {
                    for (req, (start, end)) in group.into_iter().zip(spans) {
                        // Each request's own slice, in its own original order —
                        // `generate_batch` returns results in the SAME order as
                        // the input `prompts`, so slicing `[start..end)` out of
                        // the concatenated output is exactly this request's
                        // own results, untouched by any other request's rows.
                        let _ = req.reply.send(Ok(all_results[start..end].to_vec()));
                    }
                }
                Err(e) => {
                    // A shared-call failure (e.g. a load/tokenize error) is
                    // reported to EVERY coalesced caller honestly — never
                    // silently dropped, never faked as a partial success for
                    // some callers. `RunError` has no `Clone`, so each caller
                    // gets its own freshly-built `Inference` error carrying the
                    // same message text (`Display`, via `{e}`) rather than
                    // trying to clone/share the original.
                    let msg = e.to_string();
                    for req in group {
                        let _ = req.reply.send(Err(RunError::Inference {
                            backend: "batch_infer",
                            msg: msg.clone(),
                        }));
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A fake "backend" stand-in proving the coalescing/grouping/scatter logic
    /// itself — no real model load, mirroring the same-philosophy cheap-stand-in
    /// tests already used elsewhere in this crate (e.g. `pool::tests::
    /// pool_loads_once_across_n_runs`). This does not exercise the real
    /// `LlamaBackend::generate_batch`; the real-model proof is
    /// `runners::tests::coalescer_two_concurrent_tasks_faster_than_serial`
    /// (`#[ignore]`, real weights, real timing).
    ///
    /// Verifies: (1) two concurrent submissions with the SAME max_tokens are
    /// answered correctly — each caller gets back exactly its own rows in its
    /// own order, never another caller's rows; (2) a submission with a
    /// DIFFERENT max_tokens is never merged with the others (proven indirectly:
    /// it still gets correct, non-corrupted results even though it must have
    /// gone through its own separate `generate_batch` call).
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn coalescer_scatters_results_to_the_right_caller_in_order() {
        // We cannot construct a real LlamaBackend without downloading weights,
        // and this test intentionally avoids that (see doc comment above) — so
        // this test instead drives the coalescing-worker LOGIC directly via a
        // hand-rolled substitute loop that mirrors `coalescing_worker_loop`
        // exactly, but calls a cheap fake `generate_batch`-shaped function
        // instead of the real backend. This keeps the grouping/scatter
        // correctness proof network-free while the real end-to-end proof
        // (byte-real weights, real timing) lives in runners.rs as `#[ignore]`.
        //
        // Fake "generate_batch": returns `(format!("out:{prompt}"), prompt.len())`
        // per prompt, in order — enough to prove ordering/scatter without any
        // model.
        fn fake_generate_batch(prompts: &[String], _max_tokens: u32) -> Vec<(String, usize)> {
            prompts
                .iter()
                .map(|p| (format!("out:{p}"), p.len()))
                .collect()
        }

        struct FakeRequest {
            prompts: Vec<String>,
            max_tokens: u32,
            reply: oneshot::Sender<Vec<(String, usize)>>,
        }

        let (tx, mut rx) = mpsc::unbounded_channel::<FakeRequest>();

        let worker = tokio::spawn(async move {
            while let Some(first) = rx.recv().await {
                let mut batch = vec![first];
                while let Ok(req) = rx.try_recv() {
                    batch.push(req);
                }
                let mut groups: HashMap<u32, Vec<FakeRequest>> = HashMap::new();
                for req in batch {
                    groups.entry(req.max_tokens).or_default().push(req);
                }
                // Only one iteration matters for this test's shutdown check.
                if groups.is_empty() {
                    break;
                }
                for (max_tokens, group) in groups {
                    let mut all_prompts: Vec<String> = Vec::new();
                    let mut spans: Vec<(usize, usize)> = Vec::with_capacity(group.len());
                    for req in &group {
                        let start = all_prompts.len();
                        all_prompts.extend(req.prompts.iter().cloned());
                        spans.push((start, all_prompts.len()));
                    }
                    let all_results = fake_generate_batch(&all_prompts, max_tokens);
                    for (req, (start, end)) in group.into_iter().zip(spans) {
                        let _ = req.reply.send(all_results[start..end].to_vec());
                    }
                }
            }
        });

        // Two concurrent callers, SAME max_tokens=24 — must coalesce into one
        // logical batch and each get back exactly their own rows.
        let (reply_a, rx_a) = oneshot::channel();
        tx.send(FakeRequest {
            prompts: vec!["alpha-1".to_string(), "alpha-2".to_string()],
            max_tokens: 24,
            reply: reply_a,
        })
        .unwrap();
        let (reply_b, rx_b) = oneshot::channel();
        tx.send(FakeRequest {
            prompts: vec!["beta-1".to_string()],
            max_tokens: 24,
            reply: reply_b,
        })
        .unwrap();
        // A third caller with a DIFFERENT max_tokens — must never be merged
        // with A/B's group (proven by grouping key; correctness here is that
        // it still gets back exactly its own two rows, unaffected by A/B).
        let (reply_c, rx_c) = oneshot::channel();
        tx.send(FakeRequest {
            prompts: vec!["gamma-1".to_string(), "gamma-2".to_string()],
            max_tokens: 99,
            reply: reply_c,
        })
        .unwrap();

        let a = rx_a.await.unwrap();
        let b = rx_b.await.unwrap();
        let c = rx_c.await.unwrap();

        assert_eq!(
            a,
            vec![
                ("out:alpha-1".to_string(), 7),
                ("out:alpha-2".to_string(), 7),
            ],
            "caller A must get exactly its own two rows, in its own order"
        );
        assert_eq!(
            b,
            vec![("out:beta-1".to_string(), 6)],
            "caller B must get exactly its own one row, never one of A's or C's"
        );
        assert_eq!(
            c,
            vec![
                ("out:gamma-1".to_string(), 7),
                ("out:gamma-2".to_string(), 7),
            ],
            "caller C (different max_tokens) must still get correct, unmixed results"
        );

        drop(tx);
        let _ = worker.await;
    }

    /// RE-MEASUREMENT at LARGER batch widths / more concurrent submitters
    /// (docs/internal/CREED_AND_PATH_TO_TEN.md, "Agent concurrency & parallelism
    /// model" 7.5 → 8, "Interim cross-task batching via a coalescing worker").
    ///
    /// **Context.** Implementation Log entry 67 measured this exact coalescer and
    /// found NO win at the tested widths (2 submitters × 16-32 rows each, i.e.
    /// merged widths 32-64): repeated order-controlled runs landed at 0.96x-0.98x
    /// (concurrent submission slightly SLOWER than strict serial), root-caused to
    /// this M3 Pro / Q4_K_M-Llama-3.2-1B combination being close to compute-bound
    /// already at those widths, leaving no memory-bandwidth headroom for merging
    /// to exploit. That entry left an explicit open question: *does a win regime
    /// exist at LARGER widths or MORE submitters?* This test answers it with real
    /// timed data — the deliverable, per this bundle, is measured data either way,
    /// not a forced wiring.
    ///
    /// **Method.** For each `(submitters, per_submitter_width)` config in a sweep
    /// that deliberately reaches beyond entry 67's 32-64 ceiling (up to 8
    /// submitters and merged widths of 128-256 rows):
    ///   - SERIAL arm: submit each submitter's batch, AWAIT its full reply, THEN
    ///     the next — a `LlamaCoalescer` fed one request at a time never coalesces,
    ///     so this is a plain pass-through to N sequential `generate_batch` calls,
    ///     identical to holding the raw mutex directly N times.
    ///   - CONCURRENT arm: submit all `submitters` batches at the same instant
    ///     (`futures::future::join_all`) against a FRESH warm pool/coalescer, so
    ///     they race into the SAME worker loop's channel and the worker drains +
    ///     merges them into ONE wide `generate_batch` call (confirmed per run via
    ///     `CX_COALESCE_DEBUG=1`: "drained batch of N request(s)").
    ///
    /// **Thermal-order control (mandatory on this hardware).** This exact M3 Pro
    /// measurably throttles under sustained real-inference load (documented in the
    /// Thermal facet and in `runners::tests::probe_ground_truth_bsz_scaling_
    /// same_process`, which caught the same workload ~3.5x slower in the first
    /// half of a sustained run than the second). A naive single-ordering sweep
    /// would systematically bias whichever arm runs second. So EACH config runs in
    /// BOTH orderings (serial-first and concurrent-first) and both speedups are
    /// reported — a real coalescing win must show up regardless of ordering, not
    /// only in the one that happens to favor it thermally.
    ///
    /// The assertion is a REGIME DETECTOR, not a hard win/lose gate: it fails only
    /// if some config's BOTH orderings clear 1.15x (the rung's original
    /// proof-artifact bar — a genuine, thermally-robust win worth wiring in) so
    /// such a discovery is never silently ignored; otherwise it passes and the
    /// printed table is the deliverable. Sweep results feed the Implementation
    /// Log and the committed report.
    ///
    /// `#[ignore]` because it downloads the real Llama-3.2-1B GGUF (~800MB) and
    /// needs real wall-clock time to be meaningful. Run with:
    ///   cargo test --release --features metal coalescer_width_sweep_remeasured -- --ignored --nocapture
    #[tokio::test(flavor = "multi_thread", worker_threads = 8)]
    #[ignore = "downloads the real Llama-3.2-1B GGUF (~800MB); real timed width-sweep concurrency re-measurement, run with --release --nocapture"]
    async fn coalescer_width_sweep_remeasured() {
        use crate::pool::ModelPool;

        const MAX_TOKENS: u32 = 48;
        fn make_batch(tag: &str, width: usize) -> Vec<String> {
            (0..width)
                .map(|i| format!("{tag} prompt {i}: write one short sentence about the ocean."))
                .collect()
        }

        // One arm = `submitters` batches of `width` prompts each, all with the same
        // max_tokens (so total compute budget is identical between the serial and
        // concurrent arm — only the SCHEDULING differs). Returns wall time.
        async fn run_serial_arm(tag: &str, submitters: usize, width: usize) -> std::time::Duration {
            let pool = ModelPool::new();
            let coalescer = pool
                .llama_coalescer("llama-3.2-1b-instruct-q4")
                .await
                .expect("warm llama coalescer (serial arm)");
            let batches: Vec<Vec<String>> = (0..submitters)
                .map(|s| make_batch(&format!("{tag}-serial-{s}"), width))
                .collect();
            let started = std::time::Instant::now();
            for b in batches {
                let r = coalescer
                    .generate_batch(b, MAX_TOKENS)
                    .await
                    .expect("serial batch");
                assert_eq!(r.len(), width);
            }
            started.elapsed()
        }

        async fn run_concurrent_arm(
            tag: &str,
            submitters: usize,
            width: usize,
        ) -> std::time::Duration {
            let pool = ModelPool::new();
            let coalescer = pool
                .llama_coalescer("llama-3.2-1b-instruct-q4")
                .await
                .expect("warm llama coalescer (concurrent arm)");
            // Each submitter is its own independent task racing into the SAME
            // coalescer channel — the exact shape real production would have (one
            // task per job). JoinSet (tokio-native, no extra dep) launches them all
            // as close to the same instant as spawn overhead allows, so the worker
            // loop's drain merges them into one wide generate_batch call.
            // Pre-build every batch OUTSIDE the timed region (prompt-string
            // construction is not the thing under test), then time from the first
            // spawn through the last join so the full concurrent dispatch is
            // measured symmetrically with the serial arm.
            let batches: Vec<Vec<String>> = (0..submitters)
                .map(|s| make_batch(&format!("{tag}-concurrent-{s}"), width))
                .collect();
            let started = std::time::Instant::now();
            let mut set = tokio::task::JoinSet::new();
            for batch in batches {
                let c = coalescer.clone();
                set.spawn(async move { c.generate_batch(batch, MAX_TOKENS).await });
            }
            let mut widths = Vec::with_capacity(submitters);
            while let Some(joined) = set.join_next().await {
                let r = joined
                    .expect("concurrent submitter task panicked")
                    .expect("concurrent batch");
                widths.push(r.len());
            }
            let wall = started.elapsed();
            assert_eq!(widths.len(), submitters);
            assert!(widths.iter().all(|&w| w == width));
            wall
        }

        // The sweep: deliberately reach beyond entry 67's 32-64 merged-width
        // ceiling. (submitters, per-submitter width) -> merged width when the
        // concurrent arm coalesces = submitters * width.
        //   (2, 32) -> 64    (matches the top of entry 67's prior range)
        //   (2, 64) -> 128   (new, larger)
        //   (2, 128) -> 256  (new, larger still)
        //   (4, 32) -> 128   (more submitters, same merged width as (2,64))
        //   (4, 64) -> 256   (more submitters, largest merged width)
        //   (8, 16) -> 128   (most submitters — max channel-drain pressure)
        let configs: &[(usize, usize)] = &[(2, 32), (2, 64), (2, 128), (4, 32), (4, 64), (8, 16)];

        println!("coalescer_width_sweep_remeasured (MAX_TOKENS={MAX_TOKENS}):");
        println!(
            "  {:>10}  {:>7}  {:>7}  {:>7}  {:>9}  {:>9}",
            "config", "merged", "ser1(s)", "con1(s)", "spd(s1st)", "spd(c1st)"
        );

        let mut any_robust_win = false;
        for &(submitters, width) in configs {
            let merged = submitters * width;
            // Ordering 1: serial first (cooler), concurrent second (warmer) —
            // biased AGAINST the concurrent arm if thermal drift dominates.
            let ser1 = run_serial_arm("o1", submitters, width).await;
            let con1 = run_concurrent_arm("o1", submitters, width).await;
            let spd1 = ser1.as_secs_f64() / con1.as_secs_f64().max(1e-9);
            // Ordering 2: concurrent first (cooler), serial second (warmer) —
            // biased IN FAVOR of a coalescing win if thermal drift dominates.
            let con2 = run_concurrent_arm("o2", submitters, width).await;
            let ser2 = run_serial_arm("o2", submitters, width).await;
            let spd2 = ser2.as_secs_f64() / con2.as_secs_f64().max(1e-9);

            println!(
                "  {:>4}x{:<4}  {:>7}  {:>7.3}  {:>7.3}  {:>8.2}x  {:>8.2}x",
                submitters,
                width,
                merged,
                ser1.as_secs_f64(),
                con1.as_secs_f64(),
                spd1,
                spd2,
            );

            // A win regime worth wiring in must clear the rung's proof-artifact bar
            // (>=1.15x) in BOTH orderings — i.e. robust to thermal drift, not an
            // artifact of which arm ran on the cooler machine.
            if spd1 >= 1.15 && spd2 >= 1.15 {
                any_robust_win = true;
                println!(
                    "  ^^ ROBUST WIN at {submitters}x{width} (merged {merged}): {spd1:.2}x / {spd2:.2}x — \
                     both orderings clear 1.15x; investigate wiring the coalescer in for this regime."
                );
            }
        }

        // This is a REGIME DETECTOR, deliberately asymmetric: it must SUCCEED
        // whether or not a win exists (the measured table is the deliverable), but
        // it must make a real, thermally-robust win impossible to miss. If one is
        // found, this assertion fires so the finding is surfaced loudly (update the
        // Implementation Log, the coalescer STATUS doc, and reconsider wiring it
        // into BatchInferRunner's production path); if none is found, the test
        // passes and the printed sweep is the honest "no win at any tested width"
        // record. Either outcome is a valid, real-data deliverable for this rung.
        assert!(
            !any_robust_win,
            "coalescer_width_sweep_remeasured FOUND a robust (both-ordering >=1.15x) win at some \
             tested width — this contradicts Implementation Log entry 67's 'no win' finding and is \
             a REAL result worth acting on: update the log, the coalesce.rs STATUS doc, and \
             reconsider wiring the coalescer into BatchInferRunner. (This assert intentionally \
             fails on a WIN so a genuine discovery is never silently swallowed.)"
        );
    }
}
