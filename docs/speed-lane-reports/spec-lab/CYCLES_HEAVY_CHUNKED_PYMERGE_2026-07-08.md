# Cycles Heavy Chunked Python-Merge Validation - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Purpose: stress the 4096-sample chunked path and benchmark Python/OpenImageIO merge against
  linear and tree `oiiotool`.
- Pod: RunPod secure `NVIDIA L40S`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Scenes: `scene_world_volume.xml`, `scene_cube_volume.xml`
- Samples: `4096`
- Fan-outs: `8`, `16`
- Chunks per worker: `4`
- Chunks: `32`, `64`
- Execution: actual local multi-process parallelism on one pod
- Parallel slots: `8`
- Subset retries: `1`
- Merge mode: `auto`, now benchmarking linear `oiiotool`, tree `oiiotool`, and Python/OpenImageIO
- Spend: `$0.21`
- Final independent safety check: `pods: []`, tracked pods `[]`, balance `$17.98`

## Stage Timing

| Stage | Time |
|---|---:|
| deps | `20.0s` |
| clone | `5.4s` |
| sync libs | `101.4s` |
| apply patches | `0.8s` |
| build | `360.8s` |
| binary smoke | `1.2s` |
| patch CLI smoke | `1.4s` |
| render smoke | `1.3s` |
| fan-out probes total | `212.3s` |

Cold build/sync still consumed most of the run. This remains the biggest operational reason to move
to a pushed image, persistent volume, or warm pool.

## Probe Results

| Scene | Workers / chunks | Full | Actual wall | Actual speedup | Quality | SSIM / worst tile | Python / linear / tree merge | Dynamic / LPT gain |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| `scene_world_volume.xml` | 8 / 32 | `11.95s` | `19.2552s` | `0.6206x` | exact | `1.000000000 / 1.000000000` | `0.60s / 0.66s / 6.39s` | `1.2441x / 1.2981x` |
| `scene_world_volume.xml` | 16 / 64 | `12.02s` | `28.3783s` | `0.4236x` | exact | `1.000000000 / 1.000000000` | `0.89s / 1.08s / 12.83s` | `1.2042x / 1.2514x` |
| `scene_cube_volume.xml` | 8 / 32 | `7.13s` | `17.1361s` | `0.4161x` | drift | `0.999997575 / 0.999837659` | `0.60s / 0.62s / 6.45s` | `1.2519x / 1.3203x` |
| `scene_cube_volume.xml` | 16 / 64 | `6.92s` | `29.2482s` | `0.2366x` | drift | `0.999997132 / 0.999809256` | `0.83s / 1.19s / 13.01s` | `1.2126x / 1.2486x` |

## Interpretation

- Python/OpenImageIO merge beat linear `oiiotool` in all four probes and was `7-16x` faster than the
  naive tree merge.
- Chunked scheduling now has real measured imbalance to model: dynamic assignment showed `1.20-1.25x`
  gain over static contiguous ranges; LPT showed `1.25-1.32x`.
- Actual process fan-out on one L40S is still below `1x`. The subset phase is many short Cycles
  process launches, so scene setup/startup dominates.
- World-volume is exact at both 8/32 and 16/64.
- Cube-volume remains perceptual-tier only: SSIM is excellent, but strict EXR diff drifts.

## Product Read

This run improves the scaffold, not the product speed claim. It validates:

- the right merge direction: single-process Python/OIIO or C++ reduce, not shell tree merge;
- the chunked scheduler model: chunking can recover real imbalance;
- the quality gate: exact scene and drift-risk scene are both measured.

It also strengthens the negative conclusion for naive process fan-out:

- official XML examples are still too small for process-level sample fan-out;
- premium GPUs should be used for warm-root heavy-scene, OptiX, or OIDN truth tests, not cold
  rebuilds of tiny scenes;
- the next spend should go straight to a reusable `/opt/cx-cycles` root or an H100/H200 heavy-scene
  run with a hard spend checkpoint.
