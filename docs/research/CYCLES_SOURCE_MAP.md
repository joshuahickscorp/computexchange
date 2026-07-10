# Cycles Source Map

This is the first scaffold for treating Cycles as a normal CX rendering component rather than a
separately-branded project.

## Source

- Official standalone Cycles remote: `https://projects.blender.org/blender/cycles.git`
- Mirror checked for refs: `https://github.com/blender/cycles.git`
- Default ref for the scaffold: `main`
- Stable branch observed during setup: `release/v5.1`
- Runtime checkout location: `.artifacts/cycles/fork` locally, `/root/cx-cycles` on RunPod
- Build output expected by official docs: `install/cycles`

Current remote check:

- `HEAD -> main`
- `main` at `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- release refs observed: `release/v4.2`, `release/v4.4`, `release/v4.5`, `release/v5.0`,
  `release/v5.1`

## Build Contract

The official standalone build contract is:

```bash
git clone https://projects.blender.org/blender/cycles.git
cd cycles
make update
make
./install/cycles
```

The CX scaffold splits that into ledgered stages:

1. `deps`
2. `clone`
3. `sync_libs`
4. optional `apply_patches`
5. `build`
6. `binary_smoke`
7. optional `patch_cli_smoke`
8. `render_smoke`

The split is important because it turns a long renderer build into named, timeout-bounded evidence.

## Verified Cloud Baseline

First verified official standalone build:

- Date: 2026-07-07 America/Toronto
- Pod: RunPod community `NVIDIA L40S`
- Ref: `main`
- Head: `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- `make update`: passed, `lib/` populated to `2.3G`
- Build: passed, installed `install/cycles`
- Binary size: `57M`
- Binary smoke: passed with `CX_CYCLES_SMOKE_OK=1`
- Total staged time: `295.1s`
- Spend: about `$0.04`
- Teardown: verified, `pods: []`, balance after run `$22.28`

One scaffold lesson from the run: outer `make -j8` does not control standalone Cycles' inner CMake
parallelism. The wrapper uses `PARALLEL_JOBS`, so the driver now emits `PARALLEL_JOBS=N make` when a
job count is requested.

Second verified official standalone build:

- Date: 2026-07-07 America/Toronto
- Pod: RunPod secure `NVIDIA L40S`
- Ref/head: same `main` head, `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- Build command: `PARALLEL_JOBS=8 make`
- Confirmed inner command: `cmake --build . -j 8 --target install`
- `make update`: passed, `lib/` populated to `2.3G`
- Build: passed in `207.0s`
- Binary smoke: passed with `CX_CYCLES_BINARY_SMOKE_OK=1`
- Render smoke: passed with `CX_CYCLES_RENDER_SMOKE_OK=1`
- Render smoke output: `/tmp/cx_cycles_monkey.png`, PNG `1024 x 512`, `314K`, 8 samples
- Total staged time: `342.5s`
- Spend: about `$0.16`, including failed provisioning attempts before the good secure L40S
- Teardown: verified, `pods: []`, tracked pod state `[]`, balance after run `$22.08`

During the second run, one unreachable A100 hit a transient API reset during termination. A manual
pod-list check caught it while the driver was still searching for a usable host, and it was
terminated immediately. `scripts/spec-lab/runpod.py` now keeps a pod tracked when `terminate()`
raises, so cleanup can retry instead of silently untracking a possibly-live pod.

First baseline-matrix attempt:

- Build and render smoke passed on a community `NVIDIA L40S`.
- Benchmark stage failed before rendering because the base image did not include `/usr/bin/time`.
- This was a scaffold dependency gap, not a Cycles failure.
- The dependency stage now installs the `time` package so benchmark stages can emit `CX_TIME_S`.

First successful baseline matrix:

- Date: 2026-07-07 America/Toronto
- Pod: RunPod community `NVIDIA L40S`
- Ref/head: `main`, `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- Cold staged setup:
  - deps: `19.6s`
  - clone: `7.5s`
  - `make update`: `50.6s`
  - `PARALLEL_JOBS=8 make`: `255.7s`
  - binary smoke: `4.0s`
  - render smoke: `4.1s`
- Baseline render timings:

| Scene | Samples | Cycles time | Stage elapsed | PNG size |
|---|---:|---:|---:|---:|
| `scene_monkey.xml` | 8 | `0.45s` | `4.0s` | `314K` |
| `scene_monkey.xml` | 32 | `0.90s` | `4.7s` | `263K` |
| `scene_sphere_bump.xml` | 8 | `0.30s` | `5.2s` | `59K` |
| `scene_sphere_bump.xml` | 32 | `0.44s` | `3.8s` | `62K` |

Interpretation: on these tiny official scenes, cold setup/build is about two orders of magnitude
larger than the render itself. The next product-level scaffold should avoid rebuilding Cycles per
job: prebuilt image, warm pool, or persistent volume before any renderer algorithm claims.

## Latest Frontier Update - 2026-07-08

New runner capability:

- `scripts/spec-lab/run_cycles_sample_fanout_matrix.py` now supports:
  - actual multi-process parallel subset execution via `--execution-mode parallel`;
  - premium Hopper provisioning via `--gpu-tier hopper`;
  - `sm_90` H100 CUDA binary builds with OptiX disabled;
  - local runtime-root export via `--export-runtime-tar`;
  - fresh-pod runtime-root import via `--prebuilt-root-tar`.

H100 validation:

- Pod: RunPod secure `NVIDIA H100 PCIe`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Build time: `498.2s`
- Spend: `$0.57`
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$18.75`

Actual parallel fan-out result:

| Scene | Workers / chunks | Full | Actual wall | Speedup | Quality |
|---|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 4 / 8 | `2.56s` | `4.8791s` | `0.5247x` | exact |
| `scene_world_volume.xml` | 8 / 16 | `2.55s` | `6.9733s` | `0.3657x` | exact |
| `scene_cube_volume.xml` | 4 / 8 | `2.89s` | `6.0506s` | `0.4776x` | drift, SSIM `0.999994181` |
| `scene_cube_volume.xml` | 8 / 16 | `2.88s` | `9.0479s` | `0.3183x` | drift, SSIM `0.999992617` |

Interpretation:

- Hopper compatibility is proven for the current CUDA-only fork path.
- Process-level sample fan-out is not viable on tiny official scenes when the single H100 full
  render is already under three seconds.
- H100/H200 should be reserved for bounded premium truth tests after a no-rebuild path exists, or
  for heavyweight scenes, OptiX, and external GPU denoise lanes.
- The next architecture target is persistent warm workers or in-process scheduling, not more cold
  subprocess fan-out.

No-build quality ladder update:

- Added `scripts/spec-lab/run_cycles_quality_ladder.py`.
- Added `scripts/spec-lab/test_cycles_quality_ladder.py`.
- Ledger:
  `docs/speed-lane-reports/spec-lab/cycles_quality_ladder_ledger.jsonl`
- Report:
  `docs/speed-lane-reports/spec-lab/CYCLES_QUALITY_LADDER_AND_PROVISIONING_2026-07-08.md`
- Verification:

```bash
python3 -m unittest discover -s scripts/spec-lab -p 'test_cycles*.py'
```

Result: `51` tests passed.

Successful H100 NVL no-build ladder rows against a `4096 spp` reference:

| Scene | Best delivery samples | Global SSIM | Worst tile | Speedup |
|---|---:|---:|---:|---:|
| `scene_world_volume.xml` | `64` | `1.000000000` | `1.000000000` | `7.4286x` |
| `scene_cube_volume.xml` | `64` | `0.997972352` | `0.982018952` | `7.3333x` |
| `scene_monkey.xml` | `32` | `0.999617997` | `0.975919427` | `6.6154x` |
| `scene_caustics.xml` | `4` | `1.000000000` | `1.000000000` | `5.4658x` |
| `scene_sphere_bump.xml` | `4` | `0.994660223` | `0.959727762` | `3.6575x` |
| `cx_many_glass.xml` | `64` | `0.999854288` | `0.996755943` | `3.7216x` |
| `scene_cube_surface.xml` | `8` | `0.995968115` | `0.979001121` | `2.9595x` |

The useful failure knee is `scene_monkey.xml`: `4 spp` and `8 spp` have high global SSIM but fail
worst-tile quality (`0.702331872` and `0.839460065`); `16 spp` is preview; `32 spp` reaches
delivery. This makes worst-tile gating non-negotiable.

Standalone OIDN status:

- `run_cycles_quality_ladder.py` now has a `--with-oidn` scaffold that loads bundled
  `libOpenImageDenoise.so.2` through Python `ctypes` and scores `variant=oidn` rows with denoise
  time included.
- It is not validated yet. Initial attempts hit missing Python OIIO bindings, then a transient SSH
  disconnect after switching to `OpenEXR`/`Imath`.
- Next retry should use a background remote-log/poll wrapper or a known reachable warm worker so an
  SSH drop does not erase the stage result.

Product-chain/provisioning status:

- `run_ultimate_no_reprojection.py` now accepts `--gpu-tier premium|value|any` plus bounded config
  overrides for frames, spp, resolution, and scene.
- A bounded Track 3 target was prepared (`4` frames, `1280x720`, `1024` ref spp, `256` draft spp),
  but did not execute because multiple premium and value-tier pods either had no capacity or never
  became SSH-reachable.
- Final safety check after interrupts: `pods: []`, tracked pods `[]`, balance `$14.36`.
- Interpretation: no-build tar solves build tax; warm pool/pre-warmed worker solves provisioning
  tax. Ad hoc cold provisioning is currently the dominant operational wall.

No-rebuild validation:

- Added `--validate-skip-build-pass` to `run_cycles_sample_fanout_matrix.py`.
- Added `--continue-on-probe-failure` for exploratory matrices.
- RunPod community `NVIDIA L40S` built `/opt/cx-cycles/install/cycles` once, then reran runtime
  smokes and fan-out probes without clone/sync/build.
- Skip-build runtime smokes passed in `6.6s` total.
- Skip-build 8-way probes passed:
  - `scene_world_volume.xml`: exact, SSIM `1.0`, actual speedup `0.6490x`.
  - `scene_cube_volume.xml`: drift, SSIM `0.999994098`, worst tile `0.999484419`,
    actual speedup `0.4855x`.
- 16-way actual parallel initially failed before merge because expected subset EXR files were
  missing even though subset timing lines were emitted.
- The parallel scheduler now checks for missing/empty subset output files, supports
  `--parallel-slots`, and supports `--subset-retries`.

Throttled 16-way follow-up:

- Pod: RunPod secure `NVIDIA L40S`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Build time: `478.5s`
- Runtime-root skip-build smokes: `4.2s`
- Spend: `$0.18`
- Safety: run-end independent check showed `pods: []`, tracked pods `[]`, balance `$18.24`;
  latest settled check after docs update showed `pods: []`, tracked pods `[]`, balance `$18.19`

Actual parallel fan-out with `--parallel-slots 8 --subset-retries 1`:

| Scene | Pass | Workers / chunks | Full | Actual wall | Speedup | Quality |
|---|---|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | primary | 8 / 8 | `3.80s` | `6.8399s` | `0.5556x` | exact |
| `scene_world_volume.xml` | primary | 16 / 16 | `3.78s` | `8.9071s` | `0.4244x` | exact |
| `scene_cube_volume.xml` | primary | 8 / 8 | `2.62s` | `6.6804s` | `0.3922x` | drift, SSIM `0.999994098` |
| `scene_cube_volume.xml` | primary | 16 / 16 | `2.59s` | `9.9719s` | `0.2597x` | drift, SSIM `0.999992453` |
| `scene_world_volume.xml` | skip-build | 8 / 8 | `3.79s` | `6.0200s` | `0.6296x` | exact |
| `scene_world_volume.xml` | skip-build | 16 / 16 | `3.78s` | `9.1237s` | `0.4143x` | exact |
| `scene_cube_volume.xml` | skip-build | 8 / 8 | `2.61s` | `6.3502s` | `0.4110x` | drift, SSIM `0.999994098` |
| `scene_cube_volume.xml` | skip-build | 16 / 16 | `2.78s` | `9.5061s` | `0.2924x` | drift, SSIM `0.999992453` |

Interpretation:

- `/opt/cx-cycles/install/cycles` is now a real same-pod runtime-root contract for 8/16-way probes.
- A product no-rebuild path still needs a pushed image, persistent volume, or warm pool.
- Throttled 16-way is reliable on the tiny official examples, but it is not a speed win. The next
  speed loop must use heavier warm workloads or persistent workers.

Heavy chunked Python-merge follow-up:

- Added `--merge-mode python`; `--merge-mode auto` now benchmarks linear `oiiotool`, tree
  `oiiotool`, and Python/OpenImageIO, then keeps the fastest result.
- Pod: RunPod secure `NVIDIA L40S`
- Build root: `/opt/cx-cycles`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Build time: `360.8s`
- Spend: `$0.21`
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$17.98`

