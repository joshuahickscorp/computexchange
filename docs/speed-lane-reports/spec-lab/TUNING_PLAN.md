# TUNING PLAN — pushing the two proven winners to their speed/quality frontier

*The two things that actually worked (measured, real GPU, honest — see `RUN_2026-07-06.md`):*

| winner | runner | measured | what tuning is for |
|---|---|---|---|
| **TEMPORAL ANIMATION REUSE** — render one keyframe, reproject the rest by motion vectors, re-render only the disocclusions | `pod/exp_render_temporal.py` | **3.7×–12.95× at SSIM ~0.84** (98% of pixels reprojected) | the speedup is huge but 0.84 is a *visible* loss. **Job: pull quality up to near-lossless (0.98+) while keeping a strong multiple.** |
| **SPECULATIVE TRANSCODE** — cheap-preset draft encode + selective slow re-encode of only the segments that fail a quality gate | `pod/exp_video_transcode.py` | **2.18× at SSIM 0.98** (83% of segments accepted) | already near-lossless. **Job: find every extra tenth of a multiple that keeps 0.98, and map the whole Pareto curve.** |

This document is the **experiment program** the autonomous tuner (`scripts/spec-lab/tuner.py`)
runs against those two runners. It is deliberately exhaustive: the owner asked to *"try
absolutely everything — first individually, then combined."* Every experiment below names a
**knob** (a real, optional, backward-compatible runner parameter), a **hypothesis** (what we
expect it to do to speed and to quality, and *why*), and a **decision metric** (the number
that tells us whether the hypothesis held). Nothing here is aspirational hand-waving — each
knob is wired into a runner that emits `net_speedup` and `quality` from real render/encode
wall-times and real SSIM.

---

## 0. The objective, stated precisely

We are not maximizing speedup. We are maximizing speedup **subject to a quality floor**, and we
do it **separately at four floors** so the product can offer tiers:

```
  for each q_floor in {0.90, 0.95, 0.98, 0.99}:
      maximize   net_speedup(config)
      subject to quality(config) >= q_floor
```

- `net_speedup` (maximize): real full-render/encode wall-time ÷ our-pipeline wall-time.
- `quality` (the constraint): mean SSIM of the delivered frames/segments vs the ground-truth
  full render/slow encode, `0..1`.
- A config whose `quality < q_floor`, or that errors, scores `-inf` at that tier — it is simply
  not a candidate there. The **same run** can be the winner at `0.90` and disqualified at `0.99`.

Why tiers and not one number: the two products are naturally tiered. A **preview / draft** tier
(`0.90–0.95`) sells a fast, cheap look; a **final / delivery** tier (`0.98–0.99`) sells
near-lossless. The owner's bar — "pretty much lossless" — is the `0.98` and `0.99` tiers; those
are where the hard, valuable tuning lives. The `0.90/0.95` tiers mostly fall out for free (the
untuned temporal runner already clears them at 10×+).

### The Pareto framing

Every trial is a point `(quality, net_speedup)`. The **Pareto frontier** is the set of points
that are not dominated — no other measured point is *both* higher quality *and* faster. The
frontier **is the product**: it is the menu of "the fastest we can go at each fidelity we can
guarantee." The tiered table (best config at each `q_floor`) is a discretization of that frontier
at four price points. The tuner emits both:

- `event:"tier_best"` — the winning config + speedup at each `q_floor`.
- `event:"pareto"` — the full non-dominated `(quality, speedup)` curve.

A knob "wins" if it moves the frontier **out** (up-and/or-right) at a tier we care about.
A knob is "neutral" if its points sit *on* the frontier the incumbents already define. A knob
is a "dead end" if all its points sit *under* the incumbent frontier.

---

## 1. TEMPORAL ANIMATION REUSE — the knobs, swept individually

Runner: `pod/exp_render_temporal.py`. It renders a real animated Blender/Cycles scene (spinning
monkey + orbiting metal sphere + panning camera — genuine screen-space motion and silhouette
disocclusion), reads Cycles' real motion-vector (`Vector`) and depth (`Z`) passes out of a
multilayer EXR, warps the keyframe with our own numpy code, detects disocclusions with our own
four-cue mask, composites, and scores SSIM against the true per-frame render. All render **times**
and all **SSIM** are real; the single modeled step is charging a re-rendered patch as
`disoccluded-area-fraction × full-frame time` (Cycles cost is ~linear in rendered pixel count, so
this is conservative). Every knob below is optional with a default that reproduces the original
measured behavior.

### The core tension

Temporal reuse trades quality for speed on exactly one axis: **how much of each frame is
reconstructed by cheap reprojection vs. re-rendered.** More reprojection → more speedup, more
artifacts. Every knob is a different way to shift, sharpen, or sidestep that tradeoff. The prize
knob is the one that shifts the *whole curve* — that lets us keep near-100% reprojection **and**
near-lossless quality by spending re-renders only where they actually buy fidelity.

### T1 — Keyframe interval (`keyframe_every`) — the baseline speed dial

- **Knob:** `keyframe_every ∈ {2,3,4,6,8,12,16,24}`. Render a fresh full keyframe every K frames;
  reproject the K−1 between.
- **Hypothesis:** speedup rises monotonically with K (fewer expensive keyframes, cost amortized
  over more cheap frames) and quality falls monotonically with K (reprojection error accumulates
  as the warped frame drifts further from its source). This is the dominant speed/quality dial and
  the one OFAT sweeps first and densest. The measured 3.7×@K=8 → 12.95×@K=16 is exactly this curve.
- **Decision metric:** the `net_speedup`-vs-`quality` curve as K varies. We read off the **largest
  K whose quality still clears each `q_floor`** — that K is the tier's baseline before any smarter
  knob. Expectation: at `0.98` the fixed-cadence K is small (2–4), i.e. fixed keyframing alone
  cannot be both fast and near-lossless. That failure is what motivates T2.

### T2 — ADAPTIVE keyframing (`adaptive_keyframe`, `quality_floor`) — THE near-lossless lever

