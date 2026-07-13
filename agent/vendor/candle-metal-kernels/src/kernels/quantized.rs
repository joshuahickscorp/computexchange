use crate::utils::EncoderProvider;
use crate::{
    set_params, Buffer, ComputeCommandEncoder, Device, Kernels, MetalKernelError, Source,
    RESOURCE_OPTIONS,
};
use objc2_metal::{MTLResourceUsage, MTLSize};
use std::sync::OnceLock;

// Candle's Q4_K layout is one 144-byte super-block for 256 logical values.
// Split boundaries must stay on that boundary: unlike MLX affine 4-bit, scale
// and minimum metadata live inside each packed weight block.
const Q4K_BLOCK_SIZE: usize = 256;
const Q4K_TYPE_SIZE: usize = 144;
const Q4K_SPLITK_TARGET_THREADGROUPS: usize = 512;
const Q4K_SPLITK_MAX_WORKSPACE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Q4KSplitKPlan {
    partitions: usize,
    partition_k: usize,
    partition_stride: usize,
    workspace_bytes: usize,
}

fn q4k_splitk_plan(m: usize, n: usize, k: usize, logical_batch: usize) -> Option<Q4KSplitKPlan> {
    if !(2..=5).contains(&m)
        || n == 0
        || logical_batch != 1
        || k < 2 * Q4K_BLOCK_SIZE
        || !k.is_multiple_of(Q4K_BLOCK_SIZE)
    {
        return None;
    }

    // kernel_mul_mm tiles 64 output rows (N) by 32 input rows (M).
    let current_threadgroups = n.div_ceil(64).checked_mul(m.div_ceil(32))?;
    let mut partitions = Q4K_SPLITK_TARGET_THREADGROUPS
        .checked_div(current_threadgroups)?
        .max(1)
        .min(k / Q4K_BLOCK_SIZE);

    // Every partition must contain complete Q4_K blocks and all partitions
    // have the same K span. Decrementing mirrors MLX's conservative chooser.
    while partitions > 1 && !k.is_multiple_of(partitions * Q4K_BLOCK_SIZE) {
        partitions -= 1;
    }
    if partitions <= 1 {
        return None;
    }

    let partition_stride = m.checked_mul(n)?;
    let workspace_bytes = partitions
        .checked_mul(partition_stride)?
        .checked_mul(std::mem::size_of::<f32>())?;
    if workspace_bytes > Q4K_SPLITK_MAX_WORKSPACE_BYTES {
        return None;
    }

    Some(Q4KSplitKPlan {
        partitions,
        partition_k: k / partitions,
        partition_stride,
        workspace_bytes,
    })
}

fn q4k_skinny_shape_supported(m: usize, n: usize, k: usize, logical_batch: usize) -> bool {
    (2..=5).contains(&m)
        && n > 0
        && k >= Q4K_BLOCK_SIZE
        && k.is_multiple_of(Q4K_BLOCK_SIZE)
        // The established batch-1 q4x4 path remains unchanged. On a flattened
        // rank-3 batch, attention-KV (N=512) and MLP-up (N=8192/K=2048) were
        // only break-even in repeated AB/BA runs, so keep unproven midsize
        // projections on stock MM. Only MLP-down and the 128k output head
        // cleared a repeatable >=1.05x focused gate.
        && (logical_batch == 1 || (m == 4 && (k > 2_048 || n >= 65_536)))
}

/// Flatten Candle's leading activation dimensions only when the storage is one
/// fully contiguous row-major matrix. A verifier arrives as `(batch, span, K)`;
/// Q4_K has no per-batch weight state, so that is exactly the same matmul as
/// `(batch * span, K)`. Keeping the stride proof here prevents a future padded
/// or broadcast layout from accidentally entering the skinny kernels.
fn q4k_contiguous_flattened_rows(
    src1_shape: &[usize],
    src1_stride: &[usize],
    k: usize,
) -> Option<usize> {
    if src1_shape.len() != 4 || src1_stride.len() != 4 || src1_shape[3] != k {
        return None;
    }
    let element_bytes = std::mem::size_of::<f32>();
    let row_bytes = k.checked_mul(element_bytes)?;
    let plane_bytes = src1_shape[2].checked_mul(row_bytes)?;
    let batch_bytes = src1_shape[1].checked_mul(plane_bytes)?;
    if src1_stride != [batch_bytes, plane_bytes, row_bytes, element_bytes] {
        return None;
    }
    src1_shape[0]
        .checked_mul(src1_shape[1])?
        .checked_mul(src1_shape[2])
}

