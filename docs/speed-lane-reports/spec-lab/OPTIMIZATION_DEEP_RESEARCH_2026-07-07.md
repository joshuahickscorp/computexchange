# Optimization deep research - 2026-07-07

Scope: collect the highest-leverage ways to speed up the render/transcode product after the
current CUDA campaign. This is research plus scaffolding only. No new pods were launched.

Money-safety status: direct RunPod API check at 2026-07-07 11:22 America/Toronto reported
`pods: []`, balance `$27.37`. The push-further run timed out on its first cold-cache trial,
then tore down correctly. Its result is therefore "no data", not "tighter threshold failed".

## Local frontier

Proven on real GPU:

- Denoise anchor: 5.3-9.5x on Classroom at SSIM 0.96-0.98, and 3.1-3.4x on BMW27 at
  SSIM 0.99+.
- Light-tree/convergence: 6.3x at quality 0.894 in the static/convergence test.
- VP9 transcode delivery: 5.2x at SSIM 0.99.
- Intra-pod tile fan-out: 7.8-15.1x at SSIM 1.0 as a lossless upper bound.
- Real cross-pod distribution works only after amortization. Cold provisioning made a
  3-pod job slower end-to-end despite 1.96x compute speedup.

Confirmed wall:

- Animated camera-dolly temporal reuse on Classroom fails with both 2D motion-vector warp
  and analytical 3D depth/camera reprojection.
- Analytical reprojection improved worst-tile from about 0.27 to 0.4036, but still missed
  even the preview-tier worst-tile floor of 0.85.
- The follow-up for `disocclusion_thresh` below 0.02 did not complete because a new pod lost
  the local reference cache and hit the old 900s SSH timeout.

## Highest-leverage bets

### 1. Ship-today honest stack without temporal reuse

Run `exp_render_ultimate.py` with `keyframe_every=1` so there is no reprojection. This gives
the product number for the proven path: denoise anchor plus light-tree plus VP9 delivery.
It should be the next measured result because it avoids the known temporal wall and produces
a defensible customer-facing baseline.

Experiment:

```bash
python3 scripts/spec-lab/run_research_queue.py ultimate_no_reprojection
```

If using the existing one-off driver style instead, provision one warm pod and run:

```bash
cd /root/spec-lab && python3 pod/exp_render_ultimate.py '{"frames":8,"keyframe_every":1,"draft_spp":512,"ref_spp":4096,"adaptive_threshold":0.02,"denoiser":"oidn","denoise_guides":true,"light_tree":true,"codec":"libvpx-vp9","resolution":"1920x1080","scene":"classroom","bounces":12}'
```

Expected signal: end-to-end delivered-video SSIM and worst-tile should inherit the denoise
anchor quality rather than the temporal-warp collapse.

### 2. Warm-pool architecture before more cross-pod math

The 3-pod test found the product problem: provisioning tax, not compute. A real product needs
resident workers with:

- predownloaded Blender and Python deps;
- persistent scene/reference caches on a shared volume or object store;
- resident worker process that accepts jobs over a queue instead of SSH-per-experiment;
- per-worker health, spend ceiling, idle TTL, and teardown watchdog;
- work splitting after the pod is already warm.

External support: the cluster path tracing paper reports Blender/Cycles on RTX cluster nodes
with performance and quality scaling linearly when the cluster is already built and the dataflow
is engineered for low overhead. That is consistent with our result: compute parallelism is real,
but cold-start orchestration kills one-off jobs.

Source: https://arxiv.org/abs/2110.08913

### 3. Exact sample splitting as a distribution primitive

Tile splitting is already lossless but has scheduling overhead and a future edge/compositing
surface. Blender's newer "Sample Subset" mechanism appears designed for rendering sample ranges
separately and merging them, which is a better primitive for warm workers when the task is a
single expensive frame. This is worth a focused probe:

- render EXR sample ranges on two workers or two local subprocesses;
- merge with `bpy.ops.cycles.merge_images`;
- compare merged output to one full sample-count render by SSIM and pixel delta;
- measure whether adaptive sampling/sample-count AOVs interact safely with merging.

Expected signal: if exact or near-exact, sample-subset fan-out becomes the cleanest distributed
single-frame strategy. If merge quality diverges under adaptive sampling, keep it for fixed-spp
or use it only for references.

Source: https://docs.blender.org/manual/en/latest/render/cycles/render_settings/sampling.html

### 4. OIDN guide-pass correctness and temporal denoising

OIDN docs say albedo and normal guides usually improve detail, but first-hit guides do not help
reflections/transparency; storing features for a later non-delta hit can improve reflections and
transmission. Our BMW27 "guides are inert" result may mean Blender already feeds denoising AOVs,
or our explicit flag is not changing the actual guide data. A good next test is not just guides
on/off, but guide-kind:

- first-hit albedo/normal;
- denoising albedo/normal as Blender emits them;
- non-delta/specular-followed albedo/normal if accessible;
- prefiltered aux with `cleanAux` semantics in standalone OIDN.

OIDN's current RT filter documentation says it is not temporally stable. However, the HPG 2025
program says the next major OIDN version is under development with temporal denoising and a more
advanced neural architecture. This is a high-watch item, not something to block the product on.

Sources:

- https://www.openimagedenoise.org/documentation.html
- https://highperformancegraphics.org/2025/program/

### 5. OptiX temporal denoiser as a near-term animation stabilizer

OptiX temporal denoising needs previous denoised beauty plus flow/motion vectors. The current
pipeline already learned the hard part: motion blur must be off for Cycles vector pass data.
This is different from frame synthesis: use every real rendered draft frame, then stabilize
denoising across time. It should attack flicker and sample count, not missing geometry.

