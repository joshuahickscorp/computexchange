# Cycles Hopper Batch Runtime Tar - 2026-07-08

## What Changed

- Added `patches/cycles/0004-standalone-cx-batch-manifest.patch`.
- Added standalone Cycles CLI flag `--cx-batch-manifest MANIFEST`.
- Manifest line format: `OUTPUT SAMPLES OFFSET LENGTH`.
- Added `batch` execution mode to `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`.

## Batch-Capable Runtime Tar

- GPU: RunPod secure `NVIDIA H100 PCIe`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Patch stack: `0001` through `0004`
- Build time: `526.1s`
- Batch smoke: two jobs, `CX_CYCLES_BATCH_OK jobs=2`
- Exported tar:
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
- Tar size: `192M`
- Remote runtime root: `512M install`, `3.4M examples`
- Spend: about `$0.60`

## No-Build Resident Batch Probe

- GPU: RunPod community `NVIDIA H100 NVL`
- Upload/extract: `7.2s / 3.3s`
- Runtime smokes: passed
- Scene: `scene_world_volume.xml`
- Quality: all rows exact, SSIM `1.0`, worst-tile SSIM `1.0`
- Spend: about `$0.31`
- Final safety check: `pods: []`, tracked `[]`, balance `$15.70`

| Samples | Resident workers / chunks | Full | Batch + merge wall | Actual speedup | Merge |
|---:|---:|---:|---:|---:|---:|
| `16384` | `4 / 16` | `20.77s` | `24.3121s` | `0.8543x` | `0.46s` |
| `16384` | `8 / 32` | `20.76s` | `25.6337s` | `0.8099x` | `0.52s` |
| `65536` | `4 / 16` | `80.94s` | `91.0082s` | `0.8894x` | `0.42s` |
| `65536` | `8 / 32` | `80.92s` | `91.6854s` | `0.8826x` | `0.59s` |

## Interpretation

The batch manifest patch is a good scaffold: it proves one Cycles process can execute multiple
sample-subset jobs and reuse some session state. It is not enough for single-GPU fan-out speed. The
best resident-batch row, `0.8894x`, ties the prior subprocess ceiling of `0.8908x`.

Next speed work should move to a true in-process scheduler, separate warm GPUs, or the product path
that stacks single-frame Cycles with denoise/transcode. Do not spend more H100/H200 time on one-GPU
manifest-worker fan-out alone.

## Ada/L40S Companion Tar

- Added `--gpu-tier ada` to keep `sm_89` builds on L40S.
- GPU: RunPod secure `NVIDIA L40S`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Build time: `379.4s`
- Exported tar:
  `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`
- Tar size: `191M`
- Pack/download: `19.9s / 7.9s`
- Tiny 64-sample, 2-way batch probe: exact, SSIM `1.0`
- Spend: about `$0.14`
- Final safety check: `pods: []`, tracked `[]`, balance `$15.49`

The cheap L40S/Ada lane now has a no-build batch-capable runtime root alongside the Hopper one.
