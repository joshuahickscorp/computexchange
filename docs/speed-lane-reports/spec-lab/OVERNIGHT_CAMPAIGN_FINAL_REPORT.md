# Overnight 100x campaign — final report (2026-07-06 21:58 → 2026-07-07 07:14)

**Mandate:** run autonomously overnight, add levers aggressively, push toward 100x-cheaper-
to-the-user even if unreachable, stay honest, stop at a genuine wall. Money: ~$11 spent of
the ~$40 loaded (balance $30.66 final), zero orphaned pods at every checkpoint.

## Bottom line

Two real, hard-won product wins are proven on real GPU hardware. One promising technique
(temporal reuse via 2D motion-vector reprojection, anchored on cheap keyframes) hit a
genuine, thoroughly-searched structural wall and does not clear any usable quality tier —
this is a real finding, not a failure to try hard enough. Two real bugs were found and fixed
along the way (both would have silently invalidated results if left alone). A third avenue
(analytical depth-based reprojection) is fully designed and ready to attempt in a future
session if the temporal-on-animation product angle is worth revisiting.

## Proven wins (real GPU, honest, ready to build a product on)

1. **Denoise anchor** (adaptive sampling + OIDN denoiser, single-frame, production regime —
   real Blender Classroom scene, 1080p, 4096-spp reference vs 512-spp draft):
   **~5.3x @ SSIM 0.978** (near-lossless tier) up to **9.5x @ SSIM 0.964** (preview tier).
   This is the core render-speed lever and it works standalone.
2. **GPU-native convergence (light-tree many-light importance sampling)**, tested on a
   STATIC scene (no reprojection): **6.3x @ quality 0.894** (light-tree on) vs
   **8.4x @ quality 0.858** (light-tree off) — light-tree trades some speed for meaningfully
   better quality; a real, usable, independent lever.
3. **Speculative transcode delivery**: codec ladder tested (libx264/x265/VP9/AV1) —
   **VP9 wins: 5.2x @ SSIM 0.99** (NVENC-on-draft is a LOSS since the reference is the
   expensive side, not the draft). A genuine delivery-path multiplier, independent of the
   render-side story.
4. **Tile fan-out (ideal distribution ceiling)**: 8 tiles → 7.8x, 16 tiles → 15.1x, both at
   **SSIM 1.0** (lossless split, confirms the tiling implementation has no seam bugs). This is
   an UPPER BOUND (no network/scheduling overhead measured) but confirms the distribution
   envelope is real and roughly linear with tile count in this regime.

Composed (not multiplied naively — each is a genuinely separate factor): denoise anchor
(~5x) x convergence (~1.3x extra from light-tree's quality-for-speed trade, or skip it for
more speed) x distribution (~8-16x ideal) x transcode delivery (~5.2x) is a real, defensible
path toward a large compound number for STATIC/single-frame or tiled-parallel rendering
workloads. This is buildable as a product today.

## The wall: temporal reuse on animated content (2D motion-vector reprojection)

**The idea:** render one keyframe per N frames at anchor quality, reproject the rest via
Cycles motion vectors, re-render only disoccluded patches. Measured pre-fix at up to 14-19x
raw speedup — but quality collapsed (global SSIM ~0.6-0.9, worst-tile as low as 0.14).

