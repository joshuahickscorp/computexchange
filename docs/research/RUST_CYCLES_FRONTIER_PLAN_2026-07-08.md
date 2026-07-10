# Rust/Cycles Frontier Plan - 2026-07-08

Prepared after the July 7 render-speed campaign, the Cycles fork/build probes, and the first
Rust renderer microbench. This is a planning artifact for the new frontier: build an owned
renderer/speculative render decoder path, using Cycles as reference material and compatibility
anchor rather than trying to blindly replace all of Blender/Cycles on day one.

Money-safety checkpoint before this plan: direct RunPod API check reported `pods: []` and
balance `$22.33`.

## July 8 Cycles Fork Update

After this plan was drafted, the standalone Cycles fork moved from scaffold to usable CUDA primitive:

- Patch `0001` exposes sample-subset CLI flags.
- Patch `0002` skips a crashing OpenImageDenoise CUDA device probe by default.
- Patch `0003` exposes `--disable-adaptive-sampling` for fixed-sample fan-out.
- CUDA device discovery now works on RunPod L40S.
- Default standalone CUDA paid a `269s` first-render kernel compile cliff.
- CUDA-only precompiled build with
  `-DWITH_CYCLES_DEVICE_OPTIX=OFF -DWITH_CYCLES_CUDA_BINARIES=ON -DCYCLES_CUDA_BINARIES_ARCH=sm_89`
  removed that cliff: first CUDA monkey render at 32 samples fell to `0.73s`.
- Sample fan-out works on CUDA, with the critical caveat that default adaptive sampling introduces
  real merge drift at 128+ samples. With `--disable-adaptive-sampling`, the drift collapses to
  numeric merge noise: at 4096 samples / 8-way fan-out, mean error is `2.10e-6`, max error is
  `5.37e-5`, and modeled ideal wall improves from `5.38s` full to `1.67s` (`3.22x`) even on the
  tiny official monkey scene.
- A follow-up 4096-sample multi-scene sweep found strict exactness for world-volume and caustics,
  numeric-equivalent behavior for monkey/sphere/cube-surface, and a real cube-volume drift case
  with max error up to `0.0096`. The best modeled speedup in that sweep was `3.30x`, but several
  8-way runs were hurt by uneven static sample ranges, so chunked scheduling is now a first-class
  product lever.
- A focused 16-way pass found the current clean ceiling: `scene_world_volume.xml` was strict exact
  at `5.82x` modeled speedup. Caustics was also strict exact at `2.72x`, monkey was numeric at
  `3.10x`, and cube-volume reached `3.64x` but stayed in the drift bucket. Merge overhead
  (`0.54-0.68s`) is now visible enough to deserve its own optimization lane.

This shifts the near-term product plan: before deeper Rust renderer work, build/push a precompiled
CX Cycles worker image and use it as the warm CUDA sample-fan-out substrate.

## Executive verdict

Yes: the strategic pivot is right.

But the precise target should not be "rewrite all of Cycles in Rust." That would turn into a
multi-year production-renderer project before it produces customer leverage. The right target is:

1. Fork/copy/study Cycles first, especially standalone Cycles.
2. Build a Rust renderer sidecar that owns the narrow fast path where speculation and repeated
scene structure can compound.
3. Keep Cycles/Blender as the correctness oracle, compatibility bridge, and fallback renderer.
4. Attach speculative decode to path structures, material variants, reservoirs, tiles, sample
subsets, and AOV-guided denoising, not to final-image warping alone.
5. Productize the first real speedups around repeated-scene workflows: catalog variants,
look-dev, product colorways, material sweeps, still packs, short turntables, and previews.

The key principle: do not compete with Cycles where Cycles is already excellent. Compete where
Cycles must stay general and stateless, but our product can assume repeated scenes, known quality
gates, warm workers, cached path structure, variant batches, and customer-visible accept/retry
policy.

## Why this pivot makes sense

The previous frontier found a real wall: single-source temporal image reprojection on animated
camera dolly content does not survive worst-tile quality gates. Analytical 3D reprojection improved
over 2D motion vectors, but still landed around `0.40` worst-tile, far below even a preview-tier
floor around `0.85`.

That result is not a failure of the whole speed thesis. It says the "token" was wrong. Final shaded
pixels are too late in the pipeline. They already contain visibility, material response, lighting,
view-dependent effects, disocclusions, and denoiser behavior all baked together.

The better tokenization is lower-level:

- primary ray hits;
- path vertices;
- visibility queries;
- shadow rays;
- BSDF samples;
- light samples;
- reservoirs;
- sample subsets;
- material-variant shade results;
- denoiser/AOV features;
- tiles with confidence scores.