- **Knob:** `adaptive_keyframe ∈ {False, True}`; `quality_floor ∈ {0.90,0.95,0.97,0.98,0.99}`.
  When on, the fixed cadence is **abandoned**. We reproject each frame, run a **cheap, render-free
  quality proxy** (disoccluded-area fraction + a reprojection-sharpness/edge probe against the
  keyframe), and the instant the predicted quality would fall below `quality_floor` we **promote
  that frame to a fresh full keyframe** instead of reprojecting it.
- **Hypothesis:** this is the single most important experiment in the whole plan. Fixed cadence
  wastes keyframes on easy stretches (slow pans reproject perfectly for many frames) and starves
  hard stretches (a fast rotation disoccludes badly after 2 frames). Adaptive keyframing spends
  keyframes **exactly where reprojection is about to fail and nowhere else** — so it *bounds*
  quality to `~quality_floor` while letting the keyframe interval (hence speedup) **float up on
  easy content**. The prediction: at `quality_floor=0.98` we hold `quality ≥ 0.98` with a mean
  keyframe interval well above the fixed-K that fixed cadence needed for the same floor — i.e. a
  strictly better point on the frontier. This is the knob that turns "10× at 0.84" into "near-
  lossless at a still-strong multiple."
- **Decision metric:** at each `q_floor`, compare adaptive vs. the best fixed-K (T1) at the **same
  measured quality**. Adaptive wins iff it delivers higher `net_speedup` at ≥ that quality. We also
  emit `mean_keyframe_interval` (frames ÷ keyframes actually spent) and `adaptive_proxy_vs_true_ssim`
  — the proxy is only useful if it *tracks* the true SSIM; that calibration pair tells us whether
  the controller is trustworthy or just lucky. **A miscalibrated proxy that lets quality slip below
  the floor invalidates the tier**, so we check it explicitly.

### T3 — Disocclusion threshold (`disocclusion_thresh`) — how aggressively to re-render

- **Knob:** `disocclusion_thresh ∈ {0.02,0.04,0.07,0.10,0.15,0.22,0.30}` (round-trip MV error, as a
  fraction of the frame diagonal, above which a pixel is flagged for re-render).
- **Hypothesis:** a *low* threshold flags more pixels as untrustworthy → larger re-rendered patches
  → higher quality, lower speedup. A *high* threshold trusts the warp more → smaller patches →
  faster, more artifacts. It is the fine-grain complement to `keyframe_every`: keyframes fix *when*
  we refresh, the threshold fixes *how much of each frame* we refresh. The sweet spot is the
  threshold that re-renders **only** the genuinely broken silhouette/disocclusion bands and trusts
  the rest.
- **Decision metric:** speedup-vs-quality as the threshold sweeps, at a fixed keyframe schedule.
  We want the **elbow** — the highest threshold before quality starts dropping steeply. We also
  read the per-cue coverage (`oob / divergence / depth / consistency`) to see *which* cue is driving
  the patches; if one cue dominates, that's a hint for T5.

### T4 — Reprojection method (`reproject_method`) — how we warp

- **Knob:** `reproject_method ∈ {backward, forward_splat, bidirectional}`.
  - `backward` (default, gather): for each destination pixel, sample the keyframe at
    `p + prev_motion[p]`. No scatter holes, trivially parallel, but smears across silhouettes.
  - `forward_splat`: scatter each keyframe pixel forward by its own next-motion into a z-buffered
    accumulation (nearest depth wins). Resolves fold-over correctly, but leaves fade-in holes.
  - `bidirectional`: run the backward gather (primary) and fill its holes from the forward splat
    where the splat has coverage; union the validity. Uses both cues from a single keyframe.
- **Hypothesis:** backward maximizes coverage (fewest holes → least re-render → fastest) but is the
  softest at moving silhouettes; forward is crisper at fold-over but hole-ier (more re-render →
  slower); bidirectional should give the **best quality at fixed speed** because it takes each
  method where it is strong. The interesting question is whether bidirectional's small extra warp
  cost (two numpy warps, still ~free vs a render) buys enough quality to *raise the achievable
  keyframe interval* — i.e. it may be a speed win indirectly, by making longer reprojection viable.
- **Decision metric:** for each method, the best `net_speedup` at each `q_floor` (holding the other
  knobs at their tier-best). Method A beats method B iff it is faster at ≥ the same quality. Watch
  `mean_disoccluded_frac`: the better warp should show a *lower* disocclusion fraction at equal
  quality (it trusts more pixels correctly).

### T5 — Hole filling (`hole_fill`) — what to do with the disoccluded pixels

- **Knob:** `hole_fill ∈ {rerender, nearest, inpaint}`.
  - `rerender` (default): drop the true full-render pixels into the patches; **charge the patch
    render cost** (`disocc_frac × full_frame_time`). Highest quality, real cost.
  - `nearest`: push-pull nearest-valid-neighbor flood — fill holes from surrounding reprojected
    pixels; **no re-render, patch cost = 0.**
  - `inpaint`: iterative Laplacian/diffusion fill (or cv2 inpaint) of the holes; **no re-render,
    patch cost = 0.**
- **Hypothesis:** the fill method is the *third* speed/quality axis, orthogonal to when/how-much.
  `nearest` and `inpaint` make disocclusions **free** (no render at all), so they should massively
  raise speedup — at the cost of quality wherever a hole reveals genuinely new content the fill
  can't invent. Expectation: fills are a big win for the **preview tier** (0.90/0.95, where invented
  detail is acceptable) and *lose* at the near-lossless tier (0.98/0.99, where a hallucinated
  silhouette is a visible error and `rerender` is mandatory). The load-bearing question: is there a
  *hybrid* regime where holes are small enough that `inpaint` holds 0.98? That would be a genuine
  free lunch.
