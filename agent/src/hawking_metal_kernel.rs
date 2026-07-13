//! hawking_metal_kernel.rs — a real, working port of the SINGLE HARDEST piece of
//! docs/HAWKING_PORT_PLAN.md's ~4-6 week build: "Week 2 — Metal multi-seq KV
//! kernel." This lands `mha_decode_f32_batched_multiseq` and its companion
//! `kv_scatter_append` — the slot-strided-KV multi-sequence decode attention kernel
//! from the founder's Hawking engine (`hawking-core/shaders/mha.metal`,
//! `hawking-core/shaders/common.metal`) — as real Candle custom ops that dispatch
//! runtime-compiled Metal Shading Language directly on the agent's own Metal GPU.
//!
//! WHAT THIS PROVES (BLACKHOLE discipline — a claim without a proof artifact is not
//! a claim): the kernel actually runs on real Apple Silicon hardware, actually
//! shares one dispatch across multiple independent slots with independent KV
//! history lengths (the core continuous-batching property), and its output is
//! numerically verified against TWO independent references — a hand-written CPU
//! reference (`cpu_fwd`, exercised by every CustomOp3/InplaceOp2 caller including
//! CPU-only test runs) and, in the Metal-gated test below, an entirely separate
//! implementation built from Candle's own `matmul`/`softmax` ops. See
//! `multiseq_decode_matches_independent_candle_reference` and
//! `kv_scatter_append_matches_manual_index_arithmetic`.
//!
//! WHAT THIS OP LAYER DOES NOT DO (equally load-bearing to say plainly): RoPE and
//! Q4_K projection GEMMs are not fused here; this op deliberately accepts the F32
//! Q/K/V produced upstream. The opt-in Hawking lane now wires these ops through
//! `continuous_batch`, `quantized_llama_batched`, and `HawkingRunner`, where the
//! end-to-end determinism/coherence gates live. This file proves the low-level
//! attention/KV contract; it does not by itself prove a buyer-visible result or
//! enable Hawking for a task that did not explicitly select that lane.
//!
//! Source mapping (verified by direct inspection of the real Hawking source at
//! `~/Downloads/hawking/crates/hawking-core/`, not from memory or the doc
//! comments alone):
//!   - KV layout: one big buffer per K/V, slot-strided by a STABLE region id (not
//!     the compacted batch index) — `hawking-core/src/model/qwen_dense.rs`'s
//!     `forward_tokens_multiseq_stack_tcb`, `dst = region*slot_stride +
//!     position*kv_dim + i`.
//!   - `kv_scatter_append_multiseq` — `hawking-core/shaders/common.metal`. Ported
//!     here as a single-buffer variant (`kv_scatter_append`, called once for K and
//!     once for V) — Hawking's original fuses both into one dispatch; splitting it
//!     is a straightforward simplification that preserves the identical index
//!     arithmetic (kept in lockstep with the MHA kernel's own addressing, per the
//!     source's own comment that these two must never drift apart).
//!   - `mha_decode_f32_batched_multiseq` — `hawking-core/shaders/mha.metal`. Ported
//!     verbatim: grid `(n_heads*128, batch, 1)` non-uniform threadgroups of 128,
//!     `tg_id.x` -> head, `tg_id.y` -> batch slot, a 4-phase shared-memory-tree
//!     softmax (QK dot / max-reduce / exp-sum-reduce / weighted-V-sum) over each
//!     slot's own `SEQ = positions[slot]+1` using that slot's region-offset KV.
//!
//! Determinism note: this is NOT byte-exact with the single-stream Candle decode
//! path (batched reduction order differs), matching Hawking's own finding that
//! token-level determinism is impossible across batch shapes — see
//! docs/HAWKING_PORT_PLAN.md's determinism re-gating plan. This module still emits
//! no verification-class-visible artifact on its own; the wired Hawking runner's
//! lane-level gates own that claim.

#![cfg(feature = "metal")]
#![allow(dead_code)] // not yet wired into any runner — see module docs.

use candle_core::backend::BackendStorage;
use candle_core::{CpuStorage, CustomOp3, DType, InplaceOp2, Layout, MetalStorage, Result, Shape};
use std::collections::HashMap;
use std::hash::Hash;
use std::sync::{Mutex, OnceLock, RwLock};

/// Runtime-compiled MSL source for both kernels. The library is compiled once per
/// Candle Metal device and each function pipeline once per device (see
/// `library_cache`/`pipeline_cache`) via `Device::new_library_with_source` — the
/// same runtime-source approach Hawking itself uses
/// (`hawking-core/src/metal/mod.rs`), not a build-time `.metallib`.
const SHADER_SRC: &str = r#"
#include <metal_stdlib>
using namespace metal;

// Single-buffer slot-strided KV append. Called once for K, once for V (Hawking's
// original fuses both into one dispatch; this split keeps the identical index
// arithmetic while using Candle's simpler single-mutable-tensor InplaceOp2 seam).
kernel void kv_scatter_append(
    device const float* src       [[buffer(0)]],
    device float*       dst       [[buffer(1)]],
    device const uint*  regions   [[buffer(2)]],
    device const uint*  positions [[buffer(3)]],
    constant uint&      kv_dim       [[buffer(4)]],
    constant uint&      slot_stride  [[buffer(5)]],
    uint id [[thread_position_in_grid]])
{
    uint bi = id / kv_dim;
    uint i  = id - bi * kv_dim;
    uint dst_off = regions[bi] * slot_stride + positions[bi] * kv_dim + i;
    uint src_off = bi * kv_dim + i;
    dst[dst_off] = src[src_off];
}

// Multi-sequence slot-strided-KV decode attention. One threadgroup per (head,
// batch-slot) pair; tg_id.x -> head, tg_id.y -> batch slot. 4-phase shared-memory
// tree softmax over this slot's own history length (positions[slot]+1), read from
// this slot's own region offset into the shared K/V cache buffers.
kernel void mha_decode_f32_batched_multiseq(
    device const float* q         [[buffer(0)]],
    device const float* k_cache   [[buffer(1)]],
    device const float* v_cache   [[buffer(2)]],
    device float*       out       [[buffer(3)]],
    device const uint*  positions [[buffer(4)]],
    device const uint*  regions   [[buffer(5)]],
    constant uint&      head_dim       [[buffer(6)]],
    constant uint&      n_heads        [[buffer(7)]],
    constant uint&      n_kv_heads     [[buffer(8)]],
    constant uint&      group_size     [[buffer(9)]],
    constant uint&      kv_slot_stride [[buffer(10)]],
    constant float&     scale          [[buffer(11)]],
    threadgroup float*  shmem     [[threadgroup(0)]],
    uint3 tg_id     [[threadgroup_position_in_grid]],
    uint3 tid_in_tg [[thread_position_in_threadgroup]],
    uint3 tg_dim    [[threads_per_threadgroup]])
{
    const uint h        = tg_id.x;
    const uint batch_id = tg_id.y;
    const uint tid      = tid_in_tg.x;
    const uint tg_size  = tg_dim.x;
    const uint SEQ      = positions[batch_id] + 1u;
    const uint kv_h     = h / group_size;
    const uint region   = regions[batch_id];
    device const float* k_slot = k_cache + region * kv_slot_stride;
    device const float* v_slot = v_cache + region * kv_slot_stride;
    threadgroup float* scores = shmem;
    threadgroup float* red    = shmem + SEQ;
    device const float* q_h = q + (batch_id * n_heads + h) * head_dim;

    // Phase 1: QK^T dot product per timestep, thread-strided over SEQ.
    for (uint t = tid; t < SEQ; t += tg_size) {
        device const float* kt = k_slot + (t * n_kv_heads + kv_h) * head_dim;
        float acc = 0.0f;
        for (uint i = 0; i < head_dim; ++i) acc += q_h[i] * kt[i];
        scores[t] = acc * scale;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    // Phase 2: max reduction (shared-memory tree, not simd_max — mirrors the
    // source kernel exactly).
    float local_max = -INFINITY;
    for (uint t = tid; t < SEQ; t += tg_size) local_max = max(local_max, scores[t]);
    red[tid] = local_max;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint stride = tg_size / 2u; stride > 0u; stride >>= 1u) {
        if (tid < stride) red[tid] = max(red[tid], red[tid + stride]);
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }
    float max_score = red[0];
    threadgroup_barrier(mem_flags::mem_threadgroup);

    // Phase 3: exp + sum reduction.
    float local_sum = 0.0f;
    for (uint t = tid; t < SEQ; t += tg_size) {
        float e = exp(scores[t] - max_score);
        scores[t] = e;
        local_sum += e;
    }
    red[tid] = local_sum;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint stride = tg_size / 2u; stride > 0u; stride >>= 1u) {
        if (tid < stride) red[tid] += red[tid + stride];
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }
    float inv_sum = 1.0f / red[0];
    threadgroup_barrier(mem_flags::mem_threadgroup);

    // Phase 4: weighted V sum, thread-strided over head_dim.
    device float* out_h = out + (batch_id * n_heads + h) * head_dim;
    for (uint i = tid; i < head_dim; i += tg_size) {
        float acc = 0.0f;
        for (uint t = 0; t < SEQ; ++t) {
            device const float* vt = v_slot + (t * n_kv_heads + kv_h) * head_dim;
            acc += scores[t] * vt[i];
        }
        out_h[i] = acc * inv_sum;
    }
}
"#;

const TG_SIZE: usize = 128;
/// The production Hawking scheduler hard-clamps its proven operating point to
/// B<=8 (`Config::hawking_pool_size_clamped`). Keep the public low-level ops at
/// that same evidence-backed ceiling so a hostile op cannot create an unbounded
/// grid or quadratic metadata-validation workload outside the scheduler.
const MAX_HAWKING_BATCH: usize = crate::config::HAWKING_POOL_SIZE_MAX;
/// Same hard context ceiling as `quantized_llama_batched::MAX_SEQ_LEN`. Besides
/// matching the only wired caller, this bounds CPU score scratch and Metal
/// dynamic threadgroup memory before either backend allocates or dispatches.
const MAX_HAWKING_SEQ_LEN: usize = crate::quantized_llama_batched::MAX_SEQ_LEN;
/// Device objects should be process-lifetime singletons, but `DeviceId` is
/// intentionally per instance. Bound retained Metal libraries/pipelines so a
/// caller repeatedly constructing device wrappers cannot grow these global
/// caches forever. Eviction only causes a later recompile; cloned Metal handles
/// held by in-flight dispatches remain valid.
const MAX_CACHED_METAL_DEVICES: usize = 8;