This is where a speculative decoder can work. Draft cheap/reused structure, verify the parts that
affect quality, and rerender only where the verification fails.

## What the current evidence says

### Proven wins from the speed-lane campaign

- Denoise anchor: `5.3-9.5x` on Classroom at SSIM `0.96-0.98`.
- BMW27 denoise generalization: `3.1-3.4x` at SSIM `0.99+`.
- Light-tree convergence: `6.3x` at quality `0.894`.
- VP9 transcode delivery: `5.2x` at SSIM `0.99`.
- Intra-pod tile fan-out: `7.8-15.1x` at SSIM `1.0`, a lossless ceiling for ideal local
  parallelism.
- Cross-pod distribution works in principle, but cold provisioning crushed the result:
  `1.96x` on 3 pods because `825s` provisioning dominated `73s` compute. Warm pool is mandatory.

### Confirmed negatives

- Open PGL path guiding on the tested setup was a real negative post-denoise:
  `guiding_helps_post_denoise=False`, worst-tile delta about `-0.0002`, and `1.67x` wall-clock
  overhead at equal sample count.
- The ReSTIR research fork build was not worth continuing in that branch: after fixing real build
  problems, the fork's `lib/linux_x64` precompiled dependency pin appeared empty/broken.
- Single-source frame warping should not be the product path for camera-dolly animation.

### New Rust renderer microbench signal

The local Rust renderer now has an exact decoupled-shading primitive:

- File: `renderer/src/decoupled.rs`
- Example: `renderer/examples/decoupled_micro.rs`
- Test: `renderer/tests/decoupled_shading_test.rs`

Fresh local run on July 8:

| Variants | Independent ms | Cached ms | Speedup | Primary tests | Shadow tests | Pixel diff |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 11.754 | 7.850 | 1.497x | 1.0x reduction | 1.0x reduction | 0 |
| 2 | 16.261 | 10.199 | 1.594x | 2.0x reduction | 2.0x reduction | 0 |
| 4 | 31.517 | 11.122 | 2.834x | 4.0x reduction | 4.0x reduction | 0 |
| 8 | 54.845 | 12.402 | 4.422x | 8.0x reduction | 8.0x reduction | 0 |
| 16 | 116.633 | 17.902 | 6.515x | 16.0x reduction | 16.0x reduction | 0 |
| 32 | 220.227 | 30.288 | 7.271x | 32.0x reduction | 32.0x reduction | 0 |

Cloud L40S run got a lower but still useful result:

- 8 variants: `165.099ms` independent vs `49.077ms` cached, `3.364x`.
- Primary and shadow intersection reductions were exactly `8x`.
- `max_abs_diff=0`, so this was exact reuse, not approximate reuse.

The cloud graphics stack did not expose Vulkan:

- `wgpu_vulkan_smoke`: `no vulkan(forced) adapter available`.
- `vulkaninfo`: `vkCreateInstance failed with ERROR_INCOMPATIBLE_DRIVER`.

This is not a renderer failure. It means the current RunPod PyTorch/CUDA image is compute-ready
but not graphics/Vulkan-ready. The next infrastructure milestone is a Vulkan/graphics-capable
container or template.

## Current Cycles landscape

Cycles is very much alive. It is not abandoned.

Relevant sources checked during this pass:

- Blender release notes index:
  https://developer.blender.org/docs/release_notes/
- Blender 5.2 release notes:
  https://developer.blender.org/docs/release_notes/5.2/
- Cycles 5.2 notes:
  https://developer.blender.org/docs/release_notes/5.2/cycles/
- Official Cycles mirror:
  https://github.com/blender/cycles
- Cycles build instructions:
  https://github.com/blender/cycles/blob/main/BUILDING.md
- Blender manual, Cycles GPU rendering:
  https://docs.blender.org/manual/en/latest/render/cycles/gpu_rendering.html

Useful facts:

- The official release notes page lists Blender 5.1 as the current stable release as of March 17,
  2026, with Blender 5.2 LTS in beta until July 8, 2026.
- Cycles can be built as a standalone application or as a Hydra render delegate.
- Cycles is Apache-2.0 licensed in the official standalone mirror.
- Cycles supports multiple GPU device families through Blender: CUDA, OptiX, HIP, oneAPI, and
  Metal.
- The standalone build path is intentionally available: clone Cycles, run `make update`, then
  `make`; the resulting binary is `./install/cycles`.
- Blender 5.2 Cycles adds a texture cache intended to reduce memory usage and startup time on
  scenes with many image textures.