- **Decision metric:** at each `q_floor`, the fastest fill that still clears the floor. Prediction:
  `rerender` owns 0.98/0.99; a fill owns 0.90/0.95. If `inpaint` unexpectedly clears 0.98 on this
  scene, flag it — but treat it as scene-specific until confirmed on varied content (§6, risks).

### T6 — Per-region vs per-frame re-rendering (implicit in the mask + threshold)

- **Knob:** the disocclusion **mask granularity** — controlled today by `disocclusion_thresh` (T3)
  plus the mask dilation. The runner already re-renders **per region** (only the masked pixels),
  never the whole frame. The experiment is: how *tight* should the re-rendered region be?
- **Hypothesis:** tighter regions (less dilation, higher threshold) = less re-render = faster, but
  risk leaving a 1-pixel seam un-refreshed → a visible edge → quality drop. Looser regions (more
  dilation) seal seams but re-render background that reprojected fine. There is an optimal dilation
  that seals every seam with minimal extra area.
- **Decision metric:** speedup-vs-quality as effective region size varies (via threshold). The win
  condition is the smallest re-rendered area fraction (`mean_disoccluded_frac`) that still clears
  the floor — that is literally the minimum render work for that fidelity. *Candidate follow-on
  knob (not yet a runner param): expose the dilation iteration count directly so the tuner can sweep
  seam-seal width independently of the detection threshold.*

### T7 — spp for keyframe vs. patch (`spp`, and future `keyframe_spp`/`patch_spp`)

- **Knob today:** `spp ∈ {16,25,50,100,200}` — samples per pixel for every render.
- **Hypothesis:** spp is a per-render quality/cost dial. But keyframes and patches have *different*
  quality needs: the keyframe is reused for many frames, so its noise propagates everywhere →
  it deserves **high** spp. A patch is small, often in motion, composited under a mask → it can
  tolerate **lower** spp. So the ideal is `keyframe_spp` high, `patch_spp` low: a clean source
  reused widely + cheap corrections. A single `spp` for both is a compromise; splitting them should
  strictly dominate.
- **Decision metric:** the `spp` sweep gives the baseline (higher spp → higher quality → lower
  speedup, roughly linear cost). Then the split experiment: does `keyframe_spp=100, patch_spp=25`
  beat a uniform `spp=50` at equal quality? *Runner note:* `keyframe_spp`/`patch_spp` are the next
  knobs to add (documented here as designed; the uniform `spp` sweep runs today). The two-pass cost
  model makes the split clean: charge the keyframe at `keyframe_spp` and patches at
  `patch_spp/spp × area`.

### T8 — Resolution (`resolution`)

- **Knob:** `resolution ∈ {384, 512}` (square side, px).
- **Hypothesis:** two effects, opposite signs. (a) Higher res = quadratically more render cost per
  frame — but that inflates *both* the baseline and our pipeline, so `net_speedup` is roughly
  scale-invariant (a ratio). (b) Higher res gives **denser, more accurate motion vectors and depth
  gradients** → sharper disocclusion detection → the warp can be trusted on more pixels → *higher*
  quality at equal keyframe interval. So the real hypothesis: resolution mainly moves **quality**
  (via MV/mask fidelity), only weakly moves the speedup *ratio*. Also: higher res is closer to the
  production regime where temporal reuse actually pays (RUN_2026-07-06's load-bearing finding — the
  strategy only wins on genuinely slow renders).
- **Decision metric:** at fixed keyframe schedule, does 512 clear a higher `q_floor` than 384 at
  comparable `net_speedup`? If yes, resolution is a *quality* lever (use it to reach 0.99); if the
  speedup ratio also moves materially, note the interaction. Bounded to 512 to stay inside the
  ~15-minute GPU budget; the plan flags 1080p/4K as the real-regime follow-up (§6).

### T9 — Motion-vector precision (`mv_precision`) — the distributed-cost lever

- **Knob:** `mv_precision ∈ {full, half, int}` — quantize the motion field to full float, 0.5px, or
  whole px before warping.
- **Hypothesis:** in the **distributed** product (draft on the fleet, verify on a GPU) the motion
  vectors are *transmitted*; coarser MVs are cheaper to move. Quantizing MVs should cost quality
  (sub-pixel warp accuracy is lost → softer reprojection, slightly worse silhouettes) roughly in
  proportion to the quantization step, and should cost *nothing* locally (it's cheaper, not more
  expensive, to compute). So this knob measures **how much fidelity we pay to make MVs cheap to
  ship.** Expectation: `half` is nearly free in quality (sub-half-pixel error is below SSIM's
  sensitivity here), `int` starts to visibly soften — making `half` the sweet spot for the
  distributed variant.
- **Decision metric:** the quality delta `full → half → int` at fixed everything-else. If `half`
  holds quality within noise, the distributed variant can ship half-precision MVs for free. If not,
  full precision is required and MV bandwidth is a real distributed-cost line item.

### T10 — Error feedback / residual correction (`error_feedback`)

- **Knob:** `error_feedback ∈ {False, True}`. When on, accumulate a render-free residual across the
  keyframe interval — the keyframe's own warp self-consistency error (warp it by its next-field then
  back by its prev-field; the deviation from identity is where *this warp family* smears detail) —
  and add a fraction back (growing with interval age, capped) on the accepted region.
- **Hypothesis:** reprojection error is not random; it **drifts systematically** as a frame gets
  further from its keyframe (the same regions soften the same way each step). If we can predict that
  drift render-free and correct it, we can hold quality across a **longer** interval — i.e. error
  feedback should let `keyframe_every` (or the adaptive interval) grow at fixed quality, which is a
  speed win with no extra render. **Honesty constraint:** the correction must use only the
  keyframe's real pixels (a reprojected frame has no ground truth in production); the runner enforces
  this — it never peeks at the true frame to build the correction.
- **Decision metric:** with feedback on, does the **max interval at a fixed `q_floor` increase** vs
  feedback off? If yes, it's a real speed lever. If quality is unchanged or worse, feedback is
  overfitting noise — drop it. This is the subtlest knob; treat a small win skeptically until it
  reproduces across seeds and content.

