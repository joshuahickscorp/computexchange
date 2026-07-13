# CX Q4_K Metal optimization provenance

This directory vendors `candle-metal-kernels` 0.10.2 from the Candle commit
`7c7a8c570e6a16ea24cc30a7501c8ffbbdb51680`:

- https://github.com/huggingface/candle/tree/7c7a8c570e6a16ea24cc30a7501c8ffbbdb51680/candle-metal-kernels
- upstream package license: `MIT OR Apache-2.0`; both upstream license texts
  are retained as `LICENSE-MIT` and `LICENSE-APACHE`.

The first CX change is deliberately narrow: a Q4_K-only, non-batched Metal
split-K kernel and host dispatcher for verifier matrices with `M=2..5`. The split
policy and partition-major workspace/reduction design were adapted from MLX
PR #3120, merged as commit
`38ad257088fb2193ad47e527cf6534a689f30943`:

- https://github.com/ml-explore/mlx/pull/3120
- https://github.com/ml-explore/mlx/commit/38ad257088fb2193ad47e527cf6534a689f30943
- relevant MLX files: `mlx/backend/metal/kernels/quantized.h`,
  `mlx/backend/metal/kernels/quantized.metal`, and
  `mlx/backend/metal/quantized.cpp`.
- MLX license: MIT, Copyright (c) 2023 Apple Inc.; the source files carry
  `Copyright (c) 2023-2024 Apple Inc.` headers. The upstream license is
  retained verbatim as `LICENSE-MLX`.

No MLX affine quantization/dequantization code is copied.  MLX affine 4-bit
stores packed values plus separate uniform scale/bias tensors, while Candle's
GGUF Q4_K format stores 256 values in a 144-byte super-block with embedded
scale/min metadata.  The new Metal body is a Q4_K specialization of Candle's
existing `kernel_mul_mm`, with only the partition offsets and output workspace
added.

The later Q4_K skinny-M path adapts llama.cpp's small-batch q4x4 Metal kernel,
originally added by commit `0115df2f65ac7c64dd0e5915c72ecc4a9343a130`
(PR #10581) and inspected at current commit
`4f37f519722aa3242eecb7649466b4a4a2d6d6da`:

- https://github.com/ggml-org/llama.cpp/commit/0115df2f65ac7c64dd0e5915c72ecc4a9343a130
- https://github.com/ggml-org/llama.cpp/blob/4f37f519722aa3242eecb7649466b4a4a2d6d6da/ggml/src/ggml-metal/ggml-metal.metal
- relevant upstream symbols: `kernel_mul_mv_ext_q4x4_f32_impl` and
  `kernel_mul_mv_ext_q4_K_f32_r1_2` through `_r1_5`.
- llama.cpp license: MIT, Copyright (c) 2023-2026 The ggml authors; retained
  verbatim as `LICENSE-LLAMA`.

The adapted kernel is specialized to Candle's existing Q4_K/F32 contract and
uses no llama.cpp argument structs or function constants. It dequantizes each
16-weight chunk once and reuses it across two to five activation rows, with a
tail-N output predicate. The automatic skinny dispatcher uses this q4x4 path
for M=2..3, M=4 small-output projections, and all tail-N shapes. For aligned
larger M=4 and M=5 projections it reuses Candle's faster bespoke Q4_K MV math
in a single M-high grid. The latter is never selected for tail N because the
upstream Candle MV kernel writes four rows unconditionally.

The split-K path is off by default. Set `CX_Q4K_SPLITK=1` to enable it; unset
the variable or set it to `0` to retain the stock Candle dispatch. `true`,
`yes`, and `on` are also accepted case-insensitively. Any unrecognized value
fails closed to disabled rather than activating an experimental kernel. The
value is cached on first quantized matrix dispatch, so changing it requires a
worker restart. Enablement is restricted to Q4_K, one logical batch,
`M=2..5`, K divisible into 256-value Q4_K partitions, and a bounded workspace.
All other calls use the unchanged upstream kernel.

`CX_Q4K_SKINNY_M=1` enables the newer skinny-M dispatcher and takes priority
over split-K for eligible calls. `true`, `yes`, and `on` are also accepted;
unrecognized values fail closed. Unset it to park the skinny path while still
allowing an independently enabled split-K fallback. Like the split-K flag, it
is cached at first quantized matrix dispatch and requires a worker restart.
Eligibility is restricted to Q4_K weights with their canonical packed row
stride (144 bytes per 256 values), contiguous F32 activations, `M=2..5`, and K
a multiple of 256. A later CX-only dispatcher hardening also admits a rank-3
`(batch, span, K)` activation when every byte stride proves it can be flattened
to the same `M=batch*span` matrix. No kernel body was copied or changed for that
extension: it reuses the already-proven skinny path. Repeated AB/BA profiling
kept cross-batch flattening deliberately narrower than batch 1: only total
`M=4` MLP-down (`K>2048`) and large output-head (`N>=65536`) projections
cleared the 1.05x gate; attention, hidden, MLP-up, other cross-batch widths,
padded, broadcast, and noncanonical layouts retain unchanged stock Candle
dispatch. Any other mismatch falls through to split-K, if independently
eligible and enabled, or otherwise to stock Candle dispatch.

Before enabling in production, run:

```text
cargo test --manifest-path vendor/candle-metal-kernels/Cargo.toml q4k_splitk
cargo test --manifest-path vendor/candle-metal-kernels/Cargo.toml q4k_skinny
cargo test --manifest-path vendor/candle-metal-kernels/Cargo.toml q4k_skinny_benchmark_gate -- --ignored --nocapture
cargo test --manifest-path vendor/candle-metal-kernels/Cargo.toml q4k_flattened_batch_benchmark_gate -- --ignored --nocapture
```

The first two commands cover heuristic/fallback bounds and GPU parity for
M=2..5, including a tail-N case. The ignored benchmark compares median stock,
split-K, skinny-auto, and direct batched-MV dispatch latency, but gates the
actual skinny-auto dispatcher against `CX_Q4K_SKINNY_MIN_SPEEDUP` (default
`1.0`; the older
`CX_Q4K_SPLITK_MIN_SPEEDUP` remains a compatibility fallback).
The flattened-batch gate alternates stock/optimized order and checks every
enabled cross-batch projection against `CX_Q4K_FLAT_BATCH_MIN_SPEEDUP` (default
`1.0`; use `1.05` for the target-class promotion gate).
