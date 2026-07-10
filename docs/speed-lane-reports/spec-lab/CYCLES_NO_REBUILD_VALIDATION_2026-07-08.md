# Cycles No-Rebuild Validation Report - 2026-07-08

## Run

- Driver: `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- New harness flags:
  - `--validate-skip-build-pass`
  - `--continue-on-probe-failure`
  - `--parallel-slots`
  - `--subset-retries`
- Latest pod: RunPod secure `NVIDIA L40S`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Scenes: `scene_world_volume.xml`, `scene_cube_volume.xml`
- Samples: `1024`
- Fan-outs: `8`, `16`
- Chunks: `1x` workers, so `8` and `16` chunks
- Execution mode: actual local multi-process parallelism on one pod
- Merge mode: linear `oiiotool`
- Latest settled safety check: `pods: []`, tracked pods `[]`, balance `$18.19`

Two attempts were made. The first used `2x` chunks per worker and failed at the 16-way/32-chunk
merge before the skip-build pass could run. The second used `1x` chunks per worker, continued after
probe failures, and reached the skip-build validation pass.

Total balance movement across the two attempts was about `$0.26`.

A later follow-up added throttled parallel scheduling (`--parallel-slots 8`) and one
missing-output retry. That follow-up spent `$0.18`, validated 16-way actual parallel probes, and
left no live or tracked pods.

## Stage Timing

| Stage | Time |
|---|---:|
| deps | `19.1s` |
| clone | `3.9s` |
| sync libs | `45.4s` |
| apply patches | `1.6s` |
| build | `461.5s` |
| binary smoke | `2.1s` |
| patch CLI smoke | `2.7s` |
| render smoke | `2.3s` |
| skip-build binary smoke | `2.0s` |
| skip-build patch CLI smoke | `2.3s` |
| skip-build render smoke | `2.3s` |

The no-rebuild validation pass did not clone, sync libraries, apply patches, or build. It proved
that `/opt/cx-cycles/install/cycles` can be treated as a runtime root after the initial build.

## Probe Results

### Latest Throttled Follow-Up

| Scene | Fan-out | Pass | Full | Actual wall | Actual speedup | Quality |
|---|---:|---|---:|---:|---:|---|
| `scene_world_volume.xml` | 8 | primary | `3.80s` | `6.8399s` | `0.5556x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 16 | primary | `3.78s` | `8.9071s` | `0.4244x` | exact, SSIM `1.0` |
| `scene_cube_volume.xml` | 8 | primary | `2.62s` | `6.6804s` | `0.3922x` | drift, SSIM `0.999994098`, worst tile `0.999484419` |
| `scene_cube_volume.xml` | 16 | primary | `2.59s` | `9.9719s` | `0.2597x` | drift, SSIM `0.999992453`, worst tile `0.999349480` |
| `scene_world_volume.xml` | 8 | skip-build | `3.79s` | `6.0200s` | `0.6296x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 16 | skip-build | `3.78s` | `9.1237s` | `0.4143x` | exact, SSIM `1.0` |
| `scene_cube_volume.xml` | 8 | skip-build | `2.61s` | `6.3502s` | `0.4110x` | drift, SSIM `0.999994098`, worst tile `0.999484419` |
| `scene_cube_volume.xml` | 16 | skip-build | `2.78s` | `9.5061s` | `0.2924x` | drift, SSIM `0.999992453`, worst tile `0.999349480` |

The follow-up is documented separately in
`docs/speed-lane-reports/spec-lab/CYCLES_THROTTLED_PARALLEL_VALIDATION_2026-07-08.md`.

### Primary Build Pass

| Scene | Fan-out | Full | Actual wall | Actual speedup | Quality |
|---|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 8 | `3.35s` | `4.9375s` | `0.6785x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 16 | `3.39s` | n/a | n/a | failed: missing subset EXR before merge |
| `scene_cube_volume.xml` | 8 | `2.38s` | `4.6570s` | `0.5111x` | drift, SSIM `0.999994098`, worst tile `0.999484419` |
| `scene_cube_volume.xml` | 16 | `2.36s` | n/a | n/a | failed: missing subset EXR before merge |

### Skip-Build Validation Pass

| Scene | Fan-out | Full | Actual wall | Actual speedup | Quality |
|---|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 8 | `3.38s` | `5.2080s` | `0.6490x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 16 | `3.35s` | n/a | n/a | failed: missing subset EXR before merge |
| `scene_cube_volume.xml` | 8 | `2.28s` | `4.6960s` | `0.4855x` | drift, SSIM `0.999994098`, worst tile `0.999484419` |
| `scene_cube_volume.xml` | 16 | `2.39s` | n/a | n/a | failed: missing subset EXR before merge |

## Interpretation

- The no-rebuild runtime-root path is now real for smoke tests and 8-way actual fan-out probes.
- The throttled follow-up makes it real for 16-way actual fan-out probes on these official examples
  too.
- It is not yet a full product substrate. A product-grade no-rebuild path still needs a pushed image,
  persistent volume, or warm pool so future pods do not pay the `~7.7 minute` build tax.
- 8-way actual process fan-out remains slower than a single full render on these small official
  scenes. The full render is only `2.28-3.38s`, so process startup, scene setup, and merge dominate.
- 16-way actual process fan-out is now reliable under `8` parallel slots plus one retry, but still
  slow on tiny scenes.
- Cube-volume remains a perceptual-tier candidate only: SSIM is excellent, but strict EXR diff fails.

## Harness Changes

- Added `--validate-skip-build-pass` to run runtime smokes and a second probe matrix after the build.
- Added `--continue-on-probe-failure` so exploratory matrices can ledger failures and still reach
  later probes/skip-build validation.
- Added a missing-output check in the parallel subset scheduler so future 16-way failures identify
  the subset-output problem before merge.
- Added `--parallel-slots` and `--subset-retries`; the latest follow-up used `8` slots and one retry
  to complete the 16-way matrix.

## Next

1. Move from "same-pod runtime root" to a real prebuilt image or persistent warm volume.
2. Use the existing `--skip-build` path against that image/volume so normal experiments stop
   rebuilding Cycles.
3. Treat 8/16-way official-scene actual parallel as reliability evidence only; speed work needs
   heavier scenes or persistent workers.
4. Add a formal cube-volume drift policy before calling perceptual-tier fan-out product-ready.