### T11 — Animation length (`frames`)

- **Knob:** `frames ∈ {8,12,16,24}`.
- **Hypothesis:** more frames **amortize** the fixed keyframe cost over more reprojected frames, so
  `net_speedup` should rise with length at a fixed keyframe interval (asymptoting toward
  `frames/keyframes`). It is not a quality knob per se (each frame is scored independently) but it is
  the honest way to report the *steady-state* speedup rather than a short-clip artifact where the
  first mandatory keyframe dominates.
- **Decision metric:** speedup vs. `frames` at fixed schedule — confirm it rises and asymptotes;
  report the asymptotic (long-clip) speedup as the headline, not the short-clip one. Guards against
  overclaiming on tiny clips.

---

## 2. SPECULATIVE TRANSCODE — the knobs, swept individually

Runner: `pod/exp_video_transcode.py`. It fetches a real sample clip (Big Buck Bunny / Jellyfish /
etc., synthetic complex clip only as a labeled last resort), does a **real cheap draft encode** and
a **real expensive reference encode** (both wall-timed), decodes both, computes **per-segment SSIM**
of draft vs reference, accepts a segment if SSIM ≥ gate (keep the cheap encode) else re-encodes it
at the slow preset. `net_speedup = ref_encode_time / (draft_time + rejected×per_segment_slow_time)`;
`quality = mean SSIM of delivered segments` (accepted → its draft SSIM, rejected → 1.0). Every time
is real ffmpeg wall-clock; every SSIM is real. Every knob is optional with a safe default.

### The core tension

Speculative transcode wins when the **cheap draft is good enough on most of the clip** so we rarely
pay the slow re-encode. The knobs shift three things: how cheap the draft is (bigger win when it
holds), how the reference is defined (the quality target), and how finely/smartly we localize the
segments that fail (so a re-encode is small and targeted, not a whole scene).

### X1 — Acceptance gate (`gate`) — the accept/reject dial

- **Knob:** `gate ∈ {0.90,0.93,0.95,0.97,0.98,0.99}` — the per-segment SSIM threshold to accept the
  cheap draft.
- **Hypothesis:** a *low* gate accepts more cheap segments → fewer slow re-encodes → faster, but the
  delivered quality is only as good as the accepted drafts (lower). A *high* gate re-encodes more →
  slower, higher quality. This is the primary speed/quality dial and OFAT sweeps it first. The
  measured 2.18×@0.98 sits at a fairly strict gate; loosening it should trade toward speed.
- **Decision metric:** `net_speedup` vs `quality` as the gate sweeps. Read the fastest gate that
  clears each `q_floor`. Because delivered rejected-segments are scored 1.0 (they got the reference
  encode), the *delivered* quality can exceed the gate — so the effective constraint is subtler than
  "gate = quality"; the sweep measures the real relationship.

### X2 — Draft preset ladder (`draft_preset`) — how cheap the speculation is

- **Knob:** `draft_preset ∈ {ultrafast, superfast, veryfast, faster, fast}`.
- **Hypothesis:** a faster draft preset makes the *speculation itself* cheaper (smaller
  `draft_encode_s`), directly raising the ceiling on speedup — **but** a cruder draft fails the gate
  on more segments (more re-encodes), which *lowers* speedup and can lower quality. So there is a
  non-monotone sweet spot: too cheap a draft is a false economy (you re-encode everything anyway);
  too careful a draft leaves speedup on the table. The optimal draft is *the crudest preset whose
  output still passes the gate on most segments.*
- **Decision metric:** for each draft preset, `net_speedup` at the tier's gate. The winner is the
  preset maximizing accepted-fraction × cheapness. Watch `accept_rate` — the preset that tanks
  accept_rate is too crude; the preset with high accept_rate but slow draft is too careful.

### X3 — Reference preset (`ref_preset`) — the quality target definition

- **Knob:** `ref_preset ∈ {medium, slow, slower, veryslow}`.
- **Hypothesis:** the reference *defines the deliverable* and thus the SSIM comparison. A slower,
  higher-effort reference is a **harder** target — the same draft scores *lower* SSIM against it (so
  more rejects, lower speedup) but the delivered product is genuinely better. This knob is really
  "how good is the final tier?" more than a speed knob. Crucially: `net_speedup` is defined as
  ref-time ÷ spec-cost, so a slower reference *inflates the numerator* — a `veryslow` reference makes
  speculation look better *because the thing you're avoiding is more expensive.* That is the honest
  and important framing: speculative transcode's value **grows** with how expensive the final encode
  is.
- **Decision metric:** at each `ref_preset`, the speedup at fixed gate. Expectation: speedup rises
  with reference effort (bigger thing avoided) even as accept_rate may fall. Report speedup *per
  reference tier* — "2.2× vs slow, N× vs veryslow" — because the veryslow number is the one that
  matters for the "render something you can't afford to" pitch.

### X4 — CRF pairs (`draft_crf`, `ref_crf`) — the quality/size operating points

- **Knob:** `draft_crf ∈ {23,26,28,30,32}`, `ref_crf ∈ {16,18,20,22}`.
- **Hypothesis:** CRF sets the rate/quality operating point independently of preset (preset = effort/
  speed, CRF = target quality). A higher `draft_crf` makes a smaller, cheaper draft that fails the
  gate more often; a lower `ref_crf` makes a better, larger reference that's a harder target. The two
  interact: the gate compares draft-vs-reference, so widening the CRF gap (`draft_crf` high,
  `ref_crf` low) *guarantees* more rejects. The sweet spot is the CRF pair where the draft is small
  but still visually matches the reference on easy content.
- **Decision metric:** the 2-D `(draft_crf, ref_crf)` sweep's Pareto contribution. We expect a ridge:
  along it, draft and reference CRF move together (small gap) for high accept_rate + speed; off it,
  quality or speed collapses. Coordinate ascent should find the ridge.

### X5 — Codec (`codec`) — x264 / x265 / AV1 / VP9