/// Return the logical element count while also proving that `layout` is a
/// standard row-major view. Candle's own `Shape::elem_count` and contiguous
/// stride helpers use unchecked multiplication; custom-op structs are public,
/// so keep hostile layouts from wrapping those calculations before validation.
fn checked_contiguous_elements(layout: &Layout, label: &str) -> Result<usize> {
    if layout.dims().len() != layout.stride().len() {
        candle_core::bail!("{label} shape/stride rank mismatch");
    }
    let mut elements = 1usize;
    for (&dim, &stride) in layout.dims().iter().zip(layout.stride()).rev() {
        if dim > 1 && stride != elements {
            candle_core::bail!("{label} must be contiguous row-major");
        }
        elements = elements
            .checked_mul(dim)
            .ok_or_else(|| candle_core::Error::Msg(format!("{label} element count overflow")))?;
    }
    Ok(elements)
}

fn checked_product(values: &[usize], label: &str) -> Result<usize> {
    values.iter().try_fold(1usize, |product, &value| {
        product
            .checked_mul(value)
            .ok_or_else(|| candle_core::Error::Msg(format!("{label} size overflow")))
    })
}

fn validate_storage_elements(
    layout: &Layout,
    logical_elements: usize,
    storage_elements: usize,
    label: &str,
) -> Result<()> {
    let end = layout
        .start_offset()
        .checked_add(logical_elements)
        .ok_or_else(|| candle_core::Error::Msg(format!("{label} storage offset overflow")))?;
    if end > storage_elements {
        candle_core::bail!(
            "{label} layout exceeds storage: end {end}, storage elements {storage_elements}"
        );
    }
    Ok(())
}

fn validate_metal_storage(
    storage: &MetalStorage,
    layout: &Layout,
    logical_elements: usize,
    label: &str,
) -> Result<()> {
    let end = layout
        .start_offset()
        .checked_add(logical_elements)
        .ok_or_else(|| candle_core::Error::Msg(format!("{label} storage offset overflow")))?;
    let bytes = end
        .checked_mul(DType::F32.size_in_bytes())
        .ok_or_else(|| candle_core::Error::Msg(format!("{label} byte size overflow")))?;
    if bytes > storage.buffer().length() {
        candle_core::bail!(
            "{label} layout exceeds Metal buffer: needs {bytes} bytes, buffer has {}",
            storage.buffer().length()
        );
    }
    Ok(())
}

fn checked_u32(value: usize, label: &str) -> Result<u32> {
    u32::try_from(value)
        .map_err(|_| candle_core::Error::Msg(format!("{label} exceeds Metal uint range")))
}

fn ensure_unique_regions(regions: &[u32], label: &str) -> Result<()> {
    if regions.len() > MAX_HAWKING_BATCH {
        candle_core::bail!(
            "{label} batch {} exceeds proven Hawking limit {MAX_HAWKING_BATCH}",
            regions.len()
        );
    }
    // The hard B<=8 ceiling makes this allocation-free check strictly bounded.
    for (index, region) in regions.iter().enumerate() {
        if regions[..index].contains(region) {
            candle_core::bail!("{label} contains duplicate region id {region}");
        }
    }
    Ok(())
}

fn try_zeroed_f32(len: usize, label: &str) -> Result<Vec<f32>> {
    let mut values = Vec::new();
    values.try_reserve_exact(len).map_err(|error| {
        candle_core::Error::Msg(format!(
            "unable to allocate {label} ({len} f32 values): {error}"
        ))
    })?;
    values.resize(len, 0.0);
    Ok(values)
}

#[derive(Clone, Copy, Debug)]
struct DecodeContract {
    batch: usize,
    n_heads: usize,
    head_dim: usize,
    cache_elements: usize,
    out_elements: usize,
    max_live_seq: usize,
    shmem_bytes: usize,
    head_dim_u32: u32,
    n_heads_u32: u32,
    n_kv_heads_u32: u32,
    group_size_u32: u32,
    kv_slot_stride_u32: u32,
}

fn validate_attention_metal_limits(
    contract: DecodeContract,
    device_max_threadgroup_memory: usize,
    pipeline_max_threads: usize,
    pipeline_static_threadgroup_memory: usize,
) -> Result<()> {
    if TG_SIZE > pipeline_max_threads {
        candle_core::bail!(
            "hawking multiseq decode requires {TG_SIZE} threads per threadgroup, pipeline supports {pipeline_max_threads}"
        );
    }
    let total_threadgroup_memory = contract
        .shmem_bytes
        .checked_add(pipeline_static_threadgroup_memory)
        .ok_or_else(|| {
            candle_core::Error::Msg("total threadgroup-memory size overflow".to_string())
        })?;
    if total_threadgroup_memory > device_max_threadgroup_memory {
        candle_core::bail!(
            "hawking multiseq decode needs {total_threadgroup_memory} bytes of threadgroup memory ({} dynamic + {pipeline_static_threadgroup_memory} static), device supports {}",
            contract.shmem_bytes,
            device_max_threadgroup_memory
        );
    }
    Ok(())
}

#[derive(Clone, Copy, Debug)]
struct ScatterContract {
    batch: usize,
    dst_elements: usize,
    src_elements: usize,
    total_threads: usize,
    kv_dim_u32: u32,
    slot_stride_u32: u32,
}

fn scatter_threadgroup_size(total_threads: usize, pipeline_max_threads: usize) -> Result<usize> {
    if total_threads == 0 {
        candle_core::bail!("hawking kv scatter refuses an empty dispatch");
    }
    let threads = total_threads.min(256).min(pipeline_max_threads);
    if threads == 0 {
        candle_core::bail!("hawking kv scatter pipeline supports zero threads per threadgroup");
    }
    Ok(threads)
}

/// A small synchronous single-flight cache. Warm reads share an `RwLock`; misses
/// take a separate initialization mutex and re-check the map before compiling, so
/// concurrent first-touch callers for the same key cannot both perform an
/// expensive Metal compile. A cold compile does not block unrelated warm reads.
/// Values are cloned out before use; Metal pipeline and library handles are cheap
/// retained-object clones and are declared `Send + Sync` by
/// `candle-metal-kernels`.
///
/// Failed initializers are deliberately not inserted. A transient Metal compiler
/// failure therefore remains retryable instead of poisoning the process-wide
/// cache for the rest of the agent lifetime.
struct SingleFlightCache<K, V> {
    entries: RwLock<HashMap<K, V>>,
    init: Mutex<()>,
    max_entries: usize,
}

impl<K, V> SingleFlightCache<K, V> {
    fn new(max_entries: usize) -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
            init: Mutex::new(()),
            max_entries: max_entries.max(1),
        }
    }
}

impl<K, V> SingleFlightCache<K, V>
where
    K: Eq + Hash,
    V: Clone,
{
    fn get_or_try_insert_with<E>(
        &self,
        key: K,
        init: impl FnOnce() -> std::result::Result<V, E>,
    ) -> std::result::Result<V, E> {
        // Fast path: warm callers only share a read lock.
        if let Some(value) = self
            .entries
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(&key)
            .cloned()
        {
            return Ok(value);
        }

        // Recover after a panic in another caller. A poisoned cache lock must not
        // permanently disable every subsequent inference request.
        let _init_guard = self
            .init
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);

        // Another caller may have populated this key while we waited for `init`.
        if let Some(value) = self
            .entries
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(&key)
            .cloned()
        {
            return Ok(value);
        }

        let value = init()?;
        let mut entries = self
            .entries
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if entries.len() >= self.max_entries {
            // Churn is abnormal and extremely rare. Clearing avoids a second
            // order-tracking allocation while still enforcing a hard bound.
            entries.clear();
        }
        entries.insert(key, value.clone());
        Ok(value)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum HawkingKernel {
    MultiSeqDecode,
    KvScatterAppend,
}

impl HawkingKernel {
    fn from_name(name: &str) -> Result<Self> {
        match name {
            "mha_decode_f32_batched_multiseq" => Ok(Self::MultiSeqDecode),
            "kv_scatter_append" => Ok(Self::KvScatterAppend),
            _ => candle_core::bail!("unknown Hawking Metal kernel {name:?}"),
        }
    }

    const fn name(self) -> &'static str {
        match self {
            Self::MultiSeqDecode => "mha_decode_f32_batched_multiseq",
            Self::KvScatterAppend => "kv_scatter_append",
        }
    }
}

type LibraryCache =
    SingleFlightCache<candle_core::metal_backend::DeviceId, candle_metal_kernels::metal::Library>;
type PipelineCache = SingleFlightCache<
    (candle_core::metal_backend::DeviceId, HawkingKernel),
    candle_metal_kernels::metal::ComputePipeline,
>;

fn library_cache() -> &'static LibraryCache {
    static CACHE: OnceLock<LibraryCache> = OnceLock::new();
    CACHE.get_or_init(|| LibraryCache::new(MAX_CACHED_METAL_DEVICES))
}

fn pipeline_cache() -> &'static PipelineCache {
    static CACHE: OnceLock<PipelineCache> = OnceLock::new();
    CACHE.get_or_init(|| PipelineCache::new(MAX_CACHED_METAL_DEVICES * 2))
}

/// Get or compile one named kernel function from `SHADER_SRC`. The source library
/// is compiled once per Candle `MetalDevice`, and each of the two pipeline states
/// is compiled once per device. `DeviceId` is intentionally part of both keys:
/// Metal pipeline states must never be reused with a different device instance,
/// even when both instances address the same physical GPU.
///
/// This mirrors candle-core's own (feature="ug"-gated) `MetalDevice::compile`,
/// minus the `ug` IR codegen step — we hand it real MSL source directly, so no
/// extra cargo feature is needed.
fn compile_pipeline(
    device: &candle_core::MetalDevice,
    func_name: &str,
) -> Result<candle_metal_kernels::metal::ComputePipeline> {
    let kernel = HawkingKernel::from_name(func_name)?;
    let device_id = device.id();
    pipeline_cache().get_or_try_insert_with((device_id, kernel), || {
        let raw = device.metal_device();
        let library = library_cache().get_or_try_insert_with(device_id, || {
            raw.new_library_with_source(SHADER_SRC, None).map_err(|e| {
                candle_core::Error::Metal(format!("hawking shader compile: {e}").into())
            })
        })?;
        let name = kernel.name();
        let function = library.get_function(name, None).map_err(|e| {
            candle_core::Error::Metal(format!("hawking shader get_function {name}: {e}").into())
        })?;
        raw.new_compute_pipeline_state_with_function(&function)
            .map_err(|e| {
                candle_core::Error::Metal(format!("hawking shader pipeline {name}: {e}").into())
            })
    })
}

