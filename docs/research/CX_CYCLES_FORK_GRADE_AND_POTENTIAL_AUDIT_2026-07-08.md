# CX Cycles Fork Grade And Potential Audit - 2026-07-08

This is a strict audit of the current CX Cycles fork scaffold as of July 8, 2026. It grades the
work as a product substrate, not as a research spike. The current state is good, but it is not yet
10/10.

## Evidence Base

Local artifacts inspected:

- `patches/cycles/`
- `docker/cycles/Dockerfile`
- `scripts/spec-lab/cycles_fork.py`
- `scripts/spec-lab/run_cycles_sample_fanout_matrix.py`
- `docs/research/CYCLES_SOURCE_MAP.md`
- `docs/research/CX_CYCLES_FORK_GOAL_PROMPT.md`
- `docs/research/RUST_CYCLES_FRONTIER_PLAN_2026-07-08.md`
- `docs/speed-lane-reports/spec-lab/*cycles*ledger.jsonl`

External/current anchors:

- [Cycles standalone build instructions](https://github.com/blender/cycles/blob/main/BUILDING.md)
- [Blender Cycles GPU rendering manual](https://docs.blender.org/manual/en/latest/render/cycles/gpu_rendering.html)
- [Open Image Denoise documentation](https://www.openimagedenoise.org/documentation.html)
- [NVIDIA ReSTIR PT Enhanced](https://research.nvidia.com/labs/rtr/publication/lin2026restirptenhanced/)

Verification command from this audit pass:

```bash
python3 -m unittest discover -s scripts/spec-lab -p 'test_cycles*.py'
```

Earlier result: `40` tests passed.

Updated verification after the Hopper runtime-tar scaffold:

```bash
python3 -m py_compile scripts/spec-lab/runpod.py scripts/spec-lab/run_cycles_sample_fanout_matrix.py
python3 -m unittest discover -s scripts/spec-lab -p 'test_cycles*.py'
```

Result: `43` tests passed.

Cloud verification added in this pass:

- RunPod community `NVIDIA RTX A6000`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Focus: 4096-sample chunked fan-out on exact-clean `scene_world_volume.xml` and drift-risk
  `scene_cube_volume.xml`
- Result: all four probes ran, pod teardown succeeded, independent post-run check showed
  `pods: []`, balance `$19.64`
- Spend: `$0.12`

Additional Hopper verification added after the first audit draft:

- RunPod secure `NVIDIA H100 PCIe`
- Build: CUDA precompiled, OptiX disabled, `sm_90`
- Focus: actual local parallel multi-process fan-out, not modeled sequential timing
- Result: all four probes ran, pod teardown succeeded, independent post-run check showed
  `pods: []`, tracked pods `[]`, balance `$18.75`
- Spend: `$0.57`
- Safety patch: RunPod GraphQL and SSH provisioning paths now retry transient network failures and
  refuse to continue if an unreachable pod cannot be confirmed terminated.

No-rebuild validation added after the Hopper run:

- RunPod community `NVIDIA L40S`
- Build once at `/opt/cx-cycles`, then rerun runtime smokes and probes with the skip-build path.
- Skip-build runtime smokes passed in `6.6s`.
- Skip-build 8-way `scene_world_volume.xml`: exact, SSIM `1.0`, actual speedup `0.6490x`.
- Skip-build 8-way `scene_cube_volume.xml`: drift, SSIM `0.999994098`, worst tile `0.999484419`,
  actual speedup `0.4855x`.
- 16-way actual parallel probes failed because expected subset EXR files were missing before merge.
- Final safety check showed `pods: []`, tracked pods `[]`, balance `$18.46`.

Throttled 16-way validation added after the no-rebuild run:

- RunPod secure `NVIDIA L40S`
- Build once at `/opt/cx-cycles`, then rerun the same 8/16-way matrix through skip-build.
- New harness controls: `--parallel-slots 8`, `--subset-retries 1`.
- 16-way `scene_world_volume.xml` now completes in both primary and skip-build passes with strict
  EXR exactness and SSIM `1.0`.
- 16-way `scene_cube_volume.xml` completes in both passes with SSIM `0.999992453`, worst tile
  `0.999349480`, and EXR drift.
- Actual speed remains negative on tiny official examples: skip-build 16-way speedups were
  `0.4143x` for world-volume and `0.2924x` for cube-volume.
- Run-end independent safety check showed `pods: []`, tracked pods `[]`, balance `$18.24`.
  Latest settled check after docs update showed `pods: []`, tracked pods `[]`, balance `$18.19`.

Heavy chunked Python-merge validation added after the throttled run:

- RunPod secure `NVIDIA L40S`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Focus: 4096-sample actual parallel fan-out with 32/64 chunks, exact-clean world-volume,
  drift-risk cube-volume, and auto merge including Python/OpenImageIO.
- Result: all four probes ran, pod teardown succeeded, independent post-run check showed
  `pods: []`, tracked pods `[]`, balance `$17.98`.
- Spend: `$0.21`.
- Python/OpenImageIO merge won every probe: `0.60-0.89s` versus `0.62-1.19s` linear and
  `6.39-13.01s` tree.
- Chunked dynamic scheduling modeled `1.20-1.25x` gain over static contiguous chunks; LPT modeled
  `1.25-1.32x`.
- Actual one-pod process fan-out remained below `1x`: best actual speedup was `0.6206x`.

Hopper runtime-root tar validation added after the heavy chunked run:

- RunPod community `NVIDIA H100 NVL`
- Exported a CUDA-only `sm_90` runtime root containing `install/` and `examples/`.
- Local artifact:
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
- Artifact size: `192M`; extracted remote root size: `516M`.
- Export pass build time: `388.0s`; pack `19.9s`; download `9.4s`.
- Fresh-pod import pass uploaded the tar in `8.1s`, extracted it in `3.4s`, then ran binary,
  patch-CLI, render-smoke, and exact fan-out probes without clone, sync, or build.
- Tiny import probe: 64-sample 2-way `scene_world_volume.xml`, exact, SSIM `1.0`, actual wall
  `1.3744s`, Python merge `0.32s`.
- Final independent safety check showed `pods: []`, tracked pods `[]`, balance `$17.61`.

Hopper no-build heavy fan-out validation added after the runtime-tar proof:

- RunPod community `NVIDIA H100 NVL`
- Runtime root uploaded from the Hopper `sm_90` tar; no clone, sync, or build stages.
- Scene: `scene_world_volume.xml`
- Focus: 16k/65k-sample exact-clean fan-out, oversplit and fatter chunk shapes.
- Result: all seven probes were strict EXR exact with SSIM `1.0`.
- Best actual process-fanout speedup: `0.8908x` at 65536 samples, 4-way/4 chunks.
- Oversplit best: `0.8533x` at 65536 samples, 8-way/32 chunks.
- Spend across the two no-build H100 NVL runs: about `$0.86`.
- Final independent safety check showed `pods: []`, tracked pods `[]`, balance `$16.65`.

Batch-manifest resident scaffold and no-build validation added after the process fan-out kill:

- Added `0004-standalone-cx-batch-manifest.patch`.
- Added `--cx-batch-manifest MANIFEST` to standalone Cycles, with manifest lines
  `OUTPUT SAMPLES OFFSET LENGTH`.
- H100 PCIe secure build/export validated all four patches, rendered a two-job batch smoke, and
  exported
  `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`.
- Batch-capable tar size: `192M`; remote root: `512M install`, `3.4M examples`.
- No-build H100 NVL resident-batch run uploaded/extracted the tar in `7.2s / 3.3s`, then ran
  16k/65k exact-clean world-volume probes with `4/8` resident manifest workers.
- All four resident-batch probes were strict EXR exact with SSIM `1.0`.
- Best resident-batch actual speedup: `0.8894x` at 65536 samples, 4 resident workers / 16 chunks.
- Final independent safety check showed `pods: []`, tracked pods `[]`, balance `$15.70`.

Ada/L40S batch runtime-root validation added after the resident-batch run:

- Added `--gpu-tier ada`, restricted to L40S for `sm_89` builds.
- RunPod secure `NVIDIA L40S`
- Build: CUDA precompiled, OptiX disabled, `sm_89`
- Exported
  `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`.
- Artifact size: `191M`; tar contains `install/cycles`, `scene_world_volume.xml`, and
  `scene_cube_volume.xml`.
- Tiny 64-sample, 2-way world-volume batch probe was exact with SSIM `1.0`.
- Spend: about `$0.14`.
- Final independent safety check showed `pods: []`, tracked pods `[]`, balance `$15.49`.

No-build quality ladder added after the Ada tar:

- Added `scripts/spec-lab/run_cycles_quality_ladder.py`.
- Added `scripts/spec-lab/test_cycles_quality_ladder.py`.
- Verification now passes `51` Cycles tests.
- RunPod community `NVIDIA H100 NVL`, using the Hopper batch runtime tar; no clone, sync, or build.
- Successful raw fixed-sample CUDA quality ladder against a `4096 spp` reference:
  - `scene_world_volume.xml`: `64 spp`, SSIM `1.0`, worst tile `1.0`, `7.4286x`.
  - `scene_cube_volume.xml`: `64 spp`, SSIM `0.997972352`, worst tile `0.982018952`,
    `7.3333x`.
  - `scene_monkey.xml`: `32 spp`, SSIM `0.999617997`, worst tile `0.975919427`,
    `6.6154x`.
  - `scene_caustics.xml`: `4 spp`, SSIM `1.0`, worst tile `1.0`, `5.4658x`.
  - `scene_sphere_bump.xml`: `4 spp`, SSIM `0.994660223`, worst tile `0.959727762`,
    `3.6575x`.
  - synthetic `cx_many_glass.xml`: `64 spp`, SSIM `0.999854288`, worst tile
    `0.996755943`, `3.7216x`.
  - `scene_cube_surface.xml`: `8 spp`, SSIM `0.995968115`, worst tile `0.979001121`,
    `2.9595x`.
- The useful knee is `scene_monkey.xml`: `4/8 spp` have high global SSIM but fail worst-tile
  quality (`0.702331872` and `0.839460065`); `16 spp` is preview; `32 spp` is delivery.
- Final safety check after this loop showed `pods: []`, tracked pods `[]`, balance `$14.36`.

Standalone OIDN and product-chain attempts after the ladder:

- `run_cycles_quality_ladder.py` now has a `--with-oidn` scaffold using bundled
  `libOpenImageDenoise.so.2` through Python `ctypes`, but no validated OIDN row yet.
- OIDN attempts failed cheaply: first due missing Python OpenImageIO bindings, then due SSH
  disconnect after switching to `OpenEXR`/`Imath`.
- `run_ultimate_no_reprojection.py` now supports `--gpu-tier premium|value|any` and bounded
  overrides for frames, spp, resolution, and scene.
- Bounded Track 3 no-reprojection product-chain runs did not reach workload execution because H200,
  H100, A100, L40S, and A6000 attempts were unavailable or unreachable in this window. Cleanup was
  verified after interrupts.

## Top-Line Grade

Strict current grade: `7.9 / 10`.

Research scaffold grade: `9.7 / 10`.

Product substrate grade: `7.6 / 10`.

Potential if the next two infrastructure loops land: `8.4 / 10`.

Stretch potential if OptiX/GPU-denoise/chunked scheduling/warm pool all prove out on real CX scenes:
`9.0-9.3 / 10`.

Current evidence does not justify `10 / 10`. A 10 requires actual warm-worker, no-rebuild,
multi-worker end-to-end measurements on representative CX scenes with quality gates and cost
accounting. The newest Hopper runtime-root runs make no-rebuild fresh-pod experiments real and test
heavy exact fan-out without build tax. The new batch-manifest tar also proves a minimal resident
mode, but lowers the upside of this entire one-GPU multi-process family: even at 65536 samples on
H100 NVL, the best process row was `0.8908x` and the best resident-batch row was `0.8894x`.

The latest L40S runs fix the 16-way reliability hole and improve merge strategy, and the H100 NVL
tar proof removes rebuild tax from future Hopper probes. The speed conclusion is now harder:
standalone process-level and manifest-worker sample fan-out should be treated as correctness
harnesses, not product speed levers, unless execution moves deeper in-process or across truly
separate warm GPUs.

## Facet Scores

| Facet | Score | Strict Reason |
|---|---:|---|
| Upstream build reproducibility | `7.8/10` | Official standalone clone/update/build is ledgered and repeatable; Hopper can now export a reusable runtime root. |
| Patch minimality/rebaseability | `7.2/10` | Four small patches, all targeted. The batch patch is still standalone-local. Still no automated upstream rebase check in CI. |
| CUDA enablement | `7.8/10` | CUDA discovery works after OIDN probe skip; precompiled `sm_89` and `sm_90` paths are proven, and `sm_90` is portable via tar. Still no OptiX or multi-arch image. |
| OptiX/backend breadth | `2.5/10` | OptiX is not built; HIP/oneAPI/Metal are not part of this fork path yet. |
| Worker packaging | `8.1/10` | Fresh-pod Hopper and Ada/L40S runtime-root artifacts are proven, including batch-capable `sm_90` and `sm_89` tars. Still no pushed image, persistent volume, or warm pool. |
| Benchmark rigor | `9.7/10` | Ledgers, exact/numeric/drift classes, multi-scene scans, chunked cloud pass, SSIM/worst-tile gates, actual local parallel timing, retry controls, merge bakeoff, skip-build, fresh-pod tar validation, heavy no-build H100 probes, resident-batch probes, and no-build quality-ladder rows. Still official/synthetic examples. |
| Fan-out correctness | `7.5/10` | Exact/numeric on most fixed-sample scenes; throttled 16-way now completes; cube-volume drift has perceptual evidence but no final policy. |
| Fan-out speed | `4.2/10` | Clean 16-way `5.82x` modeled remains a useful primitive, but no-build H100 heavy process fan-out capped at `0.8908x` and resident-batch capped at `0.8894x`. This family should not be the product speed path. |
| Merge strategy | `6.2/10` | Python/OpenImageIO merge now beats linear `oiiotool` in the heavy run and rejects tree merge again. Still no C++/resident in-process reducer. |
| Quality gates | `7.8/10` | Fan-out outputs include EXR diff plus SSIM, worst-tile SSIM, PNG MAE, and PNG max error; the no-build quality ladder now proves actual pass/fail knees where global SSIM hides worst-tile collapse. |
| Money safety | `9.3/10` | Repeated direct checks show zero pods; watchdog/finally/ledger discipline is good, transient API cleanup failures fail closed, and two paid H100 no-build runs tore down cleanly. |
| Product integration | `5.7/10` | Fresh-pod no-rebuild Hopper and Ada roots exist, batch mode exists, the runner can execute manifest workers, and a no-build single-frame quality ladder exists. There is still no service wrapper, warm pool, job API, cache strategy, or CX scene ingestion path. |
| Spec decode readiness | `4.7/10` | The rendering substrate is better packaged and now has quality-tier knees, but actual-speed/product-chain evidence still argues against attaching spec decode before warm workers and real-scene timing. |

## Benchmark Versus Baseline Cycles

### Build And Runtime Baseline

Stock official standalone path builds and runs:

- Official contract: clone Cycles, `make update`, `make`, run `install/cycles`.
- Verified cold standalone build on L40S: `207-295s` for official/patched non-CUDA-precompiled
  builds in prior ledgers.
- Precompiled CUDA-only build: `370-488s` in recent fan-out runs, with lib sync `40-100s`.

This is not product-acceptable as a cold path. The build scaffold works, but every cloud run paying
`~6-10 minutes` of build/sync tax made the substrate a `4/10` operationally. The Hopper runtime tar
changes the experiment path: a fresh H100 NVL pod can now upload/extract the runtime root in
`11.5s` total and run smokes/probes without clone, sync, or build. It is still not a pushed worker
image or warm pool, but it is a real no-rebuild bridge.

### CUDA Versus CPU/Default Cycles

Before CUDA precompile:

- `scene_monkey.xml`, 32 samples:
  - CPU: `0.90s`
  - CUDA first render: `269.18s`
  - Verdict: unusable cold CUDA path due to runtime kernel compile.
- `scene_monkey.xml`, 512 samples:
  - CPU: `4.70s`
  - warm CUDA: `1.01s`
  - Speedup: `4.65x`

After CUDA precompile:

- `scene_monkey.xml`, 32 samples, CUDA: `0.73s`
- `scene_monkey.xml`, 512 samples, CUDA: `1.07s`
- Verdict: precompile does not make warm CUDA much faster; it removes the catastrophic first-render
  compile cliff.

Grade for this facet: `7/10`. The cliff is solved for tested `sm_89` CUDA paths; it is not solved
as a general GPU fleet strategy yet.

### Sample Fan-Out Versus Single Cycles Render

All numbers below are modeled ideal parallel wall time from sequential subset timings plus merge
time. They are not actual multi-worker end-to-end times.

Best clean result:

- `scene_world_volume.xml`, 4096 samples, 16-way:
  - single full render: `11.99s`
  - modeled fan-out wall: `2.06s`
  - modeled speedup: `5.82x`
  - quality: strict EXR exact

Other focused 16-way results:

| Scene | Full | Modeled wall | Speedup | Quality |
|---|---:|---:|---:|---|
| `scene_monkey.xml` | `5.49s` | `1.77s` | `3.10x` | numeric |
| `scene_cube_volume.xml` | `7.03s` | `1.93s` | `3.64x` | drift, max `0.0101` |
| `scene_world_volume.xml` | `11.99s` | `2.06s` | `5.82x` | exact |
| `scene_caustics.xml` | `4.35s` | `1.60s` | `2.72x` | exact |

Latest chunked cloud result, 2026-07-08:

| Scene | Workers / chunks | Full | Dynamic wall | Speedup | Quality | SSIM / worst tile | Merge linear / tree |
|---|---:|---:|---:|---:|---|---:|---:|
| `scene_world_volume.xml` | 8 / 32 | `275.25s` | `5.02s` | `54.83x` | exact | `1.0 / 1.0` | `0.85s / 5.95s` |
| `scene_world_volume.xml` | 16 / 64 | `15.39s` | `5.14s` | `2.99x` | exact | `1.0 / 1.0` | `1.96s / 12.52s` |
| `scene_cube_volume.xml` | 8 / 32 | `12.46s` | `5.92s` | `2.10x` | drift, max `0.00941` | `0.9999976 / 0.9998478` | `1.08s / 6.70s` |
| `scene_cube_volume.xml` | 16 / 64 | `12.35s` | `5.50s` | `2.25x` | drift, max `0.01187` | `0.9999972 / 0.9998072` | `1.67s / 13.18s` |

The `54.83x` result is evidence of a first-use world-volume full-render cliff, not a fair
steady-state product speedup. The fair warm exact number from this pass is `2.99x`. The strongest
steady exact number remains the prior `5.82x` world-volume 16-way run.

Actual H100 parallel follow-up, 2026-07-08:

| Scene | Workers / chunks | Full | Actual wall | Actual speedup | Quality |
|---|---:|---:|---:|---:|---|
| `scene_world_volume.xml` | 4 / 8 | `2.56s` | `4.8791s` | `0.5247x` | exact |
| `scene_world_volume.xml` | 8 / 16 | `2.55s` | `6.9733s` | `0.3657x` | exact |
| `scene_cube_volume.xml` | 4 / 8 | `2.89s` | `6.0506s` | `0.4776x` | drift, SSIM `0.999994181` |
| `scene_cube_volume.xml` | 8 / 16 | `2.88s` | `9.0479s` | `0.3183x` | drift, SSIM `0.999992617` |

This is the most honest fan-out result so far because it measures actual concurrent Cycles
processes instead of only modeling a scheduler over sequential subset timings. It is also negative:
on an H100, these official 1024-sample examples are too small for process-level sample fan-out.

Strict grade: `5.6/10`.

Reason: `5.82x` exact is real evidence that the primitive matters. But it is still modeled, while
the actual H100 process-level version lost badly on smaller official scenes. The fan-out path
becomes `7.5/10` only after persistent warm workers or in-process scheduling reproduce speedups on
heavier CX scenes with no rebuild, faster merge, and cost accounting.

No-rebuild L40S follow-up, 2026-07-08:

| Scene | Fan-out | Pass | Full | Actual wall | Actual speedup | Quality |
|---|---:|---|---:|---:|---:|---|
| `scene_world_volume.xml` | 8 | primary | `3.35s` | `4.9375s` | `0.6785x` | exact |
| `scene_world_volume.xml` | 8 | skip-build | `3.38s` | `5.2080s` | `0.6490x` | exact |
| `scene_cube_volume.xml` | 8 | primary | `2.38s` | `4.6570s` | `0.5111x` | drift, SSIM `0.999994098` |
| `scene_cube_volume.xml` | 8 | skip-build | `2.28s` | `4.6960s` | `0.4855x` | drift, SSIM `0.999994098` |

The first no-rebuild runtime-root pass proved same-pod smokes and 8-way probes. It was not a full
product solution yet because the earlier 16-way rows did not complete in either primary or
skip-build mode when one or more expected subset EXRs were missing before merge.

Throttled L40S follow-up, 2026-07-08:

| Scene | Fan-out | Pass | Full | Actual wall | Actual speedup | Quality |
|---|---:|---|---:|---:|---:|---|
| `scene_world_volume.xml` | 16 | primary | `3.78s` | `8.9071s` | `0.4244x` | exact, SSIM `1.0` |
| `scene_world_volume.xml` | 16 | skip-build | `3.78s` | `9.1237s` | `0.4143x` | exact, SSIM `1.0` |
| `scene_cube_volume.xml` | 16 | primary | `2.59s` | `9.9719s` | `0.2597x` | drift, SSIM `0.999992453` |
| `scene_cube_volume.xml` | 16 | skip-build | `2.78s` | `9.5061s` | `0.2924x` | drift, SSIM `0.999992453` |

This fixes the subset-output reliability gap for the current official-scene lane. It does not make
tiny-scene process fan-out product-fast.

Heavy chunked Python-merge L40S follow-up, 2026-07-08:

| Scene | Workers / chunks | Full | Actual wall | Actual speedup | Quality | Merge winner | Scheduler gain |
|---|---:|---:|---:|---:|---|---|---:|
| `scene_world_volume.xml` | 8 / 32 | `11.95s` | `19.2552s` | `0.6206x` | exact | Python `0.60s` | dynamic `1.2441x`, LPT `1.2981x` |
| `scene_world_volume.xml` | 16 / 64 | `12.02s` | `28.3783s` | `0.4236x` | exact | Python `0.89s` | dynamic `1.2042x`, LPT `1.2514x` |
| `scene_cube_volume.xml` | 8 / 32 | `7.13s` | `17.1361s` | `0.4161x` | drift, SSIM `0.999997575` | Python `0.60s` | dynamic `1.2519x`, LPT `1.3203x` |
| `scene_cube_volume.xml` | 16 / 64 | `6.92s` | `29.2482s` | `0.2366x` | drift, SSIM `0.999997132` | Python `0.83s` | dynamic `1.2126x`, LPT `1.2486x` |

This is the first run where in-process Python/OpenImageIO merge consistently beat linear
`oiiotool`. It also proves chunked dynamic scheduling can materially beat static contiguous
assignment in the model. It still does not prove product fan-out speed because actual subprocess
wall-clock stayed negative.

Hopper runtime-root tar follow-up, 2026-07-08:

| Pass | GPU | Build? | Upload/extract | Smokes | Probe |
|---|---|---|---:|---:|---|
| export | H100 NVL | yes, `388.0s` | pack/download `29.3s` | passed | exact 64-sample 2-way |
| fresh import | H100 NVL | no | `8.1s / 3.4s` | passed in `2.6s` | exact 64-sample 2-way |

This is the first cross-pod no-rebuild proof. It raises the packaging score materially but is still
a tiny correctness/smoke test, not a speed proof.

Hopper no-build heavy process fan-out, 2026-07-08:

Oversplit run with `--chunks-per-worker 4`, `--parallel-slots 16`:

| Samples | Fan-out / chunks | Full | Actual wall | Actual speedup | Quality |
|---:|---:|---:|---:|---:|---|
| `16384` | `8 / 32` | `20.78s` | `32.0890s` | `0.6476x` | exact, SSIM `1.0` |
| `16384` | `16 / 64` | `20.77s` | `36.6794s` | `0.5663x` | exact, SSIM `1.0` |
| `65536` | `8 / 32` | `80.86s` | `94.7564s` | `0.8533x` | exact, SSIM `1.0` |
| `65536` | `16 / 64` | `80.91s` | `97.9701s` | `0.8259x` | exact, SSIM `1.0` |

Fatter-chunk run with `--chunks-per-worker 1`, `--parallel-slots 16`:

| Samples | Fan-out / chunks | Full | Actual wall | Actual speedup | Quality |
|---:|---:|---:|---:|---:|---|
| `65536` | `4 / 4` | `80.83s` | `90.7408s` | `0.8908x` | exact, SSIM `1.0` |
| `65536` | `8 / 8` | `80.83s` | `91.0475s` | `0.8878x` | exact, SSIM `1.0` |
| `65536` | `16 / 16` | `80.82s` | `94.0047s` | `0.8597x` | exact, SSIM `1.0` |

Interpretation: the no-build path works, but standalone subprocess fan-out still does not beat one
full H100 render. Fatter chunks are better than oversplitting, but the concurrent subset processes
settle around `90-94s` while the full render is about `80.8s`. This points to scene/BVH/device
setup and GPU contention in separate Cycles processes. The next sample-parallel path must be
resident/in-process, not more shell-launched standalone Cycles.

Hopper no-build resident-batch fan-out, 2026-07-08:

| Samples | Resident workers / chunks | Full | Batch + merge wall | Actual speedup | Quality |
|---:|---:|---:|---:|---:|---|
| `16384` | `4 / 16` | `20.77s` | `24.3121s` | `0.8543x` | exact, SSIM `1.0` |
| `16384` | `8 / 32` | `20.76s` | `25.6337s` | `0.8099x` | exact, SSIM `1.0` |
| `65536` | `4 / 16` | `80.94s` | `91.0082s` | `0.8894x` | exact, SSIM `1.0` |
| `65536` | `8 / 32` | `80.92s` | `91.6854s` | `0.8826x` | exact, SSIM `1.0` |

Interpretation: the batch-manifest mode is a real resident scaffold, but not a speed lever on one
H100 NVL. It reduces the chunk launch count but still runs multiple Cycles processes with separate
GPU contexts and scene/device state. The best row, `0.8894x`, is effectively tied with the killed
subprocess best row, `0.8908x`. This narrows the next real sample-parallel option to an in-process
Cycles scheduler or separate warm GPUs, not manifest workers on one GPU.

Ada/L40S no-build artifact follow-up, 2026-07-08:

| Artifact | GPU | Build | Pack/download | Probe | Final balance |
|---|---|---:|---:|---|---:|
| `cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz` | L40S secure | `379.4s` | `19.9s / 7.9s` | exact 64-sample 2-way batch | `$15.49` |

This closes the packaging gap for the lower-cost L40S/Ada lane. Future cheap-lane experiments no
longer need to rebuild Cycles unless they change compile-time features.

## Premium GPU Suitability

H100 is suitable, but only for the right lane:

- Good: single-render baseline throughput, Hopper compatibility, heavyweight scene probes, OptiX
  feasibility, external GPU denoise, and expensive end-to-end validation after the no-rebuild path
  exists.
- Bad: repeated cold builds and process-level sample fan-out in standalone subprocesses, including
  the no-build 65536-sample H100 NVL case that capped at `0.8908x`.

RunPod pricing observed during this pass:

| GPU | Community | Secure | Use next? |
|---|---:|---:|---|
| RTX 4090 | `$0.34/hr` | `$0.69/hr` | cheap CUDA baseline |
| RTX 5090 | `$0.69/hr` | `$0.99/hr` | likely good next-gen baseline if supported |
| L40S | `$0.79/hr` | `$0.99/hr` | best cost-controlled validation lane |
| A100 PCIe | `$1.19/hr` | `$1.39/hr` | useful comparison |
| H100 PCIe | `$1.99/hr` | `$2.89/hr` | justified for bounded no-build Hopper validation |
| H200 | `$3.59/hr` | `$4.39/hr` | acceptable for bounded heavy/warm probes after safety check |
| B200/B300 | `$5.98-6.94/hr` | `$5.89-7.39/hr` | defer; likely wrong next move for this fork |

## Ten-Out-Of-Ten Requirements

The fork reaches `10/10` only when all of these are true:

1. Prebuilt worker image is built, pushed, launched, and validated on RunPod or equivalent.
2. Normal experiments use `/opt/cx-cycles/install/cycles` without rebuilding Cycles.
3. CUDA precompiled kernels are available for the deployed GPU SKUs.
4. Actual multi-worker fan-out is measured end-to-end, not just modeled from sequential subset
   timings.
5. Chunked scheduling beats static contiguous sample ranges on at least one imbalanced scene.
6. Merge overhead is reduced below the current `0.54-0.68s` 16-way shell/`oiiotool` path.
7. Fan-out quality gates include EXR diff plus SSIM/worst-tile/perceptual gates.
8. Cube-volume or equivalent drift scenes have a policy: allowed by perceptual quality, rerendered,
   or excluded.
9. Benchmark scenes include at least one real CX target scene, not only official examples.
10. Cost model includes warm-pool idle cost, worker count, fan-out level, merge cost, and expected
    product SLA.
11. OptiX lane is either proven useful, explicitly deferred with evidence, or replaced by a better
    NVIDIA backend plan.
12. The fork remains upstream-rebaseable with patch-queue tests.

Current completion against this checklist: `7.7 / 12`, with stronger partials. Same-pod
runtime-root validation, fresh-pod Hopper runtime-tar import, heavy no-build H100 validation, actual
local parallel timing, quality gates, throttled 16-way reliability, and a faster Python merge path
are now proven. The missing product pieces remain a pushed image or warm pool, a real CX scene,
actual wall-clock scheduler leverage from resident/in-process execution, and cost/SLA accounting.

This is why the strict grade is still below 8 despite some good substrate wins.

## Potential Audit

### Highest-Probability Levers

1. Prebuilt/warm worker image
   - Potential impact: huge for experiment velocity; massive for cold-start product jobs.
   - Current score: `7.4/10`.
   - Path to `8/10`: pushed image or persistent volume, an L40S/Ada tar or multi-arch image, and a
     zero-rebuild loop that is used by default for all renderer experiments.

2. Chunked sample fan-out
   - Potential impact: low-medium in the current subprocess architecture; medium-high only if the
     renderer becomes truly in-process or runs across separate warm GPUs. Static ranges showed
     imbalance, but no-build H100 heavy rows capped at `0.8908x`, and resident manifest workers
     capped at `0.8894x`.
   - Current score: `3.2/10` for standalone subprocess fan-out, `3.8/10` for manifest-resident
     workers on one GPU, `6.5/10` for a future in-process implementation.
   - Path to `8/10`: schedule samples without multiple Cycles GPU contexts and without repeated
     per-worker scene/device state, or distribute to genuinely separate warm GPUs. Do not spend more
     premium GPU time on the same one-GPU process/manifest design.

3. In-process/tree EXR merge
   - Potential impact: medium-high at 16-way+.
   - Current score: `6/10`.
   - Path to `7/10`: keep Python/OpenImageIO as the current winner and replace it with a resident
     C++/OIIO reducer if that beats Python. Naive tree subprocess merge is rejected.

4. Real CX scenes
   - Potential impact: decisive. Official examples are too small and weird.
   - Current score: `2.5/10`.
   - New evidence: synthetic `cx_many_glass.xml` runs through the no-build quality ladder, but it is
     still not a representative CX scene.
   - Path to `8/10`: at least one representative CX scene with 1080p/4K, denoise path, and user
     quality gates.

5. External OIDN GPU denoise
   - Potential impact: medium. OIDN GPU exists, but Cycles device enumeration hit a CUDA probe
     crash.
   - Current score: `3.5/10`.
   - New evidence: standalone OIDN post-process is scaffolded through `ctypes` and bundled OIDN
     libraries, but it has not emitted a validated quality row yet.
   - Path to `7/10`: keep Cycles render-device discovery stable while invoking OIDN GPU as an
     external post-process, not through the crashing enumeration path.

6. OptiX
   - Potential impact: unknown-high on NVIDIA RTX.
   - Current score: `2/10`.
   - Path to `7/10`: build with SDK, compare OptiX vs CUDA on same scenes, measure first-render and
     warm-render behavior.

7. Premium GPU lane
   - Potential impact: medium-high when used for baseline throughput, OptiX, denoise, and heavy
     production scenes; low for naive process fan-out.
   - Current score: `6.5/10`.
   - New evidence: H100 NVL no-build quality ladders land cleanly when reachable, but H200/H100
     ad hoc provisioning was capacity/reachability hostile in this window. Warm pool matters more
     than card prestige.
   - Path to `8/10`: use the Hopper runtime tar for no-build H100/H200 truth tests, then compare
     CUDA/OptiX/OIDN on a real CX scene. H100 PCIe and H100 NVL are validated; H200 is justified for
     bounded heavy/warm probes, while B200/B300 still need a specific architecture reason.

8. ReSTIR/path reuse/speculative decode
   - Potential impact: high long-term, lower short-term.
   - Current score: `2/10` for this fork.
   - Path to `6/10`: sidecar renderer or Cycles hook proves reservoir/path reuse on a narrow scene
     class. Do not chase the old broken ReSTIR branch first.

### Strict Potential Score

Current proven potential: `7.5 / 10`.

Why not higher:

- The best speedups are modeled, not actual distributed wall-clock.
- The clean `5.82x` case is an official example scene with tiny output.
- The actual H100 parallel process result is negative at `0.32-0.52x`, proving overhead can dominate
  the very hardware we hoped would amplify fan-out.
- The heavy L40S chunked process result is still negative at `0.2366-0.6206x`, even though merge and
  scheduler modeling improved.
- The no-build H100 NVL heavy process result is still negative at `0.5663-0.8908x`, even after
  removing build tax and raising samples to 65536.
- Fresh-pod no-rebuild is proven for Hopper via tar, but not as a pushed image, warm pool, L40S/Ada
  artifact, or heavy-scene performance path.
- The no-build quality ladder is still official/synthetic scenes; real CX scene evidence is missing.
- Standalone OIDN and the Track 3 product chain are scaffolded but not validated in this loop.
- Cold provisioning failed repeatedly across premium and value GPUs, so a product path cannot rely
  on ad hoc pod launch.
- Product integration is barely started.
- OptiX and real CX scenes are unproven.

Why not lower:

- CUDA compile cliff is solved for L40S.
- Standalone CLI now exposes the right sample controls.
- Fixed-sample fan-out has exact/numeric evidence across multiple scene types.
- Chunked fan-out, auto merge benchmarking, and SSIM/worst-tile gates now run on real cloud
  hardware.
- Python/OpenImageIO merge consistently beat linear `oiiotool` in the heavy run.
- Chunked dynamic scheduling showed `1.20-1.25x` modeled gain over static contiguous chunks.
- The harness now has actual local parallel timing and Hopper validation, which is a stronger truth
  source than modeled-only scheduling.
- `/opt/cx-cycles/install/cycles` now has same-pod runtime-root validation through skip-build smokes
  and 8/16-way probes.
- The Hopper `sm_90` runtime tar proves fresh-pod no-rebuild with upload/extract in `11.5s` and
  exact smoke/fan-out validation.
- Heavy no-build H100 probes are exact and bounded, giving a trustworthy kill signal for the current
  subprocess fan-out design.
- No-build H100 quality-ladder rows now show delivery-tier low-sample points up to `7.4286x` and
  expose a real worst-tile failure knee on `scene_monkey.xml`.
- Throttled 16-way actual parallel closes the previous missing-output failure on the tested lane.
- The patch queue is small.
- Money safety is strong.

## Path To A Strict 10

### Next Loop: Get From 7.9 To 8.3

- Stop spending on standalone subprocess sample fan-out unless the architecture changes.
- Do not spend more on ad hoc premium provisioning searches. Use a known reachable worker, pushed
  image, or warm pool.
- Use the now-reliable fan-out path only as a correctness harness.
- Validate standalone OIDN rows on the quality ladder, preferably with a remote background-log
  wrapper so SSH disconnects do not erase work.
- Run the bounded Track 3 no-reprojection product-chain probe once a reachable worker is secured.
- Use `docs/research/CYCLES_RESIDENT_WORKER_ARCHITECTURE_2026-07-08.md` as the resident-worker
  starting point.
- Promote the quality-ladder policy: delivery requires global SSIM `>=0.98` and worst-tile
  `>=0.95`; preview requires global `>=0.90` and worst-tile `>=0.85`.
- Promote Python/OpenImageIO merge as the default auto winner; next reduce it further with a
  resident C++/OIIO reducer only if it beats Python.
- Update ledgers and docs.

### Next Loop After That: Get From 8.1 To 8.7

- Run one real CX-ish scene through the no-build single-frame path and stack denoise/transcode.
- Build cost model: worker count, idle pool, queue time, merge, transcode, denoise.
- Probe one high-upside lane: OptiX, external GPU OIDN, or real scene conversion.

### Later Loop: Get From 8.5 To 10

- Product API path: submit scene/job, fan out, merge, quality gate, return artifact.
- Multi-pod warm-pool SLA proof.
- Explicit spec-decode attachment point with a connect-first versus audit-first decision.

## Aggressive Paste-Back Goal Prompt

Use the current prompt in `docs/research/CX_CYCLES_FORK_GOAL_PROMPT.md`. It includes the latest
balance, no-rebuild validation, Python merge result, H100/H200 spend guidance, and next-loop
priorities.