- **Knob:** `codec ∈ {libx264, libx265, libaom-av1, libvpx-vp9}` (falls back to libx264 with a note
  if the encoder isn't installed on the pod).
- **Hypothesis:** newer codecs (x265, AV1) get **more quality per bit** but are **much slower to
  encode** — which changes the speculation economics on *both* sides. AV1's slow reference is a huge
  thing to avoid (big potential speedup numerator), but its slow *draft* is expensive too (smaller
  speedup ceiling). x264 is fast both ways (modest but reliable speedup). The hypothesis: speculative
  transcode's **relative** win is *largest on the slowest codec* (AV1) because that's where a full
  encode hurts most — if the draft/verify overhead is affordable there. This is the knob most likely
  to reveal a new, bigger multiple than the x264-only 2.18×.
- **Decision metric:** per codec, `net_speedup` at fixed gate and the delivered quality. Compare the
  *relative* speedup across codecs; flag any codec where speculation gives a materially larger
  multiple than x264. Record encoder availability (some pods lack libaom/libvpx) so a fallback isn't
  mistaken for a real x264 result.

### X6 — Segment granularity (`segments`) — how finely we localize failures

- **Knob:** `segments ∈ {4,6,8,12,16,24}`.
- **Hypothesis:** finer segmentation localizes a re-encode to a *smaller* slice, so a single hard
  moment costs less slow-encode time → higher speedup — up to a point. Too fine and (a) per-segment
  overhead and boundary effects grow, (b) SSIM on a tiny slice gets noisy → spurious rejects. Too
  coarse and one bad frame condemns a long segment to a slow re-encode. There's an optimum matched to
  how *clustered* the hard content is.
- **Decision metric:** `net_speedup` vs `segments` at fixed gate. Find the granularity that minimizes
  total re-encoded duration (rejected-fraction × clip) — that's the least slow-encode work. Interacts
  strongly with X7 (scene-aware): fixed segments cut *across* scene boundaries, wasting re-encode on
  the easy half of a segment straddling a cut.

### X7 — Scene-aware segmentation (`scene_aware`, `scene_threshold`) — cut on real boundaries

- **Knob:** `scene_aware ∈ {False, True}`; `scene_threshold ∈ {0.20,0.30,0.40}` (ffmpeg scene-cut
  sensitivity). When on, segment boundaries are placed at **detected scene cuts** instead of a uniform
  grid.
- **Hypothesis:** hard content is not uniformly distributed — it clusters at **cuts and high-motion
  scenes.** Fixed segmentation smears a cut across a segment boundary, so the segment containing a cut
  fails the gate and re-encodes its *easy* frames too. Scene-aware segmentation aligns segments to
  content, so a re-encode covers **exactly** the hard scene and nothing else → the same quality for
  less slow-encode → higher speedup. This should be a clean win at every tier, the transcode analog of
  temporal's adaptive keyframing (spend effort where the content actually needs it).
- **Decision metric:** scene-aware vs fixed at equal `segments`-count and gate: does it reject *less
  total duration* at ≥ the same quality? The threshold sub-sweep tunes sensitivity (too low → misses
  soft cuts; too high → over-segments). Expect scene-aware to move the frontier out uniformly.

### X8 — 1-pass vs 2-pass (`two_pass`) — rate control

- **Knob:** `two_pass ∈ {False, True}`. Two-pass computes a bitrate target on a first analysis pass,
  then encodes to it — better quality-per-bit at ~2× encode time.
- **Hypothesis:** two-pass mostly matters for the **reference** (the quality target) — a better
  reference at the same size is a stricter, more honest target. For the **draft**, two-pass roughly
  doubles the draft cost, which *hurts* the speedup ceiling, so a two-pass draft only pays if it
  raises accept_rate enough to offset. Likely outcome: two-pass reference = better final tier;
  two-pass draft = usually not worth it. It's a knob that mostly refines *quality definition*, not
  speed.
- **Decision metric:** four cells (draft 1/2-pass × ref 1/2-pass) at fixed gate. Keep the cell that
  clears the tier's floor fastest. Expect `{draft:1-pass, ref:2-pass}` to be the near-lossless winner.

### X9 — Per-segment adaptive gate (future knob) — vary the gate by segment difficulty

- **Knob (designed, next to add):** a per-segment gate that *adapts* — e.g. loosen the gate on
  low-complexity segments (where SSIM is a weak discriminator and the draft is surely fine) and
  tighten it on high-motion segments (where artifacts hide). Complements X1's single global gate.
- **Hypothesis:** a single global gate is a blunt instrument: SSIM's meaning varies with content
  (a 0.97 on flat content is different from 0.97 on detailed motion). A content-adaptive gate accepts
  more where acceptance is safe and re-encodes more where it isn't — moving the frontier out the same
  way adaptive keyframing does for temporal.
- **Decision metric:** adaptive-gate vs best global gate (X1) at equal delivered quality — faster iff
  the hypothesis holds. *Not yet a runner param; documented so the tuner author adds it with a safe
  default (`adaptive_gate=False` = today's global gate).*

### X10 — Hardware encoder (`hwenc`) — NVENC on the pod's GPU

- **Knob:** `hwenc ∈ {False, True}` — use NVENC (`h264_nvenc`/`hevc_nvenc`) if present.
- **Hypothesis:** NVENC is **dramatically faster** than libx264/265 (fixed-function silicon) but at
  **lower quality-per-bit.** This reshapes the whole economics: a near-free draft (NVENC) makes the
  speedup ceiling enormous, but NVENC's quality means it fails a strict gate more often. The
  interesting combos: NVENC draft + libx26x reference (cheap speculation, quality target) — or, for a
  preview tier, NVENC everywhere for raw throughput. It also tests the *distributed* story: the fleet
  drafts on whatever encoder is cheap; the GPU verifies/re-encodes.
- **Decision metric:** with `hwenc` on, `net_speedup` and quality vs the software path. Expect a large
  speedup at the preview tier and a harder call at 0.98 (does NVENC draft + software reference still
  net a win?). Record whether NVENC was actually available (else it's a labeled fallback, not a
  result).

---

## 3. COMBINED experiments — compounding the winners

Individual sweeps find each knob's shape. The combined phase asks the money question: **which knobs
compound (multiply) and which conflict (cancel)?** — and whether chaining the two winners gives a
product bigger than either alone.

### C1 — Temporal reuse → speculative transcode (the compound pipeline)

- **Setup:** the real product path. A long **animation** is rendered with temporal reuse (T-knobs),
  producing frames; those frames are then **encoded** with speculative transcode (X-knobs). The two
  speedups are on **different resources** (GPU render time vs CPU/GPU encode time) and **sequential**,
  so they **multiply**: `total_speedup ≈ render_speedup × encode_speedup` on the wall-clock of the
  whole "render + deliver a video" job.
- **Hypothesis:** near-lossless temporal (say 3–5× at 0.98 after T2/T7 tuning) **×** near-lossless
  transcode (2.2×+ at 0.98) ⇒ a **compound ~7–11× at 0.98** on the end-to-end job — the headline
  product number, and it stays near-lossless because *both* stages are individually constrained to
  the floor. The subtlety: SSIM does **not** compose linearly — two 0.98 stages in series can land
  below 0.98 (errors accumulate). So the combined floor must be enforced **end-to-end**, not per
  stage: tune each stage a notch tighter so the *composed* output clears the floor.
- **Decision metric:** measure the **end-to-end** `quality` (final delivered video vs the fully-
  rendered, slow-encoded ground truth) and the **end-to-end** `net_speedup` (naive render-all +
  slow-encode ÷ our pipeline). The win is a compound multiple at end-to-end quality ≥ floor. Report
  the *composed* SSIM explicitly and the per-stage floors that achieve it — this is where "0.98 × 0.98
  ≠ 0.98" bites and must be handled honestly.

### C2 — Cross-knob interactions within temporal (which combine vs. conflict)

Run these pairwise combinations (coordinate ascent explores them; these are the ones to check
explicitly because the interaction sign isn't obvious):

- **adaptive_keyframe × error_feedback:** *hypothesis — compounding.* Feedback arrests drift, which
  lets the adaptive controller hold quality over a longer interval before promoting a keyframe → fewer
  keyframes → faster at the same floor. Together they should beat either alone. *Risk:* feedback
  fooling the proxy into over-extending an interval (quality slips) — check `adaptive_proxy_vs_true`.
- **bidirectional × longer interval:** *hypothesis — compounding.* A better warp (fewer holes)
  directly enables a longer keyframe interval at fixed quality; the two multiply into more speedup.
- **hole_fill=inpaint × low disocclusion_thresh:** *hypothesis — conflicting.* A low threshold makes
  big masks; inpaint on big masks hallucinates a lot → quality collapse. Inpaint only pays with a
  *high* threshold (small holes). Their sweet spots pull opposite ways.
- **high keyframe_spp × low patch_spp × long interval:** *hypothesis — strongly compounding.* A clean
  source reused over a long interval + cheap patches = the theoretical best temporal config. This is
  the combination to chase at 0.98/0.99.
- **mv_precision=half × everything:** *hypothesis — neutral on quality, positive on distributed cost.*
  Verify it stays on the frontier (doesn't cost local quality) so the distributed variant can adopt it
  for free.

- **Decision metric:** for each pair, does the best joint point beat the sum of the individual best
  points' frontier positions? Compounding pairs move the frontier out *more together than apart*;
  conflicting pairs' joint optimum is *worse* than picking one knob and leaving the other at default.
  The tuner's coordinate ascent finds these automatically; this list tells the human which
  interactions to *read* in the ledger.

### C3 — Cross-knob interactions within transcode

- **scene_aware × segments:** *hypothesis — scene-aware dominates and reduces segments-sensitivity.*
  With scene-aware on, the exact `segments` count matters less (boundaries follow content), so the
  joint optimum is flatter and more robust. Confirm scene-aware makes the config less brittle.
- **codec=AV1 × loose gate × coarse draft:** *hypothesis — the max-speedup preview config.* AV1
  reference (huge thing avoided) + loose gate (few re-encodes) + ultrafast draft = the largest
  multiple, at a preview-tier quality. Find the fastest 0.90/0.95 point here.
- **ref_preset=veryslow × two_pass ref × strict gate:** *hypothesis — the near-lossless delivery
  config.* The best possible final target + strict acceptance = the honest 0.98/0.99 winner, with the
  speedup measured against that expensive target (the number that sells the "can't-afford-it" pitch).
- **hwenc draft × software ref:** *hypothesis — decouples draft cost from reference quality.* Near-free
  draft + quality reference; the question is whether NVENC's draft passes a strict gate often enough.

- **Decision metric:** same as C2 — frontier movement of the joint config vs the components.

### C4 — Distributed variants (draft on fleet / verify on GPU)

- **Setup:** the distributed product shape. **Temporal:** draft the cheap reprojections + MVs on
  commodity fleet nodes (CPU-cheap numpy warps), ship MVs (see T9: `mv_precision=half` to cut
  bandwidth) + the keyframe to a GPU that renders the disocclusion patches and composites.
  **Transcode:** fleet nodes produce the cheap draft encodes (X2), a GPU verifies per-segment SSIM
  and re-encodes rejects (X3/X8/X10). Both map onto the `draft → verify → gate` protocol.
- **Hypothesis:** distribution adds a **third** multiplier — fan-out — *if* the transferred artifact
  is small. Temporal ships MVs + one keyframe (small, esp. at half-precision MVs), so fan-out is
  cheap; transcode ships segment boundaries + draft bitstream (small). The wall-clock win is
  `per-node speedup × fan-out`, minus transfer/coordination overhead. RUN_2026-07-06's B4/C3 already
  showed the distributed rungs pass (understated, because verify ran on CPU); this measures them
  properly with a GPU verify.
- **Decision metric:** end-to-end wall-clock of the distributed pipeline vs the single-GPU pipeline at
  equal quality, with transfer time **counted honestly**. The win is real only if `net_speedup`
  including transfer exceeds the single-node number. `mv_precision` (T9) directly trades quality for
  transfer cost here — sweep it to find the bandwidth/quality knee. **Honesty:** if the GPU-verify
  isn't available this run, mark the distributed speedup `modeled:true` with the fan-out assumption
  stated, exactly as the earlier runs did.

---

## 4. The autonomous LOOP methodology

This is *how* the tuner turns the knobs above into the tiered product, hands-off, on one money-safe
GPU. Implemented in `scripts/spec-lab/tuner.py`; the search spaces are in
`scripts/spec-lab/tuning_spaces.py`.

### Phase 1 — OFAT (one factor at a time)

Sweep **each knob alone** off the base config (the proven starting point). The primary knob
(`keyframe_every` for temporal, `gate` for transcode) is swept first and densest — it has the
strongest, cleanest speed/quality signal. OFAT gives each knob's **shape** (monotone? has an elbow?
non-monotone with a sweet spot?) and seeds every later phase. It is embarrassingly parallel-safe and
cheap; it runs to completion before any climbing.

### Phase 2 — Coordinate ascent, per tier

For **each `q_floor`**, start from that tier's best *feasible* OFAT point and climb: repeatedly move
one knob to its best feasible neighbor value (neighbors = adjacent entries in the ordered value
lists) until a full pass over all knobs yields no improvement — a local optimum for that tier. This
is deterministic and resumable. Because it runs *per tier*, the winner at 0.90 and the winner at 0.99
are found independently — which is the whole point (they are different configs).

- *Feasibility:* a trial with `quality < q_floor` or an `error` scores `-inf` and is never chosen at
  that tier.
- *Neighbors:* the ordered lists in `tuning_spaces.py` define adjacency, so "move `keyframe_every`
  from 6 to its neighbor" means 4 or 8, not a random jump — the climb is smooth.

### Phase 3 — Refine until budget

While time and balance remain, **restart** coordinate ascent from other Pareto points (the two
highest-quality feasible frontier points for the hardest tier) and explore around the incumbents.
Keep only real improvements. This is the "just keep going" loop the owner asked for — it escapes
local optima by re-seeding from different frontier locations. It stops when a full refine round finds
no improvement (**convergence**) or the budget/deadline/`min-balance` floor trips (**money-safety**).

### How to read the output

Everything streams to `docs/speed-lane-reports/spec-lab/tuning_ledger.jsonl` (resumable: a
`(target, config-hash)` already measured is never re-run). Three event types:

- `event:"trial"` — one measured config: `{config, metrics:{net_speedup, quality, ...}}`. The raw
  data; every point on every plot comes from here.
- `event:"tier_best"` — the winning config + speedup at a `q_floor`. **This is the tiered product
  table** — the deliverable: "best speedup we can guarantee at 0.90 / 0.95 / 0.98 / 0.99."
- `event:"pareto"` — the full non-dominated `(quality, speedup)` frontier for a target. **This is the
  menu.** Plot speedup (y) vs quality (x); the frontier is the achievable envelope; the four
  `tier_best` points are labeled on it.

Read it top-down: (1) look at the four `tier_best` rows — that's the product. (2) Plot the `pareto`
frontier — that's the shape of the tradeoff and where the tiers sit on it. (3) Dive into `trial`
rows only to explain *why* a tier landed where it did (which knob was binding).

### Stopping / convergence criteria (explicit)

The loop halts on the **first** of:

1. **Convergence** — a full refine round produces no frontier improvement (the honest "we found the
   optimum for this search space" signal).
2. **Deadline** — `--max-minutes` watchdog force-terminates the pod (default 180m; each trial is
   time-bounded to ~15m by the runner contract).
3. **Money floor** — balance falls below `--min-balance` (default $4); the pod tears down. "Exhaust
   if necessary" with a recoverable floor.
4. **Search exhausted** — every reachable neighbor of every incumbent has been measured.

On every exit path the pod is torn down (inherited money-safety from `runpod.py`: tracked pods,
teardown on finish/exception/Ctrl-C/SIGTERM/deadline).

---

## 5. The full experiment matrix (index)

| # | knob | runner param | individual §  | trades | decision metric |
|---|---|---|---|---|---|
| T1 | keyframe interval | `keyframe_every` | 1 | speed↑/quality↓ with K | max K clearing each floor |
| T2 | **adaptive keyframing** | `adaptive_keyframe`,`quality_floor` | 1 | **bounds quality, floats speed** | speedup vs fixed-K at equal quality |
| T3 | disocclusion threshold | `disocclusion_thresh` | 1 | re-render area vs trust | elbow before quality drops |
| T4 | reprojection method | `reproject_method` | 1 | coverage vs crispness | fastest at ≥ floor |
| T5 | hole filling | `hole_fill` | 1 | free holes vs invented detail | fastest fill clearing floor |
| T6 | per-region re-render | (mask + `disocclusion_thresh`) | 1 | seam-seal vs wasted area | min area fraction at floor |
| T7 | keyframe vs patch spp | `spp` (+`keyframe_spp`/`patch_spp`) | 1 | source cleanliness vs patch cost | split beats uniform at equal quality |
| T8 | resolution | `resolution` | 1 | MV/mask fidelity (mostly quality) | higher floor reached at ~equal speedup |
| T9 | MV precision | `mv_precision` | 1 | quality vs transmit cost | quality delta full→half→int |
| T10 | error feedback | `error_feedback` | 1 | drift correction → longer interval | max interval at floor increases? |
| T11 | animation length | `frames` | 1 | keyframe amortization | speedup rises + asymptotes |
| X1 | acceptance gate | `gate` | 2 | accept cheap vs re-encode | fastest gate clearing floor |
| X2 | draft preset | `draft_preset` | 2 | draft cheapness vs accept rate | crudest preset still accepted |
| X3 | reference preset | `ref_preset` | 2 | final quality / thing-avoided size | speedup per reference tier |
| X4 | CRF pairs | `draft_crf`,`ref_crf` | 2 | quality/size operating point | the accept-rate ridge |
| X5 | codec | `codec` | 2 | quality/bit vs encode cost | relative speedup per codec |
| X6 | segment granularity | `segments` | 2 | localize failures vs SSIM noise | min re-encoded duration |
| X7 | **scene-aware seg** | `scene_aware`,`scene_threshold` | 2 | effort follows content | less duration re-encoded at ≥ quality |
| X8 | 1 vs 2 pass | `two_pass` | 2 | quality/bit vs 2× encode | fastest cell clearing floor |
| X9 | per-segment adaptive gate | (future `adaptive_gate`) | 2 | gate follows content | beats global gate at equal quality |
| X10 | hardware encoder | `hwenc` | 2 | throughput vs quality/bit | speedup + quality vs software |
| C1 | temporal → transcode | both | 3 | **compound multiply** | end-to-end speedup at composed floor |
| C2 | temporal cross-knob | temporal | 3 | compound vs conflict | joint vs component frontier |
| C3 | transcode cross-knob | transcode | 3 | compound vs conflict | joint vs component frontier |
| C4 | distributed fan-out | both | 3 | per-node × fan-out − transfer | wall-clock incl. transfer vs single-node |

---

## 6. Honest risks — where more speedup is impossible without quality loss

The owner asked for the truth about the ceiling, not just the wins. This section is load-bearing.

### The lossless ceiling is real and modality-specific

**Temporal reuse cannot be truly lossless above 1×.** The instant *any* frame is reconstructed by
warping instead of rendered, some information is approximated: sub-pixel resampling softens detail,
disocclusions reveal content the keyframe never saw, and specular/reflective surfaces (the metal
sphere) change appearance with view angle in ways no warp can predict from a single keyframe. So the
achievable near-lossless speedup is **bounded by how much of the frame is genuinely predictable from
the keyframe.** Our levers (adaptive keyframing, hi-spp keyframe, better warp, error feedback) push
that boundary *out* — but there is a hard wall: a shot with a fast view-dependent highlight or a hard
cut has *no* reprojectable structure, and there temporal reuse correctly degrades to ~1× (it spends a
keyframe every frame). **That graceful degradation is a feature, not a failure** — the marketplace
sells the win and never punishes the buyer on a hard shot — but it means the 0.99 tier's speedup on
adversarial content can be close to 1×. We must report the speedup **per content class**, not one
blended number.

- **View-dependent shading (specular/glossy/refractive):** the strongest lossless ceiling. Warping a
  mirror is wrong by construction. Expect low near-lossless speedup on reflective/refractive scenes.
- **Fast motion / large disocclusion:** big masks → most of the frame re-rendered → speedup collapses
  toward 1× at high quality. Fills can't save 0.99 here (they'd hallucinate).
- **Hard cuts:** zero reprojectable structure → a keyframe per frame. Adaptive keyframing handles this
  correctly (and cheaply) but there is *no* speedup to be had at any quality.

**Speculative transcode's ceiling is gentler but real.** Its worst case is a clip where the cheap
draft fails the gate *everywhere* (high-entropy, high-motion, grain-heavy footage) — then it
re-encodes every segment and nets ~1× (the draft cost is pure overhead). RUN_2026-07-06's B3
(high-motion/cuts) showed exactly this: 0 accepts, cheap rejects, net ≈ 1×. So transcode's
near-lossless speedup is **bounded by the fraction of the clip the cheap preset can already nail** —
high on ordinary footage (hence 2.18× at 0.98), low on adversarial footage. It never goes *below* ~1×
(rejects are cheap to detect), which is the safe-failure property.

### The regime caveat (why our numbers may *understate* the real product)

RUN_2026-07-06's load-bearing finding: on an L40S these test scenes render in seconds, so fixed
overhead (Blender launch, denoise, composite) can dwarf the compute saved — a **false negative** for
render speculation on small scenes. Temporal reuse's *paying regime* is genuinely slow renders
(minutes/frame: 1080p/4K, complex GI, long shots). Our budget-bounded tests (≤~15 min, small res) sit
below that regime, so their **speedup is a conservative floor** — the real product, on real
production renders, should do *better*, not worse. The T8 resolution knob and a deliberate
heavy-scene run (§ future work) are how we climb toward the real regime; until then, every temporal
number carries the "measured on a small scene; production is the paying regime" caveat.

### Where a knob *cannot* help

- **Below the disocclusion floor:** no warp or fill recovers content the keyframe never captured. Past
  a certain motion speed, re-rendering *is* the only correct answer — no knob buys speed there at 0.99.
- **SSIM's own blind spots:** SSIM under-penalizes some temporal artifacts (flicker, small geometric
  wobble) that a human sees. A config that games SSIM to 0.98 may still look wrong. Mitigation: report
  SSIM as the gate but flag configs whose *per-frame* SSIM variance is high (temporal instability) even
  when the mean clears the floor. VMAF/temporal metrics are the honest upgrade when available.
- **Composition of floors (C1):** two near-lossless stages in series are *not* near-lossless. The
  end-to-end floor is stricter than either stage's; the compound speedup is real but its quality must
  be measured end-to-end, and each stage tuned tighter to compensate. Don't multiply two 0.98 stages
  and claim 0.98.

### The honest stance

Every number the loop produces is a real render/encode wall-time and a real SSIM (`modeled:false`),
except the two clearly-labeled modeled steps (temporal's area→cost patch charge; distributed fan-out
when a GPU-verify isn't available). Where a knob's win is scene-specific, we say so and mark it
unconfirmed until it reproduces across seeds and content classes. The product claim is the **tiered
frontier on ordinary content, with the adversarial-content degradation stated plainly** — a fast,
near-lossless win where structure exists, a graceful ~1× where it doesn't, and never a quality
surprise to the buyer.