/// One decode attention step across `positions.len()` independent slots, sharing
/// ONE dispatch. `q` is `(batch, n_heads, head_dim)`; `k_cache`/`v_cache` are the
/// flat slot-strided buffers `(num_regions * max_seq_per_slot, n_kv_heads,
/// head_dim)`. `regions[i]` is slot i's STABLE KV region id (decoupled from the
/// compacted batch index i — the core continuous-batching property); `positions[i]`
/// is slot i's current absolute position (so its live history length is
/// `positions[i]+1`).
pub struct MultiSeqDecodeAttention {
    pub positions: Vec<u32>,
    pub regions: Vec<u32>,
    pub n_kv_heads: usize,
    pub kv_slot_stride: usize,
    pub scale: f32,
}

impl MultiSeqDecodeAttention {
    fn validate_contract(
        &self,
        q: &Layout,
        k_cache: &Layout,
        v_cache: &Layout,
    ) -> Result<DecodeContract> {
        let (batch, n_heads, head_dim) = match q.dims() {
            [batch, n_heads, head_dim] => (*batch, *n_heads, *head_dim),
            _ => candle_core::bail!("q must be (batch, n_heads, head_dim)"),
        };
        if batch == 0 || n_heads == 0 || head_dim == 0 {
            candle_core::bail!("q batch, n_heads, and head_dim must all be non-zero");
        }
        if self.positions.len() != batch || self.regions.len() != batch {
            candle_core::bail!("positions/regions length must equal non-zero q batch");
        }
        ensure_unique_regions(&self.regions, "hawking multiseq decode batch")?;
        if self.n_kv_heads == 0 {
            candle_core::bail!("n_kv_heads must be non-zero");
        }
        if self.n_kv_heads > n_heads || n_heads % self.n_kv_heads != 0 {
            candle_core::bail!(
                "n_heads ({n_heads}) must be divisible by n_kv_heads ({})",
                self.n_kv_heads
            );
        }
        if !self.scale.is_finite() || self.scale <= 0.0 {
            candle_core::bail!("attention scale must be finite and positive");
        }

        let q_elements = checked_contiguous_elements(q, "q")?;
        let expected_q = checked_product(&[batch, n_heads, head_dim], "q")?;
        if q_elements != expected_q {
            candle_core::bail!("q logical element count does not match its dimensions");
        }

        let (cache_rows, cache_kv_heads, cache_head_dim) = match k_cache.dims() {
            [rows, kv_heads, cache_head_dim] => (*rows, *kv_heads, *cache_head_dim),
            _ => candle_core::bail!(
                "k_cache must be (num_regions * max_seq_per_slot, n_kv_heads, head_dim)"
            ),
        };
        if cache_rows == 0 || cache_kv_heads == 0 || cache_head_dim == 0 {
            candle_core::bail!("k_cache dimensions must all be non-zero");
        }
        if k_cache.dims() != v_cache.dims() {
            candle_core::bail!("k_cache and v_cache shapes must match exactly");
        }
        if cache_kv_heads != self.n_kv_heads || cache_head_dim != head_dim {
            candle_core::bail!(
                "cache shape must use n_kv_heads {} and q head_dim {head_dim}",
                self.n_kv_heads
            );
        }
        let cache_elements = checked_contiguous_elements(k_cache, "k_cache")?;
        let v_cache_elements = checked_contiguous_elements(v_cache, "v_cache")?;
        if cache_elements != v_cache_elements {
            candle_core::bail!("k_cache and v_cache element counts must match");
        }

        let kv_dim = checked_product(&[self.n_kv_heads, head_dim], "kv_dim")?;
        if self.kv_slot_stride == 0 || !self.kv_slot_stride.is_multiple_of(kv_dim) {
            candle_core::bail!(
                "kv_slot_stride ({}) must be a non-zero multiple of kv_dim ({kv_dim})",
                self.kv_slot_stride
            );
        }
        if cache_elements % self.kv_slot_stride != 0 {
            candle_core::bail!(
                "cache element count ({cache_elements}) must be divisible by kv_slot_stride ({})",
                self.kv_slot_stride
            );
        }
        let max_seq_per_slot = self.kv_slot_stride / kv_dim;
        if max_seq_per_slot > MAX_HAWKING_SEQ_LEN {
            candle_core::bail!(
                "slot capacity {max_seq_per_slot} exceeds Hawking context limit {MAX_HAWKING_SEQ_LEN}"
            );
        }
        if cache_rows % max_seq_per_slot != 0 {
            candle_core::bail!(
                "cache rows ({cache_rows}) must contain whole slots of {max_seq_per_slot} rows"
            );
        }
        let num_regions = cache_elements / self.kv_slot_stride;
        if num_regions == 0 {
            candle_core::bail!("cache must contain at least one KV region");
        }
        let mut max_live_seq = 0usize;
        for (&region, &position) in self.regions.iter().zip(&self.positions) {
            let region = region as usize;
            if region >= num_regions {
                candle_core::bail!("region {region} out of bounds for {num_regions} cache regions");
            }
            let position = position as usize;
            if position >= max_seq_per_slot {
                candle_core::bail!(
                    "position {position} out of bounds for slot capacity {max_seq_per_slot}"
                );
            }
            let live_seq = position.checked_add(1).ok_or_else(|| {
                candle_core::Error::Msg("attention sequence length overflow".to_string())
            })?;
            max_live_seq = max_live_seq.max(live_seq);

            // Prove the exact shader address expression cannot wrap or leave the
            // logical cache, independently of the aggregate divisibility checks.
            let slot_start = region.checked_mul(self.kv_slot_stride).ok_or_else(|| {
                candle_core::Error::Msg("cache region offset overflow".to_string())
            })?;
            let live_elements = live_seq
                .checked_mul(kv_dim)
                .ok_or_else(|| candle_core::Error::Msg("live KV span overflow".to_string()))?;
            let slot_end = slot_start.checked_add(live_elements).ok_or_else(|| {
                candle_core::Error::Msg("live KV end offset overflow".to_string())
            })?;
            if slot_end > cache_elements {
                candle_core::bail!("live KV span exceeds cache storage");
            }
        }

        // Every buffer index in both MSL kernels is a `uint`, not `usize`.
        // Reject a logically valid host layout if its offsets would wrap on GPU.
        checked_u32(batch, "batch")?;
        let n_heads_u32 = checked_u32(n_heads, "n_heads")?;
        let head_dim_u32 = checked_u32(head_dim, "head_dim")?;
        let n_kv_heads_u32 = checked_u32(self.n_kv_heads, "n_kv_heads")?;
        let group_size_u32 = checked_u32(n_heads / self.n_kv_heads, "group_size")?;
        let kv_slot_stride_u32 = checked_u32(self.kv_slot_stride, "kv_slot_stride")?;
        checked_u32(expected_q, "q/output element count")?;
        checked_u32(cache_elements, "cache element count")?;

        let shmem_elements = max_live_seq.checked_add(TG_SIZE).ok_or_else(|| {
            candle_core::Error::Msg("threadgroup-memory element count overflow".to_string())
        })?;
        let shmem_bytes = shmem_elements
            .checked_mul(std::mem::size_of::<f32>())
            .ok_or_else(|| {
                candle_core::Error::Msg("threadgroup-memory byte count overflow".to_string())
            })?;

        Ok(DecodeContract {
            batch,
            n_heads,
            head_dim,
            cache_elements,
            out_elements: expected_q,
            max_live_seq,
            shmem_bytes,
            head_dim_u32,
            n_heads_u32,
            n_kv_heads_u32,
            group_size_u32,
            kv_slot_stride_u32,
        })
    }
}