The implication: we should not bet on Cycles being stagnant. We should bet on specialization. Our
edge must be a product-specific renderer path that Cycles cannot merge upstream because it is too
biased toward our use case.

## Research landscape to steal from

### ReSTIR and path reuse

Sources:

- ReSTIR GI:
  https://research.nvidia.com/publication/2021-06_restir-gi-path-resampling-real-time-path-tracing
- ReSTIR PT Enhanced:
  https://research.nvidia.com/labs/rtr/publication/lin2026restirptenhanced/
- RTXDI / ReSTIR PT docs:
  https://github.com/NVIDIA-RTX/RTXDI/blob/main/Doc/RestirPT.md
- ReSTIR PG:
  https://research.nvidia.com/labs/rtr/publication/zeng2025restirpg/

Most important current signal: NVIDIA's May 2026 ReSTIR PT Enhanced paper claims `2-3x` faster
ReSTIR PT, lower visual and numerical error, and better robustness. The paper's framing is exactly
the path we care about: spatiotemporal reuse of light transport samples, not final-image warp.

Our translation: ReSTIR should not be first attempted as a fragile old Cycles branch build. It
should become one module in the owned renderer, with Cycles as the reference image generator.

### wgpu and portable Rust GPU

Sources:

- wgpu:
  https://wgpu.rs/
- wgpu repository:
  https://github.com/gfx-rs/wgpu

wgpu is attractive for early portability:

- Metal works locally on Apple Silicon.
- Vulkan should work on Linux once the container exposes a real graphics stack.
- It is a credible way to keep one Rust compute/graphics codebase alive while we learn.

wgpu is not the final answer for every hard ray-tracing problem:

- RT-core access is weaker than direct OptiX/Vulkan ray tracing paths.
- A serious NVIDIA path may need OptiX/CUDA/Slang later.
- A serious Apple path may need Metal kernels for production performance.

So the staged plan is: wgpu now, backend-specific accelerators later.

### Headless Vulkan/container issue

Source:

- NVIDIA Container Toolkit release notes:
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/1.12.0/release-notes.html

NVIDIA documents headless Vulkan support when `NVIDIA_DRIVER_CAPABILITIES` includes `graphics` or
`display`. Our RunPod result is consistent with a container/runtime that exposed compute but not the
Vulkan ICD/direct-rendering devices required by wgpu Vulkan.

This needs its own tiny experiment before more cloud renderer work:

- choose a Vulkan-ready RunPod template or build an image from an NVIDIA Vulkan/CUDA base;
- set `NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics`;
- run `vulkaninfo`, `nvidia-smi`, `cargo run --example wgpu_smoke`, and a tiny shader dispatch;
- then run the decoupled microbench.

## Strategic choice

### Option A: Full Cycles rewrite in Rust

Do not choose this as the first product plan.

Pros:

- Maximum ownership.
- Long-term control over scheduling, caches, memory layout, and speculation.
- Cleaner integration with our platform and safety/cost machinery.

Cons:

- Huge compatibility scope: materials, OSL, volumes, curves, hair, geometry nodes, lights, cameras,
  color management, denoising AOVs, texture systems, motion blur, instancing, USD/Hydra, and every
  weird Blender asset users will throw at us.
- Hard to beat Cycles on general scenes.
- Long time before customer-visible advantage.

### Option B: Cycles fork with speculative/reuse patches

Useful as a reference and targeted experiment, but not the main ownership strategy yet.

Pros:

- Full production feature coverage comes along for the ride.
- Correctness comparison is straightforward.
- CUDA/OptiX/HIP/Metal support already exists.

Cons:

- Build fragility is real, especially on research branches.
- Deep kernel changes require C++/CUDA expertise and constant upstream conflict management.
- Rust speculative policy ends up outside the hot path unless we punch C ABI hooks into Cycles.

### Option C: Owned Rust renderer sidecar plus Cycles oracle/fallback

This is the recommendation.

Pros:

- Lets us build only the narrow path where 20x+ is plausible.
- Lets Cycles remain the compatibility and reference engine.
- Lets us exploit assumptions a general renderer cannot: same scene, many variants, warm cache,
  known image metrics, known product tiers, batch rendering, and accept/retry workflows.
- Lets the spec decoder attach naturally to path structure and reservoirs.

Cons:

- Early feature coverage will be small.
- We need conversion/import constraints.
- Some user scenes will fall back to Cycles for a long time.

Decision: build Option C, while keeping Option B as a compatibility/fallback and research testbed.

## Product framing

This is UX work in systems clothing.

