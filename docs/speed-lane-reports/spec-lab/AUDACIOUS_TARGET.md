# AUDACIOUS TARGET — 100× cheaper-to-the-user at SSIM ≥ 0.97

*The run plan to chase a **100× cost reduction to the buyer** on a render-and-deliver-a-video
job, at near-lossless quality, decomposed into individually-measurable factors and measured
in an ordered ladder on one A100/H100. Every number this plan produces is a real render/encode
wall-clock and a real skimage SSIM (`modeled:false`), or it is loudly flagged `modeled:true`
with the exact step named. The keystone that turns the factor product into ONE honest
end-to-end ratio is `pod/exp_render_stack.py` (compute) chained into
`pod/exp_pipeline_render_transcode.py` (compute + delivery transcode).*

**Read `RUN_2026-07-06.md` and `TUNING_PLAN.md` first** — this plan builds directly on their
measured priors (temporal 3.7–12.95× @ 0.84; transcode 2.18× @ 0.98; the load-bearing
"render-spec only pays on genuinely slow renders" finding) and their honesty contract.

---

## 0. Why "100×" and why "to the user" (frame the number honestly before chasing it)

The north star is **not** a single-lever speedup. No one render lever gets near 100× at
near-lossless — the RUN report already proved that (denoise alone went *negative* on small
scenes; bounce-hybrid is a dead end; the honest single-lever wins are 2–13×). 100× is only
reachable as a **product of independent factors on different resources**, and only "to the
user" — i.e. the number the buyer sees is `(what it would cost to render+deliver yourself on
rented cloud) ÷ (what we charge)`, which folds compute-efficiency **and** distribution
(fan-out spreads the wall-clock across cheap idle nodes the buyer never had to rent).

Three honesty rules govern the whole plan:

1. **The headline compound is ONE measured end-to-end ratio**, never a product of stage
   speedups multiplied on a slide. We measure the factors individually to *understand* the
   budget, then we measure the compound directly with `exp_render_stack.py` →
   `exp_pipeline_render_transcode.py` and report THAT number as the claim.
2. **Quality compounds DOWN faster than speed compounds UP.** Two independently-near-lossless
   stages in series are not near-lossless: their errors overlap on shared high-frequency
   regions (silhouettes, specular highlights, encode blocking on already-softened warp
   output), so end-to-end SSIM lands *below* the min of the stage SSIMs. This is the central
   risk of the whole target and is why every rung's quality is scored **end-to-end vs a true
   full-quality reference**, not per-stage.
3. **Distribution is a wall-clock/cost multiplier, not a compute multiplier.** Fan-out does
   not make the total FLOPs smaller — it makes the buyer's wall-clock and $ smaller by
   spreading fixed work across N nodes. Its honest ceiling (`exp_render_faninout.py`) is
   `T_serial / T_parallel_ideal` and is explicitly labelled an **upper bound** that ignores
   network scatter/gather; the real product pays that overhead and we say so.

---

## 1. THE TARGET DECOMPOSITION — 100× as a product of named, measurable factors

The job we are pricing: **"render a long animation you don't own the machine for, and hand me
back a finished video file."** That job decomposes into a compute-efficiency chain (all on
GPU render/encode time, measured on ONE box) times a distribution factor (wall-clock spread
across N nodes) times a delivery-transcode factor (the finished-file encode):

```
  cost_reduction_to_user  ≈   [ denoise  ×  convergence  ×  temporal-on-keyframes ]   (compute-efficiency, per-node)
                              ×  distribution                                          (fan-out across idle nodes)
                              ×  transcode                                             (delivery encode)
