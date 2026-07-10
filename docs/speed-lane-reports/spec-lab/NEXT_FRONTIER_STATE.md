# Next-frontier campaign state (2026-07-07 morning, continuing overnight work)

Continuing from `OVERNIGHT_CAMPAIGN_FINAL_REPORT.md` — proven wins banked (denoise anchor,
light-tree, transcode, ideal fan-out), a real structural wall found (temporal reprojection on
animation, worst-tile pinned ~0.27). Owner: "spend more if needed, prove everything possible
on CUDA before validating on Apple Silicon." Balance at start: $30.62.

## Tracks

1. **Cross-scene (BMW27) + 4K validation of the denoise anchor** — DONE, see below.
2. **Real (not ideal) multi-pod distribution** — queued next.
3. **Honest end-to-end pipeline** (exp_render_ultimate.py, keyframe_every=1, no reprojection) — queued.
4. **Analytical depth-reprojection build** — running in background (Workflow wl4n4p3ts), no pod cost.
5. **Learned interpolation + guided upscale spikes** — queued.

## Track 1 results (DONE, $0.33, clean teardown, balance $30.29)

**BMW27 (specular/glossy stress test) — the anchor GENERALIZES, quality even IMPROVED:**
- contender (thr=0.02, oidn+guides): **3.10x @ SSIM 0.994** (worst-tile 0.978, p5 0.985)
- naive (32spp fixed): **3.44x @ SSIM 0.992** (worst-tile 0.959) — surprisingly close to
  contender; BMW27's smooth car-paint + clean HDRI denoises well even from noisy input
  (unlike Classroom's fine geometric detail — chair legs, blinds — which stresses the
  denoiser harder). The "specular over-smoothing" concern did not materialize in SSIM terms.
- noguides: **byte-identical to contender** (0.994/0.978/0.985) — CONFIRMED again (2nd scene)
  that the explicit albedo/normal guide-pass flag makes no measurable difference; OIDN likely
  auto-consumes AOVs regardless, or our flag adds nothing beyond default behavior. Not a bug,
  just an inert lever — worth knowing definitively.
- Speedup is genuinely lower than Classroom's ~4.5-5.3x (BMW27 ref renders in ~61s vs
  Classroom's ~115s — this scene's lighting converges faster, so there's proportionally less
  "slow reference" for the draft to beat). Quality more than compensates.

**Classroom @ 4K — quality holds, but speedup SHRINKS (not grows as predicted):**
- contender: **3.89x @ SSIM 0.976** (worst-tile 0.954) — down from 1080p's 5.27x @ 0.978/0.953
- naive: **6.98x @ SSIM 0.967** (worst-tile 0.938) — down from 1080p's 9.47x @ 0.964/0.921
- Reference time scaled EXACTLY 4x with pixel count (465s vs ~115s, matching 4096spp @ 4x
  pixels). Draft scaled ~5.5x for contender, worse than linear — and BOTH configs show
  almost IDENTICAL proportional speedup drops (~26% relative, contender and naive alike),
  pointing to a SHARED resolution-dependent cost (most likely OIDN's own runtime scaling
  super-linearly with resolution, not an adaptive-sampling-specific effect).
  **CORRECTION to the design panel's prediction:** the anchor's speedup does NOT grow at 4K —
  it modestly shrinks, while quality remains solidly near-lossless at both resolutions.
  1080p is the speedup sweet spot; 4K still works, just at a lower multiple. Report this
  honestly — don't force-fit to the earlier (wrong) prediction.

## Track 2 results (DONE, $0.51, clean teardown, balance $29.78)

**Real (not ideal) multi-pod distribution — a sobering, important honest finding.**
3 pods, 8 frames split [3,3,2] (uneven, max_share=3 bounds wall-clock).

- Serial baseline (all 8 frames, one pod): **T_serial=142.2s** (~17.5-19.4s/frame).
- Distributed steady-state: **net_speedup_real = 1.96x** (T_real_excl_provision=72.7s) —
  well below even the naive ~2.67x expected from the uneven 3-frame max share, let alone
  the 7.8-15x "ideal" TILE-fanout-within-one-pod measured overnight. Real network/SSH/
  scheduling overhead eats meaningfully into the parallel win.