Actual 4096-sample parallel fan-out with `--chunks-per-worker 4`, `--parallel-slots 8`, and
`--subset-retries 1`:

| Scene | Workers / chunks | Full | Actual wall | Speedup | Quality | Merge winner | Dynamic / LPT gain |
|---|---:|---:|---:|---:|---|---|---:|
| `scene_world_volume.xml` | 8 / 32 | `11.95s` | `19.2552s` | `0.6206x` | exact | Python `0.60s` | `1.2441x / 1.2981x` |
| `scene_world_volume.xml` | 16 / 64 | `12.02s` | `28.3783s` | `0.4236x` | exact | Python `0.89s` | `1.2042x / 1.2514x` |
| `scene_cube_volume.xml` | 8 / 32 | `7.13s` | `17.1361s` | `0.4161x` | drift, SSIM `0.999997575` | Python `0.60s` | `1.2519x / 1.3203x` |
| `scene_cube_volume.xml` | 16 / 64 | `6.92s` | `29.2482s` | `0.2366x` | drift, SSIM `0.999997132` | Python `0.83s` | `1.2126x / 1.2486x` |

Interpretation:

- Python/OpenImageIO is the first merge path to consistently beat linear `oiiotool`; keep this lane
  and consider a C++ in-process reducer next.
- Chunked scheduling recovered real imbalance in the model, but process-launch overhead still made
  actual one-pod fan-out slower than a single render.
- This is the strongest evidence yet that the product path is warm persistent workers or in-process
  scheduling, not more shell-subprocess fan-out.

Hopper runtime-root tar proof:

- Added `--export-runtime-tar` to pack and download a built runtime root containing `install/` and
  `examples/`.
- Added `--prebuilt-root-tar` to upload and extract that runtime root on a fresh pod, then run
  runtime smokes and fan-out probes without clone, sync, or build.
- Artifact:
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- Artifact size: `192M`
- Tar contents verified locally:
  - `install/cycles`
  - `examples/scene_cube_volume.xml`
  - `examples/scene_world_volume.xml`
- Export pass:
  - Pod: RunPod community `NVIDIA H100 NVL`
  - Build: CUDA precompiled, OptiX disabled, `sm_90`
  - Build time: `388.0s`
  - Pack time: `19.9s`
  - Download time: `9.4s`
  - Tiny 64-sample, 2-way world-volume probe: exact, SSIM `1.0`