impl CustomOp3 for MultiSeqDecodeAttention {
    fn name(&self) -> &'static str {
        "hawking-mha-decode-multiseq"
    }

    fn cpu_fwd(
        &self,
        s1: &CpuStorage,
        l1: &Layout,
        s2: &CpuStorage,
        l2: &Layout,
        s3: &CpuStorage,
        l3: &Layout,
    ) -> Result<(CpuStorage, Shape)> {
        let contract = self.validate_contract(l1, l2, l3)?;
        let q_storage = match s1 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("q must be f32"),
        };
        let k_storage = match s2 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("k_cache must be f32"),
        };
        let v_storage = match s3 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("v_cache must be f32"),
        };
        validate_storage_elements(l1, contract.out_elements, q_storage.len(), "q")?;
        validate_storage_elements(l2, contract.cache_elements, k_storage.len(), "k_cache")?;
        validate_storage_elements(l3, contract.cache_elements, v_storage.len(), "v_cache")?;
        let q = &q_storage[l1.start_offset()..l1.start_offset() + contract.out_elements];
        let k_cache = &k_storage[l2.start_offset()..l2.start_offset() + contract.cache_elements];
        let v_cache = &v_storage[l3.start_offset()..l3.start_offset() + contract.cache_elements];

        let group_size = contract.n_heads / self.n_kv_heads;
        let mut out = try_zeroed_f32(contract.out_elements, "attention output")?;
        let mut score_storage = try_zeroed_f32(contract.max_live_seq, "attention scores")?;
        for bi in 0..contract.batch {
            let region = self.regions[bi] as usize;
            let seq = self.positions[bi] as usize + 1;
            let slot_start = region * self.kv_slot_stride;
            let slot_end = slot_start + self.kv_slot_stride;
            let k_slot = &k_cache[slot_start..slot_end];
            let v_slot = &v_cache[slot_start..slot_end];
            for h in 0..contract.n_heads {
                let kv_h = h / group_size;
                let q_h =
                    &q[(bi * contract.n_heads + h) * contract.head_dim..][..contract.head_dim];
                let scores = &mut score_storage[..seq];
                for t in 0..seq {
                    let kt = &k_slot[(t * self.n_kv_heads + kv_h) * contract.head_dim..]
                        [..contract.head_dim];
                    let acc: f32 = q_h.iter().zip(kt).map(|(a, b)| a * b).sum();
                    scores[t] = acc * self.scale;
                }
                let max_score = scores.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
                let mut sum = 0f32;
                for s in scores.iter_mut() {
                    *s = (*s - max_score).exp();
                    sum += *s;
                }
                let inv_sum = 1.0 / sum;
                let out_h = &mut out[(bi * contract.n_heads + h) * contract.head_dim..]
                    [..contract.head_dim];
                for i in 0..contract.head_dim {
                    let mut acc = 0f32;
                    for t in 0..seq {
                        let vt = v_slot[(t * self.n_kv_heads + kv_h) * contract.head_dim + i];
                        acc += scores[t] * vt;
                    }
                    out_h[i] = acc * inv_sum;
                }
            }
        }
        Ok((
            CpuStorage::F32(out),
            Shape::from((contract.batch, contract.n_heads, contract.head_dim)),
        ))
    }

    fn metal_fwd(
        &self,
        s1: &MetalStorage,
        l1: &Layout,
        s2: &MetalStorage,
        l2: &Layout,
        s3: &MetalStorage,
        l3: &Layout,
    ) -> Result<(MetalStorage, Shape)> {
        use candle_metal_kernels::utils::set_param;
        use objc2_metal::MTLResourceUsage;
        use objc2_metal::MTLSize;
        use objc2_metal::{MTLComputePipelineState as _, MTLDevice as _};

        let contract = self.validate_contract(l1, l2, l3)?;
        if l1.start_offset() != 0 || l2.start_offset() != 0 || l3.start_offset() != 0 {
            candle_core::bail!("hawking multiseq decode requires unsliced (offset 0) inputs");
        }
        if s1.dtype() != DType::F32 || s2.dtype() != DType::F32 || s3.dtype() != DType::F32 {
            candle_core::bail!("hawking multiseq decode requires f32 q/k_cache/v_cache");
        }
        if s1.device().id() != s2.device().id() || s1.device().id() != s3.device().id() {
            candle_core::bail!("hawking multiseq decode inputs must be on the same Metal device");
        }
        validate_metal_storage(s1, l1, contract.out_elements, "q")?;
        validate_metal_storage(s2, l2, contract.cache_elements, "k_cache")?;
        validate_metal_storage(s3, l3, contract.cache_elements, "v_cache")?;

        let device = s1.device().clone();
        let device_max_threadgroup_memory =
            device.metal_device().as_ref().maxThreadgroupMemoryLength();
        if contract.shmem_bytes > device_max_threadgroup_memory {
            candle_core::bail!(
                "hawking multiseq decode needs {} bytes of threadgroup memory, device supports {device_max_threadgroup_memory}",
                contract.shmem_bytes
            );
        }
        let pipeline = compile_pipeline(&device, "mha_decode_f32_batched_multiseq")?;
        validate_attention_metal_limits(
            contract,
            device_max_threadgroup_memory,
            pipeline.max_total_threads_per_threadgroup(),
            pipeline.as_ref().staticThreadgroupMemoryLength(),
        )?;
        let positions_buf = device.new_buffer_with_data(&self.positions)?;
        let regions_buf = device.new_buffer_with_data(&self.regions)?;
        let out_buf = device.new_buffer(contract.out_elements, DType::F32, "hawking_mha_out")?;

        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&pipeline);
        set_param(&encoder, 0, s1.buffer());
        set_param(&encoder, 1, s2.buffer());
        set_param(&encoder, 2, s3.buffer());
        set_param(&encoder, 3, &*out_buf);
        set_param(&encoder, 4, &*positions_buf);
        set_param(&encoder, 5, &*regions_buf);
        set_param(&encoder, 6, contract.head_dim_u32);
        set_param(&encoder, 7, contract.n_heads_u32);
        set_param(&encoder, 8, contract.n_kv_heads_u32);
        set_param(&encoder, 9, contract.group_size_u32);
        set_param(&encoder, 10, contract.kv_slot_stride_u32);
        set_param(&encoder, 11, self.scale);

        encoder.set_threadgroup_memory_length(0, contract.shmem_bytes);

        for buf in [
            s1.buffer(),
            s2.buffer(),
            s3.buffer(),
            &*positions_buf,
            &*regions_buf,
        ] {
            encoder.use_resource(buf, MTLResourceUsage::Read);
        }
        encoder.use_resource(&*out_buf, MTLResourceUsage::Write);

        let threadgroups = MTLSize {
            width: contract.n_heads,
            height: contract.batch,
            depth: 1,
        };
        let threads_per_tg = MTLSize {
            width: TG_SIZE,
            height: 1,
            depth: 1,
        };
        encoder.dispatch_thread_groups(threadgroups, threads_per_tg);

        Ok((
            MetalStorage::new(out_buf, device, contract.out_elements, DType::F32),
            Shape::from((contract.batch, contract.n_heads, contract.head_dim)),
        ))
    }
}

/// Slot-strided KV append: writes `src` (`(batch, kv_dim)`) into `dst` (the flat
/// `(num_regions * max_seq_per_slot * kv_dim)` cache buffer) at each slot's own
/// `region*slot_stride + position*kv_dim` offset. Call once with K as `src`/`dst`
/// pair and once with V — see module docs for why this splits Hawking's fused
/// single-dispatch original into two calls of the same kernel.
pub struct KvScatterAppend {
    pub regions: Vec<u32>,
    pub positions: Vec<u32>,
    pub kv_dim: usize,
    pub slot_stride: usize,
}

impl KvScatterAppend {
    fn validate_contract(&self, dst: &Layout, src: &Layout) -> Result<ScatterContract> {
        let batch = self.regions.len();
        if batch == 0 {
            candle_core::bail!("hawking kv scatter batch must be non-zero");
        }
        if self.positions.len() != batch {
            candle_core::bail!("positions and regions lengths must match");
        }
        ensure_unique_regions(&self.regions, "hawking kv scatter batch")?;
        if self.kv_dim == 0 {
            candle_core::bail!("kv_dim must be non-zero");
        }
        if self.slot_stride == 0 || !self.slot_stride.is_multiple_of(self.kv_dim) {
            candle_core::bail!(
                "slot_stride ({}) must be a non-zero multiple of kv_dim ({})",
                self.slot_stride,
                self.kv_dim
            );
        }

        let (dst_rows, dst_kv_heads, dst_head_dim) = match dst.dims() {
            [rows, kv_heads, head_dim] => (*rows, *kv_heads, *head_dim),
            _ => candle_core::bail!(
                "kv cache must be (num_regions * max_seq_per_slot, n_kv_heads, head_dim)"
            ),
        };
        if dst_rows == 0 || dst_kv_heads == 0 || dst_head_dim == 0 {
            candle_core::bail!("kv cache dimensions must all be non-zero");
        }
        let dst_row_elements = checked_product(&[dst_kv_heads, dst_head_dim], "kv cache row")?;
        if dst_row_elements != self.kv_dim {
            candle_core::bail!(
                "kv cache row width ({dst_row_elements}) must equal kv_dim ({})",
                self.kv_dim
            );
        }
        let dst_elements = checked_contiguous_elements(dst, "kv cache")?;
        if dst_elements % self.slot_stride != 0 {
            candle_core::bail!(
                "kv cache element count ({dst_elements}) must be divisible by slot_stride ({})",
                self.slot_stride
            );
        }

        if !matches!(src.dims().len(), 2 | 3) {
            candle_core::bail!("src must be (batch, kv_dim) or (batch, n_kv_heads, head_dim)");
        }
        if src.dims()[0] != batch {
            candle_core::bail!("src first dimension must equal the non-zero scatter batch");
        }
        if src.dims()[1..].contains(&0) {
            candle_core::bail!("src per-row dimensions must be non-zero");
        }
        let src_row_elements = checked_product(&src.dims()[1..], "src row")?;
        if src_row_elements != self.kv_dim {
            candle_core::bail!(
                "src row width ({src_row_elements}) must equal kv_dim ({})",
                self.kv_dim
            );
        }
        let src_elements = checked_contiguous_elements(src, "src")?;
        let total_threads = batch
            .checked_mul(self.kv_dim)
            .ok_or_else(|| candle_core::Error::Msg("scatter grid size overflow".to_string()))?;
        if src_elements != total_threads {
            candle_core::bail!("src element count must equal batch * kv_dim");
        }

        let max_seq_per_slot = self.slot_stride / self.kv_dim;
        if max_seq_per_slot > MAX_HAWKING_SEQ_LEN {
            candle_core::bail!(
                "slot capacity {max_seq_per_slot} exceeds Hawking context limit {MAX_HAWKING_SEQ_LEN}"
            );
        }
        if dst_rows % max_seq_per_slot != 0 {
            candle_core::bail!(
                "kv cache rows ({dst_rows}) must contain whole slots of {max_seq_per_slot} rows"
            );
        }
        let num_regions = dst_elements / self.slot_stride;
        if num_regions == 0 {
            candle_core::bail!("kv cache must contain at least one region");
        }
        for (&region, &position) in self.regions.iter().zip(&self.positions) {
            let region = region as usize;
            if region >= num_regions {
                candle_core::bail!("region {region} out of bounds for {num_regions} cache regions");
            }
            let position = position as usize;
            if position >= max_seq_per_slot {
                candle_core::bail!(
                    "position {position} out of bounds for slot capacity {max_seq_per_slot}"
                );
            }
            let dst_offset = region
                .checked_mul(self.slot_stride)
                .and_then(|offset| {
                    position
                        .checked_mul(self.kv_dim)
                        .and_then(|position_offset| offset.checked_add(position_offset))
                })
                .ok_or_else(|| {
                    candle_core::Error::Msg("scatter destination overflow".to_string())
                })?;
            let dst_end = dst_offset.checked_add(self.kv_dim).ok_or_else(|| {
                candle_core::Error::Msg("scatter destination end overflow".to_string())
            })?;
            if dst_end > dst_elements {
                candle_core::bail!("scatter destination exceeds kv cache");
            }
        }

        checked_u32(batch, "scatter batch")?;
        checked_u32(total_threads, "scatter grid size")?;
        checked_u32(dst_elements, "kv cache element count")?;
        let kv_dim_u32 = checked_u32(self.kv_dim, "kv_dim")?;
        let slot_stride_u32 = checked_u32(self.slot_stride, "slot_stride")?;

        Ok(ScatterContract {
            batch,
            dst_elements,
            src_elements,
            total_threads,
            kv_dim_u32,
            slot_stride_u32,
        })
    }
}