```

### The named factors, each with a runner, a realistic prior, and a kill line

| factor | what it measures | runner | realistic prior | prior source |
|---|---|---|---|---|
| **denoise** | reach reference SSIM from a NOISY low-spp draft via OIDN + albedo/normal guides (the biggest single lever, but overhead-bound on small scenes) | `exp_cycles_render_prod.py` | **~8×** on a genuinely slow high-spp render; **<1× (negative)** on a fast small scene | RUN R1: negative on 256²; regime note: pays only minutes/frame |
| **convergence** | reach reference SSIM with fewer *effective* samples via adaptive sampling + light-tree, **no denoiser** (truly lossless) | `exp_render_convergence.py` | **~2–4×** | TUNING T-analog; adaptive C2 hit 2.79× |
| **temporal-on-keyframes** | render keyframes with the full anchor stack, reproject the between-frames by Cycles motion vectors, re-render only disocclusions | `exp_render_stack.py` (keystone) / `exp_render_temporal.py` | **~4–8×** on animation at near-lossless (12.95× exists but only at 0.84) | RUN X4: 3.7×@k8, 12.95×@k16 @ 0.84 |
| **distribution** | split one frame/job across N nodes; ideal fan-out envelope | `exp_render_faninout.py` | **~4–16× ideal** (upper bound; real = minus network) | RUN C3 3.28× understated; faninout ceiling |
| **transcode** | cheap-preset draft encode + selective slow re-encode of only failing segments (the delivery leg) | `exp_video_transcode.py` | **~2.2×** at 0.98 (grows vs slower reference codecs) | RUN R4: **2.18× @ 0.98, measured, modeled:false** |

### The honest compound expectation at near-lossless (this is the load-bearing paragraph)

Multiplying the **optimistic** priors gives a headline that looks trivially past 100×:

```
  8 (denoise) × 3 (convergence) × 6 (temporal) × 8 (distribution) × 2.2 (transcode)  ≈  2,534×
```

**That number is a lie of composition and we will not claim it.** Three deflations apply, and
all three are measured, not assumed:

1. **The compute-efficiency levers OVERLAP, they do not fully multiply.** Denoise and
   convergence both attack "reach reference SSIM from fewer path-traced samples" — they share
   the *same* sample-budget resource. A denoised 512-spp draft and an adaptive-sampled draft
   are competing for the same savings; stacking them yields far less than 8×3=24×. The stack
   runner charges them together against ONE reference, so the measured combined compute lever
   is realistically **~10–20×**, not ~24×+. Temporal is genuinely orthogonal (it attacks
   *frame count*, a different axis) and *does* multiply against the per-frame lever — but only
   on animation, and its near-lossless multiple (0.97+) is **~4×**, not the 12.95× that lives
   at 0.84.
2. **Quality compounds DOWN on shared error regions.** Denoise softens high-frequency detail;
   convergence's under-sampled tiles are noisiest exactly where detail lives; temporal warp
   smears the same silhouettes; the delivery encoder then spends its bits worst on
   already-degraded high-frequency content. These errors **co-locate** — the worst 8×8 tile of
   each stage tends to be the same tile — so end-to-end worst-tile SSIM falls faster than any
   single stage. To hold *end-to-end* SSIM ≥ 0.97 we must run each stage a notch *tighter*
   than its own 0.97 point, which **costs speed back**. This is why per-tile worst/p5 SSIM is
   reported at every rung: a lever that lifts the global mean while collapsing a tile is caught
   and disqualified.
3. **Distribution is an upper bound, not a delivered number.** The 8–16× fan-out is
   `T_serial/T_parallel_ideal` with zero comms; real cross-pod fan-out pays scatter/gather +
   scheduling. Treat it as **~4–8× delivered** for the cost claim.

**Honest compound expectation at SSIM ≥ 0.97:**

```
  compute-efficiency (denoise×convergence, overlapping, near-lossless)   ~10–15×
      × temporal-on-keyframes (animation only, near-lossless)             ~3–4×
      × distribution (delivered, not ideal)                               ~4–8×
      × transcode (delivery, measured)                                    ~2.2×
  --------------------------------------------------------------------------------
  realistic near-lossless band:   ~10×15 × 3.5 × 6 × 2.2  ≈  260× OPTIMISTIC ceiling
  honest target we commit to prove: ~100×, with a credible path, and a REAL floor of
       the compound stack alone (compute × temporal, one box, measured) of ~20–40×
       BEFORE distribution, which is the number we can prove without a multi-pod cluster.