- Fresh-pod import pass:
  - Pod: RunPod community `NVIDIA H100 NVL`
  - Upload time: `8.1s`
  - Extract time: `3.4s`
  - Extracted root size: `516M`
  - Binary smoke: `0.7s`
  - Patch CLI smoke: `1.0s`
  - Render smoke: `0.9s`
  - Tiny 64-sample, 2-way world-volume probe: exact, SSIM `1.0`, actual wall `1.3744s`,
    Python merge `0.32s`
  - No clone, sync, or build stages ran.
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$17.61`.

Interpretation:

- Hopper no-rebuild is now real as a portable runtime-root artifact, not only a same-pod
  skip-build pass.
- This does not improve the official-scene process fan-out speed result; it removes build tax from
  future Hopper experiments.
- The artifact is `sm_90`-oriented. L40S/Ada lanes need a separate `sm_89` tar or a multi-arch
  image.
- The next premium spend should use the prebuilt tar on a heavier warm workload, not rebuild Cycles.

Hopper no-build heavy fan-out follow-up:

- Pod: RunPod community `NVIDIA H100 NVL`
- Runtime root: uploaded from
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- Runs skipped clone, sync, and build.
- Scene: `scene_world_volume.xml`
- Quality: all probes exact, SSIM `1.0`, worst-tile SSIM `1.0`
- Spend: about `$0.86` across the two heavy no-build runs.
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$16.65`.

Oversplit run, `--chunks-per-worker 4`, `--parallel-slots 16`:

| Samples | Workers / chunks | Full | Actual wall | Speedup | Scheduler gain |
|---:|---:|---:|---:|---:|---:|
| `16384` | 8 / 32 | `20.78s` | `32.0890s` | `0.6476x` | dynamic `1.0507x`, LPT `1.0959x` |
| `16384` | 16 / 64 | `20.77s` | `36.6794s` | `0.5663x` | dynamic `1.2669x`, LPT `1.2936x` |
| `65536` | 8 / 32 | `80.86s` | `94.7564s` | `0.8533x` | dynamic `1.0746x`, LPT `1.0842x` |
| `65536` | 16 / 64 | `80.91s` | `97.9701s` | `0.8259x` | dynamic `1.1841x`, LPT `1.2417x` |

Fatter-chunk run, `--chunks-per-worker 1`, `--parallel-slots 16`:

| Samples | Workers / chunks | Full | Actual wall | Speedup |
|---:|---:|---:|---:|---:|
| `65536` | 4 / 4 | `80.83s` | `90.7408s` | `0.8908x` |
| `65536` | 8 / 8 | `80.83s` | `91.0475s` | `0.8878x` |
| `65536` | 16 / 16 | `80.82s` | `94.0047s` | `0.8597x` |

Interpretation:

- This is the cleanest negative result for shell-launched process sample fan-out.
- Removing build tax, increasing sample count, using H100 NVL, using exact-clean output, and trying
  both oversplit and fatter chunk shapes still did not clear `1x`.
- The best row was `0.8908x` at 65536 samples, 4-way/4 chunks.
- In fatter-chunk rows, every concurrent subset process took about `90s`, while the full render took
  about `80.8s`. This strongly suggests scene/BVH/device setup and GPU contention erase sample
  subdivision gains in separate standalone processes.
- Do not spend more premium GPU time on this exact subprocess architecture. The next sample-parallel
  lane must be resident workers, in-process Cycles scheduling, or a custom harness that keeps
  scene/BVH/device state warm.
- Resident-worker architecture note:
  `docs/research/CYCLES_RESIDENT_WORKER_ARCHITECTURE_2026-07-08.md`

Batch-manifest resident scaffold:

- Added patch `0004-standalone-cx-batch-manifest.patch`.
- New CLI flag: `--cx-batch-manifest MANIFEST`.
- Manifest lines: `OUTPUT SAMPLES OFFSET LENGTH`.
- The mode keeps one standalone Cycles process alive, loads the scene once, then renders multiple
  sample-subset jobs by resetting the existing `Session` with a new output driver and sample range.
- Cloud validation:
  - Pod: RunPod secure `NVIDIA H100 PCIe`
  - Build: CUDA precompiled, OptiX disabled, `sm_90`
  - Patch stack: `0001` through `0004`
  - Build time: `526.1s`
  - Binary smoke exposed `--cx-batch-manifest`
  - Patch CLI smoke rendered two batch jobs and emitted `CX_CYCLES_BATCH_OK jobs=2`
  - The second batch job skipped the full kernel-load/BVH-build block visible in the first job,
    which is useful evidence of intra-process reuse.
  - Exported artifact:
    `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
  - Artifact size: `192M`; remote runtime root: `512M install`, `3.4M examples`
  - Tiny 64-sample, 2-way world-volume probe: exact, SSIM `1.0`, actual speedup `0.6333x`
  - Spend: about `$0.60`
  - Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$16.01`

No-build resident batch fan-out:

- Pod: RunPod community `NVIDIA H100 NVL`
- Runtime root: uploaded from
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
- Upload/extract: `7.2s / 3.3s`
- Runtime smokes: binary `0.5s`, patch CLI `1.5s`, render `0.8s`
- Scene: `scene_world_volume.xml`
- Execution mode: `batch`, meaning `4` or `8` resident manifest workers, each rendering `4`
  sample chunks after one scene load.
