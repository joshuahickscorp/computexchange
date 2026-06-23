//! Warm model pool — load each backend ONCE, reuse it across every task.
//!
//! The baseline reloaded the model from disk inside every `run()`. That made N
//! tasks of the same model pay N model loads. The pool collapses that to one:
//! each backend is resolved to a canonical model id and lazily loaded behind a
//! `tokio::sync::OnceCell`, so concurrent first-touchers race to a single load
//! and everyone after reuses the warm handle.
//!
//! Sharing rules follow each backend's borrow:
//! - `Embedder::embed` is `&self` → handed out as a bare `Arc<Embedder>`; many
//!   tasks embed through one model concurrently (Candle's Metal queue serializes
//!   the GPU work internally).
//! - `LlamaBackend`/`WhisperBackend` decode with `&mut self` (KV cache, growing
//!   token buffer) → guarded by a `tokio::Mutex` inside the `Arc`. Heavy GPU
//!   compute serializes behind that mutex; the win is still real: no per-task
//!   reload, and S3 GET/PUT for other tasks overlaps with the held compute.
//!
//! `loads()` exposes a process-wide counter incremented inside every real load,
//! so a test can prove "loaded once across N runs" without the network.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use tokio::sync::{Mutex, OnceCell};

use crate::runners::{Embedder, LlamaBackend, RunError, WhisperBackend};

/// Process-wide count of real backend loads. Incremented once per model actually
/// pulled into memory (not per task). The pool's whole purpose is to keep this
/// at "one per distinct model" no matter how many tasks arrive.
static LOAD_COUNT: AtomicUsize = AtomicUsize::new(0);

/// Number of real model loads so far this process. The warm-pool proof asserts
/// this stays at 1 across N same-model runs (see `tests::pool_loads_once`).
pub fn loads() -> usize {
    LOAD_COUNT.load(Ordering::SeqCst)
}

/// Bump the load counter from inside a loader. Backends call this the instant
/// before (or after) the expensive `::load`, so it counts genuine loads only.
pub(crate) fn note_load() {
    LOAD_COUNT.fetch_add(1, Ordering::SeqCst);
}

/// A lazily-initialized, mutex-guarded warm backend.
type Warm<T> = Arc<OnceCell<Arc<Mutex<T>>>>;

/// The warm model pool. Cloneable (cheap `Arc` bumps) so every spawned task gets
/// its own handle to the same shared backends.
#[derive(Clone, Default)]
pub struct ModelPool {
    /// The single embedder (one embed model in the catalogue). `&self` use → no
    /// mutex; shared directly.
    embedder: Arc<OnceCell<Arc<Embedder>>>,
    /// Llama backends keyed by canonical id (`llama-3.2-1b-instruct-q4`, or a
    /// `qwen` family id). `&mut self` decode → mutex-guarded.
    llama: Arc<Mutex<HashMap<String, Warm<LlamaBackend>>>>,
    /// Whisper backends keyed by canonical id (`whisper-tiny` | `whisper-base`).
    whisper: Arc<Mutex<HashMap<String, Warm<WhisperBackend>>>>,
}

impl ModelPool {
    pub fn new() -> Self {
        Self::default()
    }

    /// Get (loading once) the shared embedder. Concurrent first calls race into
    /// one load via the `OnceCell`; all later calls reuse the warm handle.
    pub async fn embedder(&self) -> Result<Arc<Embedder>, RunError> {
        self.embedder
            .get_or_try_init(|| async {
                let e = tokio::task::spawn_blocking(|| {
                    note_load();
                    Embedder::load()
                })
                .await
                .map_err(join_err("embed"))??;
                Ok::<_, RunError>(Arc::new(e))
            })
            .await
            .cloned()
    }

    /// Get (loading once) the warm Llama backend for `model_ref`, keyed by its
    /// canonical id so `""`, the catalogue id, and the HF repo all share a model.
    pub async fn llama(&self, model_ref: &str) -> Result<Arc<Mutex<LlamaBackend>>, RunError> {
        let key = canonical_llama_id(model_ref);
        let cell = self.slot(&self.llama, &key).await;
        let model_ref = model_ref.to_string();
        cell.get_or_try_init(|| async {
            let b = tokio::task::spawn_blocking(move || {
                note_load();
                LlamaBackend::load(&model_ref)
            })
            .await
            .map_err(join_err("batch_infer"))??;
            Ok::<_, RunError>(Arc::new(Mutex::new(b)))
        })
        .await
        .cloned()
    }