The user does not care whether a speedup came from a denoiser, a reservoir, a light tree, a cached
primary hit, a sample subset, a warm pool, or VP9 delivery. They care about:

- how fast can I see a trustworthy result;
- how much can I explore without waiting;
- does the final look match the approved look;
- can the system tell me when it is uncertain;
- can it recover by spending more compute only where needed.

So the renderer should expose a progressive trust ladder:

1. Draft preview in seconds.
2. Verified preview with tile confidence.
3. Client-safe render.
4. Final/reference.
5. Batch/variant pack.

The speculative renderer does not need to be perfectly unbiased at every intermediate tier. It needs
to be honest about confidence, self-correcting, and measured against our worst-tile/SSIM gates.

## The owned renderer target

Name it something boring internally for now: `cx-renderer`.

Initial target:

- Rust-first host system.
- wgpu compute/graphics backend first.
- CPU reference backend always present.
- Simple path tracer with:
  - triangles;
  - BVH;
  - area lights;
  - environment maps;
  - diffuse and GGX materials;
  - metallic/roughness workflow;
  - simple glass later;
  - albedo/normal/depth/motion AOVs;
  - deterministic sampling;
  - tile-level metrics;
  - cached path/hit structure;
  - material-variant re-shading.

Explicit non-goals for the first 60 days:

- full Blender material graph;
- full OSL;
- production volumes;
- hair/curves;
- arbitrary Geometry Nodes;
- exact Cycles matching on all scenes;
- general film/VFX rendering.

## How spec decode attaches

The speculative renderer should be designed like a decoder with draft and verify stages.

### Tokens

Potential "tokens" in a render decoder:

- camera ray packet;
- primary hit;
- G-buffer sample;
- path vertex;
- next-event light sample;
- shadow visibility result;
- BSDF lobe choice;
- material shade result;
- reservoir;
- tile;
- sample subset;
- denoiser feature vector;
- variant result.

### Draft stage

Draft candidates can come from:

- previous material variant;
- previous sample subset;
- previous frame;
- neighboring pixels;
- lower-resolution render;
- lower-spp render;
- Cycles reference cache;
- analytical reprojection;
- learned predictor;
- reservoir from ReSTIR-like reuse;
- path guiding distribution;
- cached primary visibility;
- cached shadow visibility.

### Verify stage

Verification can happen at multiple costs:

- exact re-shade with cached hit data;
- re-test visibility only;
- recompute BSDF/pdf/MIS weight;
- compare depth/normal/material ID;
- reservoir weight validation;
- tile confidence score;
- AOV consistency check;
- denoised tile SSIM against a cheap reference;
- final worst-tile gate.

### Accept/reject policy

Acceptance should be local:

- accept material re-shade when geometry and visibility are unchanged;
- accept reservoir candidates when target PDF and visibility pass;
- accept tile output when worst-tile predictor is above threshold;
- reject/fallback only hard regions instead of rerendering the whole frame.

This turns rendering into a scheduler:

1. Generate draft work.
2. Verify cheap.
3. Spend more compute on rejected regions.
4. Denoise.
5. Score.
6. Retry only failed tiles/samples.
7. Deliver the highest tier that clears.

## Architecture

### Layer 1: Cycles reference harness

Purpose: keep us honest.

Tasks:

- build official standalone Cycles, not the old ReSTIR research fork;
- render a tiny standard scene set;
- render matching scenes through Blender CLI;
- normalize color management/OCIO;
- export EXR AOVs;
- preserve reference outputs with content-addressed names.

Deliverables:

- `scripts/spec-lab/run_cycles_standalone_build.py`;
- `scripts/spec-lab/pod/exp_cycles_standalone_smoke.py`;
- `docs/research/CYCLES_SOURCE_MAP.md`;
- a reference image corpus under artifact storage, not pod-local disk.

Gate:

- official standalone Cycles builds on one local machine and one RunPod image;
- sample scenes render;
- Blender CLI and standalone Cycles are understood well enough to explain deltas.

### Layer 2: Rust renderer core

Purpose: own the hot path.

Modules:

- scene representation;
- BVH build/update;
- CPU reference path tracer;
- wgpu backend;
- deterministic sampler;
- material system subset;
- AOV buffers;
- tile scheduler;
- image output/EXR;
- metrics hooks.

Gate:

- white furnace test passes;
- CPU and GPU outputs agree within tolerance;
- simple scenes visually match reference;
- no hidden nondeterminism in repeated runs.

### Layer 3: Decoupled path-structure cache

Purpose: exploit repeated geometry and repeated visibility.

Already-started primitive:

