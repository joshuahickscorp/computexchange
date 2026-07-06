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
//! WHAT THIS DOES NOT DO (equally load-bearing to say plainly): this is NOT the
//! wired Hawking lane. Missing, and explicitly out of scope for this change: RoPE
//! fusion (candle's own rotary embeddings apply upstream of this op today), the
//! Q4_K quantized projection GEMMs (this op accepts plain F32 Q/K/V — whatever
//! upstream projection produced them), wiring into `continuous_batch::Scheduler`'s
//! `decode_plan`, a `HawkingRunner`, prefix-KV reuse, and the cross-worker
//! determinism re-gate (weeks 3-6 of the port plan). Landing this does not change
//! `continuous_batch.rs`'s inert-by-default behavior — nothing calls this module
//! yet. It exists so week 3's wiring has a real, tested kernel to call instead of
//! the documented-only seam.
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
//! docs/HAWKING_PORT_PLAN.md's determinism re-gating plan. This module emits no
//! verification-class-visible output on its own (nothing calls it yet), so no
//! re-gate is needed until week 3's wiring lands.

#![cfg(feature = "metal")]
#![allow(dead_code)] // not yet wired into any runner — see module docs.

use candle_core::backend::BackendStorage;
use candle_core::{CpuStorage, CustomOp3, DType, InplaceOp2, Layout, MetalStorage, Result, Shape};

/// Runtime-compiled MSL source for both kernels. Compiled once per pipeline (see
/// `pipeline_cache`) via `Device::new_library_with_source` — the same runtime-source
/// approach Hawking itself uses (`hawking-core/src/metal/mod.rs`), not a
/// build-time `.metallib`.
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

