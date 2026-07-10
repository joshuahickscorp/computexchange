# CX Cycles Fork Goal Prompt

You are Codex working in `/Users/scammermike/Downloads/computexchange`.

Continue the CX Cycles fork frontier as an iterative, evidence-first loop. Money safety comes first:
before and after any RunPod work, confirm zero live pods and never persist credentials to disk.

## Current Proven State

- Official standalone Cycles builds from `https://projects.blender.org/blender/cycles.git`.
- Patch queue:
  - `0001-standalone-sample-subset-cli.patch`
  - `0002-device-skip-oidn-cuda-probe-by-default.patch`
  - `0003-standalone-disable-adaptive-sampling-cli.patch`
  - `0004-standalone-cx-batch-manifest.patch`
- CUDA L40S device discovery works after skipping the fragile OIDN CUDA probe.
- Default CUDA path has a huge first-render compile cliff: `269.18s` on first monkey CUDA render.
- CUDA-only precompiled build removes that cliff:
  - CMake args:
    `-DWITH_CYCLES_DEVICE_OPTIX=OFF -DWITH_CYCLES_CUDA_BINARIES=ON -DCYCLES_CUDA_BINARIES_ARCH=sm_89`
  - first CUDA monkey 32-sample render: `0.73s`
  - CUDA monkey 512-sample render: `1.07s`
- Sample fan-out works on CUDA, with the important fixed-sample caveat:
  - default adaptive sampling is strict-exact only at the 64-sample toy probe and drifts at 128+
  - `--disable-adaptive-sampling` collapses the drift to numeric merge noise
  - 4096 samples, 8-way, adaptive off: `5.38s` full vs `1.67s` modeled ideal wall, `3.22x`
  - 4096 samples, 8-way error: mean `2.10e-6`, RMS `6.17e-6`, max `5.37e-5`
- Multi-scene 4096-sample scan:
  - `scene_world_volume.xml` and `scene_caustics.xml`: strict exact at 2/4/8-way
  - monkey/sphere/cube-surface: numeric-equivalent at 2/4/8-way under mean/RMS/max tolerances
  - `scene_cube_volume.xml`: drift case, max error `0.0060-0.0096`; requires perceptual gate
  - modeled speedups ranged `1.26-3.30x`, with static chunk imbalance hurting several 8-way runs
- Focused 16-way scan:
  - `scene_world_volume.xml`: strict exact, `5.82x` modeled speedup
  - `scene_caustics.xml`: strict exact, `2.72x`
  - `scene_monkey.xml`: numeric-equivalent, `3.10x`
  - `scene_cube_volume.xml`: `3.64x` but drift, max error `0.0101`
  - merge overhead reached `0.54-0.68s`, so merge strategy is now an optimization target
- A6000 chunked fan-out cloud pass:
  - 4096 samples, `scene_world_volume.xml`, 16 workers / 64 chunks: `2.99x` modeled warm exact
  - 4096 samples, `scene_cube_volume.xml`, 16 workers / 64 chunks: `2.25x` modeled perceptual drift
  - naive tree merge lost badly to linear `oiiotool`
- H100 PCIe actual-parallel follow-up:
  - build worked with `sm_90`
  - all probes tore down cleanly; final balance was `$18.75`, with `pods: []`
  - process-level sample fan-out was negative on 1024-sample official scenes: `0.32-0.52x`
  - conclusion: H100 is suitable for premium baseline, OptiX/OIDN, and heavyweight truth tests, but
    not for tiny cold process fan-out
- L40S no-rebuild validation:
  - build-once root `/opt/cx-cycles/install/cycles` was validated through a skip-build pass
  - skip-build runtime smokes passed in `6.6s`
  - skip-build 8-way world-volume was exact, SSIM `1.0`, actual speedup `0.6490x`
  - skip-build 8-way cube-volume was drift, SSIM `0.999994098`, worst tile `0.999484419`,
    actual speedup `0.4855x`