- independent render: trace and shade each variant;
- cached render: trace path structure once, re-shade variants;
- exact equality for current simplified material family.

Next extensions:

- move from toy sphere to triangle mesh;
- store primary hit, normal, barycentrics, material id;
- store shadow ray endpoints and visibility where invariant;
- separate geometry cache from material cache;
- add invalidation rules for camera, geometry, material, light changes;
- record cache hit rate and correctness deltas.

Gate:

- `8x` variant traversal reduction on mesh scenes;
- `>5x` wall-clock speedup for 16 material/color variants;
- zero or bounded pixel delta for exact-invariant cases.

### Layer 4: Sample-subset/fan-out scheduler

Purpose: make warm-pool distribution exact.

Use cases:

- split one expensive frame across devices;
- render sample ranges independently;
- merge deterministic samples;
- avoid tile-boundary artifacts;
- keep exactness where possible.

Cycles already has sample-subset concepts in newer versions, so this also becomes a bridge between
our renderer and Cycles.

Gate:

- sample subset merge matches a monolithic fixed-spp render within numerical tolerance;
- adaptive sampling interactions are characterized;
- warm worker can receive sample range jobs without cold setup.

### Layer 5: Reservoir/reuse module

Purpose: attack global illumination and temporal reuse at the path level.

First version:

- direct-light reservoir for spatial reuse;
- target PDF validation;
- visibility validation;
- MIS accounting;
- per-tile confidence.

Second version:

- temporal reservoir proposer from previous frame;
- analytical reprojection as one candidate source;
- disocclusion/history rejection;
- reciprocal neighbor selection and duplication-map ideas from ReSTIR PT Enhanced;
- guide distribution fitting from accepted reservoirs.

Gate:

- equal-time MSE/SSIM improvement over baseline on static scenes;
- no catastrophic worst-tile failures on camera motion;
- if temporal candidates fail, they degrade to fresh sampling rather than image garbage.

### Layer 6: Denoise/upscale integration

Purpose: keep the proven anchor and improve animation UX.

Short path:

- keep OIDN/OptiX as external reference denoisers;
- emit albedo/normal/depth/motion AOVs;
- test OptiX temporal denoiser;
- run guided upscaling tests.

Owned path:

- train small denoiser/upscaler on our own AOV/noise distribution;
- deploy via ONNX/ORT first;
- later port hot kernels to Rust/GPU.

Gate:

- match or beat OIDN on our scene distribution at acceptable latency;
- reduce flicker without view-warp artifacts;
- never hide tile failures from the confidence system.

### Layer 7: Product/warm-pool system

Purpose: turn speed into customer-visible UX.

Required:

- resident workers;
- no pod-local-only reference cache;
- object-store/shared-volume scene cache;
- job queue;
- tile/sample/variant scheduler;
- spend ceiling;
- idle TTL;
- watchdog teardown;
- telemetry ledger;
- per-job quality report.

Gate:

- a 3-worker warm job beats one worker end-to-end;
- no orphaned pods under failure injection;
- references persist across pod death;
- cold-start cost is amortized over batch jobs.

## Projected numbers

These are deliberately aggressive targets. They are not promises. They are a map of where the
speedup could come from if several independent levers stack.

### Conservative product target: 5-10x

Likely in days/weeks with existing proof:

- denoise anchor: `3-9x`;
- VP9/transcode delivery: `5.2x` on delivery leg;
- tile/sample fan-out inside a warm worker: `7.8-15.1x` ceiling;
- no temporal reuse required.

Best fit:

- still renders;
- preview tiers;
- short non-temporal jobs;
- single-scene batches where final quality can use proven Cycles/OIDN.

### Ambitious near-term target: 10-20x

Likely for variant batches if the Rust renderer sidecar advances:

- decoupled traversal reuse: measured exact `4.4x` at 8 variants locally, `7.27x` at 32 variants;
- denoise anchor: `3-9x` depending on scene/quality tier;
- warm scheduling avoids cold-start tax.

This does not mean `4.4 * 9 = 39.6x` automatically, because the stages overlap and bottlenecks
move. But `10-20x` is plausible when:

- geometry and camera are fixed;
- many material/color variants are requested;
- output tier accepts denoised lower-spp samples;
- workers are warm;
- the renderer avoids repeated traversal.

Best fit:

- product colorways;
- catalog renders;
- A/B style boards;
- lighting look-dev with constrained changes.

### Frontier target: 20-30x

Plausible with a narrow product contract:

- cached primary/visibility/path structure;
- material variant re-shading;
- sample-subset warm-pool fan-out;
- denoise anchor;
- transcode delivery;
- tile retry only on failed regions.