- Quality: all four probes exact, SSIM `1.0`, worst-tile SSIM `1.0`
- Spend: about `$0.31`
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$15.70`

| Samples | Workers / chunks | Full | Batch + merge wall | Speedup | Merge |
|---:|---:|---:|---:|---:|---:|
| `16384` | 4 / 16 | `20.77s` | `24.3121s` | `0.8543x` | `0.46s` |
| `16384` | 8 / 32 | `20.76s` | `25.6337s` | `0.8099x` | `0.52s` |
| `65536` | 4 / 16 | `80.94s` | `91.0082s` | `0.8894x` | `0.42s` |
| `65536` | 8 / 32 | `80.92s` | `91.6854s` | `0.8826x` | `0.59s` |

Interpretation:

- The batch manifest scaffold is real and reusable, but it does not solve the one-GPU fan-out wall.
- The best resident-batch row, `0.8894x`, is effectively tied with the previous best subprocess row,
  `0.8908x`.
- Reducing process launches from one process per chunk to one resident process per worker did not
  clear `1x`; the remaining wall is GPU contention plus repeated per-worker scene/device state,
  not shell launch overhead or EXR merge.
- The next speed lane should be a deeper in-process scheduler, multi-GPU/multi-pod warm distribution,
  or the product pivot of single-frame render plus denoise/transcode. Do not run more H100/H200
  time on manifest-worker fan-out alone.

Ada/L40S batch runtime-root tar:

- Added `--gpu-tier ada`, currently restricted to L40S so `sm_89` builds do not accidentally run on
  A100/Hopper hardware.
- Pod: RunPod secure `NVIDIA L40S`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Patch stack: `0001` through `0004`
- Build time: `379.4s`
- Exported artifact:
  `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`
- Artifact size: `191M`
- Tar contents verified locally:
  - `install/cycles`
  - `examples/scene_cube_volume.xml`
  - `examples/scene_world_volume.xml`
- Pack/download: `19.9s / 7.9s`
- Tiny 64-sample, 2-way world-volume batch probe: exact, SSIM `1.0`
- Spend: about `$0.14`
- Safety: final independent check showed `pods: []`, tracked pods `[]`, balance `$15.49`

Interpretation:

- Both premium Hopper and cheaper Ada/L40S lanes now have batch-capable no-build runtime roots.
- Future renderer experiments should default to `--prebuilt-root-tar` unless they explicitly test a
  new compile-time feature.

## Source Landmarks

Local source inspection lives under ignored `.artifacts/cycles/source`.

Important files/directories:

- `GNUmakefile`
  - CMake wrapper used by the official standalone flow.
  - `PARALLEL_JOBS` controls `cmake --build . -j`.
  - `BUILD_CMAKE_ARGS` is the right hook for future compile-time feature toggles.

- `BUILDING.md`
  - Official standalone and Hydra build instructions.
  - Confirms required dependencies are handled by `make update` and precompiled libs.

- `README.md`
  - Documents the example XML scenes and CLI usage.
  - Example render form: `./cycles --samples 100 --output ./image.png scene_monkey.xml`.

- `examples/`
  - Official smoke/benchmark candidates:
    - `scene_monkey.xml`
    - `scene_sphere_bump.xml`
    - `scene_cube_surface.xml`
    - `scene_cube_volume.xml`
    - `scene_world_volume.xml`
    - `scene_caustics.xml`
    - `scene_osl_stripes.xml`

- `src/app/cycles_standalone.cpp`
  - Standalone CLI entry point.
  - Reads XML/USD, applies camera dimensions, initializes `Session`, creates the combined output
    pass, and attaches `OIIOOutputDriver`.
  - Exposed CLI levers include:
    - `--device`
    - `--samples`
    - `--output`
    - `--threads`
    - `--width`
    - `--height`
    - `--tile-size`
    - `--list-devices`
    - `--profile`

- `src/app/cycles_xml.cpp`
  - XML scene ingestion path. This is the simplest bridge for CX-generated test scenes.

- `src/session/`
  - Runtime session, buffers, tile manager, merge helpers, denoising integration.
  - `session.cpp` applies sample subset controls:
    - `set_use_sample_subset`
    - `set_sample_subset_offset`
    - `set_sample_subset_length`
  - This is the official Cycles-side anchor for exact sample-range fan-out experiments.
  - The standalone CLI does not expose these sample-subset controls today; a CX fork patch or a
    tiny C++ harness is needed for exact sample-range benchmarking outside Blender.

## Carried CX Patch Queue

Patch directory:

```text
patches/cycles/
```

Current patch:

- `0001-standalone-sample-subset-cli.patch`
  - Adds `--sample-subset-offset` and `--sample-subset-length` to `src/app/cycles_standalone.cpp`.
  - Uses the existing `SessionParams` fields:
    - `use_sample_subset`
    - `sample_subset_offset`
    - `sample_subset_length`
  - Purpose: make exact sample-range fan-out measurable in standalone Cycles, without requiring
    Blender's UI/API path.
- `0002-device-skip-oidn-cuda-probe-by-default.patch`
  - Skips the OpenImageDenoise CUDA device-support probe unless
    `CX_CYCLES_PROBE_OIDN_CUDA` is explicitly set.
  - Reason: on RunPod L40S, CUDA itself initialized and opened `/dev/nvidia0`, then device
    enumeration segfaulted immediately after loading `libOpenImageDenoise_device_cuda.so.2.4.1`.
  - Purpose: prioritize stable CUDA render-device discovery; CX already has a separate proven
    denoise path.
- `0003-standalone-disable-adaptive-sampling-cli.patch`
  - Adds `--disable-adaptive-sampling` to `src/app/cycles_standalone.cpp`.
  - Reason: Cycles defaults adaptive sampling on, and the upstream sample-subset path rescales
    adaptive thresholds for subsets. That is useful for distributed adaptive rendering, but it
    breaks strict fixed-sample merge math.
  - Purpose: give CX a fixed-sample mode for sample fan-out probes and production fan-out jobs.

Verified patched-fork build:

- Date: 2026-07-07 America/Toronto
- Pod: RunPod community `NVIDIA L40S`
- Patch stage: `CX_CYCLES_PATCH_APPLIED=0001-standalone-sample-subset-cli.patch`
- `make update`: passed with expected "unstaged changes" note because the patch modifies source.
- Build: passed in `258.1s`
- Binary smoke: passed
- Patch CLI smoke: passed
  - `--help` exposed the new sample-subset flags.
  - Rendered `examples/scene_monkey.xml` with:
    `--samples 8 --sample-subset-offset 0 --sample-subset-length 8`
  - Output: `/tmp/cx_cycles_subset_monkey.png`, PNG `1024 x 512`, `314K`
- Normal render smoke: passed
- Total staged time: `371.5s`
- Spend: about `$0.09`
- Teardown: verified, `pods: []`, tracked state `[]`, balance after run `$21.78`

First sample-subset exactness probe:

- Date: 2026-07-07 America/Toronto
- Pod: RunPod community `NVIDIA L40S`
- Patch queue: `0001-standalone-sample-subset-cli.patch`
- Scene: `examples/scene_monkey.xml`
- Samples: `8`
- Method:
  - render full `/tmp/cx_full.exr` at 8 samples;
  - render `/tmp/cx_sub0.exr` with offset `0`, length `4`;
  - render `/tmp/cx_sub1.exr` with offset `4`, length `4`;
  - merge subsets with `oiiotool sub0 sub1 --add --mulc 0.5`;
  - diff merged EXR against full EXR.
- Result:
  - `oiiotool --diff`: `PASS`
  - `CX_SUBSET_DIFF_RC=0`
  - Full EXR: `5.3M`
  - Merged EXR: `5.3M`
  - Subset EXRs: `5.4M` each
  - Probe stage elapsed: `16.1s`
- Teardown: verified, `pods: []`, tracked state `[]`, balance after run `$21.74`

Interpretation: the first CX fork hook is genuinely useful. Standalone patched Cycles can now split
sample ranges and merge them exactly for this official scene. This is a clean primitive for warm
worker fan-out before any speculative decoding.

## CUDA Device Frontier - 2026-07-08

Device matrix before the OIDN probe patch:

- Build: patched standalone Cycles on RunPod community `NVIDIA L40S`
- Device inventory: empty because `--list-devices` crashed before printing devices
- `--device CUDA`: exit `139` for every tested scene/sample count
- `--device OPTIX`: exit `139` for every tested scene/sample count
- Diagnostic result: CUDA reached `/dev/nvidia0`, then segfaulted after loading
  `libOpenImageDenoise_device_cuda.so.2.4.1`

Device matrix after `0002-device-skip-oidn-cuda-probe-by-default.patch`:

- Device inventory:
  - `CUDA NVIDIA L40S`
  - `CPU AMD EPYC 7453 28-Core Processor`
- `OPTIX` result: `Unknown device: OPTIX`
- Interpretation: CUDA is valid and usable; OptiX is not built by the default standalone config
  because the pod does not provide the OptiX SDK.

Cold CUDA without precompiled kernels:

| Scene | Samples | CPU time | CUDA time | Note |
|---|---:|---:|---:|---|
| `scene_monkey.xml` | 32 | `0.90s` | `269.18s` | first CUDA render paid runtime kernel compile |
| `scene_monkey.xml` | 512 | `4.70s` | `1.01s` | warm CUDA, `4.65x` vs CPU |
| `scene_sphere_bump.xml` | 32 | `0.45s` | `0.66s` | tiny scene, CUDA overhead dominates |
| `scene_sphere_bump.xml` | 512 | `1.02s` | `0.71s` | warm CUDA, `1.44x` vs CPU |

Precompiled CUDA-only build:

```bash
BUILD_CMAKE_ARGS="-DWITH_CYCLES_DEVICE_OPTIX=OFF -DWITH_CYCLES_CUDA_BINARIES=ON -DCYCLES_CUDA_BINARIES_ARCH=sm_89"
```

Results:

- Build time: `466.6s` in the focused matrix, `446.9s` in the CUDA subset probe
- First CUDA render no longer paid the `269s` cold compile cliff
- `scene_monkey.xml`, 32 samples, first CUDA render: `0.73s`
- `scene_monkey.xml`, 512 samples, CUDA render: `1.07s`
- Failed variant: enabling `WITH_CYCLES_CUDA_BINARIES=ON` without disabling OptiX tried to compile
  OptiX kernels and failed on missing `optix.h`

CUDA sample-subset and fan-out frontier:

- Build: precompiled CUDA-only, `sm_89`, patches `0001`, `0002`, and later `0003`
- Scene: `scene_monkey.xml`
- Device: `CUDA`
- Strict exact result before the adaptive investigation:
  - 64 samples, 2-way fan-out, default adaptive sampling: `PASS`, `CX_SUBSET_DIFF_RC=0`
  - Probe stage elapsed: `13.3s`
- Boundary scan with default adaptive sampling:
  - 64 samples, 2-way: strict `PASS`
  - 128/256/512/1024/2048/4096 samples, 2-way: strict diff failed
  - 4096 samples, 2-way drift: mean `3.48e-4`, RMS `1.41e-3`, max `0.0368`
  - Interpretation: this is not acceptable as "lossless"; adaptive sampling changes the effective
    sample decisions between full and subset renders.
- Adaptive-off scan with `--disable-adaptive-sampling`:

| Samples | Fan-out | Strict OIIO diff | Mean error | RMS error | Max error | Full time | Ideal parallel wall | Modeled speedup |
|---:|---:|---|---:|---:|---:|---:|---:|---:|
| 64 | 2 | `PASS` | `0` | `0` | `0` | `0.70s` | `0.95s` | `0.74x` |
| 64 | 4 | `PASS` | `0` | `0` | `0` | `0.75s` | `1.07s` | `0.70x` |
| 64 | 8 | `PASS` | `0` | `0` | `0` | `0.71s` | `1.07s` | `0.66x` |
| 128 | 2 | numeric only | `5.39e-8` | `9.45e-8` | `1.07e-6` | `0.78s` | `0.95s` | `0.82x` |
| 128 | 4 | numeric only | `5.09e-8` | `8.97e-8` | `1.13e-6` | `0.77s` | `1.04s` | `0.74x` |
| 128 | 8 | numeric only | `5.01e-8` | `8.83e-8` | `1.13e-6` | `0.76s` | `1.05s` | `0.72x` |
| 512 | 2 | numeric only | `1.29e-7` | `3.60e-7` | `7.39e-6` | `1.18s` | `1.16s` | `1.02x` |
| 512 | 4 | numeric only | `1.24e-7` | `3.39e-7` | `7.03e-6` | `1.19s` | `1.07s` | `1.11x` |
| 512 | 8 | numeric only | `1.21e-7` | `3.28e-7` | `6.56e-6` | `1.18s` | `1.07s` | `1.10x` |
| 4096 | 2 | numeric only | `2.08e-6` | `6.58e-6` | `5.91e-5` | `5.42s` | `3.23s` | `1.68x` |
| 4096 | 4 | numeric only | `2.13e-6` | `6.33e-6` | `5.72e-5` | `5.35s` | `2.22s` | `2.41x` |
| 4096 | 8 | numeric only | `2.10e-6` | `6.17e-6` | `5.37e-5` | `5.38s` | `1.67s` | `3.22x` |

- Multi-scene adaptive-off scan at 4096 samples on a secure L40S:

| Scene | 2-way speedup | 4-way speedup | 8-way speedup | Quality note |
|---|---:|---:|---:|---|
| `scene_monkey.xml` | `1.42x` | `2.93x` | `1.64x` | numeric at 2/4/8-way |
| `scene_sphere_bump.xml` | `1.26x` | `1.44x` | `1.88x` | numeric at 2/4/8-way |
| `scene_cube_surface.xml` | `1.42x` | `2.04x` | `0.93x` | numeric at 2/4/8-way |
| `scene_cube_volume.xml` | `1.32x` | `1.92x` | `3.30x` | drift; max error `0.0060-0.0096` |
| `scene_world_volume.xml` | `1.83x` | `3.05x` | `2.67x` | strict exact at 2/4/8-way |
| `scene_caustics.xml` | `1.48x` | `1.81x` | `1.29x` | strict exact at 2/4/8-way |

The multi-scene result changes the next patch target: static contiguous sample ranges are uneven.
Several 8-way runs lose speed to a slow subset. The production fan-out scheduler should split a
frame into more sample chunks than workers and assign chunks dynamically.

- Focused 16-way adaptive-off scan at 4096 samples:

| Scene | 16-way speedup | Quality note | Merge overhead |
|---|---:|---|---:|
| `scene_monkey.xml` | `3.10x` | numeric; max error `5.15e-5` | `0.68s` |
| `scene_cube_volume.xml` | `3.64x` | drift; max error `0.0101` | `0.54s` |
| `scene_world_volume.xml` | `5.82x` | strict exact | `0.56s` |
| `scene_caustics.xml` | `2.72x` | strict exact | `0.57s` |

Interpretation: 16-way fan-out is a real lever on scenes where render time dominates and subset
times are balanced. Merge overhead is now visible, so future work should benchmark in-process or
tree-style merging, not only `oiiotool` as a shell step.

- Chunked fan-out cloud pass on 2026-07-08:

Harness changes proven by this run:

- `--chunks-per-worker` and `--chunk-count` split sample ranges into more chunks than modeled
  workers.
- The parser now models static contiguous assignment, online dynamic assignment, and LPT assignment
  from measured chunk timings.
- `--merge-mode auto` benchmarks linear `oiiotool` merge against a shell-level tree merge and keeps
  the faster output.
- Fan-out probes now emit PNG-space SSIM, worst-tile SSIM, MAE, and max error in addition to EXR
  diff statistics.

Run conditions:

- Pod: RunPod community `NVIDIA RTX A6000` after one community L40S candidate failed CUDA
  verification and was terminated.
- Ref/head: `main`, `a3df10e09dc32a97f03f35f7b1997fbb4228c239`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Cold setup/build on A6000:
  - deps: `15.0s`
  - clone: `5.2s`
  - sync libs: `123.7s`
  - build: `441.9s`
- Spend: `$0.12`
- Teardown: verified after run, `pods: []`, balance `$19.64`

Chunked fan-out results at 4096 samples:

| Scene | Workers / chunks | Full | Modeled dynamic wall | Speedup | EXR quality | SSIM / worst tile | Merge linear / tree |
|---|---:|---:|---:|---:|---|---:|---:|
| `scene_world_volume.xml` | 8 / 32 | `275.25s` | `5.02s` | `54.83x` | exact | `1.0 / 1.0` | `0.85s / 5.95s` |
| `scene_world_volume.xml` | 16 / 64 | `15.39s` | `5.14s` | `2.99x` | exact | `1.0 / 1.0` | `1.96s / 12.52s` |
| `scene_cube_volume.xml` | 8 / 32 | `12.46s` | `5.92s` | `2.10x` | drift; max `0.00941` | `0.9999976 / 0.9998478` | `1.08s / 6.70s` |
| `scene_cube_volume.xml` | 16 / 64 | `12.35s` | `5.50s` | `2.25x` | drift; max `0.01187` | `0.9999972 / 0.9998072` | `1.67s / 13.18s` |

Strict interpretation:

- The `54.83x` world-volume result includes a first-use volume/kernel cliff in the full render and
  should not be reported as steady-state speedup. The warm 16/64 world-volume result is the fairer
  comparison from this run: `2.99x`, strict EXR exact, SSIM `1.0`.
- Chunked dynamic scheduling beat static by only `0.4-3.2%` on the measured exact scene and by
  `-0.5-1.6%` on cube-volume. These official chunks were already fairly balanced; chunking is now
  scaffolded, but the product still needs a more imbalanced real scene to prove scheduler leverage.
- Shell-level tree merge was worse than linear `oiiotool` by `5-8x`. The next merge lane should be
  in-process OpenImageIO/C++/Python or fewer, larger chunks, not a naive tree of subprocess calls.
- Cube-volume remains EXR-drift even with chunking. The new perceptual metrics are extremely high,
  so this scene is a policy question rather than an automatic rejection: exact tier excludes it,
  perceptual tier may allow it with thresholds.

The harness now classifies fan-out diffs as:

- `exact`: OIIO strict diff passes.
- `numeric`: mean <= `2e-5`, RMS <= `2e-5`, and max <= `1e-4`.
- `drift`: larger mismatch, such as the default-adaptive 4096-sample run.

Product interpretation:

- The fork is no longer just a CPU compatibility scaffold.
- A CUDA L40S worker image with `sm_89` precompiled kernels is a plausible runtime base.
- Sample-range fan-out works on CUDA, but fixed-sample jobs must pass
  `--disable-adaptive-sampling`.
- "Lossless" should only mean strict exact at low-count cases that pass OIIO. For product purposes,
  the stronger claim is numerical equivalence under a declared tolerance, with final SSIM/worst-tile
  gates above that.
- On tiny official scenes, fan-out overhead dominates until sample counts are high. At 4096 fixed
  samples, even this toy scene shows modeled `3.22x` ideal 8-way speedup.
- Across official example scenes, 4096-sample fan-out showed `1.26-3.30x` modeled speedup depending
  on scene and chunk balance. World-volume and caustics passed strict exactness; cube-volume needs
  an SSIM/worst-tile quality check before being allowed into an automatic fan-out tier.
- The best clean sample-fan-out result is now `5.82x` at 4096 samples / 16-way on
  `scene_world_volume.xml`, with strict EXR equality.
- The largest observed speedup is the 2026-07-08 chunked `54.83x` world-volume cold-cliff result,
  but that is not a steady-state product number. It is evidence to warm kernels/scenes before
  benchmarking and to separate first-use latency from render throughput.
- The next infrastructure target is a pushed prebuilt image or warm pool. Rebuilding Cycles per job
  is now the largest artificial cost left in this scaffold.

- `src/integrator/`
  - Path tracing scheduler and CPU/GPU work implementations.
  - Likely long-term hook point for path/reservoir reuse, but not the first patch target.

- `src/device/`
  - CPU, CUDA, OptiX, HIP, oneAPI, Metal, and multi-device backends.
  - Useful for understanding how far the official engine already abstracts hardware.

- `src/bvh/`
  - BVH construction and backend-specific acceleration paths.
  - Rust sidecar should learn from this shape before inventing its own production BVH story.

- `src/scene/`
  - Scene graph, cameras, lights, integrator settings, passes, shaders, geometry.
  - This is the compatibility boundary: CX should initially support a subset and fall back to
    Cycles for the rest.

## Local Scaffold

Inspect the manifest without cloning:

```bash
python3 scripts/spec-lab/cycles_fork.py --pretty
```

Run cheap scaffold tests:

```bash
python3 scripts/spec-lab/test_cycles_fork.py
```

The local source checkout belongs under `.artifacts/`, which is ignored. Do not vendor the full
Cycles source tree into this repo until there is a specific patch we need to carry.

## Cloud Scaffold

Dry-run the RunPod driver:

```bash
python3 scripts/spec-lab/run_cycles_fork_build.py --dry-run
```

Build official standalone Cycles on a money-safe pod:

```bash
python3 scripts/spec-lab/run_cycles_fork_build.py --ref main --jobs 0
```

The driver uses the same safety pattern as the other spec-lab runs:

- direct balance check through `runpod.balance()`;
- tracked pod state;
- remote watchdog;
- stage ledger;
- `finally` teardown.

Ledger:

```text
docs/speed-lane-reports/spec-lab/cycles_fork_ledger.jsonl
```

## Why This Exists

Cycles remains the compatibility oracle and fallback. The owned Rust renderer should only take over
where CX has a structural advantage:

- repeated scene;
- many variants;
- cached path or visibility structure;
- warm workers;
- known quality gates;
- selective tile/sample escalation.

This scaffold is the bridge between those two worlds: build and smoke official Cycles first, then
attach CX-specific speculative renderer work beside it.