```

The 100× is **plausible but not owed**. The single number this plan is built to *prove
honestly* is the compound-stack ratio from the keystone runner (compute-efficiency × temporal,
one box) — realistically **20–40× at ≥0.97** — with distribution and transcode as the
*measured-separately, multiplied-in-with-stated-overhead* factors that carry it toward 100×.
If the compound-stack alone lands at 25× @ 0.97 and distribution delivers a real 4× and
transcode a real 2.2×, the buyer sees ~220× — and every factor in that product is a receipt.
If quality-compounding forces the stack down to 8× @ 0.97, we report that honestly and the
100× is not reached without more distribution than we can prove on this budget. **We commit to
the measured number, whatever it is.**

---

## 2. THE EXPERIMENT LADDER — the exact ordered sequence on an A100/H100

The ladder measures each factor **in isolation first** (so we know the per-lever budget and
can attribute a compound miss to the right lever), then measures the **compound directly**.
The single most expensive artifact — the **4096-spp OptiX ground-truth reference render** — is
rendered ONCE and **reused across every rung** via each runner's on-disk reference cache
(keyed by scene/res/spp/bounces/seed). Rung 0 exists solely to prime that cache; every later
rung is a cache HIT on the reference, so the ladder pays the reference cost exactly once.

**Invocation form (every rung):** `python3 pod/<runner>.py "$(cat rung_N.json)"`, config is
`json.loads(sys.argv[1])`. Each runner emits exactly ONE JSON line; on any failure exactly one
`{"error":...}` line and exit 0. Fixed across all rungs unless noted: `"scene":"classroom"`,
`"resolution":"1920x1080"`, `"ref_spp":4096`, `"bounces":12`, `"seed":0`, `"device":"AUTO"`
(→ OptiX on the A100/H100).

---

### Rung 0 — prime the reference (single-frame denoise anchor, also the denoise factor)

Renders the 4096-spp OptiX ground-truth ONCE (caches it for the whole ladder) AND measures the
**denoise** factor: a noisy low-spp draft + OIDN + albedo/normal guides vs that reference.

```json
{ "scene":"classroom", "resolution":"1920x1080", "ref_spp":4096, "bounces":12,
  "draft_spp":128, "adaptive":true, "adaptive_threshold":0.01, "adaptive_min_samples":16,
  "denoiser":"oidn", "denoise_guides":true, "device":"AUTO" }
```
Runner: `exp_cycles_render_prod.py`. Reads: `net_speedup` (= denoise factor), `quality`,
`worst_tile_ssim`, `p5_tile_ssim`. (This runner uses a fixed internal seed — it does not read a
`seed` knob — so the whole ladder shares seed 0 by construction; do not pass `seed` here.)

- **PASS** if `net_speedup ≥ 4` AND `quality ≥ 0.97` AND `worst_tile_ssim ≥ 0.90`.
- **KILL** the denoise lever from the compound if `quality < 0.95` at *any* speedup (denoiser
  is blurring detail — the whole reason we report worst-tile) OR `net_speedup < 1` (overhead >
  savings — the RUN R1 negative regime; means this scene/res is still too cheap and we must go
  heavier, not that denoise is dead).
- **Note:** this rung's `ref_render_s` is the reference cost the whole ladder amortizes.

---

### Rung 1 — convergence factor (truly lossless, no denoiser)

Reach the reference SSIM with fewer effective samples via adaptive sampling + light-tree, **no
denoiser** — the honest "lossless" lever. Sweeps the adaptive threshold and emits the
samples-to-reference-SSIM frontier.

```json
{ "scene":"classroom", "resolution":"1920x1080", "ref_spp":4096, "bounces":12,
  "samples":512, "adaptive_thresholds":[0.05,0.02,0.01,0.005], "adaptive_min_samples":16,
  "use_light_tree":true, "use_guiding":false, "target_ssim":0.97, "seed":0, "device":"AUTO" }