This is the first "unrealistic but not crazy" milestone.

Required constraints:

- fixed camera/geometry for a batch;
- constrained material edits;
- known scene class;
- warm workers;
- confidence-gated delivery.

The product should actively steer users into this shape: "upload one scene, generate 24 approved
variants fast."

### Moonshot target: 30-50x

Plausible only when many levers compound across a batch, not as a single general-renderer speedup.

Potential stack:

- 8-16x traversal work reduction for 8-16 variants;
- 3-5x denoise/sample reduction at target tier;
- 2-4x warm parallel fan-out after amortization;
- 1.5-3x reservoir/path reuse on difficult lighting;
- delivery/transcode optimization on video outputs.

The hard part is not multiplying all those numbers. The hard part is keeping enough of them
non-overlapping. The scheduler must know which bottleneck dominates each job.

Best fit:

- scene-stable variant packs;
- turntable/product videos with repeated geometry;
- many outputs from one warm scene;
- preview-to-final workflows where only failed tiles escalate.

### Extreme target: 50-75x

This is a business/workflow target, not a raw per-frame integrator target.

It becomes conceivable when:

- one expensive scene setup is amortized over dozens/hundreds of outputs;
- geometry/path caches persist;
- worker pool is warm;
- material variants dominate the work;
- only a subset of outputs need final reference quality;
- the system delivers verified previews immediately and finalizes in the background.

Example shape:

- one interior/product scene;
- 64 material/color variants;
- fixed camera set;
- path structure generated once per camera;
- variants re-shaded cheaply;
- denoised previews delivered first;
- reference/final computed only for selected variants.

In that shape, the customer experiences 50x+ because the baseline is "render every variant from
scratch and wait." The renderer experiences a smaller but still powerful combination of exact
reuse, warm scheduling, and selective escalation.

### 100x class target

Do not promise this in product language yet.

It is only plausible if the denominator is a naive, cold, from-scratch batch workflow and the
numerator is:

- warm pool;
- cached scene;
- cached path structure;
- many variants;
- cheap verified previews;
- selective finalization;
- optimized delivery.

This can be a private north-star metric, not a public claim.

## What "copy first" means

Copying/forking should be done as a source-map exercise, not as an immediate rewrite.

Immediate steps:

1. Clone official Cycles standalone outside the main repo or into a controlled vendor/research
   directory.
2. Build it locally and on a known-good Linux image.
3. Render its example XML scenes.
4. Map source directories:
   - kernel/device;
   - scene sync;
   - integrator;
   - BVH;
   - shader nodes;
   - lights;
   - sampling;
   - film/buffers;
   - denoising AOVs;
   - Hydra delegate.
5. Write `CYCLES_SOURCE_MAP.md` in our docs.
6. Identify the smallest subset we want to mirror in Rust.
7. Identify exact compatibility boundaries:
   - what we import;
   - what we ignore;
   - what falls back to Cycles.

This keeps the work grounded. It also avoids rediscovering production-renderer lessons the hard
way.

## Roadmap

### Phase 0 - 48 hours: orientation and infrastructure

Objective: prove we can build, compare, and run the minimum platform safely.

Tasks:

- Keep RunPod money check as first step for every cloud run.
- Build official standalone Cycles, not the old ReSTIR branch.
- Create a Cycles source map document.
- Fix cloud graphics backend:
  - select/build Vulkan-capable container;
  - expose `graphics` capability;
  - pass `vulkaninfo`;
  - pass `wgpu_smoke`;
  - rerun decoupled microbench on L40S/RTX.
- Preserve all reference renders outside pod-local disk.
- Run local Rust renderer sweep as part of CI-ish script.

Deliverables:

- `docs/research/CYCLES_SOURCE_MAP.md`;
- `scripts/spec-lab/run_cycles_standalone_build.py`;
- `scripts/spec-lab/run_renderer_backend_matrix.py`;
- a small artifact manifest with local/cloud renderer microbench results.

Go/no-go:

- If official Cycles standalone cannot be built quickly, keep Blender CLI as oracle and defer
  standalone.
- If Vulkan is not available on RunPod templates, switch Linux GPU work to CUDA/OptiX prototypes
  sooner.

### Phase 1 - Week 1: Rust renderer narrow core

Objective: move from toy primitive to simple mesh renderer.

Tasks:

- Add triangle mesh support.
- Add BVH build and traversal.
- Add deterministic camera sampler.
- Add diffuse/GGX/metallic roughness material subset.
- Add area light and HDRI.
- Add EXR/PNG output.
- Add AOV output: albedo, normal, depth, material id.
- Port decoupled cache to mesh scenes.
- Add benchmark scenes:
  - Cornell-ish box;
  - product sphere/teapot/material balls;
  - BMW-like glossy object subset if import is ready.

Gate metrics:

- white furnace still passes;
- CPU/GPU outputs agree within tolerance;
- 8 material variants show `>3x` wall-clock speedup on mesh scene;
- 16 variants show `>5x` speedup;
- exact cases have zero or bounded pixel delta.

### Phase 2 - Weeks 2-3: GPU path and backend realism

Objective: stop proving only CPU algorithms; get real GPU data.

Tasks:

- wgpu compute backend for path tracing kernels.
- GPU buffers for scene, BVH, rays, hits, AOVs.
- Persistent GPU cache for path structures.
- GPU decoupled re-shading for variants.
- Backend matrix:
  - Apple Metal;
  - RunPod Vulkan on L40S/RTX;
  - fallback CPU;
  - optional CUDA/OptiX feasibility spike.

Gate metrics:

- Metal local smoke and benchmark pass.
- L40S/RTX Vulkan smoke passes.
- Decoupled variant benchmark improves over CPU for moderate scene.
- Memory use is bounded and measured.

### Phase 3 - Weeks 3-5: Cycles compatibility bridge

Objective: know exactly when to use the Rust renderer and when to fall back.

Tasks:

- Implement import for a constrained format:
  - glTF first, or
  - Cycles XML if standalone path is easier, or
  - USD subset if Hydra bridge becomes useful.
- Build scene classifier:
  - supported materials;
  - unsupported features;
  - fallback reason.
- Create side-by-side renderer:
  - render with Rust;
  - render with Cycles;
  - compare SSIM/worst-tile;
  - store deltas.
- Add color-management normalization.

Gate metrics:

- 5-10 curated scenes classify correctly.
- Supported scenes render end-to-end.
- Unsupported scenes fall back cleanly.
- Quality deltas are explained, not mysterious.

### Phase 4 - Weeks 5-8: Spec decode v1

Objective: turn caching into a general draft/verify scheduler.

Tasks:

- Define render token data structures.
- Add draft sources:
  - cached primary hits;
  - cached shadow visibility;
  - previous material variant;
  - low-spp sample subset;
  - neighboring tile candidates.
- Add verification:
  - geometry/material consistency;
  - visibility retest;
  - BSDF/pdf validation;
  - tile confidence.
- Add accept/reject scheduler:
  - cheap accept;
  - partial retry;
  - full fallback.
- Emit quality report per output.

Gate metrics:

- Variant scenes clear `10x+` vs naive baseline for preview tier.
- Failed regions are localized.
- No silent quality failure: every bad output either fails the gate or escalates.

### Phase 5 - Months 2-3: Reservoir/ReSTIR module

Objective: attack lighting variance and temporal reuse below the image level.

Tasks:

- Direct-light reservoir prototype.
- Spatial reuse pass.
- Visibility and target-PDF validation.
- Temporal candidate proposer from previous frame.
- Analytical reprojection as candidate source, not final image.
- ReSTIR PT Enhanced ideas:
  - reciprocal neighbor selection;
  - footprint-based reconnection;
  - duplication maps;
  - disocclusion noise reduction.
- ReSTIR PG-style guide fitting from accepted reservoirs.

Gate metrics:

- `2x+` equal-quality improvement on at least one lighting-hard scene.
- Temporal camera motion degrades gracefully.
- Worst-tile gate beats image reprojection wall by a wide margin.

Kill criteria:

- If reservoirs do not beat denoise-only on our scene distribution, do not sink months into
  unbiased academic polish. Keep the variant/cache product path.

### Phase 6 - Months 3-4: Warm-pool productization

Objective: make cloud distribution actually profitable and fast.

Tasks:

- Resident worker.
- Persistent scene/reference cache.
- Shared artifact store.
- Queue protocol.
- Sample/tile/variant work splitting.
- Spend guard.
- Idle TTL.
- Watchdog teardown.
- Per-output quality ledger.

Gate metrics:

- 3 warm workers beat 1 worker end-to-end.
- No orphaned pods in failure injection.
- Batch speedup improves with output count.
- Customer-visible preview arrives before full finalization.

## Experiment queue

### Queue A: cheap local

1. Keep running `cargo test --release`.
2. Add `cargo bench` or JSON benchmark harness for decoupled variants.
3. Add mesh primitive and repeat variant sweep.
4. Add small image output and pixel-diff fixture.
5. Add CPU-vs-wgpu Metal comparison.