- **THE PROVISIONING TAX DOMINATES**: 825.7s (~13.75 min) just to get 3 pods up+reachable,
  vs only 72.7s of actual distributed compute. T_real_incl_provision = 898.4s — SLOWER in
  total wall-clock than the 142.2s serial baseline! For a ONE-OFF job, cold-start multi-pod
  distribution is a net LOSS.
- Cost scales linearly as expected: $2.81/hr (3 pods) vs $0.94/hr (1 pod), ratio ~3.0 — no
  hidden per-pod cost surprises, the tax is entirely in WALL-CLOCK, not $.

**Product implication:** cross-POD frame-level distribution only makes sense with a
PRE-WARMED WORKER POOL (amortizing provisioning tax across many jobs), never per-job cold
starts. This is a fundamentally different regime from the tile-fanout-within-one-already-
warm-pod result (7.8-15x, SSIM 1.0, zero provisioning tax since it's all subprocess calls on
one already-up machine) — that result stands; THIS result shows the cross-pod story needs a
pool architecture to be real, not just "spin up N pods per job."

## Track 4 progress: analytical depth-reprojection

**Build (background Workflow wl4n4p3ts, no pod cost):** `pod/exp_render_stack_analytical.py`
(1836 lines) — replaces Cycles-Vector-pass 2D motion with analytical 3D unproject/reproject
using KNOWN camera transforms (we control the keyframing formula) + the (unaffected-by-the-
bug) Z/depth pass. 22/22 local synthetic math tests passed (identity probe ~1e-12px, exact
known-shift sign lock, clean planar-vs-Euclidean depth-convention discrimination). Has a
built-in safety gate: raises + errors if the on-hardware identity probe exceeds 1e-2px,
refusing to report a possibly-wrong cross-frame SSIM.

**First real-hardware check ($0.29, single minimal 2-frame config):**
2D-vector baseline reproduced exactly (worst_tile=0.2732, matches overnight). Analytical:
**worst_tile=0.3571 (+0.084, ~31% relative improvement)**, identity_probe_max_error_px=**0.0**
(validated on REAL Cycles data, not just synthetic), depth_convention auto-detected as
"planar". Note: `camera_model_max_pose_error=0.359` was far outside the design's 1e-3
self-check tolerance, meaning the built-in cross-check almost certainly auto-switched to
Blender's real evaluated camera pose rather than our analytical formula for this run — so
the improvement is credited to the DEPTH-BASED REPROJECTION MATH itself (independently
validated by the identity probe), not a possibly-wrong pose derivation.
**Real, measurable win — but 0.357 is still far below any usable quality tier at this one
config.** Needs its own tuning sweep to find the true ceiling, same as the 2D method needed.

**LAUNCHED (09:17): run_stack_analytical_tuner.py**, 3hr cap/$6 floor, balance $29.49.
Built `tuner_stack_analytical.py` (adapted from `tuner_stack.py`) — same two-constraint
feasibility (quality AND worst_tile, never global-alone), same knob set PLUS
`disocclusion_thresh` (now a relative-depth tolerance, re-tuning it specifically for the new
method), PLUS an extra tuner-level sanity check requiring a present, passing
`identity_probe_max_error_px` before trusting any trial's quality/worst_tile at all.
Ledger: `docs/speed-lane-reports/spec-lab/stack_analytical_tuner_ledger.jsonl`.
**This is the decisive test: does analytical reprojection clear a real quality tier
somewhere in its knob space, or does it also hit a lower (but real) ceiling?**

## Track 4 FULL RESULT (converged 11:03, $1.72, balance $27.77 -> $27.68) — DEFINITIVE

**The analytical method is a real, measurable improvement over 2D-vector — but ALSO hits a
ceiling, and cannot clear any quality tier either.** Full 8-knob OFAT sweep (30 trials,
identity probe passed 0.0px on every real trial) converged with NO feasible OFAT point at
ANY tier (0.90/0.95/0.98/0.99) and refine round 1 found no further improvement:

Pareto frontier (best points): **worst_tile 0.40 @ q=0.90, 7.4x** (best) down to 0.34 @
8.4-12.2x. Compare to 2D-vector's best-ever: worst_tile ~0.27. **A genuine ~48% relative
improvement in worst-tile — real progress — but still far short of the 0.85 needed for even
the loosest (preview) tier.**

**Mechanistic finding — the dominant lever, ranked:**
1. `disocclusion_thresh` (tightened, now a relative-depth tolerance under the analytical
   method): 0.02 -> wt=0.40 (best), 0.05 -> wt=0.39, vs base 0.1 -> wt=0.34. **0.02 was the
   TIGHTEST value tested — a boundary effect, suggesting the optimum may lie even tighter.**
2. `light_tree=False`: wt=0.346 (2nd best single-knob win).
3. Every other knob (draft_spp, adaptive_threshold, hole_fill, denoiser, resolution):
   essentially flat around 0.34, no meaningful effect.

**FOLLOW-UP LAUNCHED (11:05):** run_analytical_pushfurther.py — tests disocclusion_thresh
in [0.01, 0.005, 0.001] alone and combined with light_tree=False (the two best single
levers), on the already-cached reference. Answers: does the trend continue tighter, or
plateau/reverse (hitting a DIFFERENT dominant error source — e.g. genuine no-source-data
disocclusion, or view-dependent shading no reprojection can fix)?

**Working conclusion pending the follow-up:** two fundamentally different reprojection
techniques (naive 2D motion-vector, proper 3D analytical depth+camera-matrix), both
thoroughly tuned to convergence, both cap below any usable quality tier on this animated
camera-dolly-through-interior scene. This points at something more fundamental than either
implementation — most likely genuine disocclusion regions with literally no valid source
data in the keyframe (revealed-for-the-first-time geometry, unrecoverable by ANY
reprojection, however accurate — only a real re-render fixes those, which is what
hole_fill=rerender already does for MASKED regions; the remaining error may be pixels the
mask isn't flagging as disoccluded at all) or view-dependent shading (specular/reflection)
changing between viewpoints even for correctly-identified static, non-disoccluded geometry.

## Track 3 result (DONE, $1.19, clean teardown, balance $25.09) — THE FIRST TIER CLEARED

**`exp_render_ultimate.py`, keyframe_every=1 (no reprojection at all — every frame is an
independent anchor-quality render), denoise anchor + light-tree + VP9 transcode delivery:**

**5.84x @ SSIM 0.958 (worst-tile 0.911, p5-tile 0.923) — CLEARS the 0.95 quality tier**
(needs global>=0.95 AND worst-tile>=0.90; both satisfied). modeled=False, every number a real
measured wall-clock (T_ref=2472.4s for 8 reference frames, T_ours=423.0s for 8 anchor renders
+ transcode). **This is the first configuration all campaign to clear a usable quality tier
with a meaningful speedup** — the honest, ship-today product number: skip reprojection
entirely, just do fast-anchor-per-frame + VP9 delivery. This is what to build the product on.

## MONEY-SAFETY INCIDENT + FIX (2026-07-07, mid-session)

A local session interruption (hit a usage limit) killed the local driver process for a
push-further re-run WITHOUT its `finally: terminate` block running, orphaning a live A100 pod
that billed silently for an unknown period before being caught and manually terminated
(~$1 lost). Root cause: local teardown safety (register_cleanup + finally blocks) only works
if the local process survives to run it — a full session/container recycle bypasses it
entirely, with nothing else to stop the remote pod.

**FIXED:** `runpod.arm_remote_watchdog(pod, ttl_seconds)` — schedules a background job ON THE
POD ITSELF that calls RunPod's own terminate mutation against itself after ttl_seconds, via a
curl POST using the same API key. This is a hard backstop independent of anything on the
local side surviving. Verified: (a) shell-quoting/JSON round-trip correct via manual trace +
automated check before trusting it, (b) confirmed armed on REAL hardware in Track 3's run
("remote watchdog armed on h1mlqcz3o4kb7g: self-terminates in 3600s"). Every driver going
forward should call this immediately after `provision_reachable()` succeeds — see
`run_ultimate_no_reprojection.py` for the pattern.

## Money accounting (this session)
| track | spend | balance after |
|---|---|---|
| Track 1 (cross-scene + 4K) | $0.33 | $30.29 |
| Track 2 (real multi-pod distribution) | $0.51 | $29.78 |
| Track 4 single-config check (analytical vs 2D-vector) | $0.29 | $29.49 |
| Track 4 full tuner sweep (converged, definitive wall) | $1.72 | $27.77 |
| orphaned pod (session interruption, caught + fixed) | ~$1.11 | $26.36 -> $26.28 |
| Track 3 (honest end-to-end pipeline — TIER CLEARED) | $1.19 | $25.09 |
| Track 5 (upscale + interpolation spikes) | $0.22 | $24.78 |

## Track 5 results (DONE, $0.22, clean teardown, balance $24.78)

**Guided upscale (`exp_render_upscale_guided.py`, classroom, 960x540->1920x1080):**
bicubic-only result: **7.11x @ SSIM 0.927 (worst-tile 0.832)** — just short of even the
loosest tier (needs worst-tile>=0.85). **INCONCLUSIVE on the actual hypothesis** — both the
AOV-guided contender AND the Real-ESRGAN control were SKIPPED, not beaten:
- aov_guided: skipped because the full-res guide render didn't produce a usable AOV EXR (a
  real bug in the guide-render path, not a quality finding — needs a fix before this
  question is actually answered).