```
Runner: `exp_render_convergence.py`. Reads: `frontier[]`, headline `net_speedup`, `quality`,
`worst_tile_ssim`, `p5_tile_ssim`, `hit_target_ssim`, `device_mismatch` (must be false — same
box as rung 0's cached reference).

- **PASS** if some frontier point has `quality ≥ 0.97` with `net_speedup ≥ 2` and
  `device_mismatch=false`.
- **KILL** if the frontier is flat (no point clears 0.97 above 1.2×) — convergence's lossless
  ceiling is hit on this scene; fall back to leaning on denoise + temporal.

---

### Rung 2 — temporal-on-keyframes factor (the animation lever), near-lossless sweep

The single biggest multiplier on animation. Sweep `keyframe_every` to find the **largest K
that still clears 0.97** — that K is the near-lossless temporal factor (expected ~4×, not the
12.95× that lives at 0.84).

Run three points (cache-hit on the reference each time):
```json
{ "scene":"classroom", "resolution":"1920x1080", "spp":512, "frames":24,
  "keyframe_every":4, "disocclusion_thresh":0.1, "hole_fill":"rerender",
  "reproject_method":"backward", "adaptive_keyframe":false, "seed":0, "device":"AUTO" }
```
then `"keyframe_every":8`, then `"keyframe_every":16` (same JSON otherwise). Runner:
`exp_render_temporal.py`. Reads: `net_speedup`, `quality`, `worst_tile_ssim`, `p5_tile_ssim`,
`per_frame_worst_tile_ssim`.

- **PASS** if the K=4 (or K=8) point clears `quality ≥ 0.97` AND `worst_tile_ssim ≥ 0.90` at
  `net_speedup ≥ 3`.
- **KILL** the near-lossless temporal factor down to whatever K clears 0.97; if even K=4 is
  below 0.97, the scene's specular/motion content has no reprojectable structure at
  near-lossless — record the honest ~1× degradation and lean on adaptive keyframing in the
  compound (rung 4).

---

### Rung 3 — distribution factor (fan-out ceiling, upper bound)

Split ONE reference-quality frame into N tiles, render each as its own real subprocess, report
the ideal fan-out envelope. Explicitly an **upper bound** (`net_speedup_label =
"ideal_fanout_ceiling_upper_bound"`).

```json
{ "scene":"classroom", "resolution":"1920x1080", "spp":4096, "bounces":12,
  "tiles":8, "seed":0, "device":"AUTO" }
```
then `"tiles":16`. Runner: `exp_render_faninout.py`. Reads: `net_speedup` (ideal ceiling),
`T_serial`, `T_parallel_ideal`, `load_balance_efficiency`, `quality` (composite lossless-split
sanity, MUST be ~1.0), `sum_tiles_over_full_frame`.

- **PASS** if `quality ≥ 0.98` (lossless split intact — a lower value is a seam bug, reported
  not hidden) AND `load_balance_efficiency ≥ 0.6` (fan-out is worth it).
- **KILL/DISCOUNT:** `net_speedup` here is the *ceiling*, not the delivered number. Record it
  as the upper bound; the delivered distribution factor for the cost claim is discounted to
  ~half the ceiling to account for the unmeasured network scatter/gather. `T_serial` is the
  honest single-box cost reported alongside.

---

### Rung 4 — THE COMPOUND KEYSTONE (compute-efficiency × temporal, ONE measured ratio)

`exp_render_stack.py` is the keystone: it renders keyframes with the **full anchor stack**
(adaptive convergence + OIDN denoise + albedo/normal guides + light-tree at low `draft_spp`),
reprojects the between-frames by Cycles motion vectors, re-renders only disocclusions, and
measures ONE honest ratio `T_ref / T_stack` end-to-end. This is where denoise × convergence ×
temporal are measured **together as one number against one reference**, so the overlap and the
quality-compounding are captured — not multiplied on a slide.

```json
{ "scene":"classroom", "resolution":"1920x1080", "ref_spp":4096, "bounces":12,
  "frames":24, "keyframe_every":8, "draft_spp":512, "adaptive_threshold":0.01,
  "adaptive_min_samples":16, "denoiser":"oidn", "denoise_guides":true, "light_tree":true,
  "disocclusion_thresh":0.1, "hole_fill":"rerender", "cam_motion":1.0, "seed":0, "device":"AUTO" }