    /// Get (loading once) the warm Whisper backend for `model_ref`, keyed by the
    /// canonical id (`whisper-tiny` | `whisper-base`).
    pub async fn whisper(&self, model_ref: &str) -> Result<Arc<Mutex<WhisperBackend>>, RunError> {
        let key = canonical_whisper_id(model_ref);
        let cell = self.slot(&self.whisper, &key).await;
        let model_ref = model_ref.to_string();
        cell.get_or_try_init(|| async {
            let b = tokio::task::spawn_blocking(move || {
                note_load();
                WhisperBackend::load(&model_ref)
            })
            .await
            .map_err(join_err("whisper"))??;
            Ok::<_, RunError>(Arc::new(Mutex::new(b)))
        })
        .await
        .cloned()
    }

    /// Fetch (or create) the per-key `OnceCell` slot under the short map lock. The
    /// lock is held only long enough to clone out an `Arc<OnceCell>`; the actual
    /// (slow) load happens on the cell, outside this lock, so distinct models load
    /// in parallel and never serialize behind the map.
    async fn slot<T>(&self, map: &Arc<Mutex<HashMap<String, Warm<T>>>>, key: &str) -> Warm<T> {
        let mut g = map.lock().await;
        g.entry(key.to_string())
            .or_insert_with(|| Arc::new(OnceCell::new()))
            .clone()
    }

    /// Canonical ids of the models currently WARM in this pool (warm-routing, D3).
    ///
    /// Only FULLY-LOADED backends count: `slot()` inserts an empty `OnceCell` the
    /// instant a model is first requested, so map membership alone would report a
    /// model that is still loading (or whose load failed) as warm. We gate on
    /// `cell.get().is_some()` — initialized means the `note_load()` load returned —
    /// so every id here is genuinely resident, never a load-in-flight or a fabricated
    /// entry. The heartbeat sends this verbatim; the control plane upserts a
    /// worker_model_state row per id so the scheduler can prefer a warm worker.
    pub async fn loaded_model_ids(&self) -> Vec<String> {
        let mut ids = Vec::new();
        if self.embedder.get().is_some() {
            ids.push(EMBEDDER_ID.to_string());
        }
        collect_warm_ids(&*self.llama.lock().await, &mut ids);
        collect_warm_ids(&*self.whisper.lock().await, &mut ids);
        ids.sort(); // stable order so the heartbeat payload is deterministic
        ids
    }
}

/// Canonical id of the single embedder model (matches `models::EMBED_SPEC` and the
/// `all-minilm-l6-v2` catalogue id). Reported by `loaded_model_ids` when the
/// embedder is warm.
const EMBEDDER_ID: &str = "all-minilm-l6-v2";

/// Push the ids of every FULLY-INITIALIZED slot in a per-model warm map into `ids`.
/// The warm gate is `cell.get().is_some()`: a slot whose `OnceCell` has not yet
/// resolved (a load in flight, or a failed load) is skipped, so an id is reported
/// only when its backend is genuinely resident. Generic over the warmed value so the
/// gate is unit-testable with a cheap stand-in (no real model load) — the same
/// philosophy as `pool_loads_once_across_n_runs`.
fn collect_warm_ids<T>(map: &HashMap<String, Warm<T>>, ids: &mut Vec<String>) {
    for (id, cell) in map.iter() {
        if cell.get().is_some() {
            ids.push(id.clone());
        }
    }
}

/// Map a `spawn_blocking` join failure (panic/cancel) to a typed inference error.
fn join_err(backend: &'static str) -> impl Fn(tokio::task::JoinError) -> RunError {
    move |e| RunError::Inference {
        backend,
        msg: format!("worker thread failed: {e}"),
    }
}

/// Canonical Llama-family id for pool keying. Qwen refs key separately so the two
/// models never collide on one warm slot; everything else is the catalogue id.
fn canonical_llama_id(model_ref: &str) -> String {
    if model_ref.to_ascii_lowercase().contains("qwen") {
        "qwen2.5-0.5b-instruct-q4".to_string()
    } else {
        "llama-3.2-1b-instruct-q4".to_string()
    }
}