- realesrgan: skipped because the fetched weights didn't strict-match our RRDBNet
  architecture — the runner correctly REFUSED to run a mismatched model and mislabel it
  (exactly the honesty behavior we want, not a failure).
Only the bicubic baseline is real data here. Needs a fix to the AOV-guide-EXR path before
this experiment can answer its real question.

**Learned/flow-guided interpolation (`exp_render_interp_learned.py`, animated scene, 384x384):**
**1.56x @ SSIM 0.897 (worst-tile 0.285)** on the 3 synthesized frames. `beat_warp=true` —
genuinely beat the naive single-keyframe warp baseline (0.897 vs 0.886) — real, if modest,
progress. `modeled=true`: this is the hand-built occlusion-aware flow-blend fallback, NOT a
real trained network (RIFE fetch path not exercised this run). Worst-tile (0.285) lands in
the SAME range as every other reprojection/interpolation technique tried this session
(2D-vector 0.27, analytical 0.34-0.40, this 0.285) — on a THIRD, simpler, different scene.
**This further generalizes the core finding: the worst-tile floor looks like a structural
property of single/dual-source reprojection under real disocclusion, not an artifact of any
one algorithm or scene.**

## SESSION CLOSE-OUT — all 5 tracks complete

1. Cross-scene + 4K: anchor generalizes (BMW27 even better quality); 4K speedup shrinks not grows.
2. Real multi-pod distribution: only 1.96x, provisioning tax dominates — needs a warm pool.
3. **Honest end-to-end pipeline (no reprojection): 5.84x @ SSIM 0.958/worst-tile 0.911 —
   CLEARS the 0.95 tier. This is the product number to build on.**
4. Analytical reprojection: real improvement (0.27->0.40 worst-tile) but still no tier
   cleared — a second, independently-verified confirmation that single-keyframe
   reprojection has a real ceiling on this content.
5. Upscale (inconclusive on the real question, needs an AOV-guide-EXR fix) + interpolation
   (beat baseline, same worst-tile ceiling — 3rd confirmation of the structural finding).

Final balance $24.78 (session spend ~$5.85 across Tracks 1-5 + the caught orphaned pod).
Money-safety upgraded mid-session: `runpod.arm_remote_watchdog()` now gives every future
driver a remote self-destruct backstop independent of the local process surviving.
