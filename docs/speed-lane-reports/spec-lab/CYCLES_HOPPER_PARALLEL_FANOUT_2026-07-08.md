# Cycles Hopper Parallel Fan-Out Report - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Pod: RunPod secure `NVIDIA H100 PCIe`
- GPU tier: `hopper`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Scenes: `scene_world_volume.xml`, `scene_cube_volume.xml`
- Samples: `1024`
- Fan-outs: `4`, `8`
- Chunks: `2x` workers, so `8` and `16` chunks
- Execution mode: actual local multi-process parallelism on one H100 pod
- Merge mode: linear `oiiotool`
- Spend: `$0.57`
- Final safety check: `pods: []`, tracked pods `[]`, balance `$18.75`

## Stage Timing

| Stage | Time |
|---|---:|
| deps | `19.1s` |
| clone | `4.9s` |
| sync libs | `88.4s` |
| apply patches | `0.4s` |
| build | `498.2s` |
| binary smoke | `1.0s` |
| patch CLI smoke | `1.0s` |
| render smoke | `0.9s` |
| probes total | `83.6s` |

The H100-specific CUDA build path worked. The cost problem is still the cold build/sync tax, not
Hopper compatibility.

## Results

| Scene | Workers / chunks | Full | Subset phase | Merge | Actual wall | Actual speedup | Quality |
|---|---:|---:|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 4 / 8 | `2.56s` | `4.4191s` | `0.46s` | `4.8791s` | `0.5247x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 8 / 16 | `2.55s` | `6.3833s` | `0.59s` | `6.9733s` | `0.3657x` | exact, SSIM `1.0` |
| `scene_cube_volume.xml` | 4 / 8 | `2.89s` | `5.6006s` | `0.45s` | `6.0506s` | `0.4776x` | drift, SSIM `0.999994181`, worst tile `0.999546095` |
| `scene_cube_volume.xml` | 8 / 16 | `2.88s` | `8.4679s` | `0.58s` | `9.0479s` | `0.3183x` | drift, SSIM `0.999992617`, worst tile `0.999405931` |

## Interpretation

- H100 is suitable for premium single-render throughput and for testing Hopper compatibility.
- H100 is not suitable for process-level sample fan-out on these small 1024-sample official scenes.
  The full render is already `2.55-2.89s`, so process launch, scene load, BVH, kernel setup, and
  EXR merge dominate.
- Actual parallel measurement is harsher than the prior modeled sequential wall-clock. This is good
  evidence: the substrate now measures real local parallel contention instead of only an idealized
  scheduler model.
- Quality stayed excellent. World-volume remained strict exact; cube-volume remained perceptual
  drift with very high SSIM, reinforcing that cube-volume needs a tier policy rather than being
  called exact.
- A more expensive H200/B200 lane is not justified for this fan-out method yet. The next premium
  spend should target heavier scenes, persistent in-process workers, OptiX, or external GPU denoise,
  not more copies of process-level fan-out on tiny examples.

## Decision

Keep H100 in the toolkit, but use it sparingly:

1. Use H100/H200 only after a warm/prebuilt root exists.
2. Use premium GPUs for heavyweight baseline renders, OptiX feasibility, GPU denoise, and Hopper
   compatibility.
3. Do not expect process-level sample fan-out to beat a single H100 until the workload is large
   enough to amortize repeated Cycles process startup, scene load, and merge.
4. For fan-out, prioritize persistent workers or in-process sample scheduling before buying more
   expensive GPUs.