/// Canonical Whisper id for pool keying (`whisper-base` if the ref says "base",
/// else `whisper-tiny`) — matches `models::whisper_spec`'s repo choice.
fn canonical_whisper_id(model_ref: &str) -> String {
    if model_ref.to_ascii_lowercase().contains("base") {
        "whisper-base".to_string()
    } else {
        "whisper-tiny".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_ids_match_catalogue() {
        assert_eq!(canonical_llama_id(""), "llama-3.2-1b-instruct-q4");
        assert_eq!(
            canonical_llama_id("unsloth/Llama-3.2-1B-Instruct-GGUF"),
            "llama-3.2-1b-instruct-q4"
        );
        assert_eq!(
            canonical_llama_id("Qwen/Qwen2.5"),
            "qwen2.5-0.5b-instruct-q4"
        );
        assert_eq!(canonical_whisper_id("whisper-tiny"), "whisper-tiny");
        assert_eq!(canonical_whisper_id("openai/whisper-base"), "whisper-base");
        assert_eq!(canonical_whisper_id(""), "whisper-tiny");
    }

    /// THE WARM-POOL PROOF (non-network). This exercises the exact load-once
    /// mechanism every pool getter uses — a per-key `OnceCell` whose initializer
    /// bumps a counter — with a fake loadable instead of a real model. We fire N
    /// concurrent first-touchers at one cell; the counter must read 1, proving N
    /// tasks of the same model trigger exactly ONE load (the baseline did N).
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn pool_loads_once_across_n_runs() {
        use std::sync::atomic::AtomicUsize;
        // A standalone counter so this test is independent of any real loads the
        // process may have done elsewhere.
        let loads = Arc::new(AtomicUsize::new(0));
        // One shared cell, exactly as `ModelPool` holds per canonical id.
        let cell: Warm<u32> = Arc::new(OnceCell::new());

        const N: usize = 16;
        let mut handles = Vec::with_capacity(N);
        for _ in 0..N {
            let cell = cell.clone();
            let loads = loads.clone();
            handles.push(tokio::spawn(async move {
                let v: Arc<Mutex<u32>> = cell
                    .get_or_try_init(|| async {
                        // Stand-in for `Backend::load()` — a counted, fake load.
                        let n = tokio::task::spawn_blocking(move || {
                            loads.fetch_add(1, Ordering::SeqCst);
                            42u32 // the "loaded model"
                        })
                        .await
                        .unwrap();
                        Ok::<_, RunError>(Arc::new(Mutex::new(n)))
                    })
                    .await
                    .unwrap()
                    .clone();
                let got = *v.lock().await;
                got
            }));
        }
        for h in handles {
            assert_eq!(h.await.unwrap(), 42, "every caller sees the one warm value");
        }
        // The whole point: N concurrent same-key getters → exactly ONE load.
        assert_eq!(
            loads.load(Ordering::SeqCst),
            1,
            "warm pool must load a model once and reuse it across N runs"
        );
    }

    /// Warm-id lister proof (warm-routing, D3), non-network. A fresh `ModelPool`
    /// reports no warm ids (nothing loaded). And `collect_warm_ids` — the exact gate
    /// `loaded_model_ids` uses for the llama/whisper maps — lists only the slots whose
    /// `OnceCell` has actually resolved: a slot that EXISTS but is not yet initialized
    /// (a load in flight, the state `slot()` leaves a cell in before the load returns)
    /// is excluded. We drive a cheap `Warm<u32>` map (a stand-in for a backend, same
    /// philosophy as `pool_loads_once_across_n_runs`) so the test never loads a model,
    /// proving the heartbeat can only advertise a genuinely-resident model.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn loaded_model_ids_lists_only_warm_backends() {
        // A cold pool has no warm models.
        let pool = ModelPool::new();
        assert!(
            pool.loaded_model_ids().await.is_empty(),
            "a cold pool has no warm models"
        );

        // Mirror the pool's per-model warm map with a cheap value type. One slot is
        // initialized (warm), one exists but is uninitialized (loading), one model has
        // no slot at all (never requested).
        let mut map: HashMap<String, Warm<u32>> = HashMap::new();
        let warm: Warm<u32> = Arc::new(OnceCell::new());
        warm.set(Arc::new(Mutex::new(7u32)))
            .expect("init warm slot");
        map.insert("llama-3.2-1b-instruct-q4".to_string(), warm);
        // A slot left in the exact state `slot()` creates before a load resolves.
        map.insert(
            "qwen2.5-0.5b-instruct-q4".to_string(),
            Arc::new(OnceCell::new()),
        );

        let mut ids = Vec::new();
        collect_warm_ids(&map, &mut ids);
        assert_eq!(
            ids,
            vec!["llama-3.2-1b-instruct-q4".to_string()],
            "only the FULLY-loaded slot is warm; a loading slot is excluded"
        );
    }

    /// Real-model warm-pool proof: N embed runs through the ACTUAL `ModelPool`
    /// trigger exactly one MiniLM load (the process-wide `loads()` counter rises by
    /// 1). `#[ignore]` because it downloads ~90MB on first run. Run with:
    ///   cargo test --release pool_loads_real_model_once -- --ignored --nocapture
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "downloads all-MiniLM-L6-v2 (~90MB) and loads it through the real pool"]
    async fn pool_loads_real_model_once() {
        let pool = ModelPool::new();
        let before = loads();
        const N: usize = 8;
        let mut handles = Vec::with_capacity(N);
        for i in 0..N {
            let pool = pool.clone();
            handles.push(tokio::spawn(async move {
                let e = pool.embedder().await.expect("warm embedder");
                let v = e.embed(&[format!("warm pool run {i}")]).expect("embed");
                assert_eq!(v.len(), 1);
            }));
        }
        for h in handles {
            h.await.unwrap();
        }
        assert_eq!(
            loads() - before,
            1,
            "the real pool must load MiniLM exactly once across {N} runs"
        );
    }
}