fn q4k_skinny_prefers_batched_mv(m: usize, n: usize, k: usize) -> bool {
    // Candle's bespoke Q4_K MV math is faster once four or more vectors create
    // enough independent threadgroups, but its upstream implementation writes
    // four output rows unconditionally. Keep all tail-N calls on the safe
    // llama.cpp-derived q4x4 kernel.
    n.is_multiple_of(4) && (m >= 5 || (m == 4 && (n > 512 || k > 2_048)))
}

fn env_flag(value: Option<std::ffi::OsString>) -> bool {
    value
        .and_then(|v| v.into_string().ok())
        .map(|v| {
            matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn q4k_splitk_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| env_flag(std::env::var_os("CX_Q4K_SPLITK")))
}

fn q4k_skinny_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| env_flag(std::env::var_os("CX_Q4K_SKINNY_M")))
}

#[derive(Debug, Clone, Copy)]
pub enum GgmlDType {
    Q4_0,
    Q4_1,
    Q5_0,
    Q5_1,
    Q8_0,
    Q8_1,
    Q2K,
    Q3K,
    Q4K,
    Q5K,
    Q6K,
    Q8K,
    F16,
    F32,
    BF16,
}

#[allow(clippy::too_many_arguments)]
pub fn call_quantized_matmul_mv_t(
    device: &Device,
    ep: impl EncoderProvider,
    kernels: &Kernels,
    dtype: GgmlDType,
    (b, m, n, k): (usize, usize, usize, usize),
    lhs: &Buffer,
    lhs_offset: usize,
    rhs: &Buffer,
    dst_offset: usize,
    dst: &Buffer,
) -> Result<(), MetalKernelError> {
    // Everything is in reverse
    let ne00 = k as i64;
    let ne01 = n as i64;
    let ne02 = b as i64;
    let ne03 = 1i64;

    let nb00 = 0i64;
    let nb01 = 0i64;
    let nb02 = 0i64;

    let ne10 = k as i64;
    let ne11 = m as i64;
    let ne12 = b as i64;
    let ne13 = 1i64;

    let nb10 = 0i64;
    let nb11 = 0i64;
    let nb12 = 0i64;

    let ne0 = n as i64;
    let ne1 = m as i64;
    let r2: u32 = (ne12 / ne02) as u32;
    let r3: u32 = (ne13 / ne03) as u32;

    let (nth0, nth1, align) = match dtype {
        GgmlDType::Q4_0
        | GgmlDType::Q4_1
        | GgmlDType::Q5_0
        | GgmlDType::Q5_1
        | GgmlDType::Q8_0
        | GgmlDType::Q8_1 => {
            let nth0 = 8;
            let nth1 = 8;
            let align = 8;
            (nth0, nth1, align)
        }
        GgmlDType::Q2K => {
            // Fixing a bug in Metal for GGML
            // https://github.com/ggerganov/llama.cpp/blob/b8109bc0139f15a5b321909f47510b89dca47ffc/ggml-metal.m#L1576
            let nth0 = 2;
            let nth1 = 32;
            let align = 4;
            (nth0, nth1, align)
        }
        GgmlDType::Q4K => {
            let nth0 = 4;
            let nth1 = 8;
            let align = 4;
            (nth0, nth1, align)
        }
        GgmlDType::Q3K | GgmlDType::Q5K => {
            let nth0 = 2;
            let nth1 = 32;
            let align = 4;
            (nth0, nth1, align)
        }
        GgmlDType::Q6K => {
            let nth0 = 2;
            let nth1 = 32;
            let align = 2;
            (nth0, nth1, align)
        }
        GgmlDType::F16 | GgmlDType::BF16 | GgmlDType::Q8K => {
            // Original implem uses rows
            let nth0 = 32;
            let nth1 = 1;
            let align = 8;
            (nth0, nth1, align)
        }
        GgmlDType::F32 => {
            let nth0 = 32;
            let nth1 = 1;
            let align = 8;
            (nth0, nth1, align)
        }
    };
    let thread_groups_count = MTLSize {
        width: divide(ne01 as usize, align),
        height: ne11 as usize,
        depth: (ne12 * ne13) as usize,
    };
    let threads_per_threadgroup = MTLSize {
        width: nth0,
        height: nth1,
        depth: 1,
    };
    let name = match dtype {
        GgmlDType::Q4_0 => "kernel_mul_mv_q4_0_f32",
        GgmlDType::Q4_1 => "kernel_mul_mv_q4_1_f32",
        GgmlDType::Q5_0 => "kernel_mul_mv_q5_0_f32",
        GgmlDType::Q5_1 => "kernel_mul_mv_q5_1_f32",
        GgmlDType::Q8_0 => "kernel_mul_mv_q8_0_f32",
        GgmlDType::Q8_1 => "kernel_mul_mv_q8_1_f32",
        GgmlDType::Q2K => "kernel_mul_mv_q2_K_f32",
        GgmlDType::Q3K => "kernel_mul_mv_q3_K_f32",
        GgmlDType::Q4K => "kernel_mul_mv_q4_K_f32",
        GgmlDType::Q5K => "kernel_mul_mv_q5_K_f32",
        GgmlDType::Q6K => "kernel_mul_mv_q6_K_f32",
        GgmlDType::Q8K => "kernel_mul_mv_q8_K_f32",
        GgmlDType::F16 => "kernel_mul_mv_f16_f32",
        GgmlDType::BF16 => "kernel_mul_mv_bf16_f32",
        GgmlDType::F32 => "kernel_mul_mv_f32_f32",
    };

    let pipeline = kernels.load_pipeline(device, Source::Quantized, name)?;
    let encoder = ep.encoder();
    let encoder: &ComputeCommandEncoder = encoder.as_ref();
    encoder.set_compute_pipeline_state(&pipeline);

    set_params!(
        encoder,
        (
            rhs,
            (lhs, lhs_offset),
            (dst, dst_offset),
            ne00,
            ne01,
            ne02,
            nb00,
            nb01,
            nb02,
            ne10,
            ne11,
            ne12,
            nb10,
            nb11,
            nb12,
            ne0,
            ne1,
            r2,
            r3
        )
    );
    encoder.use_resource(lhs, MTLResourceUsage::Read);
    encoder.use_resource(rhs, MTLResourceUsage::Read);
    encoder.use_resource(dst, MTLResourceUsage::Write);

    encoder.dispatch_thread_groups(thread_groups_count, threads_per_threadgroup);
    Ok(())
}