```
Runner: `exp_render_stack.py`. Reads: `net_speedup` (= compound compute×temporal), `quality`
(end-to-end SSIM of delivered frames vs the 4096-spp reference), `worst_tile_ssim`,
`p5_tile_ssim`, `per_frame_global_ssim`, `per_frame_worst_tile_ssim`, `reproject_accept_frac`,
`modeled` (true only if `hole_fill="rerender"` AND a frame actually disoccludes — the one
area-scaled crop-cost model, named in `note`).

- **PASS** if `net_speedup ≥ 15` AND `quality ≥ 0.97` AND `worst_tile_ssim ≥ 0.90`. This is
  the honest compound-stack floor the whole plan is built to prove.
- **KILL/TUNE:** if `quality < 0.97` but `net_speedup` is high, quality-compounding bit — tune
  each stage tighter (lower `keyframe_every` to 4, lower `adaptive_threshold` to 0.005, raise
  `draft_spp`) and re-run; the honest deliverable is the fastest point that holds 0.97
  end-to-end, whatever multiple that is. If `net_speedup < 8` @ 0.97, the compound stack is
  weaker than the sum of parts (heavy overlap) — report it and the 100× leans harder on
  distribution.
- **Second config (the near-lossless push, run only if the first misses 0.97):**
  `"keyframe_every":4, "draft_spp":1024, "adaptive_threshold":0.005`.

---

### Rung 5 — transcode factor (the delivery leg, measured)

The finished-file encode. Cheap-preset draft + selective slow re-encode of failing segments.
This is the RUN-report's realest, already-measured win (2.18× @ 0.98, modeled:false).

```json
{ "scene":"classroom", "seconds":6, "segments":8, "gate":0.97,
  "draft_preset":"veryfast", "ref_preset":"slow", "draft_crf":26, "ref_crf":18,
  "codec":"libx264", "scene_aware":true, "scene_threshold":0.3, "two_pass":false, "hwenc":false }
```
Runner: `exp_video_transcode.py`. Reads: `net_speedup`, `quality`, per-segment accept rate.

- **PASS** if `net_speedup ≥ 2` AND `quality ≥ 0.97`.
- **KILL** to ~1× (safe failure) if the draft fails the gate everywhere — record it; transcode
  never goes below ~1× (rejects are cheap to detect), so this factor is a floor, not a risk.

---

### Rung 6 — THE END-TO-END COMPOUND (render-stack → delivery transcode, ONE ratio)

The product path. `exp_pipeline_render_transcode.py` renders the animation with temporal reuse
AND encodes the result with speculative transcode, and measures ONE end-to-end ratio: naive
(render-every-frame-full + slow-encode) ÷ our pipeline, with end-to-end SSIM of the final
*decoded video* vs the fully-rendered slow-encoded ground truth. **This is the headline
compound number** (compute × temporal × transcode; distribution multiplied in separately from
rung 3 with its stated overhead).

```json
{ "resolution":"1920x1080", "frames":24, "keyframe_every":8,
  "spp":512, "disocclusion_thresh":0.1, "draft_preset":"veryfast", "ref_preset":"slow",
  "draft_crf":26, "ref_crf":18, "gate":0.97, "segments":8, "fps":24, "seed":0, "device":"AUTO" }