### Queue B: cloud infrastructure

1. Run money safety check.
2. Test Vulkan-capable container.
3. Run `vulkaninfo`.
4. Run `wgpu_smoke`.
5. Run decoupled microbench on L40S.
6. Repeat on RTX 4090/5090-class if available.
7. Compare A100: likely good for CUDA compute, less useful for graphics/Vulkan path.

### Queue C: Cycles reference

1. Build official standalone Cycles.
2. Render example XML scene.
3. Render same conceptual scene in Blender CLI.
4. Export EXR/AOVs.
5. Store references in artifact storage.
6. Write source map.

### Queue D: product-shape tests

1. Product colorway batch: 8/16/32 variants.
2. Fixed camera, material-only changes.
3. Lighting-only changes.
4. Camera-only changes.
5. Geometry changes.
6. Classify what reuse survives.

### Queue E: speculative decode tests

1. Cached primary hits only.
2. Cached primary plus shadow visibility.
3. Cached path prefix.
4. Reservoir direct-light reuse.
5. Temporal reservoir candidates.
6. Tile confidence and selective retry.

## Team and skills needed

Solo/near-term owner work:

- Rust renderer scaffolding.
- RunPod drivers and safety.
- benchmark harnesses.
- Cycles source mapping.
- simple CPU path tracer.
- product scheduler prototypes.

Specialist work eventually needed:

- GPU rendering engineer;
- CUDA/OptiX/Vulkan/Metal expertise;
- renderer material/BSDF engineer;
- ML denoising/upscaling engineer if we pursue owned denoiser;
- infrastructure engineer for warm-pool production.

Practical hiring/contracting target:

- first specialist should be a GPU rendering generalist who can read Cycles, write Rust/C++,
  understand BVHs/path tracing, and debug CUDA/Vulkan/Metal.

## Risks

### Risk: Cycles keeps improving

This is guaranteed, not hypothetical. Mitigation: specialize where Cycles cannot assume repeated
scene batches and product-specific quality gates.

### Risk: Rust/wgpu leaves performance on the table

Likely for ultimate NVIDIA performance. Mitigation: use wgpu to learn and ship portability, then
add backend-specific kernels where the bottleneck is proven.

### Risk: compatibility swamp

Very high if we promise general Blender support. Mitigation: sidecar renderer with explicit
support classifier and Cycles fallback.

### Risk: speculative reuse creates silent artifacts

Mitigation: worst-tile gates, confidence reports, accept/reject at local granularity, and automatic
escalation.

### Risk: 20x+ requires batch-shape constraints

True. Mitigation: make the product UX steer users into favorable shapes instead of pretending every
single render can be 50x faster.

### Risk: cloud graphics stack wastes money

Already observed. Mitigation: make Vulkan/graphics smoke a hard preflight before renderer pods do
real work.

## What to build next

Build these in order:

1. `CYCLES_SOURCE_MAP.md`
   - prove we understand what we are copying/forking.

2. Vulkan-capable renderer pod smoke
   - stop spending renderer time on compute-only images.

3. Mesh-based decoupled renderer
   - prove the exact reuse primitive survives beyond the toy scene.

4. Product colorway benchmark
   - first real 20x-shaped workflow.

5. Cycles comparison harness
   - keep visual correctness honest.

6. Spec-decode scheduler v1
   - draft/verify/retry with quality report.

7. Warm-pool worker
   - make distribution real.

## Suggested first milestone definition

Milestone name: `cx-renderer-m1-variant-cache`.

Target:

- Import or construct a simple mesh product scene.
- Render 16 material/color variants.
- Cache geometry/path structure once.
- Re-shade variants.
- Compare against independent renders.
- Produce image outputs and JSON metrics.
- Run locally on Metal and in cloud on Vulkan/L40S.

Success:

- `>5x` wall-clock speedup for 16 variants on at least one non-toy mesh scene.
- bounded/zero pixel delta for exact-invariant cases.
- cloud backend works without Vulkan failure.
- no live pods after run.

Stretch:

- `>10x` for 32 variants.
- denoise/upscale preview tier included.
- Cycles reference comparison included.

## Strategic final read

Rebuilding Cycles is too broad. Owning a Cycles-informed Rust render decoder is exactly the right
frontier.

The big speedups probably do not come from one magic algorithm. They come from changing the unit of
work:

- from "render this image from scratch";
- to "verify and complete this scene-specific batch using cached path structure, reservoirs,
  denoising, warm workers, and selective escalation."

That is where 20x, 30x, 50x, and maybe 75x live.