- L40S throttled 16-way validation:
  - `--parallel-slots 8 --subset-retries 1` closes the missing-output failure on the tested lane
  - skip-build 16-way world-volume was exact, SSIM `1.0`, actual speedup `0.4143x`
  - skip-build 16-way cube-volume was drift, SSIM `0.999992453`, worst tile `0.999349480`,
    actual speedup `0.2924x`
  - this is a reliability win, not a speed win; tiny official scenes are overhead-bound
- L40S heavy chunked Python-merge validation:
  - 4096 samples, 8/16 workers, 32/64 chunks, actual parallel, exact-clean world-volume plus
    drift-risk cube-volume
  - Python/OpenImageIO merge won every probe: `0.60-0.89s` vs linear `0.62-1.19s` and tree
    `6.39-13.01s`
  - dynamic scheduling modeled `1.20-1.25x` gain over static contiguous chunks; LPT modeled
    `1.25-1.32x`
  - actual process fan-out still lost: best actual speedup was `0.6206x`
- H100 NVL runtime-root tar validation:
  - exported a CUDA-only Hopper `sm_90` runtime root containing `install/` and `examples/`
  - local artifact:
    `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz`
  - artifact size: `192M`; extracted root size on pod: `516M`
  - export pass build `388.0s`, pack `19.9s`, download `9.4s`
  - fresh-pod import uploaded in `8.1s`, extracted in `3.4s`, then ran binary, patch CLI, render
    smoke, and exact tiny fan-out without clone, sync, or build
  - import probe: `scene_world_volume.xml`, 64 samples, 2-way, exact, SSIM `1.0`, actual wall
    `1.3744s`, Python merge `0.32s`
- H100 NVL no-build heavy fan-out validation:
  - ran from the Hopper runtime tar, no clone/sync/build
  - exact-clean `scene_world_volume.xml`, 16k and 65k samples
  - oversplit best: 65536 samples, 8-way/32 chunks, `0.8533x`
  - fatter-chunk best: 65536 samples, 4-way/4 chunks, `0.8908x`
  - all seven heavy probes were exact with SSIM `1.0`
  - conclusion: standalone subprocess fan-out is a correctness harness, not the product speed path
- H100 batch-manifest resident scaffold:
  - exported `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`
  - H100 PCIe secure build validated all four patches and the two-job batch smoke
  - H100 NVL no-build resident-batch matrix used `4/8` resident manifest workers and `16/32`
    chunks on exact-clean world-volume
  - all four resident-batch probes were exact with SSIM `1.0`
  - best resident-batch speedup was still only `0.8894x` at 65536 samples, 4 workers / 16 chunks
  - conclusion: one-GPU manifest workers are also a correctness/scaffold lane, not a product speed
    lane
- Ada/L40S batch runtime tar:
  - added `--gpu-tier ada`, restricted to L40S for `sm_89` builds
  - exported `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`
  - L40S secure build time `379.4s`; tar size `191M`
  - tiny 64-sample, 2-way batch probe was exact with SSIM `1.0`
  - conclusion: both Hopper and Ada/L40S lanes now have no-build batch-capable runtime roots
- No-build H100 NVL quality ladder:
  - added `scripts/spec-lab/run_cycles_quality_ladder.py`
  - added `scripts/spec-lab/test_cycles_quality_ladder.py`
  - `51` Cycles tests pass
  - report:
    `docs/speed-lane-reports/spec-lab/CYCLES_QUALITY_LADDER_AND_PROVISIONING_2026-07-08.md`
  - best delivery-tier rows vs a `4096 spp` reference:
    - `scene_world_volume.xml`: `64 spp`, SSIM `1.0`, worst tile `1.0`, `7.4286x`
    - `scene_cube_volume.xml`: `64 spp`, SSIM `0.997972352`, worst tile `0.982018952`,
      `7.3333x`
    - `scene_monkey.xml`: `32 spp`, SSIM `0.999617997`, worst tile `0.975919427`,
      `6.6154x`
    - `scene_caustics.xml`: `4 spp`, SSIM `1.0`, worst tile `1.0`, `5.4658x`
    - `scene_sphere_bump.xml`: `4 spp`, SSIM `0.994660223`, worst tile `0.959727762`,
      `3.6575x`
    - synthetic `cx_many_glass.xml`: `64 spp`, SSIM `0.999854288`, worst tile
      `0.996755943`, `3.7216x`
    - `scene_cube_surface.xml`: `8 spp`, SSIM `0.995968115`, worst tile `0.979001121`,
      `2.9595x`
  - important knee: `scene_monkey.xml` at `4/8 spp` has high global SSIM but fails worst-tile
    quality (`0.702331872` and `0.839460065`), `16 spp` is preview, `32 spp` is delivery
  - current quality policy: delivery requires global SSIM `>=0.98` and worst-tile `>=0.95`;
    preview requires global `>=0.90` and worst-tile `>=0.85`