```
Runner: `exp_pipeline_render_transcode.py` (classroom scene and bounces are hardwired here — it
does not read `scene`/`bounces` knobs, so they are omitted above rather than passed inertly).
Reads: `net_speedup` (end-to-end compound),
`quality` (end-to-end decoded-video SSIM vs ground truth), `render_speedup`,
`transcode_speedup`, `modeled` (false — the one shared area-scaled patch-cost step is the only
modeled element, named in `note`).

- **PASS** (the target rung) if end-to-end `net_speedup ≥ 20` AND end-to-end `quality ≥ 0.97`.
  Multiplying in the delivered distribution factor (rung 3, discounted) then gives the
  buyer-facing cost-reduction figure.
- **KILL/REPORT:** whatever the measured end-to-end ratio is, THAT is the claim. If it lands at
  12× @ 0.97 end-to-end, the compute×temporal×transcode product is 12×, and 100× requires a
  proven ~8× distribution factor (rung 3 ceiling ~16×, delivered ~8× is credible). If it lands
  at 30× @ 0.97, 100× needs only ~3.3× distribution — comfortably inside the fan-out ceiling.

---

### Ladder summary (order, runner, factor, kill line)

| rung | runner | factor measured | pass line | reuses reference |
|---|---|---|---|---|
| 0 | `exp_cycles_render_prod.py` | denoise (+ primes reference) | ≥4× @ 0.97, worst-tile ≥0.90 | renders it |
| 1 | `exp_render_convergence.py` | convergence (lossless) | ≥2× @ 0.97, no device_mismatch | HIT |
| 2 | `exp_render_temporal.py` | temporal-on-keyframes | ≥3× @ 0.97, worst-tile ≥0.90 | HIT |
| 3 | `exp_render_faninout.py` | distribution (ceiling) | quality ≥0.98, LB-eff ≥0.6 | HIT |
| **4** | **`exp_render_stack.py`** | **compound compute×temporal (keystone)** | **≥15× @ 0.97** | HIT |
| 5 | `exp_video_transcode.py` | transcode (delivery) | ≥2× @ 0.97 | n/a (video) |
| **6** | **`exp_pipeline_render_transcode.py`** | **end-to-end compound (headline)** | **≥20× @ 0.97** | HIT |

---

## 3. THE HONEST-CEILING PROTOCOL — how we know we hit the wall

The single rule: **the compound speedup we claim is ONE measured end-to-end ratio** (rung 6,
or rung 4 for the compute-only stack), never a product of stage speedups. The isolated-factor
rungs (0–3, 5) exist to *attribute* and *budget*, not to be multiplied into the headline. When
they disagree with the compound (the product of isolated factors > the measured compound), the
**measured compound is the truth** and the gap is the honest overlap/quality-compounding tax —
we report the gap, we do not report the product.

Three concrete walls, each with a detectable signature in the emitted metrics:

1. **The denoise bias floor — the SSIM frontier flattens.** As `draft_spp` drops, denoise
   speedup rises but `quality` and especially `worst_tile_ssim` stop tracking and flatten (the
   denoiser is inventing/blurring detail it cannot recover). Signature: on the convergence
   frontier (`exp_render_convergence.py`) and the denoise sweep, the `quality`-vs-`net_speedup`
   curve goes horizontal — more speed buys **zero** quality back. That flat is the lossless
   floor; we stop there and read the last point that still clears 0.97 worst-tile.

2. **The temporal warp ceiling — worst-tile / per-frame SSIM collapses before global does.**
   Temporal reuse cannot be lossless above 1× (sub-pixel resampling softens detail;
   view-dependent specular on the metal/glossy surfaces is wrong by construction; hard
   disocclusions reveal unseen content). Signature: as `keyframe_every` rises,
   `per_frame_worst_tile_ssim` and `p5_tile_ssim` fall **faster** than the global mean — the
   damage co-locates on silhouettes/highlights. When worst-tile drops below 0.90 while global
   still reads 0.97, we have hit the warp ceiling for that K; the honest near-lossless K is the
   last one where worst-tile holds. On adversarial content (fast specular, hard cuts) temporal
   correctly degrades to ~1× — that graceful no-op is the wall, reported per content class, not
   blended away.

3. **The distribution network wall — the ceiling is an upper bound and stays labelled one.**
   `exp_render_faninout.py` emits `net_speedup` as `T_serial/T_parallel_ideal` with
   `net_speedup_label="ideal_fanout_ceiling_upper_bound"` and `sum_tiles_over_full_frame > 1`
   (the real per-tile launch/BVH-rebuild overhead of naive fan-out). We NEVER put the ideal
   ceiling in the buyer-facing number. The delivered distribution factor is discounted (≈½ the
   ceiling) until a real multi-pod run measures scatter/gather; if/when that run happens, the
   delivered number replaces the discount. Signature we're at the wall: `load_balance_efficiency`
   falls (tiles uneven) or `sum_tiles_over_full_frame` grows past ~1.5 (per-tile fixed overhead
   eats the fan-out) — beyond that, more tiles is negative.

**The compounding-DOWN check (runs at every compound rung):** the end-to-end `quality`
(rung 4, rung 6) is scored against the TRUE full-quality reference/ground-truth video, not
against any stage's output. If end-to-end `worst_tile_ssim` < the min of the stages'
worst-tile SSIMs, that is quality-compounding biting — we tune each stage tighter and pay speed
back until the *end-to-end* worst-tile clears 0.90 and global clears 0.97. We never certify a
tier on global SSIM alone; a config that games the global mean while collapsing a tile is
disqualified.

---

## 4. COST / TIME ESTIMATE for the full ladder (A100/H100, order-of-magnitude)

Anchored on the RUN report's real economics: the entire 14-rung autonomous run cost **~$0.05**
of GPU time, and the whole session (soaks + 3 lab runs + real-content track) totalled
**~$1.21**. This ladder is heavier (1080p @ 4096-spp reference, 24-frame animation) but still
small. Assume an A100/H100 at **~$2/hr** (RunPod community/secure spot range).

| rung | dominant cost | est. wall-time (A100/H100) |
|---|---|---|
| 0 | the 4096-spp 1080p reference render (paid ONCE) + one denoised draft | 4–10 min |
| 1 | 4 adaptive-threshold draft renders (reference cached) | 3–6 min |
| 2 | 3 temporal runs × 24 frames (keyframes + patches; reference cached) | 6–12 min |
| 3 | 2 fan-out runs (8 + 16 tiles, each a full-frame's worth of pixels) | 4–8 min |
| **4** | keystone: 24-frame anchored stack (+ optional near-lossless re-run) | 8–15 min |
| 5 | transcode: real ffmpeg draft + selective slow re-encode on ~6s clip | 2–4 min |
| **6** | end-to-end: temporal render + speculative encode, 24 frames | 8–15 min |
| — | Blender self-bootstrap + classroom download (once, cached) | 3–5 min |

**Total wall-clock: ~40–75 min** on one A100/H100. At ~$2/hr that is **~$1.3–2.5** for the
full ladder, plus a few cents of pod idle/teardown. Budget an **order of ~$3–5** to cover a
re-run of the keystone rung (rung 4) and the near-lossless push at rung 6. The reference-reuse
design is what keeps it cheap: the single most expensive artifact (the 4096-spp 1080p render,
~4–10 min alone) is paid once and cached for rungs 1–4. Distribution's *real* multi-pod
measurement (replacing the rung-3 discount with a delivered number) is a separate, larger spend
(2+ pods, scatter/gather) and is **out of scope for this single-box ladder** — flagged as the
follow-up that converts the distribution ceiling into a delivered factor.

**Money-safety:** inherited from the harness — tracked pod, teardown on every exit path
(finish/exception/deadline/`min-balance` floor), each rung time-bounded to ~15 min by the
runner contract. Budget is freed but not infinite; ~$5 is a comfortable ceiling for the whole
single-box ladder with one re-run.

---

## VERDICT — which single rung to run FIRST for maximum information

**Run Rung 4 first — the keystone `exp_render_stack.py` compound (compute-efficiency × temporal
on the anchored keyframes), one measured end-to-end ratio at 1080p/24-frame/near-lossless.**
It is the single highest-information rung because it is the *load-bearing joint* of the entire
100× thesis: it renders the 4096-spp reference (priming the cache the whole ladder reuses, so
it also does rung 0's job), and it measures denoise × convergence × temporal **together against
one reference** — which means it directly exposes the two failure modes that would sink the
target: (a) whether the compute levers *overlap* into far less than their product, and (b)
whether quality compounds DOWN below 0.97 end-to-end when the stages stack. A single stack run
tells us the honest compound-stack multiple at ≥0.97 (the plan's floor, ~15–40×) and its
worst-tile behavior; if it clears ~15× at 0.97 with worst-tile ≥0.90, the 100× target is
credible and the rest of the ladder just fills in the distribution and transcode factors that
carry it there. If it lands at 8× because the levers overlap and quality compounds down, we
learn *that* on the first run — before spending anything on the isolated-factor rungs — and can
honestly re-scope the target or lean the 100× harder on distribution. No other rung can refute
or confirm the thesis in one shot; the keystone can.
