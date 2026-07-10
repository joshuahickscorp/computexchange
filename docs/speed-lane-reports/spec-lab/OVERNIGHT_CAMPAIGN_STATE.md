# Overnight 100x campaign — state doc

**Mandate (owner, 2026-07-06 night):** run autonomously overnight, no check-ins needed.
Keep adding new levers, iterate aggressively toward the 100x-cheaper-to-the-user goal,
even if 100x turns out unreachable — push to find the REAL ceiling. Budget is not the
constraint (~$42 balance, <$2/hr GPU, user explicitly OK'd up to ~20hrs). Stop when we hit
a genuine wall (diminishing returns) or a safety cap, whichever first. Every number stays
HONEST (see the integrity-audit fixes already applied) — a wall we can prove beats a
number we can't trust.

## Hard safety caps (apply to every phase)
- **Balance floor: $5.00** (hard stop before provisioning; existing drivers use --min-balance 6 for margin)
- **Wall-clock cap: 14 hours** from first pod-up tonight (~21:58) => stop provisioning after ~11:58 next day, regardless of remaining balance/ideas. Note to self: this is a SAFETY cap, not a target — a genuine wall found earlier ends the campaign sooner.
- **Diminishing-returns stop:** if 3 consecutive experiment PHASES (not individual trials) fail to move the Pareto frontier at ANY quality tier we track (0.90/0.95/0.98/0.99, both global and worst-tile), treat that as "wall found" and stop early.
- **One pod driver process at a time.** Never launch a new RunPod-provisioning driver while another is still holding a tracked pod. Always confirm via `runpod.gql("query { myself { pods { id } } }")` -> `[]` (note: access as `d["myself"]["pods"]`, gql() already unwraps `data`) before firing the next driver.
- Every driver: `runpod.register_cleanup()` (atexit+SIGTERM), deadline watchdog, teardown in `finally`.

## Results so far (this session, all real/honest post-integrity-fix)
- **Denoise anchor** (`exp_cycles_render_prod.py`, single frame, Classroom 1080p, real L40S):
  best near-lossless **5.27x @ SSIM 0.9776** (thr 0.02); ceiling ~0.98 global (denoiser bias floor,
  more samples do NOT clear it — 1024spp/thr0.005 only reached 0.9805 at 2.83x, dominated);
  preview tier **9.47x @ 0.9636** (naive spp32). OptiX denoiser faster/lower-quality than OIDN.
  guides on/off gave IDENTICAL SSIM (0.9787/0.9571/0.9608 to 4dp) -> Blender's OIDN already
  consumes AOVs regardless of our explicit flag; not a bug, just means no extra headroom there.
- **Transcode delivery**: VP9 5.23x @ 0.9875 (16 segments, gate 0.97) beats x264 4.35x, AV1 4.33x,
  x265 1.98x; NVENC-on-draft is a LOSS (0.59x, reference is the expensive side). Pre-integrity-fix
  numbers (transcode fix landed same session) — directionally trustworthy, re-verify if used for pricing.
- **Temporal alone**: DEAD END for lossless — 1.0x @ 1.0 at every near-lossless tier (must
  re-render every frame to hold the floor). Only useful anchored to cheap keyframes (the keystone).
- **Bounce-reduction alone**: dead end (~1.7x GI cost). g-buffer/tiles: speed-neutral-or-negative alone.
- **Keystone compound** (`exp_render_stack.py`, denoise+light-tree+temporal on ONE animated shot,
  ONE honest end-to-end ratio): IN PROGRESS tonight — this is the load-bearing joint of the 100x
  thesis (does the compute stack actually multiply, or overlap/compound-down on quality).
- **⚠️ FINDING (22:18): stack-kf4 (keyframe_every=4) → 14.34x but quality COLLAPSED: global
  SSIM 0.7189, worst-tile 0.1427. stack-kf6 → 14.37x, quality 0.7047, worst-tile IDENTICAL
  0.1427.** Per-frame breakdown (per_frame_global_ssim/per_frame_worst_tile_ssim, both trials):
  frame 1 (the FIRST reprojected frame, gap=1 from keyframe 0) ALREADY scores only 0.6626
  global / 0.1427 worst-tile — identically in both trials, because frames 1-3 are algorithmically
  the SAME computation (same keyframe 0, same gap) regardless of keyframe_every. This is NOT a
  "further gap = worse" story — it fails at the MINIMUM possible gap. mean_disoccluded_frac was
  only ~0.074 (7%), meaning the mask thinks 93% of the frame reprojected cleanly, yet the
  COMPOSITE still only scores 0.66 global — the "cleanly reprojected" 93% must itself be wrong,
  OR the mask is under-detecting real disocclusion. convergence (0.89-0.86 quality) and fan-out
  (SSIM 1.0, lossless) rungs on the SAME pod were clean, isolating the bug to the reprojection/
  mask path specifically (not the anchor render, not tiling). Ruled out: motion_blur is correctly
  enabled (use_motion_blur=True, shutter=0.01) so camera motion IS in the Vector pass by design;
  warp_gather's math (sx=xs+mv.x, sy=ys+mv.y, bilinear backward-gather) matches the ORIGINAL
  proven exp_render_temporal.py implementation. Root cause still open as of 22:45 — a live-pod
  diagnostic dump (run_diag_repro.py, minimal frames=2/keyframe_every=2 repro, ~$0.15-0.30) is
  running now to decompose: SSIM(raw warp vs true ref) vs SSIM(composite vs true ref) vs
  SSIM(naive static-keyframe-copy vs true ref, the zero-motion-compensation baseline) vs
  SSIM(frame's own anchor render vs true ref, the upper bound). If raw-warp SSIM is BELOW the
  naive-static-copy baseline, motion compensation is making things WORSE — strong evidence of a
  sign/scale/convention bug in the motion vectors or warp application, not just a masking gap.
  tuner_stack.py's sweep is HELD until this is understood (no point re-discovering the same
  wall at every keyframe_every value if it's a fixable bug).

- **DIAGNOSTIC ROUND 1 (22:45-22:57, minimal frames=2/keyframe_every=2 repro, $0.12):**
  motion_prev is EXACTLY zero everywhere (mean=std=absmax=0.0, not just small — a perfectly
  flat zero field, which rules OUT "small real motion" and points at "the Vector pass isn't
  encoding motion at all" for this config). ssim_raw_warp_vs_ref (0.578) EXACTLY equals
  ssim_naive_static_keyframe_vs_ref (0.578) — numerically proven (diag_key0.png and
  diag_warp_raw.png are byte-identical files) that with zero motion the "warp" is a pure
  identity copy of the keyframe, no reprojection happening. BUT
  ssim_own_anchor_render_vs_ref_UPPERBOUND = 0.9645 — frame 1's OWN anchor-quality render
  (whatever camera position it actually used) matches the true reference well, which
  complicates a simple "anchor pipeline camera frozen" theory (if truly frozen at frame 0's
  stale position while ref1 shows real motion, this upper-bound number would likely be lower).
  Visual inspection of diag_ref1/diag_key0/diag_anchor1 PNGs was suggestive but NOT conclusive
  (subtle framing differences could be misread by eye at this dolly magnitude, 0.05 world
  units/frame). Root cause NOT yet settled as of 23:04.

- **DIAGNOSTIC ROUND 2 (running now, ~23:04):** two more targeted, cheap tests in one pod
  session: (a) dump BOTH ref_0001 and ref_0002 (reference-vs-reference, eliminating the
  anchor pipeline as a variable) with an SSIM comparison — settles whether the REFERENCE
  pipeline itself shows real camera motion frame-to-frame; (b) a NO-RENDER bpy script that
  opens the classroom.blend, applies the exact same camera-keyframe-setup code, and prints
  cam.location + the DEPSGRAPH-EVALUATED matrix_world.translation at frames 1/2/4/8 —
  settles definitively whether Blender's own camera transform is actually changing per frame,
  independent of Cycles' Vector-pass export. If BOTH ref frames look identical AND the camera
  print shows no change across frames, the bug is in camera keyframing/depsgraph evaluation
  (e.g. missing depsgraph update, or F-curve not applied). If the camera print DOES show
  correct movement, the bug is isolated to Cycles' Vector-pass generation for camera-only
  ego-motion (may need a different motion_blur_shutter, additional Cycles setting, or a
  different approach entirely e.g. manually diffing camera matrices frame-to-frame instead
  of relying on the Vector AOV for the camera-motion component).

- **DIAGNOSTIC ROUND 2 RESULTS (23:04-23:14, $0.14) — DEFINITIVE:** (a) REF0_VS_REF1_SSIM =
  0.5779 — the TRUE reference frames (ground truth, no denoise, full 4096spp) already differ
  this much frame-to-frame. Visually the two dumped PNGs look near-identical to a human
  glance (no obvious dolly visible), yet score 0.58 SSIM — the classic signature of a SMALL
  but real camera shift that SSIM penalizes harshly via sub-pixel misalignment of fine detail
  (alphabet strip text, radiator grille lines). This is NOT "motion too large" — it's real,
  small, structurally-significant motion a WORKING warp should mostly correct (closing most
  of the gap toward the ~0.96 anchor upper-bound), not a scene-design problem.
  (b) Camera-transform check (no-render bpy script, depsgraph-evaluated matrix_world):
  cam.location and matrix_world.translation change EXACTLY as coded — frame1→frame2 delta =
  (+0.0500, +0.0000, +0.0200), matching DOLLY_PER_FRAME/RISE_PER_FRAME precisely; 6 F-curves
  present. **Camera keyframing is 100% correct — DEFINITIVELY ruled out as the bug.**
  CONCLUSION: the camera genuinely moves (small, correct amount); the reference render
  correctly reflects that motion; yet the exported Vector/motion pass reads as EXACTLY zero.
  The bug is squarely in Cycles' Vector-pass generation/export for this runner's specific
  render config — not scene design, not camera setup. Leading hypothesis: OIDN denoiser +
  guide-pass settings (denoising_input_passes='RGB_ALBEDO_NORMAL', prefilter) interfering
  with/overwriting the Vector channel in the multilayer EXR. Testing now (round 3,
  denoiser=none/guides=false, run_diag_repro3.py) — if that restores non-zero vectors, the
  fix is to read Vector BEFORE denoising touches the EXR, or render Vector in a separate
  un-denoised pass/file.

- **DIAGNOSTIC ROUND 3 RESULTS (23:17-23:27, denoiser=none/guides=false) — HYPOTHESIS
  REFUTED.** motion_prev/motion_next STILL exactly zero (mean=std=absmax=0.0) with the
  denoiser fully disabled. ssim_composite_vs_ref actually got WORSE (0.5707 vs 0.6170) —
  makes sense, since without denoising the anchor patch pixels are noisier. **The OIDN
  denoiser/guide-pass setup is DEFINITIVELY NOT the cause.** The bug is deeper: Cycles'
  Vector pass reads zero for CAMERA-ONLY ego-motion on fully-static geometry loaded from an
  external .blend, independent of denoising. The one thing that differs from the ORIGINAL
  proven exp_render_temporal.py (which worked, 0.83-0.88 SSIM): that runner built its scene
  FROM SCRATCH with independently-ANIMATED OBJECTS (rotating monkey + orbiting sphere); the
  keystone opens an EXTERNAL .blend (Classroom) with ZERO object animation — 100% of apparent
  motion is camera ego-motion on static geometry. Leading candidates now: (a) a Cycles/bpy
  camera-motion-vector timing/settings gotcha specific to a freshly-opened external scene
  (e.g. depsgraph/previous-frame-cache not warmed before the first frame_set+render); (b) an
  EXR channel-read subtlety specific to Classroom's own view-layer setup; (c) if neither
  pans out, an analytical depth+known-camera-matrix reprojection (bypass Cycles' Vector pass
  entirely — we control the camera transform exactly, so we can unproject/reproject
  ourselves using the Z/depth pass, which is unaffected by this bug).

- **DISPATCHED (23:28): fix-camera-motion-vector-bug workflow** (session task wnukp054a) —
  3 parallel hypothesis agents (Cycles settings/timing, EXR channel audit, analytical
  fallback design) followed by one implement+verify agent with full money-safe RunPod
  authority (up to 6 real-pod attempts, $10 balance floor, stops and reports honestly if it
  can't find a working fix — does not fabricate success). tuner_stack.py's sweep and all
  temporal-reprojection work stays HELD until this returns. Non-reprojection levers
  (convergence/light-tree, fan-out/distribution, transcode codec ladder) are UNAFFECTED by
  this bug and remain valid to pursue in parallel if the fix workflow runs long.

- **ROOT CAUSE FOUND + FIXED (23:51) — CONFIRMED by INDEPENDENT re-verification (00:04-00:17,
  $0.13):** Cycles EMPTIES the Vector (motion) pass whenever render motion blur is ENABLED —
  documented, intentional Cycles behavior (Blender issue T48908, "by design"; the pass is
  greyed out in the UI when motion blur is on). The original code had this EXACTLY BACKWARDS
  — it turned motion blur ON specifically believing it was REQUIRED to populate the Vector
  pass. Fix: `scene.render.use_motion_blur = False` in exp_render_stack.py:600 (the workflow's
  own final report was garbled/incomplete, so this was independently re-verified with the
  SAME diagnostic script that originally found the zero-vector bug, not just trusted).
  **Independent verification result:** motion_prev is now genuinely non-zero (mean=2.16,
  std=13.9, absmax=108px at 1920x1080 — consistent with the workflow's own reported
  480x270-scale numbers once rescaled by the 4x resolution ratio, a strong cross-check).
  Quality breakdown: naive-static-copy 0.578 -> raw-warp 0.630 -> composite 0.692 -> anchor
  upper-bound 0.965. **Real, meaningful, correctly-proportioned improvement — the fix
  works** — but composite quality (0.69) is still well below any usable tier. This is now a
  TUNING problem, not a correctness bug: `disocc_frac` jumped 0.078->0.163 (real cues firing
  for the first time) and disocclusion_thresh=0.1 (rt_thresh_px=220, ~11% of frame width) was
  never tuned against REAL motion data before (it's been operating on all-zero vectors this
  entire session) — exactly what `tuner_stack.py` (already built, held all night) exists to
  fix. motion_next is still ~zero (doesn't affect the core backward-warp, only the fwd/bwd
  consistency mask cue — a secondary refinement, not blocking).
  **NEXT: fire run_stack_tuner.py now** — the correctness blocker is cleared, tuning can proceed.

- **LAUNCHED (00:18): run_stack_tuner.py**, 4hr cap / $6 floor, on the FIXED exp_render_stack.py
  (motion_blur=False). Sweeps draft_spp/adaptive_threshold/keyframe_every/hole_fill/
  resolution/light_tree/denoiser, feasibility requires BOTH quality>=q_floor AND
  worst_tile_ssim>=q_floor-0.05 at each tier (0.90/0.95/0.98/0.99), coordinate-ascent per
  tier, refine-until-budget. Ledger: docs/speed-lane-reports/spec-lab/stack_tuner_ledger.jsonl.
  Balance at launch: $40.10. Persistent monitor bpi7z5dg2 armed for the whole run.

- **TUNER RUN 1 COMPLETE (04:21) — RESULT INVALIDATED BY A SEPARATE BUG, DO NOT TRUST YET.**
  Full 4hr budget ($5.63 spent) produced only 5 trials — the ENTIRE budget was consumed
  sweeping just ONE knob (keyframe_every: 2,3,4,6,8); draft_spp/adaptive_threshold/hole_fill/
  resolution/light_tree/denoiser were NEVER TESTED. Pareto frontier: q0.67/wt0.14:18.9x,
  q0.76/wt0.17:14.4x, q0.87/wt0.27:11.2x — no quality tier (0.90/0.95/0.98/0.99) cleared.
  **ROOT CAUSE OF THE WASTED BUDGET: exp_render_stack.py has NO reference-render caching at
  all** — grep confirms PASS 1 unconditionally re-renders all 8 reference frames every single
  invocation (~15-40min), which is why every one of the 5 trials took ~46-51 min uniformly
  regardless of keyframe_every (tuner_stack.py's design comment WRONGLY assumed the runner
  cached its reference — it never did). This means tonight's "no tier cleared" conclusion is
  PREMATURE — it's based on a budget-starved sweep of 1 of 7 knobs, not a thorough search.
  **FIXED (04:29):** added real reference caching to exp_render_stack.py, keyed by
  (scene_key, res, ref_spp, bounces, frames, seed, cam_motion), stored under
  {_CACHE_ROOT}/ref_cache/{sha1-hash}/ as per-frame .npy + a manifest.json (T_ref/devices).
  A cache hit reports the REAL historical per-frame render times (never fabricated/zeroed),
  new `ref_cache_hit` metric added to the emitted JSON for transparency. Compiles clean.
  **VERIFYING NOW (04:29):** run_cache_verify.py — same config run twice on one pod; trial 2
  should be dramatically faster with matching T_ref_s (proves real reuse, not a shortcut).
  Once confirmed, RE-LAUNCH run_stack_tuner.py for a proper, thorough multi-knob sweep — the
  "ceiling" conclusion above must be re-earned with real search coverage before it's trusted.

- **CACHE FIX CONFIRMED (04:39, $0.14):** same 2-frame config run twice on one pod. Trial 1
  (cache miss): wall=318.6s, T_ref_s=227.09. Trial 2 (cache hit): wall=59.9s — **5.3x
  faster** — with T_ref_s=227.09 (EXACT match) and quality=0.8632 (EXACT match), proving the
  cache returns the true historical reference honestly, not a shortcut/fabrication.
  Scaled to the full 8-frame tuner config: first trial still costs ~46min (uncached), but
  every subsequent trial should now take a few minutes instead of ~46min — roughly a 10x
  improvement in trials-per-budget. **RE-LAUNCHED (04:40): run_stack_tuner.py, 4hr cap/$6
  floor, balance $34.26.** The 9 existing ledger entries (5 real trials + 4 tier_best/pareto
  events from the pre-fix run) are honestly-measured and get reused by the tuner's cache
  (skips re-testing identical configs) — no need to discard them. Persistent monitor
  bv44fi75t armed. THIS is the run whose Pareto frontier should actually be trusted as
  covering the real search space (draft_spp/adaptive_threshold/keyframe_every/hole_fill/
  resolution/light_tree/denoiser), not just one knob.

## Tonight's phase queue (execute in order; each phase = one pod session; re-plan between phases)
1. **[RUNNING now]** Keystone ladder v2 (`run_stack_ladder.py`): stack-kf4, stack-kf6,
   conv-lighttree-on/off, faninout-t8/t16. Log: scratchpad/stack_ladder2.log. Monitor: bklvjc88b (persistent).
2. **Stack coordinate-ascent tuner** — sweep the keystone's own knobs (draft_spp, adaptive_threshold,
   keyframe_every, hole_fill, resolution, bounces, light_tree) per quality tier, reusing the cached
   animated reference across trials, objective: maximize net_speedup s.t. quality>=q_floor AND
   worst_tile_ssim>=tile_floor. This finds the REAL Pareto frontier of the compound, not just hand-picked points.
3. **Cross-scene validation** — re-run the anchor + keystone's best config on BMW27 (glossy/specular
   stress case) to make sure results aren't Classroom-specific overfit. Cheap (scene param already supported).
4. **Real multi-pod distribution** — NOT the ideal fan-out upper bound (already measured); actually
   provision 2-4 pods concurrently, split real work (frames or tiles) across them, measure REAL
   wall-clock including provisioning + network scatter/gather. Tests whether the "ideal" ceiling survives contact.
5. **Learned interpolation + guided upscale real runs** (`exp_render_interp_learned.py`,
   `exp_render_upscale_guided.py`) — both already built, not yet run on real GPU.
6. **The ultimate combined pipeline** — denoise+light-tree+temporal+ (upscale?) + transcode, ALL
   composed end to end on one animated shot, ONE honest ratio vs the true full pipeline. New runner,
   build if not already queued.
7. **Denoiser-bias-floor mitigation spikes** (only if phases above show it's the binding constraint) —
   e.g. multi-frame/temporal-stabilized denoise, partial raw/denoised blend in high-freq regions.
8. **4K confirmation arm** on whichever config is the best near-lossless point, if time/budget remain.

New lever ideas discovered mid-run get APPENDED to this queue, not silently substituted — log the
addition + why here before running it.

## Tool inventory built tonight (all py_compile-clean, independently verified)
- `scripts/spec-lab/tuner_stack.py` + `run_stack_tuner.py` — coordinate-ascent tuner for the
  KEYSTONE (`exp_render_stack.py`). Feasibility requires BOTH quality>=q_floor AND
  worst_tile_ssim>=tile_floor(q_floor)=q_floor-0.05 — a good global average can never hide a
  collapsed tile (exactly tonight's kf=4 failure mode). Base config starts at keyframe_every=2
  (tight, given the collapse). Sweeps keyframe_every/draft_spp/adaptive_threshold/hole_fill/
  light_tree/denoiser, with resolution swept LAST (the only knob that invalidates the cached
  reference). Usage: `RUNPOD_API_KEY=... python3 scripts/spec-lab/run_stack_tuner.py
  --max-minutes 240 --min-balance 6`. **THIS IS THE NEXT POD SESSION** once the current ladder tears down.
- `scripts/spec-lab/run_multipod_distribution.py` + `pod/exp_render_frame_subset.py` — real
  (not ideal) multi-pod distribution. Sequential provisioning by default (money-safety: the
  tracked-pods JSON has no lock, concurrent writes risk dropping a pod from the anti-orphan
  ledger). Reports T_real_including_provisioning AND excluding (fair steady-state number) +
  provisioning tax + $/hr N-pods-vs-1 separately. NOT yet run on real hardware.
- `pod/exp_render_ultimate.py` — full combined pipeline (denoise+light-tree+temporal-on-
  keyframes+speculative-transcode) on one animated shot, ONE honest T_ref/T_ours ratio,
  end-to-end SSIM (global+worst-tile+p5) on the FINAL decoded delivered video vs the true
  reference video. Reuses exp_render_stack's fixed-overhead-aware crop-cost model and
  compute_ssim_global_and_tiles. NOT yet run on real hardware.
(Note: the build workflow's stack-tuner agent hit a StructuredOutput retry-cap failure on its
FINAL summary call, but had already written both files correctly before that — verified independently
by grep/compile rather than trusting its self-report. Lesson: always independently verify when a
workflow leg errors, don't assume "failed" == "no files written".)

## Honesty rules (carry from the integrity audit — do not regress)
- Every TIME = real whole-subprocess wall-clock, same box, denoise/encode INCLUDED, nothing excluded.
- Every SSIM = real skimage on real pixels; report GLOBAL + worst-tile + p5-tile, not global alone.
- `modeled:false` only when every emitted number is a real measurement; say exactly which step is
  modeled otherwise (e.g. the fixed-overhead-aware crop-cost model in the keystone's rerender path).
- Compound speedup = ONE measured end-to-end ratio, NEVER the product of stage-reported speedups.
- No silent fallback that mislabels a substitution as success; error paths emit `{"error":...}`, never a fabricated number.
- If a lever turns out inert or a dead end, SAY SO plainly and move on — don't keep re-testing a
  refuted idea hoping for a different answer.

## Resume instructions (if this session compacts/restarts overnight)
Read this file + `docs/speed-lane-reports/spec-lab/stack_ladder_ledger.jsonl` (append-only results)
+ check `ps aux | grep run_` for any live driver + `python3 -c` a quick
`runpod.gql("query { myself { pods { id } clientBalance } }")` for ground truth. Continue the phase
queue above from wherever it left off. Money-safety params (balance floor $5, wall-clock cap ~11:58
next day, one-driver-at-a-time) are non-negotiable regardless of which context is running this.