- Standalone OIDN scaffold:
  - `run_cycles_quality_ladder.py --with-oidn` loads bundled `libOpenImageDenoise.so.2` through
    Python `ctypes`, writes OIDN EXRs, and scores `variant=oidn` rows with denoise time included
  - not validated yet: attempts hit missing Python OIIO bindings, then a transient SSH disconnect
    after switching to `OpenEXR`/`Imath`
- Track 3 no-reprojection product-chain runner:
  - `run_ultimate_no_reprojection.py` now accepts `--gpu-tier premium|value|any` plus bounded
    overrides for frames, spp, resolution, and scene
  - bounded product-chain probes did not reach workload execution because premium/value cold pods
    were unavailable or unreachable in this window
  - conclusion: warm pool or known reachable pre-warmed worker is now mandatory before spending on
    product-chain timing
- Resident-worker architecture note:
  `docs/research/CYCLES_RESIDENT_WORKER_ARCHITECTURE_2026-07-08.md`
  - latest settled safety check: `pods: []`, tracked pods `[]`, balance `$14.36`

## Objective

Turn the fork from a build scaffold into a product substrate:

1. Use the Hopper `sm_90` runtime tar for premium no-build experiments; do not rebuild Cycles on
   H100/H200 unless the artifact is invalid.
2. Add a cheap-lane `sm_89` runtime tar or pushed multi-arch worker image so L40S/Ada experiments
   also stop rebuilding Cycles.
3. Convert the prebuilt-root experiment path into normal practice for all micro-experiments.
4. Stop spending on standalone subprocess or one-GPU manifest-worker sample fan-out unless the
   architecture changes.
5. Design the next sample-parallel lane around a true in-process Cycles scheduler, genuinely
   separate warm GPUs, or a product pivot that stacks single-frame render with denoise/transcode.
6. Measure merge overhead, numeric equivalence, and final quality gates with EXR diffs plus SSIM.
7. Estimate warm-pool economics against the old cold-provision multi-pod result.
8. Keep notes in `docs/research/CYCLES_SOURCE_MAP.md` and ledgers under
   `docs/speed-lane-reports/spec-lab/`.
9. Re-grade strictly after each loop; current strict score is `7.9/10` overall, `7.6/10` product
   substrate, `9.7/10` research scaffold, `7.2/10` proven sample-parallel potential, and
   `7.8/10` proven single-frame quality-tier potential.

## Aggressive Targets

- Eliminate per-job Cycles build time entirely.
- Eliminate first-render CUDA compile time entirely.
- Show numeric-equivalent 2-way, 4-way, 8-way, and 16-way sample fan-out on CUDA for a real CX
  scene, not just `scene_monkey.xml`.
- Convert modeled chunked scheduling gains into actual wall-clock gains only after moving deeper
  than one-GPU manifest workers: true in-process scheduling or separate warm GPUs.
- Reduce 16-way merge overhead below the new Python/OpenImageIO path (`0.83-0.89s` at 64 chunks)
  only if the replacement really beats Python.
- Find one path to a plausible `20x+` product speedup by stacking:
  - prebuilt worker image;
  - warm pool;
  - CUDA;
  - resident/in-process sample fan-out, if it proves out;
  - denoise anchor;
  - VP9/transcode delivery.
- Treat `50x+` and `75x+` as stretch product numbers that require warm-pool removal of provisioning
  tax plus scene classes where denoise/sample fan-out/transcode multiply cleanly. Do not claim them
  from the monkey-scene microbench.
