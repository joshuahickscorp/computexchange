# Cycles Chunked Fan-Out Report - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Pod: RunPod community `NVIDIA RTX A6000`
- Ref: Cycles `main` at `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Scenes: `scene_world_volume.xml`, `scene_cube_volume.xml`
- Samples: `4096`
- Fan-outs: `8`, `16`
- Chunks: `4x` workers, so `32` and `64` chunks
- Quality gates: EXR diff, SSIM, worst-tile SSIM, PNG MAE, PNG max error
- Merge modes: linear `oiiotool` versus shell-level tree `oiiotool`, auto-select faster
- Spend: `$0.12`
- Final safety check: `pods: []`, balance `$19.64`

## Results

| Scene | Workers / chunks | Full | Dynamic wall | Speedup | Quality | SSIM / worst tile | Linear / tree merge |
|---|---:|---:|---:|---:|---|---:|---:|
| `scene_world_volume.xml` | 8 / 32 | `275.25s` | `5.02s` | `54.83x` | exact | `1.0 / 1.0` | `0.85s / 5.95s` |
| `scene_world_volume.xml` | 16 / 64 | `15.39s` | `5.14s` | `2.99x` | exact | `1.0 / 1.0` | `1.96s / 12.52s` |
| `scene_cube_volume.xml` | 8 / 32 | `12.46s` | `5.92s` | `2.10x` | drift, max `0.00941` | `0.9999976 / 0.9998478` | `1.08s / 6.70s` |
| `scene_cube_volume.xml` | 16 / 64 | `12.35s` | `5.50s` | `2.25x` | drift, max `0.01187` | `0.9999972 / 0.9998072` | `1.67s / 13.18s` |

## Interpretation

- The `54.83x` world-volume 8/32 result is not a fair steady-state product speedup. The full render
  hit a first-use world-volume/kernel cliff. Keep it as cold-cliff evidence, not as a throughput
  claim.
- The fair warm exact result from this run is `2.99x` on world-volume 16/64.
- The strongest steady exact result remains the earlier `5.82x` world-volume 16-way run.
- Chunked dynamic scheduling is scaffolded and proven on real hardware, but it only moved the
  measured official scenes by `-0.5%` to `+3.2%` versus static chunk grouping. We need a real
  imbalanced CX scene to justify chunking as a major lever.
- Naive tree merge is rejected. It was `5-8x` slower than linear `oiiotool` because it pays repeated
  process and EXR IO overhead.
- Cube-volume remains EXR-drift, but perceptual metrics are very high. It should be excluded from
  exact tier and considered only under an explicit perceptual tier policy.

## Next

## Hopper Follow-Up

An H100 PCIe follow-up was run after this report to replace modeled timing with actual local
parallel process timing:

- Pod: RunPod secure `NVIDIA H100 PCIe`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Execution mode: actual parallel multi-process subsets on one pod
- Samples: `1024`
- Spend: `$0.57`
- Final safety check: `pods: []`, tracked pods `[]`, balance `$18.75`

| Scene | Workers / chunks | Full | Subset phase | Merge | Actual speedup | Quality |
|---|---:|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 4 / 8 | `2.56s` | `4.4191s` | `0.46s` | `0.5247x` | exact |
| `scene_world_volume.xml` | 8 / 16 | `2.55s` | `6.3833s` | `0.59s` | `0.3657x` | exact |
| `scene_cube_volume.xml` | 4 / 8 | `2.89s` | `5.6006s` | `0.45s` | `0.4776x` | SSIM `0.999994181` |
| `scene_cube_volume.xml` | 8 / 16 | `2.88s` | `8.4679s` | `0.58s` | `0.3183x` | SSIM `0.999992617` |

This does not invalidate sample fan-out as a primitive; it invalidates process-level fan-out on tiny
official examples when a single H100 already renders the full frame in under three seconds. H100 is
useful for premium baseline throughput, Hopper compatibility, and heavyweight scene probes. It is
not the next fan-out lever until workers are persistent or the scene is much heavier.

## Next

1. Make `/opt/cx-cycles/install/cycles` a real no-rebuild worker path.
2. Stop using premium GPUs for cold builds; cache or prebuild before the next H100/H200 experiment.
3. Replace process-level sample fan-out with persistent warm workers or in-process scheduling.
4. Replace or reduce merge overhead with in-process OpenImageIO/Python/C++ or larger chunks.
5. Add an explicit exact/perceptual tier policy for drift scenes.
6. Find or convert one representative CX scene that is heavy enough for fan-out to amortize startup.