/// Compile one named kernel function from `SHADER_SRC` into a pipeline. Mirrors
/// candle-core's own (feature="ug"-gated) `MetalDevice::compile`, minus the `ug`
/// IR codegen step — we hand it real MSL source directly, so no extra cargo
/// feature is needed. Fresh-compiled per call; a warm pool is a follow-up
/// (the correctness proof, not the performance one, is this change's job).
fn compile_pipeline(
    device: &candle_core::MetalDevice,
    func_name: &str,
) -> Result<candle_metal_kernels::metal::ComputePipeline> {
    let raw = device.metal_device();
    let lib = raw
        .new_library_with_source(SHADER_SRC, None)
        .map_err(|e| candle_core::Error::Metal(format!("hawking shader compile: {e}").into()))?;
    let func = lib.get_function(func_name, None).map_err(|e| {
        candle_core::Error::Metal(format!("hawking shader get_function {func_name}: {e}").into())
    })?;
    raw.new_compute_pipeline_state_with_function(&func)
        .map_err(|e| {
            candle_core::Error::Metal(format!("hawking shader pipeline {func_name}: {e}").into())
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
        let (batch, n_heads, head_dim) = match l1.dims() {
            [b, h, d] => (*b, *h, *d),
            _ => candle_core::bail!("q must be (batch, n_heads, head_dim)"),
        };
        if self.positions.len() != batch || self.regions.len() != batch {
            candle_core::bail!("positions/regions length must equal batch");
        }
        let q = match s1 {
            CpuStorage::F32(v) => &v[l1.start_offset()..],
            _ => candle_core::bail!("q must be f32"),
        };
        let k_cache = match s2 {
            CpuStorage::F32(v) => &v[l2.start_offset()..],
            _ => candle_core::bail!("k_cache must be f32"),
        };
        let v_cache = match s3 {
            CpuStorage::F32(v) => &v[l3.start_offset()..],
            _ => candle_core::bail!("v_cache must be f32"),
        };
        let group_size = n_heads / self.n_kv_heads;
        let mut out = vec![0f32; batch * n_heads * head_dim];
        for bi in 0..batch {
            let region = self.regions[bi] as usize;
            let seq = self.positions[bi] as usize + 1;
            let k_slot = &k_cache[region * self.kv_slot_stride..];
            let v_slot = &v_cache[region * self.kv_slot_stride..];
            for h in 0..n_heads {
                let kv_h = h / group_size;
                let q_h = &q[(bi * n_heads + h) * head_dim..][..head_dim];
                let mut scores = vec![0f32; seq];
                for t in 0..seq {
                    let kt = &k_slot[(t * self.n_kv_heads + kv_h) * head_dim..][..head_dim];
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
                let out_h = &mut out[(bi * n_heads + h) * head_dim..][..head_dim];
                for i in 0..head_dim {
                    let mut acc = 0f32;
                    for t in 0..seq {
                        let vt = v_slot[(t * self.n_kv_heads + kv_h) * head_dim + i];
                        acc += scores[t] * vt;
                    }
                    out_h[i] = acc * inv_sum;
                }
            }
        }
        Ok((
            CpuStorage::F32(out),
            Shape::from((batch, n_heads, head_dim)),
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

        let (batch, n_heads, head_dim) = match l1.dims() {
            [b, h, d] => (*b, *h, *d),
            _ => candle_core::bail!("q must be (batch, n_heads, head_dim)"),
        };
        if self.positions.len() != batch || self.regions.len() != batch {
            candle_core::bail!("positions/regions length must equal batch");
        }
        if l1.start_offset() != 0 || l2.start_offset() != 0 || l3.start_offset() != 0 {
            candle_core::bail!("hawking multiseq decode requires unsliced (offset 0) inputs");
        }
        if s1.dtype() != DType::F32 || s2.dtype() != DType::F32 || s3.dtype() != DType::F32 {
            candle_core::bail!("hawking multiseq decode requires f32 q/k_cache/v_cache");
        }

        let device = s1.device().clone();
        let positions_buf = device.new_buffer_with_data(&self.positions)?;
        let regions_buf = device.new_buffer_with_data(&self.regions)?;
        let out_count = batch * n_heads * head_dim;
        let out_buf = device.new_buffer(out_count, DType::F32, "hawking_mha_out")?;

        let pipeline = compile_pipeline(&device, "mha_decode_f32_batched_multiseq")?;
        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&pipeline);
        set_param(&encoder, 0, s1.buffer());
        set_param(&encoder, 1, s2.buffer());
        set_param(&encoder, 2, s3.buffer());
        set_param(&encoder, 3, &*out_buf);
        set_param(&encoder, 4, &*positions_buf);
        set_param(&encoder, 5, &*regions_buf);
        set_param(&encoder, 6, head_dim as u32);
        set_param(&encoder, 7, n_heads as u32);
        set_param(&encoder, 8, self.n_kv_heads as u32);
        set_param(&encoder, 9, (n_heads / self.n_kv_heads) as u32);
        set_param(&encoder, 10, self.kv_slot_stride as u32);
        set_param(&encoder, 11, self.scale);

        let max_seq = self.positions.iter().copied().max().unwrap_or(0) as usize + 1;
        let shmem_bytes = (max_seq + TG_SIZE) * std::mem::size_of::<f32>();
        encoder.set_threadgroup_memory_length(0, shmem_bytes);

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
            width: n_heads,
            height: batch,
            depth: 1,
        };
        let threads_per_tg = MTLSize {
            width: TG_SIZE,
            height: 1,
            depth: 1,
        };
        encoder.dispatch_thread_groups(threadgroups, threads_per_tg);

        Ok((
            MetalStorage::new(out_buf, device, out_count, DType::F32),
            Shape::from((batch, n_heads, head_dim)),
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
        let batch = self.regions.len();
        let dst = match s1 {
            CpuStorage::F32(v) => v,
            _ => candle_core::bail!("kv cache must be f32"),
        };
        let dst_base = l1.start_offset();
        let src = match s2 {
            CpuStorage::F32(v) => &v[l2.start_offset()..],
            _ => candle_core::bail!("src must be f32"),
        };
        for bi in 0..batch {
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

        if l1.start_offset() != 0 || l2.start_offset() != 0 {
            candle_core::bail!("hawking kv scatter requires unsliced (offset 0) inputs");
        }
        let batch = self.regions.len();
        let total = batch * self.kv_dim;
        let device = s1.device().clone();

        let regions_buf = device.new_buffer_with_data(&self.regions)?;
        let positions_buf = device.new_buffer_with_data(&self.positions)?;

        let pipeline = compile_pipeline(&device, "kv_scatter_append")?;
        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&pipeline);
        set_param(&encoder, 0, s2.buffer());
        set_param(&encoder, 1, s1.buffer());
        set_param(&encoder, 2, &*regions_buf);
        set_param(&encoder, 3, &*positions_buf);
        set_param(&encoder, 4, self.kv_dim as u32);
        set_param(&encoder, 5, self.slot_stride as u32);

        encoder.use_resource(s2.buffer(), MTLResourceUsage::Read);
        encoder.use_resource(&*regions_buf, MTLResourceUsage::Read);
        encoder.use_resource(&*positions_buf, MTLResourceUsage::Read);
        encoder.use_resource(s1.buffer(), MTLResourceUsage::Write);

        let tg = total.min(256);
        let grid = MTLSize {
            width: total,
            height: 1,
            depth: 1,
        };
        let threadgroup = MTLSize {
            width: tg.max(1),
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
        let extra_off = 1usize * s.slot_stride() + 3 /* lens[1] */ * s.kv_dim();
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
        let slot0 = 0 * nh * hd..1 * nh * hd;
        let slot1 = 1 * nh * hd..2 * nh * hd;
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
        let cache_t =
            candle_core::Tensor::from_vec(cache.clone(), num_regions * slot_stride, &device)
                .unwrap();
        let src_t = candle_core::Tensor::from_vec(src1.clone(), 2 * kv_dim, &device).unwrap();
        cache_t.inplace_op2(&src_t, &op1).unwrap();
        cache = cache_t.to_vec1::<f32>().unwrap();

        let mut expected = vec![0f32; num_regions * slot_stride];
        expected[0 * slot_stride + 0 * kv_dim..0 * slot_stride + 0 * kv_dim + kv_dim]
            .copy_from_slice(&src1[0..kv_dim]);
        expected[1 * slot_stride + 2 * kv_dim..1 * slot_stride + 2 * kv_dim + kv_dim]
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
        let cache_t =
            candle_core::Tensor::from_vec(cache, num_regions * slot_stride, &device).unwrap();
        let src_t2 = candle_core::Tensor::from_vec(src2.clone(), 2 * kv_dim, &device).unwrap();
        cache_t.inplace_op2(&src_t2, &op2).unwrap();
        let cache2 = cache_t.to_vec1::<f32>().unwrap();

        expected[0 * slot_stride + 1 * kv_dim..0 * slot_stride + 1 * kv_dim + kv_dim]
            .copy_from_slice(&src2[0..kv_dim]);
        expected[1 * slot_stride + 3 * kv_dim..1 * slot_stride + 3 * kv_dim + kv_dim]
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

        let cache_cpu =
            candle_core::Tensor::zeros(num_regions * slot_stride, DType::F32, &Device::Cpu)
                .unwrap();
        let src_cpu = candle_core::Tensor::from_vec(src.clone(), 2 * kv_dim, &Device::Cpu).unwrap();
        cache_cpu.inplace_op2(&src_cpu, &op).unwrap();
        let expected = cache_cpu.to_vec1::<f32>().unwrap();

        let cache_metal =
            candle_core::Tensor::zeros(num_regions * slot_stride, DType::F32, &device).unwrap();
        let src_metal = candle_core::Tensor::from_vec(src, 2 * kv_dim, &device).unwrap();
        cache_metal.inplace_op2(&src_metal, &op).unwrap();
        let actual: Vec<f32> = cache_metal.to_vec1().unwrap();

        assert_eq!(
            actual, expected,
            "REAL METAL GPU kv_scatter_append must match the CPU reference"
        );
    }
}