impl InplaceOp2 for KvScatterAppend {
    fn name(&self) -> &'static str {
        "hawking-kv-scatter-append"
    }

    fn cpu_fwd(
        &self,
        s1: &mut CpuStorage,
        l1: &Layout,
        s2: &CpuStorage,
        l2: &Layout,
    ) -> Result<()> {
        let contract = self.validate_contract(l1, l2)?;
        let dst = match s1 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("kv cache must be f32"),
        };
        let src_storage = match s2 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("src must be f32"),
        };
        validate_storage_elements(l1, contract.dst_elements, dst.len(), "kv cache")?;
        validate_storage_elements(l2, contract.src_elements, src_storage.len(), "src")?;
        let dst_base = l1.start_offset();
        let src = &src_storage[l2.start_offset()..l2.start_offset() + contract.src_elements];
        for bi in 0..contract.batch {
            let dst_off = dst_base
                + self.regions[bi] as usize * self.slot_stride
                + self.positions[bi] as usize * self.kv_dim;
            let src_off = bi * self.kv_dim;
            dst[dst_off..dst_off + self.kv_dim]
                .copy_from_slice(&src[src_off..src_off + self.kv_dim]);
        }
        Ok(())
    }

    fn metal_fwd(
        &self,
        s1: &mut MetalStorage,
        l1: &Layout,
        s2: &MetalStorage,
        l2: &Layout,
    ) -> Result<()> {
        use candle_metal_kernels::utils::set_param;
        use objc2_metal::MTLResourceUsage;
        use objc2_metal::MTLSize;

        let contract = self.validate_contract(l1, l2)?;
        if l1.start_offset() != 0 || l2.start_offset() != 0 {
            candle_core::bail!("hawking kv scatter requires unsliced (offset 0) inputs");
        }
        if s1.dtype() != DType::F32 || s2.dtype() != DType::F32 {
            candle_core::bail!("hawking kv scatter requires f32 cache/src");
        }
        if s1.device().id() != s2.device().id() {
            candle_core::bail!("hawking kv scatter inputs must be on the same Metal device");
        }
        if s1.buffer() == s2.buffer() {
            candle_core::bail!("hawking kv scatter src must not alias the mutable cache buffer");
        }
        validate_metal_storage(s1, l1, contract.dst_elements, "kv cache")?;
        validate_metal_storage(s2, l2, contract.src_elements, "src")?;
        let device = s1.device().clone();

        let pipeline = compile_pipeline(&device, "kv_scatter_append")?;
        let tg = scatter_threadgroup_size(
            contract.total_threads,
            pipeline.max_total_threads_per_threadgroup(),
        )?;
        let regions_buf = device.new_buffer_with_data(&self.regions)?;
        let positions_buf = device.new_buffer_with_data(&self.positions)?;

        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&pipeline);
        set_param(&encoder, 0, s2.buffer());
        set_param(&encoder, 1, s1.buffer());
        set_param(&encoder, 2, &*regions_buf);
        set_param(&encoder, 3, &*positions_buf);
        set_param(&encoder, 4, contract.kv_dim_u32);
        set_param(&encoder, 5, contract.slot_stride_u32);

        encoder.use_resource(s2.buffer(), MTLResourceUsage::Read);
        encoder.use_resource(&*regions_buf, MTLResourceUsage::Read);
        encoder.use_resource(&*positions_buf, MTLResourceUsage::Read);
        encoder.use_resource(s1.buffer(), MTLResourceUsage::Write);

        let grid = MTLSize {
            width: contract.total_threads,
            height: 1,
            depth: 1,
        };
        let threadgroup = MTLSize {
            width: tg,
            height: 1,
            depth: 1,
        };
        encoder.dispatch_threads(grid, threadgroup);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use candle_core::{Device, Tensor, D};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Barrier};

    #[test]
    fn cache_concurrent_first_touch_compiles_once() {
        const CALLERS: usize = 16;
        let cache = Arc::new(SingleFlightCache::<(&'static str, u8), usize>::new(16));
        let ready = Arc::new(Barrier::new(CALLERS));
        let compile_count = Arc::new(AtomicUsize::new(0));

        let threads: Vec<_> = (0..CALLERS)
            .map(|_| {
                let cache = Arc::clone(&cache);
                let ready = Arc::clone(&ready);
                let compile_count = Arc::clone(&compile_count);
                std::thread::spawn(move || {
                    ready.wait();
                    cache
                        .get_or_try_insert_with(("device-1", 0), || {
                            compile_count.fetch_add(1, Ordering::SeqCst);
                            Ok::<_, ()>(42)
                        })
                        .unwrap()
                })
            })
            .collect();

        for thread in threads {
            assert_eq!(thread.join().unwrap(), 42);
        }
        assert_eq!(
            compile_count.load(Ordering::SeqCst),
            1,
            "concurrent first-touch callers must share one compilation"
        );
    }

    #[test]
    fn cache_does_not_make_failed_compile_permanent() {
        let cache = SingleFlightCache::<u8, usize>::new(16);
        let compile_count = AtomicUsize::new(0);

        let first = cache.get_or_try_insert_with(7, || {
            compile_count.fetch_add(1, Ordering::SeqCst);
            Err::<usize, _>("transient compiler failure")
        });
        assert_eq!(first.unwrap_err(), "transient compiler failure");

        let second = cache
            .get_or_try_insert_with(7, || {
                compile_count.fetch_add(1, Ordering::SeqCst);
                Ok::<_, &str>(99)
            })
            .unwrap();
        let third = cache
            .get_or_try_insert_with(7, || {
                compile_count.fetch_add(1, Ordering::SeqCst);
                Ok::<_, &str>(100)
            })
            .unwrap();

        assert_eq!(second, 99);
        assert_eq!(third, 99, "the first successful value must stay cached");
        assert_eq!(
            compile_count.load(Ordering::SeqCst),
            2,
            "one failed attempt and one successful retry should run"
        );
    }

    #[test]
    fn cache_is_hard_bounded_under_device_id_churn() {
        let cache = SingleFlightCache::<u8, usize>::new(2);
        for key in 0..10 {
            assert_eq!(
                cache
                    .get_or_try_insert_with(key, || Ok::<_, ()>(key as usize))
                    .unwrap(),
                key as usize
            );
            assert!(
                cache
                    .entries
                    .read()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .len()
                    <= 2,
                "global Metal cache must never exceed its configured resource bound"
            );
        }
    }

    /// A deterministic pseudo-random f32 generator (no `rand` dependency needed in
    /// the test — a fixed LCG is enough for a reproducible, non-degenerate input).
    fn lcg_f32(seed: &mut u64) -> f32 {
        *seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
        ((*seed >> 33) as f32 / u32::MAX as f32) * 2.0 - 1.0
    }

    struct Scenario {
        batch: usize,
        n_heads: usize,
        n_kv_heads: usize,
        head_dim: usize,
        max_seq_per_slot: usize,
        num_regions: usize,
        lens: Vec<usize>,  // history length (>=1) per slot, indexed by slot
        regions: Vec<u32>, // region id per slot
        q: Vec<f32>,       // (batch, n_heads, head_dim)
        k_cache: Vec<f32>, // (num_regions*max_seq_per_slot, n_kv_heads, head_dim)
        v_cache: Vec<f32>,
    }

    impl Scenario {
        fn kv_dim(&self) -> usize {
            self.n_kv_heads * self.head_dim
        }
        fn slot_stride(&self) -> usize {
            self.max_seq_per_slot * self.kv_dim()
        }
        fn positions(&self) -> Vec<u32> {
            self.lens.iter().map(|&l| (l - 1) as u32).collect()
        }
    }

    fn build_scenario() -> Scenario {
        let n_heads = 4;
        let n_kv_heads = 2;
        let head_dim = 8;
        let max_seq_per_slot = 16;
        let num_regions = 3;
        let lens = vec![5usize, 3, 7];
        let regions = vec![0u32, 1, 2];
        let batch = lens.len();
        let kv_dim = n_kv_heads * head_dim;
        let slot_stride = max_seq_per_slot * kv_dim;

        let mut seed = 42u64;
        let q: Vec<f32> = (0..batch * n_heads * head_dim)
            .map(|_| lcg_f32(&mut seed))
            .collect();
        let mut k_cache = vec![0f32; num_regions * slot_stride];
        let mut v_cache = vec![0f32; num_regions * slot_stride];
        for (slot, &region) in regions.iter().enumerate() {
            let region = region as usize;
            for t in 0..lens[slot] {
                let off = region * slot_stride + t * kv_dim;
                for i in 0..kv_dim {
                    k_cache[off + i] = lcg_f32(&mut seed);
                    v_cache[off + i] = lcg_f32(&mut seed);
                }
            }
        }
        Scenario {
            batch,
            n_heads,
            n_kv_heads,
            head_dim,
            max_seq_per_slot,
            num_regions,
            lens,
            regions,
            q,
            k_cache,
            v_cache,
        }
    }

    fn assert_error_contains<T>(result: Result<T>, expected: &str) {
        let error = match result {
            Ok(_) => panic!("expected error containing {expected:?}"),
            Err(error) => error.to_string(),
        };
        assert!(
            error.contains(expected),
            "expected error containing {expected:?}, got {error:?}"
        );
    }

    fn valid_decode_op() -> MultiSeqDecodeAttention {
        MultiSeqDecodeAttention {
            positions: vec![2, 4],
            regions: vec![0, 1],
            n_kv_heads: 2,
            kv_slot_stride: 8 * 2 * 4,
            scale: 0.5,
        }
    }

    fn valid_decode_layouts() -> (Layout, Layout, Layout) {
        (
            Layout::contiguous((2, 4, 4)),
            Layout::contiguous((2 * 8, 2, 4)),
            Layout::contiguous((2 * 8, 2, 4)),
        )
    }

    fn valid_scatter_op() -> KvScatterAppend {
        KvScatterAppend {
            regions: vec![0, 1],
            positions: vec![2, 4],
            kv_dim: 8,
            slot_stride: 8 * 8,
        }
    }

    fn valid_scatter_layouts() -> (Layout, Layout) {
        (
            Layout::contiguous((2 * 8, 2, 4)),
            Layout::contiguous((2, 2, 4)),
        )
    }

    #[test]
    fn decode_rejects_empty_zero_and_unproven_batch_dimensions() {
        let (_, cache, v_cache) = valid_decode_layouts();
        let empty = MultiSeqDecodeAttention {
            positions: vec![],
            regions: vec![],
            ..valid_decode_op()
        };
        assert_error_contains(
            empty.validate_contract(&Layout::contiguous((0, 4, 4)), &cache, &v_cache),
            "must all be non-zero",
        );

        let one = MultiSeqDecodeAttention {
            positions: vec![0],
            regions: vec![0],
            ..valid_decode_op()
        };
        assert_error_contains(
            one.validate_contract(&Layout::contiguous((1, 0, 4)), &cache, &v_cache),
            "must all be non-zero",
        );
        assert_error_contains(
            one.validate_contract(&Layout::contiguous((1, 4, 0)), &cache, &v_cache),
            "must all be non-zero",
        );

        let too_wide = MultiSeqDecodeAttention {
            positions: vec![0; MAX_HAWKING_BATCH + 1],
            regions: (0..=MAX_HAWKING_BATCH as u32).collect(),
            ..valid_decode_op()
        };
        assert_error_contains(
            too_wide.validate_contract(
                &Layout::contiguous((MAX_HAWKING_BATCH + 1, 4, 4)),
                &cache,
                &v_cache,
            ),
            "exceeds proven Hawking limit",
        );
    }

    #[test]
    fn decode_rejects_invalid_head_grouping_and_metadata() {
        let (q, cache, v_cache) = valid_decode_layouts();
        let zero_kv_heads = MultiSeqDecodeAttention {
            n_kv_heads: 0,
            ..valid_decode_op()
        };
        assert_error_contains(
            zero_kv_heads.validate_contract(&q, &cache, &v_cache),
            "n_kv_heads must be non-zero",
        );

        let non_divisible = MultiSeqDecodeAttention {
            n_kv_heads: 3,
            ..valid_decode_op()
        };
        assert_error_contains(
            non_divisible.validate_contract(&q, &cache, &v_cache),
            "must be divisible",
        );

        let short_positions = MultiSeqDecodeAttention {
            positions: vec![0],
            ..valid_decode_op()
        };
        assert_error_contains(
            short_positions.validate_contract(&q, &cache, &v_cache),
            "length must equal",
        );

        let duplicate_regions = MultiSeqDecodeAttention {
            regions: vec![0, 0],
            ..valid_decode_op()
        };
        assert_error_contains(
            duplicate_regions.validate_contract(&q, &cache, &v_cache),
            "duplicate region",
        );

        for scale in [0.0, -1.0, f32::NAN, f32::INFINITY] {
            let invalid_scale = MultiSeqDecodeAttention {
                scale,
                ..valid_decode_op()
            };
            assert_error_contains(
                invalid_scale.validate_contract(&q, &cache, &v_cache),
                "finite and positive",
            );
        }
    }

    #[test]
    fn decode_rejects_shape_and_layout_mismatches() {
        let op = valid_decode_op();
        let (q, cache, v_cache) = valid_decode_layouts();
        assert_error_contains(
            op.validate_contract(&Layout::contiguous((2, 16)), &cache, &v_cache),
            "q must be",
        );
        let q_noncontiguous = Layout::new(Shape::from((2, 4, 4)), vec![16, 1, 4], 0);
        assert_error_contains(
            op.validate_contract(&q_noncontiguous, &cache, &v_cache),
            "q must be contiguous",
        );
        let q_bad_stride_rank = Layout::new(Shape::from((2, 4, 4)), vec![1], 0);
        assert_error_contains(
            op.validate_contract(&q_bad_stride_rank, &cache, &v_cache),
            "shape/stride rank mismatch",
        );
        assert_error_contains(
            op.validate_contract(&q, &Layout::contiguous((16, 8)), &v_cache),
            "k_cache must be",
        );
        assert_error_contains(
            op.validate_contract(&q, &cache, &Layout::contiguous((8, 2, 4))),
            "shapes must match",
        );
        assert_error_contains(
            op.validate_contract(&q, &Layout::contiguous((16, 1, 4)), &v_cache),
            "shapes must match",
        );
        let cache_noncontiguous = Layout::new(Shape::from((16, 2, 4)), vec![8, 1, 2], 0);
        assert_error_contains(
            op.validate_contract(&q, &cache_noncontiguous, &cache_noncontiguous),
            "k_cache must be contiguous",
        );
    }

    #[test]
    fn decode_rejects_stride_region_and_position_bounds() {
        let (q, cache, v_cache) = valid_decode_layouts();
        let zero_stride = MultiSeqDecodeAttention {
            kv_slot_stride: 0,
            ..valid_decode_op()
        };
        assert_error_contains(
            zero_stride.validate_contract(&q, &cache, &v_cache),
            "non-zero multiple",
        );
        let non_divisible_stride = MultiSeqDecodeAttention {
            kv_slot_stride: 65,
            ..valid_decode_op()
        };
        assert_error_contains(
            non_divisible_stride.validate_contract(&q, &cache, &v_cache),
            "non-zero multiple",
        );
        let partial_region_stride = MultiSeqDecodeAttention {
            kv_slot_stride: 6 * 2 * 4,
            ..valid_decode_op()
        };
        assert_error_contains(
            partial_region_stride.validate_contract(&q, &cache, &v_cache),
            "must be divisible by kv_slot_stride",
        );
        let over_context = MultiSeqDecodeAttention {
            kv_slot_stride: (MAX_HAWKING_SEQ_LEN + 1) * 2 * 4,
            ..valid_decode_op()
        };
        let over_context_cache = Layout::contiguous((2 * (MAX_HAWKING_SEQ_LEN + 1), 2, 4));
        assert_error_contains(
            over_context.validate_contract(&q, &over_context_cache, &over_context_cache),
            "exceeds Hawking context limit",
        );
        let bad_region = MultiSeqDecodeAttention {
            regions: vec![0, 2],
            ..valid_decode_op()
        };
        assert_error_contains(
            bad_region.validate_contract(&q, &cache, &v_cache),
            "region 2 out of bounds",
        );
        let bad_position = MultiSeqDecodeAttention {
            positions: vec![2, 8],
            ..valid_decode_op()
        };
        assert_error_contains(
            bad_position.validate_contract(&q, &cache, &v_cache),
            "position 8 out of bounds",
        );
    }

    #[test]
    fn decode_rejects_host_integer_overflow_and_metal_uint_overflow() {
        let op = MultiSeqDecodeAttention {
            positions: vec![0],
            regions: vec![0],
            n_kv_heads: 1,
            kv_slot_stride: 1,
            scale: 1.0,
        };
        let tiny_cache = Layout::contiguous((1, 1, 1));
        let overflow_q = Layout::new(Shape::from((1, usize::MAX, 2)), vec![usize::MAX, 2, 1], 0);
        assert_error_contains(
            op.validate_contract(&overflow_q, &tiny_cache, &tiny_cache),
            "element count overflow",
        );

        let above_u32 = u32::MAX as usize + 1;
        let too_many_heads = Layout::contiguous((1, above_u32, 1));
        assert_error_contains(
            op.validate_contract(&too_many_heads, &tiny_cache, &tiny_cache),
            "n_heads exceeds Metal uint range",
        );

        let enormous_cache = Layout::contiguous((above_u32, 1, 1));
        assert_error_contains(
            op.validate_contract(
                &Layout::contiguous((1, 1, 1)),
                &enormous_cache,
                &enormous_cache,
            ),
            "cache element count exceeds Metal uint range",
        );
    }

    #[test]
    fn decode_cpu_rejects_dtype_and_storage_bounds_without_panicking() {
        let op = MultiSeqDecodeAttention {
            positions: vec![0],
            regions: vec![0],
            n_kv_heads: 1,
            kv_slot_stride: 4,
            scale: 0.5,
        };
        let q_layout = Layout::contiguous((1, 2, 4));
        let cache_layout = Layout::contiguous((1, 1, 4));
        let q_wrong_dtype = CpuStorage::F64(vec![0.0; 8]);
        let q = CpuStorage::F32(vec![0.0; 8]);
        let cache = CpuStorage::F32(vec![0.0; 4]);
        assert_error_contains(
            op.cpu_fwd(
                &q_wrong_dtype,
                &q_layout,
                &cache,
                &cache_layout,
                &cache,
                &cache_layout,
            ),
            "q must be f32",
        );
        assert_error_contains(
            op.cpu_fwd(
                &CpuStorage::F32(vec![0.0; 1]),
                &q_layout,
                &cache,
                &cache_layout,
                &cache,
                &cache_layout,
            ),
            "q layout exceeds storage",
        );
        assert_error_contains(
            op.cpu_fwd(
                &q,
                &q_layout,
                &CpuStorage::F32(vec![0.0; 1]),
                &cache_layout,
                &cache,
                &cache_layout,
            ),
            "k_cache layout exceeds storage",
        );
        assert_error_contains(
            op.cpu_fwd(
                &q,
                &Layout::contiguous_with_offset((1, 2, 4), usize::MAX),
                &cache,
                &cache_layout,
                &cache,
                &cache_layout,
            ),
            "q storage offset overflow",
        );
    }

    #[test]
    fn scatter_rejects_empty_oversized_and_inconsistent_metadata() {
        let (dst, src) = valid_scatter_layouts();
        let empty = KvScatterAppend {
            regions: vec![],
            positions: vec![],
            ..valid_scatter_op()
        };
        assert_error_contains(
            empty.validate_contract(&dst, &src),
            "batch must be non-zero",
        );
        let short_positions = KvScatterAppend {
            positions: vec![0],
            ..valid_scatter_op()
        };
        assert_error_contains(
            short_positions.validate_contract(&dst, &src),
            "lengths must match",
        );
        let duplicates = KvScatterAppend {
            regions: vec![0, 0],
            ..valid_scatter_op()
        };
        assert_error_contains(duplicates.validate_contract(&dst, &src), "duplicate region");
        let too_wide = KvScatterAppend {
            regions: (0..=MAX_HAWKING_BATCH as u32).collect(),
            positions: vec![0; MAX_HAWKING_BATCH + 1],
            ..valid_scatter_op()
        };
        assert_error_contains(
            too_wide.validate_contract(&dst, &Layout::contiguous((MAX_HAWKING_BATCH + 1, 2, 4))),
            "exceeds proven Hawking limit",
        );
    }

    #[test]
    fn scatter_rejects_invalid_dimensions_stride_and_shapes() {
        let (dst, src) = valid_scatter_layouts();
        let zero_dim = KvScatterAppend {
            kv_dim: 0,
            ..valid_scatter_op()
        };
        assert_error_contains(
            zero_dim.validate_contract(&dst, &src),
            "kv_dim must be non-zero",
        );
        let zero_stride = KvScatterAppend {
            slot_stride: 0,
            ..valid_scatter_op()
        };
        assert_error_contains(
            zero_stride.validate_contract(&dst, &src),
            "non-zero multiple",
        );
        let non_divisible_stride = KvScatterAppend {
            slot_stride: 65,
            ..valid_scatter_op()
        };
        assert_error_contains(
            non_divisible_stride.validate_contract(&dst, &src),
            "non-zero multiple",
        );
        let partial_region_stride = KvScatterAppend {
            slot_stride: 6 * 8,
            ..valid_scatter_op()
        };
        assert_error_contains(
            partial_region_stride.validate_contract(&dst, &src),
            "must be divisible by slot_stride",
        );
        let over_context = KvScatterAppend {
            slot_stride: (MAX_HAWKING_SEQ_LEN + 1) * 8,
            ..valid_scatter_op()
        };
        assert_error_contains(
            over_context.validate_contract(
                &Layout::contiguous((2 * (MAX_HAWKING_SEQ_LEN + 1), 2, 4)),
                &src,
            ),
            "exceeds Hawking context limit",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&Layout::contiguous(128), &src),
            "kv cache must be",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&Layout::contiguous((16, 1, 4)), &src),
            "row width",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&dst, &Layout::contiguous(16)),
            "src must be",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&dst, &Layout::contiguous((2, 1, 2, 4))),
            "src must be",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&dst, &Layout::contiguous((1, 16))),
            "first dimension",
        );
        assert_error_contains(
            valid_scatter_op().validate_contract(&dst, &Layout::contiguous((2, 2, 3))),
            "row width",
        );
    }

    #[test]
    fn scatter_rejects_noncontiguous_region_position_and_integer_overflow() {
        let op = valid_scatter_op();
        let (dst, src) = valid_scatter_layouts();
        let dst_noncontiguous = Layout::new(Shape::from((16, 2, 4)), vec![8, 1, 2], 0);
        assert_error_contains(
            op.validate_contract(&dst_noncontiguous, &src),
            "kv cache must be contiguous",
        );
        let dst_bad_stride_rank = Layout::new(Shape::from((16, 2, 4)), vec![1], 0);
        assert_error_contains(
            op.validate_contract(&dst_bad_stride_rank, &src),
            "shape/stride rank mismatch",
        );
        let src_noncontiguous = Layout::new(Shape::from((2, 2, 4)), vec![8, 1, 2], 0);
        assert_error_contains(
            op.validate_contract(&dst, &src_noncontiguous),
            "src must be contiguous",
        );
        let bad_region = KvScatterAppend {
            regions: vec![0, 2],
            ..valid_scatter_op()
        };
        assert_error_contains(
            bad_region.validate_contract(&dst, &src),
            "region 2 out of bounds",
        );
        let bad_position = KvScatterAppend {
            positions: vec![2, 8],
            ..valid_scatter_op()
        };
        assert_error_contains(
            bad_position.validate_contract(&dst, &src),
            "position 8 out of bounds",
        );

        let huge = usize::MAX;
        let huge_op = KvScatterAppend {
            regions: vec![0],
            positions: vec![0],
            kv_dim: huge,
            slot_stride: huge,
        };
        assert_error_contains(
            huge_op.validate_contract(
                &Layout::contiguous((1, 1, huge)),
                &Layout::contiguous((1, huge)),
            ),
            "scatter grid size exceeds Metal uint range",
        );
    }

    #[test]
    fn scatter_cpu_rejects_dtype_and_storage_bounds_without_panicking() {
        let op = KvScatterAppend {
            regions: vec![0],
            positions: vec![0],
            kv_dim: 4,
            slot_stride: 8,
        };
        let dst_layout = Layout::contiguous((2, 1, 4));
        let src_layout = Layout::contiguous((1, 4));
        let mut dst = CpuStorage::F32(vec![0.0; 8]);
        assert_error_contains(
            op.cpu_fwd(
                &mut CpuStorage::F64(vec![0.0; 8]),
                &dst_layout,
                &CpuStorage::F32(vec![0.0; 4]),
                &src_layout,
            ),
            "kv cache must be f32",
        );
        assert_error_contains(
            op.cpu_fwd(
                &mut dst,
                &dst_layout,
                &CpuStorage::U32(vec![0; 4]),
                &src_layout,
            ),
            "src must be f32",
        );
        assert_error_contains(
            op.cpu_fwd(
                &mut CpuStorage::F32(vec![0.0; 1]),
                &dst_layout,
                &CpuStorage::F32(vec![0.0; 4]),
                &src_layout,
            ),
            "kv cache layout exceeds storage",
        );
        assert_error_contains(
            op.cpu_fwd(
                &mut CpuStorage::F32(vec![0.0; 8]),
                &dst_layout,
                &CpuStorage::F32(vec![0.0; 1]),
                &src_layout,
            ),
            "src layout exceeds storage",
        );
        assert_error_contains(
            op.cpu_fwd(
                &mut CpuStorage::F32(vec![0.0; 8]),
                &dst_layout,
                &CpuStorage::F32(vec![0.0; 4]),
                &Layout::contiguous_with_offset((1, 4), usize::MAX),
            ),
            "src storage offset overflow",
        );
    }

    #[test]
    fn dispatch_resource_limits_fail_closed() {
        let contract = DecodeContract {
            batch: 1,
            n_heads: 1,
            head_dim: 1,
            cache_elements: 1,
            out_elements: 1,
            max_live_seq: 1,
            shmem_bytes: 1024,
            head_dim_u32: 1,
            n_heads_u32: 1,
            n_kv_heads_u32: 1,
            group_size_u32: 1,
            kv_slot_stride_u32: 1,
        };
        assert_error_contains(
            validate_attention_metal_limits(contract, 512, TG_SIZE, 0),
            "threadgroup memory",
        );
        assert_error_contains(
            validate_attention_metal_limits(contract, 2048, TG_SIZE - 1, 0),
            "requires 128 threads",
        );
        assert_error_contains(
            validate_attention_metal_limits(contract, 1200, TG_SIZE, 200),
            "1024 dynamic + 200 static",
        );
        assert_error_contains(scatter_threadgroup_size(0, 256), "empty dispatch");
        assert_error_contains(scatter_threadgroup_size(1, 0), "supports zero threads");
        assert_eq!(scatter_threadgroup_size(1024, 192).unwrap(), 192);
    }

    /// An INDEPENDENT reference implementation built from Candle's own well-tested
    /// `matmul`/`softmax` ops (not a copy of the kernel's or `cpu_fwd`'s hand-rolled
    /// math) — per-slot standard scaled-dot-product attention over that slot's own
    /// history, on whatever device the input tensors live on.
    fn candle_reference(s: &Scenario, device: &Device) -> Result<Vec<f32>> {
        let group_size = s.n_heads / s.n_kv_heads;
        let scale = 1.0 / (s.head_dim as f64).sqrt();
        let mut out = vec![0f32; s.batch * s.n_heads * s.head_dim];
        for (slot, &region) in s.regions.iter().enumerate() {
            let region = region as usize;
            let seq = s.lens[slot];
            let slot_stride = s.slot_stride();
            let kv_dim = s.kv_dim();
            let k_hist: Vec<f32> =
                s.k_cache[region * slot_stride..region * slot_stride + seq * kv_dim].to_vec();
            let v_hist: Vec<f32> =
                s.v_cache[region * slot_stride..region * slot_stride + seq * kv_dim].to_vec();
            let k_t = Tensor::from_vec(k_hist, (seq, s.n_kv_heads, s.head_dim), device)?;
            let v_t = Tensor::from_vec(v_hist, (seq, s.n_kv_heads, s.head_dim), device)?;
            for h in 0..s.n_heads {
                let kv_h = h / group_size;
                let k_h = k_t.narrow(1, kv_h, 1)?.squeeze(1)?; // (seq, head_dim)
                let v_h = v_t.narrow(1, kv_h, 1)?.squeeze(1)?; // (seq, head_dim)
                let q_off = (slot * s.n_heads + h) * s.head_dim;
                let q_h = Tensor::from_vec(
                    s.q[q_off..q_off + s.head_dim].to_vec(),
                    (1, s.head_dim),
                    device,
                )?;
                let scores = (q_h.matmul(&k_h.t()?)? * scale)?; // (1, seq)
                let probs = candle_nn::ops::softmax(&scores, D::Minus1)?;
                let out_h = probs.matmul(&v_h)?; // (1, head_dim)
                let out_h: Vec<f32> = out_h.flatten_all()?.to_vec1()?;
                out[(slot * s.n_heads + h) * s.head_dim..][..s.head_dim].copy_from_slice(&out_h);
            }
        }
        Ok(out)
    }

    fn assert_close(a: &[f32], b: &[f32], atol: f32, ctx: &str) {
        assert_eq!(a.len(), b.len(), "{ctx}: length mismatch");
        for (i, (x, y)) in a.iter().zip(b).enumerate() {
            assert!(
                (x - y).abs() <= atol,
                "{ctx}: mismatch at {i}: {x} vs {y} (atol {atol})"
            );
        }
    }

    #[test]
    fn cpu_fwd_matches_independent_candle_reference() {
        let s = build_scenario();
        let device = Device::Cpu;
        let op = MultiSeqDecodeAttention {
            positions: s.positions(),
            regions: s.regions.clone(),
            n_kv_heads: s.n_kv_heads,
            kv_slot_stride: s.slot_stride(),
            scale: 1.0 / (s.head_dim as f32).sqrt(),
        };
        let q = Tensor::from_vec(s.q.clone(), (s.batch, s.n_heads, s.head_dim), &device).unwrap();
        let k_cache = Tensor::from_vec(
            s.k_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let v_cache = Tensor::from_vec(
            s.v_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let out = q.apply_op3_no_bwd(&k_cache, &v_cache, &op).unwrap();
        let out: Vec<f32> = out.flatten_all().unwrap().to_vec1().unwrap();
        let reference = candle_reference(&s, &device).unwrap();
        assert_close(&out, &reference, 1e-5, "cpu_fwd vs candle reference");
    }

    #[test]
    fn metal_fwd_matches_independent_candle_reference() {
        let device = match Device::new_metal(0) {
            Ok(d) => d,
            Err(_) => {
                eprintln!("skipping: no Metal device available on this host");
                return;
            }
        };
        let s = build_scenario();
        let op = MultiSeqDecodeAttention {
            positions: s.positions(),
            regions: s.regions.clone(),
            n_kv_heads: s.n_kv_heads,
            kv_slot_stride: s.slot_stride(),
            scale: 1.0 / (s.head_dim as f32).sqrt(),
        };
        let q = Tensor::from_vec(s.q.clone(), (s.batch, s.n_heads, s.head_dim), &device).unwrap();
        let k_cache = Tensor::from_vec(
            s.k_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let v_cache = Tensor::from_vec(
            s.v_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let out = q.apply_op3_no_bwd(&k_cache, &v_cache, &op).unwrap();
        let out: Vec<f32> = out.flatten_all().unwrap().to_vec1().unwrap();

        let reference = candle_reference(&s, &Device::Cpu).unwrap();
        assert_close(
            &out,
            &reference,
            1e-3,
            "REAL METAL GPU vs independent candle reference",
        );

        // Also cross-check against the hand-rolled CPU reference (cpu_fwd), on CPU
        // tensors, so a hypothetical error shared between cpu_fwd and the Metal
        // kernel (e.g. both misreading the source) is still caught by the
        // independent candle_reference check above, while this catches the
        // opposite: cpu_fwd and Metal disagreeing with each other.
        let cpu_device = Device::Cpu;
        let q_cpu =
            Tensor::from_vec(s.q.clone(), (s.batch, s.n_heads, s.head_dim), &cpu_device).unwrap();
        let k_cpu = Tensor::from_vec(
            s.k_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &cpu_device,
        )
        .unwrap();
        let v_cpu = Tensor::from_vec(
            s.v_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &cpu_device,
        )
        .unwrap();
        let out_cpu = q_cpu.apply_op3_no_bwd(&k_cpu, &v_cpu, &op).unwrap();
        let out_cpu: Vec<f32> = out_cpu.flatten_all().unwrap().to_vec1().unwrap();
        assert_close(&out, &out_cpu, 1e-3, "REAL METAL GPU vs cpu_fwd");
    }

    #[test]
    fn metal_backend_rejects_invalid_contracts_before_dispatch() {
        let device = match Device::new_metal(0) {
            Ok(device) => device,
            Err(_) => {
                eprintln!("skipping: no Metal device available on this host");
                return;
            }
        };

        let q_base = Tensor::zeros((2, 2, 4), DType::F32, &device).unwrap();
        let q_sliced = q_base.narrow(0, 1, 1).unwrap();
        let cache = Tensor::zeros((1, 1, 4), DType::F32, &device).unwrap();
        let decode = MultiSeqDecodeAttention {
            positions: vec![0],
            regions: vec![0],
            n_kv_heads: 1,
            kv_slot_stride: 4,
            scale: 0.5,
        };
        assert_error_contains(
            q_sliced.apply_op3_no_bwd(&cache, &cache, &decode),
            "requires unsliced",
        );

        let q = Tensor::zeros((1, 2, 4), DType::F32, &device).unwrap();
        let wrong_dtype_cache = Tensor::zeros((1, 1, 4), DType::U32, &device).unwrap();
        assert_error_contains(
            q.apply_op3_no_bwd(&wrong_dtype_cache, &wrong_dtype_cache, &decode),
            "requires f32",
        );

        let (q_storage, _) = q.storage_and_layout();
        let q_metal = match &*q_storage {
            candle_core::Storage::Metal(storage) => storage,
            _ => panic!("expected Metal storage"),
        };
        assert_error_contains(
            validate_metal_storage(
                q_metal,
                &Layout::contiguous((1_000_000, 1, 1)),
                1_000_000,
                "q",
            ),
            "layout exceeds Metal buffer",
        );
        drop(q_storage);

        let scatter_cache = Tensor::zeros((2, 1, 4), DType::F32, &device).unwrap();
        let scatter_src = Tensor::zeros((1, 4), DType::F32, &device).unwrap();
        let out_of_bounds_scatter = KvScatterAppend {
            regions: vec![0],
            positions: vec![2],
            kv_dim: 4,
            slot_stride: 8,
        };
        assert_error_contains(
            scatter_cache.inplace_op2(&scatter_src, &out_of_bounds_scatter),
            "position 2 out of bounds",
        );

        let scatter_src_wrong_dtype = Tensor::zeros((1, 4), DType::U32, &device).unwrap();
        let scatter = KvScatterAppend {
            regions: vec![0],
            positions: vec![0],
            kv_dim: 4,
            slot_stride: 8,
        };
        assert_error_contains(
            scatter_cache.inplace_op2(&scatter_src_wrong_dtype, &scatter),
            "requires f32",
        );
    }

    /// Proves the multi-seq property that actually matters: slots at DIFFERENT
    /// history lengths, sharing one dispatch, do not corrupt each other — each
    /// slot's output depends only on its own region's history, not on the other
    /// slots' (different-length) histories present in the same call.
    #[test]
    fn slots_are_independent_across_different_history_lengths() {
        let device = Device::Cpu;
        let mut s = build_scenario();
        let regions = s.regions.clone();
        let n_kv_heads = s.n_kv_heads;
        let kv_slot_stride = s.slot_stride();
        let scale = 1.0 / (s.head_dim as f32).sqrt();
        let op = |lens: &[usize]| MultiSeqDecodeAttention {
            positions: lens.iter().map(|&l| (l - 1) as u32).collect(),
            regions: regions.clone(),
            n_kv_heads,
            kv_slot_stride,
            scale,
        };
        let q = Tensor::from_vec(s.q.clone(), (s.batch, s.n_heads, s.head_dim), &device).unwrap();
        let k_cache = Tensor::from_vec(
            s.k_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let v_cache = Tensor::from_vec(
            s.v_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();

        // Baseline: slot lengths [5, 3, 7] (mismatched on purpose).
        let out_a = q
            .apply_op3_no_bwd(&k_cache, &v_cache, &op(&s.lens))
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();

        // Grow region 1's (slot 1's) history by one token WITHOUT touching slot 0
        // or slot 2's regions or query. Slot 1's output must change; slots 0 and 2
        // must not, proving no cross-slot leakage through the shared dispatch.
        let mut seed = 999u64;
        let extra_off = s.slot_stride() + 3 /* lens[1] */ * s.kv_dim();
        for i in 0..s.kv_dim() {
            s.k_cache[extra_off + i] = lcg_f32(&mut seed);
            s.v_cache[extra_off + i] = lcg_f32(&mut seed);
        }
        let mut lens2 = s.lens.clone();
        lens2[1] = 4;
        let k_cache2 = Tensor::from_vec(
            s.k_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let v_cache2 = Tensor::from_vec(
            s.v_cache.clone(),
            (s.num_regions * s.max_seq_per_slot, s.n_kv_heads, s.head_dim),
            &device,
        )
        .unwrap();
        let out_b = q
            .apply_op3_no_bwd(&k_cache2, &v_cache2, &op(&lens2))
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();

        let hd = s.head_dim;
        let nh = s.n_heads;
        let slot0 = 0..nh * hd;
        let slot1 = nh * hd..2 * nh * hd;
        let slot2 = 2 * nh * hd..3 * nh * hd;
        assert_close(
            &out_a[slot0.clone()],
            &out_b[slot0],
            1e-6,
            "slot 0 must be unaffected by slot 1's growth",
        );
        assert_close(
            &out_a[slot2.clone()],
            &out_b[slot2],
            1e-6,
            "slot 2 must be unaffected by slot 1's growth",
        );
        let changed = out_a[slot1.clone()]
            .iter()
            .zip(&out_b[slot1])
            .any(|(a, b)| (a - b).abs() > 1e-6);
        assert!(
            changed,
            "slot 1's own output must change when its own history grows"
        );
    }

    #[test]
    fn kv_scatter_append_matches_manual_index_arithmetic() {
        let device = Device::Cpu;
        let kv_dim = 4usize;
        let max_seq_per_slot = 6usize;
        let num_regions = 2usize;
        let slot_stride = max_seq_per_slot * kv_dim;
        let mut cache = vec![0f32; num_regions * slot_stride];

        // Step 1: append token at position 0 for region 0, position 2 for region 1.
        let src1 = vec![1.0, 2.0, 3.0, 4.0, /* slot1 */ 5.0, 6.0, 7.0, 8.0];
        let op1 = KvScatterAppend {
            regions: vec![0, 1],
            positions: vec![0, 2],
            kv_dim,
            slot_stride,
        };
        let cache_t = candle_core::Tensor::from_vec(
            cache.clone(),
            (num_regions * max_seq_per_slot, 1, kv_dim),
            &device,
        )
        .unwrap();
        let src_t = candle_core::Tensor::from_vec(src1.clone(), (2, kv_dim), &device).unwrap();
        cache_t.inplace_op2(&src_t, &op1).unwrap();
        cache = cache_t.flatten_all().unwrap().to_vec1::<f32>().unwrap();

        let mut expected = vec![0f32; num_regions * slot_stride];
        let region0_position0 = 0;
        expected[region0_position0..region0_position0 + kv_dim].copy_from_slice(&src1[0..kv_dim]);
        let region1_position2 = slot_stride + 2 * kv_dim;
        expected[region1_position2..region1_position2 + kv_dim]
            .copy_from_slice(&src1[kv_dim..2 * kv_dim]);
        assert_eq!(
            cache, expected,
            "first scatter must land at region*slot_stride + position*kv_dim"
        );

        // Step 2: a second append at a different position must not disturb step 1's data.
        let src2 = vec![
            9.0, 10.0, 11.0, 12.0, /* slot1 */ 13.0, 14.0, 15.0, 16.0,
        ];
        let op2 = KvScatterAppend {
            regions: vec![0, 1],
            positions: vec![1, 3],
            kv_dim,
            slot_stride,
        };
        let cache_t = candle_core::Tensor::from_vec(
            cache,
            (num_regions * max_seq_per_slot, 1, kv_dim),
            &device,
        )
        .unwrap();
        let src_t2 = candle_core::Tensor::from_vec(src2.clone(), (2, kv_dim), &device).unwrap();
        cache_t.inplace_op2(&src_t2, &op2).unwrap();
        let cache2 = cache_t.flatten_all().unwrap().to_vec1::<f32>().unwrap();

        let region0_position1 = kv_dim;
        expected[region0_position1..region0_position1 + kv_dim].copy_from_slice(&src2[0..kv_dim]);
        let region1_position3 = slot_stride + 3 * kv_dim;
        expected[region1_position3..region1_position3 + kv_dim]
            .copy_from_slice(&src2[kv_dim..2 * kv_dim]);
        assert_eq!(
            cache2, expected,
            "second scatter must not disturb the first append's data"
        );
    }

    #[test]
    fn kv_scatter_append_metal_matches_cpu() {
        let device = match Device::new_metal(0) {
            Ok(d) => d,
            Err(_) => {
                eprintln!("skipping: no Metal device available on this host");
                return;
            }
        };
        let kv_dim = 4usize;
        let max_seq_per_slot = 6usize;
        let num_regions = 2usize;
        let slot_stride = max_seq_per_slot * kv_dim;
        let src: Vec<f32> = vec![1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0];
        let op = KvScatterAppend {
            regions: vec![0, 1],
            positions: vec![2, 4],
            kv_dim,
            slot_stride,
        };

        let cache_cpu = candle_core::Tensor::zeros(
            (num_regions * max_seq_per_slot, 1, kv_dim),
            DType::F32,
            &Device::Cpu,
        )
        .unwrap();
        let src_cpu =
            candle_core::Tensor::from_vec(src.clone(), (2, kv_dim), &Device::Cpu).unwrap();
        cache_cpu.inplace_op2(&src_cpu, &op).unwrap();
        let expected = cache_cpu.flatten_all().unwrap().to_vec1::<f32>().unwrap();

        let cache_metal = candle_core::Tensor::zeros(
            (num_regions * max_seq_per_slot, 1, kv_dim),
            DType::F32,
            &device,
        )
        .unwrap();
        let src_metal = candle_core::Tensor::from_vec(src, (2, kv_dim), &device).unwrap();
        cache_metal.inplace_op2(&src_metal, &op).unwrap();
        let actual: Vec<f32> = cache_metal.flatten_all().unwrap().to_vec1().unwrap();

        assert_eq!(
            actual, expected,
            "REAL METAL GPU kv_scatter_append must match the CPU reference"
        );
    }
}