Experiment:

- render every frame at lower spp with vector/Z/denoise AOVs;
- run OptiX temporal denoise externally or via a small SDK wrapper;
- compare to per-frame OIDN and OptiX static denoise on temporal metrics plus SSIM/worst-tile.

Source: https://raytracing-docs.nvidia.com/optix9/api/group__optix__host__api__denoiser.html

### 6. AOV-guided low-res beauty plus neural or analytic upscale

The existing `exp_render_upscale_guided.py` is aligned with current research: render fewer pixels,
then reconstruct with guide buffers. AMD's neural denoising/upscaling work uses noisy color plus
albedo, normal, roughness, depth, and specular-hit distance, with temporal accumulation and motion
vectors. Our current AOV-guided upscaler is analytic, so it is a cheap baseline. A learned model
trained on our own Cycles data is the bigger bet.

Run next:

```bash
cd /root/spec-lab && python3 pod/exp_render_upscale_guided.py '{"scene":"classroom","low_res":"960x540","full_res":"1920x1080","method":"all","spp":4096,"guide_spp":16,"bounces":12}'
```

If the analytic guided arm clears SSIM/worst-tile, productize. If it loses but bicubic is close,
move to a learned joint denoise/upscale only after gathering a scene-pair dataset.

Source: https://gpuopen.com/learn/neural_supersampling_and_denoising_for_real-time_path_tracing/

### 7. Bidirectional interpolation plus auxiliary viewpoints

The current temporal wall is consistent with missing source data: a single keyframe cannot reveal
geometry hidden in that keyframe. The HPG 2025 split-rendering paper uses bidirectional reprojection
from sparse frames, object motion, auxiliary viewpoints for disocclusions, and shadow tracking.

This suggests a more promising animation reuse test than "previous keyframe only":

- render bracketing keyframes, not just previous keyframe;
- generate intermediate with bidirectional reproject/blend;
- render one or two cheap auxiliary camera views targeted at disoccluded regions;
- compare against the current analytical single-source method.

This will not be free, but it directly targets the observed failure mode.

Source: https://derthomy.github.io/ImageBasedSpatioTemporalInterpolation/

### 8. ReSTIR-family path reuse, not image reuse

ReSTIR is the serious long-term path for temporal/spatial reuse because it reuses light/path
samples rather than final shaded pixels. This is not a small script tweak in Blender; it likely
means a Cycles fork, a custom renderer, or an RTXDI-style prototype. But it is the right research
direction if animation reuse remains strategic.

Relevant newer directions:

- ReSTIR GI reports 9.3x to 166x MSE improvements at 1 spp in test scenes with denoising.
- ReSTIR PT Enhanced reports 2-3x faster ReSTIR PT with lower error and better robustness.
- ReSTIR PG closes the loop by extracting guiding distributions from accepted resampled paths
  to improve the next frame's initial candidates.

Sources:

- https://research.nvidia.com/publication/2021-06_restir-gi-path-resampling-real-time-path-tracing
- https://research.nvidia.com/labs/rtr/publication/lin2026restirptenhanced/
- https://research.nvidia.com/labs/rtr/publication/zeng2025restirpg/
- https://github.com/NVIDIA-RTX/RTXDI/blob/main/Doc/RestirPT.md

### 9. Neural radiance caching

Neural radiance caching trains while rendering and supports dynamic scenes without pretraining.
It targets indirect lighting rather than final-image interpolation. It is attractive if the product
can own a custom integrator layer. It is not an immediate Blender CLI lever.

Source: https://arxiv.org/abs/2106.12372

### 10. Scene-aware render settings, but only with tile floors

The low-risk knobs are already being tested: adaptive threshold, spp, denoiser, light tree, bounces.
Additional scene-specific knobs worth adding to a risk-gated tuner:

- diffuse/glossy/transmission/transparent bounce caps separately, not only total bounces;
- clamp direct/indirect fireflies;
- caustics on/off;
- light-tree on/off per scene class, because it can cost overhead in simple lighting;
- persistent data within a resident Blender process;
- border/crop render for identified hard tiles;
- per-AOV or light-group denoising for diffuse/specular separation.

Every one of these can bias output, so gate by global SSIM, worst-tile SSIM, p5 tile, and temporal
flicker metrics.

## Recommended next run order

1. `ultimate_no_reprojection`: honest product number without temporal reuse.
2. `upscale_guided_all`: determine whether low-res beauty plus full-res guides is a usable pixel
   lever.
3. `interp_bidirectional`: run the existing learned/flow interpolation spike; if it fails, build
   the auxiliary-view variant before spending more on single-source reprojection.
4. `sample_subset_probe`: prove exact sample-split merging locally or on one warm pod.
5. `optix_temporal_denoise_probe`: animation denoise stability, not frame synthesis.
6. `bmw27_analytical_animation`: one cheap scene-specific wall check, only after the above.

## Strategic read

Do not abandon temporal reuse as a research area, but stop treating single-source image
reprojection as the product path for animated camera-dolly interiors. The product path should be:

1. per-frame denoise anchor;
2. warm-pool distribution;
3. exact tile/sample fan-out;
4. transcode delivery;
5. optional low-res guided upscaling if it clears worst-tile;
6. optional temporal denoise for animation stability.

The long-term animation breakthrough is likely path/sample reuse (ReSTIR, path guiding, radiance
caching) or bidirectional/auxiliary-view interpolation, not a tighter depth-disocclusion threshold.