- Spend more when it buys truth. H100/H200 are acceptable and preferred for bounded heavy/warm
  validation after safety checks and with a hard stop condition. Give a progress/spend update before
  crossing about `$5` on a premium run, then estimate how much more truth the next increment buys.

## Guardrails

- Do not chase final-image temporal reprojection for animation unless it is a cheap comparison.
- Do not rebuild Cycles for every micro-experiment once an image or prebuilt-root path exists.
- Treat OptiX as a separate build track requiring SDK support; do not block CUDA progress on it.
- Keep the fork minimal and upstream-rebaseable.

## Paste-Back Goal

```text
/goal Continue the active CX Cycles fork goal in /Users/scammermike/Downloads/computexchange.
Be aggressive and iterative. The latest checked RunPod state was pods: [], tracked: [], balance
$14.36. You may spend up to about $8 if the experiments are scoped, bounded, and teardown-safe.
Never persist credentials. Check pods and balance before and after cloud work. Do not run concurrent
RunPod drivers because tracked-pod cleanup is shared.

Current strict grade: 7.9/10 overall, 7.6/10 product substrate, 9.7/10 research scaffold, 7.2/10
proven sample-parallel potential, 7.8/10 proven single-frame quality-tier potential. Treat 10/10
as the floor for an edified CX Cycles substrate and push above it if evidence allows.

Primary mission: build the edified version of Cycles for CX before restarting spec decode.

Required loop:
1. Keep using the proven Hopper runtime tar:
   `.artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz`. Do not rebuild
   Cycles on H100/H200 unless the artifact fails. For L40S/Ada, use
   `.artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz`.
2. Treat standalone subprocess sample fan-out and one-GPU manifest-worker fan-out as killed for
   product speed: the best no-build H100 heavy subprocess result was `0.8908x`, and the best
   resident-batch result was `0.8894x`. Use them only as correctness harnesses.
3. Design or scaffold the next architecture: true in-process Cycles scheduler, separate warm-GPU
   workers, or the product chain around single-frame render + denoise + transcode. Start from
   `docs/research/CYCLES_RESIDENT_WORKER_ARCHITECTURE_2026-07-08.md`.
4. Treat Python/OpenImageIO merge as the current winner. Attack merge overhead next with a resident
   C++/OIIO reducer only if it can beat Python.
5. Use the current quality policy: delivery requires global SSIM >=0.98 and worst-tile >=0.95;
   preview requires global >=0.90 and worst-tile >=0.85. Do not let global SSIM hide a bad tile.
6. Find or construct one real CX-ish heavy scene where scheduling, denoise, and premium GPU choices
   matter.
7. Use H100/H200 when it buys faster truth on a heavy/warm workload, but do not spend another loop
   on cold ad hoc premium provisioning searches. In this window H200/H100/A100/L40S/A6000 attempts
   repeatedly failed capacity or SSH reachability. Secure a known reachable worker, prebuilt image,
   or warm pool first. Give a spend/progress update before crossing about $5 on a premium run, then
   estimate the next spend increment.
   Do not touch B200/B300 without a specific architecture reason and hard stop.
8. Next high-upside lanes, in order:
   validate `run_cycles_quality_ladder.py --with-oidn` on a known reachable worker, ideally with a
   remote background-log/poll wrapper so SSH disconnects do not erase stage results;
   run the bounded Track 3 no-reprojection product-chain probe;
   convert or build one real CX-ish scene;
   probe OptiX only if SDK support is immediately available.
9. Update docs/research/CYCLES_SOURCE_MAP.md, docs/research/CX_CYCLES_FORK_GRADE_AND_POTENTIAL_AUDIT_2026-07-08.md,
   and docs/speed-lane-reports/spec-lab/.
10. Re-grade strictly, redo the potential audit, and state whether spec decode should connect next or
   wait for another Cycles loop.

Do not stop at plans if cloud budget remains and safety is clean. Do not claim 10/10 from modeled
timings. Preserve money safety even while spending aggressively.
```