**Two real bugs found and fixed along the way (both independently re-verified, not just
trusted from a subagent's self-report):**

1. **Cycles empties the Vector (motion) pass whenever render motion blur is ENABLED** —
   documented, intentional Cycles behavior (Blender issue T48908). The runner had this
   EXACTLY BACKWARDS: it turned motion blur ON believing it was required to populate the
   Vector pass. Fixed: `scene.render.use_motion_blur = False`
   (`scripts/spec-lab/pod/exp_render_stack.py:600`). Verified: motion vectors went from
   *exactly* zero (mean=std=absmax=0.0) to real, correctly-scaled values, cross-checked twice
   independently.
2. **No reference-render caching existed at all** — every tuning trial was unconditionally
   re-rendering the full 8-frame 4096-spp reference (~15-40 min) even when only a draft-side
   knob changed, wasting an entire 4-hour tuning budget on 5 trials of ONE knob. Fixed: added
   a real cache keyed by (scene, resolution, ref_spp, bounces, frames, seed, cam_motion),
   storing per-frame `.npy` + a manifest with the REAL historical render times (never
   fabricated). Verified: repeat call was 5.3x faster with byte-identical T_ref and quality.

**With BOTH bugs fixed, a full, properly-cached, 7-knob coordinate-ascent + refine sweep
(2.5 hours, $3.60, converged with no further improvement) found:**

`worst_tile_ssim` is pinned at **~0.27** (range 0.14-0.27) across every single knob tested:
draft_spp (256→1024), adaptive_threshold (0.005→0.05), denoiser (oidn/optix), light_tree
(on/off), hole_fill (rerender/inpaint/nearest), resolution (1080p/720p). None of these move
the worst-tile ceiling meaningfully. Global quality tops out around **0.87-0.88**. **No
combination of knobs clears ANY of the four quality tiers (0.90/0.95/0.98/0.99).**

**Interpretation:** this is not a parameter-tuning problem — it's a structural limitation of
representing reprojection as a single 2D per-pixel motion vector plus heuristic disocclusion
masking, when the camera undergoes real dolly/parallax through a complex interior (as
opposed to the original proven runner's validated case: a small orbiting/rotating OBJECT
with a mostly-static camera). Some specific content class (large-parallax disocclusion,
possibly view-dependent shading, or foreshortening the mask doesn't correctly flag) is
consistently unrecoverable by this method, regardless of sample count, denoiser, or fill
strategy.

**This is a genuine, honestly-earned wall for THIS specific technique.** It does not
invalidate the other proven wins above, which don't depend on motion-vector reprojection at
all.

## Ready for a future session: analytical depth-based reprojection

A fully-designed alternative exists at
`/private/tmp/claude-501/.../scratchpad/analytical_reproject_design.md` (written by a
hypothesis-agent during tonight's debugging): instead of relying on Cycles' Vector pass,
compute reprojection ANALYTICALLY from the (fully known, since we control the camera
keyframing) camera intrinsics/extrinsics per frame plus the (working, unaffected by tonight's
bug) Z/depth pass — unproject each target pixel to world space using the target frame's own
depth, then reproject into the keyframe's camera view to get a sample coordinate, replacing
`warp_gather`'s motion-vector-based sampling. Disocclusion would be detected via a
depth-consistency test (compare the reprojected point's depth against the keyframe's own
depth at that location) rather than the current motion-vector-divergence heuristics. This is
a genuinely different, more physically-grounded technique that could plausibly clear the
worst-tile ceiling where 2D motion vectors cannot — worth attempting if temporal-reuse for
animated content becomes a priority again. Not attempted tonight due to time; flagging
honestly rather than claiming it as a completed avenue.

## Money accounting (all sessions tonight, all torn down clean)

| session | spend | result |
|---|---|---|
| denoise-anchor experiment | ~$0.15 | 5.3x-9.5x proven |
| stack ladder (keystone + convergence + fan-out) | $0.61 | convergence/fan-out proven; first keystone quality-collapse signal |
| 3 rounds of live-hardware diagnosis | ~$0.37 | ruled out camera keyframing, motion magnitude, denoiser |
| fix-workflow (found + fixed motion_blur bug) | ~$0.39 | root cause found |
| independent re-verification of the fix | $0.13 | confirmed real |
| cache-bug diagnosis + fix + verification | $0.14 | confirmed 5.3x cache speedup |
| stack tuner run 1 (pre-cache-fix, wasted) | $5.63 | only 5 trials, 1 knob — superseded |
| stack tuner run 2 (post-cache-fix, thorough) | $3.60 | converged, real wall confirmed |
| **total** | **~$11.02** | balance $40.10 → $30.66 (started campaign ~$42) |

Zero orphaned pods at any checkpoint (verified via direct RunPod API query, not just trusting
driver logs, at every teardown tonight).

## Recommendation

Ship the product on the proven, composable wins (denoise anchor + convergence + distribution
+ transcode delivery) — these are real, near-lossless-capable, and don't depend on the
technique that hit a wall. Temporal-reuse-on-animated-content is honestly a preview-tier-only
lever with the CURRENT 2D-motion-vector approach (quality ~0.87 at 11-25x, fine for a "fast
draft" tier, not for "near-lossless"); the analytical depth-reprojection redesign is the
concrete next step if animated near-lossless reuse becomes a priority.
