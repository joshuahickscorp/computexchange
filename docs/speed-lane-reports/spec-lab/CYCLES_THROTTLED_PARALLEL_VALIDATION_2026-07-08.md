# Cycles Throttled Parallel Validation - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- Purpose: rerun the actual parallel 8/16-way matrix after adding lower-concurrency scheduling and
  missing-output retry.
- Pod: RunPod secure `NVIDIA L40S`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Scenes: `scene_world_volume.xml`, `scene_cube_volume.xml`
- Samples: `1024`
- Fan-outs: `8`, `16`
- Chunks: `1x` workers, so `8` and `16` chunks
- Execution: actual local multi-process parallelism on one pod
- Parallel slots: `8`
- Subset retries: `1`
- Merge mode: linear `oiiotool`
- Skip-build validation: enabled
- Spend: `$0.18`
- Run-end independent safety check: `pods: []`, tracked pods `[]`, balance `$18.24`
- Latest settled safety check after docs update: `pods: []`, tracked pods `[]`, balance `$18.19`

## Stage Timing

| Stage | Time |
|---|---:|
| deps | `25.3s` |
| clone | `4.4s` |
| sync libs | `87.0s` |
| apply patches | `0.8s` |
| build | `478.5s` |
| binary smoke | `1.1s` |
| patch CLI smoke | `1.5s` |
| render smoke | `1.3s` |
| skip-build runtime smokes | `4.2s` |
| skip-build fan-out probes | `71.9s` |

The no-rebuild validation pass reused `/opt/cx-cycles/install/cycles` in the same pod. It did not
clone, sync libraries, apply patches, or build.

## Primary Build Pass

| Scene | Fan-out | Full | Actual wall | Actual speedup | Quality | SSIM / worst tile | Merge |
|---|---:|---:|---:|---:|---|---:|---:|
| `scene_world_volume.xml` | 8 | `3.80s` | `6.8399s` | `0.5556x` | exact | `1.000000000 / 1.000000000` | `0.41s` |
| `scene_world_volume.xml` | 16 | `3.78s` | `8.9071s` | `0.4244x` | exact | `1.000000000 / 1.000000000` | `0.51s` |
| `scene_cube_volume.xml` | 8 | `2.62s` | `6.6804s` | `0.3922x` | drift | `0.999994098 / 0.999484419` | `0.42s` |
| `scene_cube_volume.xml` | 16 | `2.59s` | `9.9719s` | `0.2597x` | drift | `0.999992453 / 0.999349480` | `0.50s` |

## Skip-Build Validation Pass

| Scene | Fan-out | Full | Actual wall | Actual speedup | Quality | SSIM / worst tile | Merge |
|---|---:|---:|---:|---:|---|---:|---:|
| `scene_world_volume.xml` | 8 | `3.79s` | `6.0200s` | `0.6296x` | exact | `1.000000000 / 1.000000000` | `0.39s` |
| `scene_world_volume.xml` | 16 | `3.78s` | `9.1237s` | `0.4143x` | exact | `1.000000000 / 1.000000000` | `0.56s` |
| `scene_cube_volume.xml` | 8 | `2.61s` | `6.3502s` | `0.4110x` | drift | `0.999994098 / 0.999484419` | `0.39s` |
| `scene_cube_volume.xml` | 16 | `2.78s` | `9.5061s` | `0.2924x` | drift | `0.999992453 / 0.999349480` | `0.52s` |

## Interpretation

- The prior 16-way missing-output failure is fixed for this official-scene lane by throttling
  subset concurrency to `8` slots and retaining one retry.
- The no-rebuild runtime-root contract is stronger: after the build, the same pod can run runtime
  smokes plus 8/16-way fan-out probes from `/opt/cx-cycles/install/cycles`.
- This is a reliability win, not a speed win. On 1024-sample official examples, a single full CUDA
  render takes only `2.59-3.80s`, so process startup, scene setup, and merge dominate.
- Cube-volume remains a perceptual-tier candidate only. SSIM and worst-tile SSIM are excellent, but
  strict EXR diff fails.
- The next money-useful experiment should not be another cold build of these tiny scenes. It should
  either use a prebuilt/persistent `/opt/cx-cycles` root or move to a heavier scene where render
  time dominates process overhead.

## Next

1. Build a pushed image, persistent volume, or warm-pool path containing
   `/opt/cx-cycles/install/cycles`.
2. Run the existing driver with `--skip-build` against that prebuilt root.
3. Use heavier CX-ish scenes and `--chunks-per-worker > 1`; tiny official examples are now
   reliability tests, not speedup evidence.
4. Keep H100/H200 for bounded no-rebuild heavy-scene or OptiX/OIDN truth tests, not cold tiny-scene
   fan-out.