/// - src0 is usually weight
/// - src1 is usually xs
#[allow(clippy::too_many_arguments)]
pub fn call_quantized_matmul_mm_t(
    device: &Device,
    ep: impl EncoderProvider,
    kernels: &Kernels,
    dtype: GgmlDType,
    src0_shape: &[usize],
    src0_stride: &[usize],
    src0: &Buffer,
    src1_shape: &[usize],
    src1_stride: &[usize],
    src1: &Buffer,
    src1_offset: usize,
    dst_shape: &[usize],
    dst_offset: usize,
    dst: &Buffer,
) -> Result<(), MetalKernelError> {
    call_quantized_matmul_mm_t_impl(
        device,
        ep,
        kernels,
        dtype,
        src0_shape,
        src0_stride,
        src0,
        src1_shape,
        src1_stride,
        src1,
        src1_offset,
        dst_shape,
        dst_offset,
        dst,
        None,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
fn call_quantized_matmul_mm_t_impl(
    device: &Device,
    ep: impl EncoderProvider,
    kernels: &Kernels,
    dtype: GgmlDType,
    src0_shape: &[usize],
    src0_stride: &[usize],
    src0: &Buffer,
    src1_shape: &[usize],
    src1_stride: &[usize],
    src1: &Buffer,
    src1_offset: usize,
    dst_shape: &[usize],
    dst_offset: usize,
    dst: &Buffer,
    splitk_override: Option<bool>,
    skinny_override: Option<bool>,
) -> Result<(), MetalKernelError> {
    // Everything is in reverse
    let ne00 = src0_shape[src0_shape.len() - 1] as i64;
    let ne01 = src0_shape[src0_shape.len() - 2] as i64;
    let ne02 = src0_shape[src0_shape.len() - 3] as i64;
    let ne03 = src0_shape[src0_shape.len() - 4] as i64;

    let nb01 = src0_stride[src0_stride.len() - 2] as i64;
    let nb02 = src0_stride[src0_stride.len() - 3] as i64;
    let nb03 = src0_stride[src0_stride.len() - 4] as i64;

    let ne11 = src1_shape[src1_shape.len() - 2] as i64;
    let ne12 = src1_shape[src1_shape.len() - 3] as i64;
    let ne13 = src1_shape[src1_shape.len() - 4] as i64;

    let nb10 = src1_stride[src1_stride.len() - 1] as i64;
    let nb11 = src1_stride[src1_stride.len() - 2] as i64;
    let nb12 = src1_stride[src1_stride.len() - 3] as i64;
    let nb13 = src1_stride[src1_stride.len() - 4] as i64;

    let ne0 = dst_shape[dst_shape.len() - 1] as i64;
    let ne1 = dst_shape[dst_shape.len() - 2] as i64;
    let r2 = (ne12 / ne02) as u32;
    let r3 = (ne13 / ne03) as u32;

    let encoder = ep.encoder();
    let encoder: &ComputeCommandEncoder = encoder.as_ref();

    let use_splitk = splitk_override.unwrap_or_else(q4k_splitk_enabled);
    let use_skinny = skinny_override.unwrap_or_else(q4k_skinny_enabled);
    let q4k_row_bytes = (ne00 as usize / Q4K_BLOCK_SIZE) * Q4K_TYPE_SIZE;
    let q4k_flattened_rows = q4k_contiguous_flattened_rows(src1_shape, src1_stride, ne00 as usize);
    let q4k_layout_supported = matches!(dtype, GgmlDType::Q4K)
        && ne02 == 1
        && ne03 == 1
        && ne0 == ne01
        && ne1 == ne11
        && nb01 as usize == q4k_row_bytes
        && q4k_flattened_rows.is_some();

    if use_skinny && q4k_layout_supported {
        let flattened_rows = q4k_flattened_rows.expect("layout guard proved flattened rows");
        if q4k_skinny_shape_supported(
            flattened_rows,
            ne01 as usize,
            ne00 as usize,
            (ne12 * ne13) as usize,
        ) {
            return dispatch_q4k_skinny(
                device,
                encoder,
                kernels,
                src0,
                src1,
                src1_offset,
                dst,
                dst_offset,
                nb01 as u64,
                nb11 as u64,
                ne00 as usize,
                ne01 as usize,
                flattened_rows,
            );
        }
    }

    if use_splitk && q4k_layout_supported {
        if let Some(plan) = q4k_splitk_plan(
            ne11 as usize,
            ne01 as usize,
            ne00 as usize,
            (ne12 * ne13) as usize,
        ) {
            return dispatch_q4k_splitk(
                device,
                encoder,
                kernels,
                src0,
                src1,
                src1_offset,
                dst,
                dst_offset,
                nb01 as u64,
                nb10 as u64,
                nb11 as u64,
                ne01 as usize,
                ne11 as usize,
                plan,
            );
        }
    }

    let thread_groups_count = MTLSize {
        width: divide(ne11 as usize, 32),
        height: divide(ne01 as usize, 64),
        depth: (ne12 * ne13) as usize,
    };
    let threads_per_threadgroup = MTLSize {
        width: 128,
        height: 1,
        depth: 1,
    };
    let name = match dtype {
        GgmlDType::Q4_0 => "kernel_mul_mm_q4_0_f32",
        GgmlDType::Q4_1 => "kernel_mul_mm_q4_1_f32",
        GgmlDType::Q5_0 => "kernel_mul_mm_q5_0_f32",
        GgmlDType::Q5_1 => "kernel_mul_mm_q5_1_f32",
        GgmlDType::Q8_0 => "kernel_mul_mm_q8_0_f32",
        GgmlDType::Q2K => "kernel_mul_mm_q2_K_f32",
        GgmlDType::Q3K => "kernel_mul_mm_q3_K_f32",
        GgmlDType::Q4K => "kernel_mul_mm_q4_K_f32",
        GgmlDType::Q5K => "kernel_mul_mm_q5_K_f32",
        GgmlDType::Q6K => "kernel_mul_mm_q6_K_f32",
        GgmlDType::F16 => "kernel_mul_mm_f16_f32",
        GgmlDType::BF16 => "kernel_mul_mm_bf16_f32",
        GgmlDType::F32 => "kernel_mul_mm_f32_f32",
        GgmlDType::Q8_1 => Err(MetalKernelError::UnsupportedDTypeForOp("Q8_1", "qmatmul"))?,
        GgmlDType::Q8K => Err(MetalKernelError::UnsupportedDTypeForOp("Q8K", "qmatmul"))?,
    };

    let pipeline = kernels.load_pipeline(device, Source::Quantized, name)?;
    encoder.set_compute_pipeline_state(&pipeline);

    set_params!(
        encoder,
        (
            src0,
            (src1, src1_offset),
            (dst, dst_offset),
            ne00,
            ne02,
            nb01,
            nb02,
            nb03,
            ne12,
            nb10,
            nb11,
            nb12,
            nb13,
            ne0,
            ne1,
            r2,
            r3
        )
    );
    encoder.use_resource(src0, MTLResourceUsage::Read);
    encoder.use_resource(src1, MTLResourceUsage::Read);
    encoder.use_resource(dst, MTLResourceUsage::Write);

    encoder.set_threadgroup_memory_length(0, 8192);

    encoder.dispatch_thread_groups(thread_groups_count, threads_per_threadgroup);
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn dispatch_q4k_skinny(
    device: &Device,
    encoder: &ComputeCommandEncoder,
    kernels: &Kernels,
    src0: &Buffer,
    src1: &Buffer,
    src1_offset: usize,
    dst: &Buffer,
    dst_offset: usize,
    nb01: u64,
    nb11: u64,
    k: usize,
    n: usize,
    m: usize,
) -> Result<(), MetalKernelError> {
    if q4k_skinny_prefers_batched_mv(m, n, k) {
        return call_quantized_matmul_mv_t(
            device,
            encoder,
            kernels,
            GgmlDType::Q4K,
            (1, m, n, k),
            src1,
            src1_offset,
            src0,
            dst_offset,
            dst,
        );
    }

    let (name, k_lanes) = match m {
        2 => ("kernel_mul_mv_q4_K_f32_skinny_m2", 16),
        3 => ("kernel_mul_mv_q4_K_f32_skinny_m3", 8),
        4 => ("kernel_mul_mv_q4_K_f32_skinny_m4", 8),
        5 => ("kernel_mul_mv_q4_K_f32_skinny_m5", 8),
        _ => unreachable!("Q4_K skinny-M dispatch is guarded to M=2..5"),
    };
    let pipeline = kernels.load_pipeline(device, Source::Quantized, name)?;
    encoder.set_compute_pipeline_state(&pipeline);
    set_params!(
        encoder,
        (
            src0,
            (src1, src1_offset),
            (dst, dst_offset),
            nb01,
            nb11,
            k as i64,
            n as i64
        )
    );
    encoder.use_resource(src0, MTLResourceUsage::Read);
    encoder.use_resource(src1, MTLResourceUsage::Read);
    encoder.use_resource(dst, MTLResourceUsage::Write);

    let rows_per_threadgroup = (32 / k_lanes) * 2;
    encoder.dispatch_thread_groups(
        MTLSize {
            width: n.div_ceil(rows_per_threadgroup),
            height: 1,
            depth: 1,
        },
        MTLSize {
            width: 32,
            height: 2,
            depth: 1,
        },
    );
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn dispatch_q4k_splitk(
    device: &Device,
    encoder: &ComputeCommandEncoder,
    kernels: &Kernels,
    src0: &Buffer,
    src1: &Buffer,
    src1_offset: usize,
    dst: &Buffer,
    dst_offset: usize,
    nb01: u64,
    nb10: u64,
    nb11: u64,
    n: usize,
    m: usize,
    plan: Q4KSplitKPlan,
) -> Result<(), MetalKernelError> {
    let partials = device.new_buffer(plan.workspace_bytes, RESOURCE_OPTIONS)?;
    let split_pipeline =
        kernels.load_pipeline(device, Source::Quantized, "kernel_mul_mm_q4_K_f32_split_k")?;
    encoder.set_compute_pipeline_state(&split_pipeline);

    set_params!(
        encoder,
        (
            src0,
            (src1, src1_offset),
            &partials,
            nb01,
            nb10,
            nb11,
            n as i64,
            m as i64,
            plan.partition_k as i64,
            plan.partition_stride as u64
        )
    );
    encoder.use_resource(src0, MTLResourceUsage::Read);
    encoder.use_resource(src1, MTLResourceUsage::Read);
    encoder.use_resource(&partials, MTLResourceUsage::Write);
    encoder.set_threadgroup_memory_length(0, 8192);
    encoder.dispatch_thread_groups(
        MTLSize {
            width: m.div_ceil(32),
            height: n.div_ceil(64),
            depth: plan.partitions,
        },
        MTLSize {
            width: 128,
            height: 1,
            depth: 1,
        },
    );

    let reduce_pipeline =
        kernels.load_pipeline(device, Source::Quantized, "kernel_reduce_q4_K_split_k_f32")?;
    encoder.set_compute_pipeline_state(&reduce_pipeline);
    encoder.set_threadgroup_memory_length(0, 0);
    set_params!(
        encoder,
        (
            &partials,
            (dst, dst_offset),
            plan.partition_stride as u64,
            plan.partitions as u32
        )
    );
    encoder.use_resource(&partials, MTLResourceUsage::Read);
    encoder.use_resource(dst, MTLResourceUsage::Write);
    encoder.dispatch_threads(
        MTLSize {
            width: plan.partition_stride,
            height: 1,
            depth: 1,
        },
        MTLSize {
            width: 256.min(plan.partition_stride),
            height: 1,
            depth: 1,
        },
    );
    Ok(())
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
pub(crate) fn call_quantized_matmul_mm_t_for_test(
    device: &Device,
    ep: impl EncoderProvider,
    kernels: &Kernels,
    dtype: GgmlDType,
    src0_shape: &[usize],
    src0_stride: &[usize],
    src0: &Buffer,
    src1_shape: &[usize],
    src1_stride: &[usize],
    src1: &Buffer,
    src1_offset: usize,
    dst_shape: &[usize],
    dst_offset: usize,
    dst: &Buffer,
    splitk: bool,
    skinny: bool,
) -> Result<(), MetalKernelError> {
    call_quantized_matmul_mm_t_impl(
        device,
        ep,
        kernels,
        dtype,
        src0_shape,
        src0_stride,
        src0,
        src1_shape,
        src1_stride,
        src1,
        src1_offset,
        dst_shape,
        dst_offset,
        dst,
        Some(splitk),
        Some(skinny),
    )
}

fn divide(m: usize, b: usize) -> usize {
    m.div_ceil(b)
}

#[cfg(test)]
mod splitk_tests {
    use super::*;

    #[test]
    fn q4k_splitk_heuristic_covers_verifier_shapes_and_fallbacks() {
        let p = q4k_splitk_plan(4, 2_048, 2_048, 1).unwrap();
        assert_eq!(p.partitions, 8);
        assert_eq!(p.partition_k, 256);

        let p = q4k_splitk_plan(4, 8_192, 2_048, 1).unwrap();
        assert_eq!(p.partitions, 4);
        assert_eq!(p.partition_k, 512);

        let p = q4k_splitk_plan(4, 2_048, 8_192, 1).unwrap();
        assert_eq!(p.partitions, 16);
        assert_eq!(p.partition_k, 512);

        assert!(q4k_splitk_plan(1, 2_048, 2_048, 1).is_none());
        assert!(q4k_splitk_plan(6, 2_048, 2_048, 1).is_none());
        assert!(q4k_splitk_plan(4, 2_048, 2_048, 2).is_none());
        assert!(q4k_splitk_plan(4, 2_048, 384, 1).is_none());
        assert!(q4k_splitk_plan(4, 128_256, 2_048, 1).is_none());

        assert!(q4k_skinny_shape_supported(2, 65, 512, 1));
        assert!(q4k_skinny_shape_supported(5, 8_192, 2_048, 1));
        assert!(!q4k_skinny_shape_supported(4, 2_048, 2_048, 2));
        assert!(!q4k_skinny_shape_supported(2, 2_048, 8_192, 2));
        assert!(!q4k_skinny_shape_supported(4, 512, 2_048, 2));
        assert!(q4k_skinny_shape_supported(4, 512, 8_192, 2));
        assert!(!q4k_skinny_shape_supported(4, 8_192, 2_048, 2));
        assert!(q4k_skinny_shape_supported(4, 128_256, 2_048, 2));
        assert!(!q4k_skinny_shape_supported(1, 2_048, 2_048, 1));
        assert!(!q4k_skinny_shape_supported(6, 2_048, 2_048, 1));
        assert!(!q4k_skinny_shape_supported(4, 2_048, 384, 1));

        assert_eq!(
            q4k_contiguous_flattened_rows(&[1, 2, 2, 2_048], &[32_768, 16_384, 8_192, 4], 2_048,),
            Some(4)
        );
        assert_eq!(
            q4k_contiguous_flattened_rows(&[1, 4, 1, 2_048], &[32_768, 8_192, 8_192, 4], 2_048,),
            Some(4)
        );
        assert_eq!(
            q4k_contiguous_flattened_rows(&[1, 2, 2, 2_048], &[32_768, 16_384, 8_196, 4], 2_048,),
            None,
            "padded rows must stay on stock Candle dispatch"
        );
        assert!(!q4k_skinny_prefers_batched_mv(2, 2_048, 2_048));
        assert!(!q4k_skinny_prefers_batched_mv(3, 2_048, 2_048));
        assert!(!q4k_skinny_prefers_batched_mv(4, 512, 2_048));
        assert!(!q4k_skinny_prefers_batched_mv(5, 65, 2_048));
        assert!(q4k_skinny_prefers_batched_mv(4, 2_048, 2_048));
        assert!(q4k_skinny_prefers_batched_mv(4, 512, 8_192));
        assert!(q4k_skinny_prefers_batched_mv(5, 2_048, 2_048));
    }

    #[test]
    fn q4k_splitk_flag_is_explicit_opt_in() {
        assert!(!env_flag(None));
        for disabled in ["", "0", "false", "no", "off", "not-a-valid-mode"] {
            assert!(!env_flag(Some(disabled.into())));
        }
        assert!(env_flag(Some("1".into())));
        assert!(env_flag(Some("TRUE".into())));
        assert!(env_flag(Some(" on ".into())));
    }
}
