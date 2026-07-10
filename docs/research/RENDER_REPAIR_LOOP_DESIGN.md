# Render Repair Loop — reference-free worst-tile repair for exp_render_stack.py (2026-07-09)

Status: BUILT + LOCALLY VERIFIED (design + local build wave; zero cloud, zero money).
Target: lift the integrated 4K receipt's worst tiles 0.91 -> 0.95 (the strict delivery
gate `cx_integrated_speculation.DELIVERY_WORST_TILE = 0.95`) WITHOUT paying more samples
everywhere. The gate is NOT touched; the repair loop exists to MEET it.

Code: `scripts/spec-lab/pod/exp_render_stack.py` (PASS 3.5 + helpers, default OFF)
Adapter: `scripts/spec-lab/cx_render_spec_adapter.py` (`from_stack_metrics` repair path)
Tests: `scripts/spec-lab/test_render_repair_loop.py` (24 tests, all local/synthetic)

## 0. Measured ground truth this design stands on (REAL RUN 3, A100 SECURE, 4K, kf=1)

| quantity | value | source |
|---|---|---|
| receipt | 5.561x @ global 0.9854 / worst-tile 0.9095, `modeled=false` | integrated ledger RUN 3 |
| per-frame worst tiles | 0.9095 / 0.9169 / 0.9275 / 0.9379 (all < 0.95; p5 tile 0.9664) | receipt |
| T_ref | 4546.76 s (per-frame ref ~1136.7 s @ 4096 spp) | receipt `per_frame_ref_s` |
| T_stack | 817.36 s = 4 anchors (203.14/202.59/202.94/204.18) + calib O | receipt |
| fixed overhead O | 4.5146 s | `fixed_overhead_s` |
| anchor pixel-trace P (512-cap adaptive+OIDN+guides) | 198.70 s/frame | `mean_keyframe_pixel_trace_s` |
| ref trace rate r | (1136.69 - 4.51)/4096 = 0.2764 s/spp/frame | DERIVED |
| anchor non-trace bundle D (spp-independent) | P - 512*r ~= 57.2 s/frame | DERIVED |
| grading grid | 8x8 = 64 tiles/frame; 4K tiles exactly 480x270 | `_tile_rects` (unit-tested) |
| the gap | <= ~3 tiles/frame below 0.95, ~4-10 shot-wide, concentrated in earlier frames | receipt |

The rejected lever the task names — draft 1024 everywhere — models to ~2.8x: it guts the
multiplier while spending 61 of 64 tiles' extra samples where nothing is broken.

## 1. Selector — DECISION: two-independent-draft divergence (reference-free)

Render a second cheap draft **B** per frame: `selection_draft_spp` (default 64), adaptive
OFF, denoiser OFF (raw MC — exactly the `noisy_b` recipe of `exp_mint_denoise_pairs.py`),
seed = `seed + selection_seed_offset` (default 7919), same deterministic camera path.
Per-tile score on the grading grid, same `_tone()`, same `_tile_rects()`:

```
score[gy,gx] = 1 - SSIM(tone(delivered_A)[tile], tone(B_raw)[tile])
```

Noise2Noise logic: A (delivered 512-cap + OIDN) and B (64 spp raw) are independent
estimates of the same signal; the tiles where they disagree most are the highest-variance
tiles — precisely the ones that defeated 512-cap adaptive + OIDN (dark-GI/glossy tiles).

- **Why B is raw, not denoised:** the anchor's non-trace bundle D ~= 57 s/frame at 4K is
  spp-independent; a denoised selection draft would cost ~O + 64r + D ~= 79 s/frame
  (selection alone would eat the multiplier). Raw B costs ~O + 64r ~= 22 s (charged
  conservatively at (64/512)*P + O = 29.4 s in the model).
- **Known limitation, named:** raw-vs-denoised divergence detects VARIANCE, not a shared
  denoiser BIAS — a tile where OIDN hallucinates identically across seeds escapes. The
  post-hoc `selector_recall` (measurement-only) quantifies this every run.
- **Cycles adaptive error estimates:** the per-pixel adaptive error buffer is not exposed
  as a pass in Blender 4.2; the Debug Sample Count pass saturates at the cap for exactly
  the tiles we care about and is blind to post-denoise error. Logged-signal follow-up
  only; not load-bearing in v1.
- **Reference-freedom is STRUCTURAL:** the selector is a pure function of
  (delivered, second draft) divergence scores. `true_colors` is never passed into any
  repair-path function; a unit test extracts the PASS 3.5 source block and asserts the
  reference is never touched between the BEGIN/END markers, and that no repair helper
  even accepts a reference argument. SSIM-vs-reference stays measurement-only, computed
  AFTER delivery in the grading block (untouched).

## 2. Repair policy

- **Selection rule:** rank all (frame, tile) pairs GLOBALLY by divergence; repair the top
  `repair_top_k` (default 12, shot-wide) subject to `repair_max_per_frame` (default 8)
  and an optional floor `repair_min_divergence` (default 0.0 = rank-only; zero-divergence
  tiles are never selected). The measured need is ~4-10 failing tiles shot-wide and
  concentrated in earlier frames — a global budget puts repairs where they are needed and
  keeps cost bounded and deterministic (ties break on (frame, gy, gx)).
- **Repair render:** the SAME anchor stack (adaptive + OIDN + guides + light-tree) at
  `repair_spp` = `repair_spp_multiplier * draft_spp` (default 4x -> 2048 at 4K; explicit
  `repair_spp` overrides) AND `repair_adaptive_threshold` = `adaptive_threshold / 2`
  (default 0 -> halved). The threshold halving is LOAD-BEARING: pixels that converged at
  thr=0.02 take no more samples just because the cap rose — cap 4x + thr/2 guarantees
  real extra samples on the failed tile.
- **Mechanics — real border renders, not modeled crops:** Blender render border
  (`use_border=True`, `use_crop_to_border=False` so the EXR stays full-res; untouched
  pixels black). ONE subprocess per frame loops all that frame's regions in-session
  (.blend load + BVH paid once per frame — the amortization the crop-model comment
  already predicts). Every second is REAL wall-clock. The repair pass adds NO modeled
  step: in kf=1 all-anchor mode `modeled` stays false.
- **Tile grid = grading grid, 1:1:** the tile-rect iterator was factored out of
  `compute_ssim_global_and_tiles` into `_tile_rects(h, w, grid)` (behavior-identical,
  unit-tested for parity incl. remainder rows/cols) and is used for scoring, selection
  and repair rects. A repaired tile maps exactly onto a graded tile by construction.
- **Seams — margin + outward feather:** each selected tile is rendered with
  `repair_margin_px` (default 16). Composite `delivered' = (1-a)*delivered + a*repair`
  with a=1 over the ENTIRE graded tile plus the innermost margin-feather px, ramping
  linearly 1->0 across the outer `repair_feather_px` (default 12) of the margin
  (Chebyshev distance -> continuous everywhere, max step 1/feather). Consequences:
  (i) the graded tile receives PURE repair pixels — its score is the repair render's
  score; (ii) the transition band lives in NEIGHBOR tiles as a convex blend of accepted
  pixels with strictly-better pixels — no hard edge exists anywhere; (iii) adjacent
  selected tiles are UNIONED into one region before rendering (no interior ramps, no
  double render; merged borders are pairwise disjoint, unit-tested); margins clamp at
  frame edges. Residual risks named: OIDN on a bordered region lacks full-frame context
  near the crop edge (the feather band discounts exactly those pixels; margin 32 is the
  escalation), and neighbor-tile SSIM can move slightly — the receipt reports per-tile
  scores before AND after repair so any neighbor regression is visible in the ledger.
- **Y-flip hazard, named + tested:** numpy rects are top-left-origin; Blender's border is
  normalized bottom-left-origin — `border_min_y = (RES_Y - y1)/RES_Y`,
  `border_max_y = (RES_Y - y0)/RES_Y`. `numpy_rect_to_blender_border()` mirrors the
  embedded-script math exactly; a unit test round-trips random rects and greps the
  embedded script for the exact formulas (lockstep guard).
- **Seed:** repair keeps the anchor seed (`repair_seed_offset` default 0) — noise-field
  continuity is moot post-feather.
- **Escalation/kill:** v1 does ONE repair pass. If post-hoc grading shows a repaired tile
  still < 0.95, the receipt prunes exactly as today (gate untouched) and the ledger
  carries the divergence rank of every failing tile — the honest tuning signal. No
  silent retry loops.

## 3. Integration points (as implemented; default OFF => legacy receipts byte-identical)

1. **Params** (all in the runner's JSON config): `repair_enabled` (False), `repair_selector`
   ("two_draft", only v1 value — anything else fails loudly), `repair_top_k` (12),
   `repair_max_per_frame` (8), `repair_min_divergence` (0.0), `repair_spp_multiplier`
   (4.0), `repair_spp` (0 => multiplier), `repair_adaptive_threshold` (0 => thr/2),
   `selection_draft_spp` (64), `selection_seed_offset` (7919), `repair_seed_offset` (0),
   `repair_margin_px` (16), `repair_feather_px` (12, clamped <= margin). The grid is not
   a param: repair uses `GRADING_TILE_GRID` (8), the same constant the grading call uses.
2. **`BLENDER_SCENE_SCRIPT`:** optional `CX_BORDERS` env (JSON list of pixel rects
   `[x0,y0,x1,y1)` in numpy top-left convention) + `CX_OUT_PATTERN` (contains
   `{region}`). Present: loop regions in-session with a real render border, emit
   `CX_RENDER_DONE_REGION=<i>` sentinels + final `CX_RENDER_DONE`. Absent: byte-identical
   legacy behavior.
3. **`run_blender_frame`:** new kwargs `borders=None, out_pattern=None`; bordered mode
   returns the LIST of resolved per-region EXR paths. All existing call sites unchanged.
4. **Pure helpers** (unit-testable without Blender): `_tile_rects`,
   `tile_divergence_scores`, `select_repair_tiles`, `merge_and_margin_rects`,
   `build_feather_alpha`, `feather_composite`, `numpy_rect_to_blender_border`,
   `per_tile_ssim_map` (measurement-only).
5. **PASS 3.5 — REPAIR** sits between PASS 3 and the SSIM grading block, wrapped in
   BEGIN/END markers a unit test greps to prove the reference is never read there.
   Pre-repair frame copies are kept for post-hoc grading (bookkeeping, not charged).
6. **Metrics** (emitted ONLY when `repair_enabled` — a default run's metrics are
   byte-identical to the legacy runner, proven three ways below): the full param echo,
   `selection_cost_s`, `repair_cost_s`, `repair_total_s` (sum; ALREADY inside `T_stack_s`,
   reported for decomposition, never re-added), `per_frame_selection_draft_s`,
   `per_frame_selection_scoring_s`, `per_frame_repair_render_s`,
   `per_frame_repair_composite_s`, `repaired_tile_indices` (per-frame [gy,gx] lists),
   `repaired_tile_count`, `selector_scores` (per-frame max/p95 + the selected list with
   divergences), and the measurement-only post-hoc fields
   `per_frame_worst_tile_ssim_pre_repair`, `selector_recall` (fraction of tiles grading
   <0.95 pre-repair that the selector caught), `repaired_tile_ssim_after` (pre/post SSIM
   per repaired tile). `modeled` logic unchanged — repair contributes no modeled term.
7. **Adapter** (`from_stack_metrics`): when `repair_total_s` is present, `repair_cost` is
   the REAL measured `repair_total_s` (replacing the `fixed_overhead_s` lower-bound
   stand-in), `draft_cost = T_stack - repair_total_s`, `units = frames*64`,
   `repaired_units = repaired_tile_count` — so the SpecEngine accepted/repaired fractions
   are real per-tile counts. `total_product_time` stays == T_stack (no double charge).
   The no-repair path is byte-identical to the pre-change adapter.
8. **Driver:** intentionally NOT wired this wave (design + local only). The next cloud
   run adds a `repair_enabled: true` job variant to
   `run_integrated_production_benchmark.py` as a pass-through knob.

## 4. Honest-accounting spec

Charged to T_stack, all real measured wall-clock: the selection-draft subprocess (incl.
its O + EXR write), divergence-scoring numpy time (an in-pipeline DECISION, so it is
pipeline cost — unlike grading SSIM), the bordered repair subprocesses (incl. one O per
frame), and the feather-composite numpy time (incl. reading the repair EXRs). NOT
charged: all SSIM-vs-reference grading (unchanged, measurement-only, runs after
delivery) and the pre-repair-copy bookkeeping. `net_speedup = T_ref / T_stack` stays the
ONE ratio; no per-stage product anywhere. The selection draft is charged even when it
results in zero repairs (unit-tested).

## 5. The arithmetic — every row MODELED (from RUN 3 measured constants; only a real run makes them numbers)

Cost models: conservative charges everything at the anchor rate (tile @ s spp:
`1.193*(s/512)*P/64` => 14.82 s/tile @2048); central charges trace at the ref rate +
area-scaled fixed bundle (`1.193*(s*0.2764 + 57.2)/64` => 11.62 s/tile @2048). Selection
29.35 s/frame (conservative (64/512)*P + O), scoring 5 s/frame, composite 0.5 s/frame,
one repair-O per frame. Fixed overhead of enabling the loop = 157.5 s/shot => ceiling
**4.66x even at zero repairs**.

Uniform per-frame K of 64 tiles @ 4x spp (T_stack' = 817.4 + 157.5 + 4*K*c):

| K/frame | conservative | central |
|---|---|---|
| 4 | 1211.9 s -> 3.75x | 1160.7 s -> 3.92x |
| 8 | 1449.0 s -> 3.14x | 1346.6 s -> 3.38x |
| 12 | 1686.0 s -> 2.70x | 1532.5 s -> 2.97x |

**Honest finding: uniform per-frame K in {8,12} does NOT stay >4x.** What does — and why
the GLOBAL budget is the default:

| shot budget B (global rank) | conservative | central |
|---|---|---|
| 8 | 1093.4 s -> 4.16x | 1067.8 s -> 4.26x |
| 12 (default) | 1152.7 s -> 3.94x | 1114.3 s -> 4.08x |
| 12 with selection_draft_spp=32 | 1103.0 s -> 4.12x | — |

So **B=8-12 holds >4x (4.1-4.3x central; conservative B=12 sits at 3.94x, recovered to
4.12x by the selection_draft_spp=32 lever)** — with 3-8x coverage margin over the
measured failing-tile count. Quality expectation (MODELED, weakest link, named): 4x cap
+ thr/2 + OIDN roughly halves residual noise std; worst tile 0.9095 -> expected >= 0.95,
but the divergence -> post-repair-SSIM mapping is UNPROVEN until measured — if a repaired
tile still fails, the receipt prunes exactly as today. Margin-32 escalation priced:
B=12 central drops to ~3.99x.

## 6. Local validation (this wave — REAL results, all $0)

- `python3 -m py_compile` passes on the runner + adapter + test file; both embedded
  Blender scripts (`BLENDER_SCENE_SCRIPT`, `GPU_PROBE_SCRIPT`) extract and `compile()`
  clean.
- `test_render_repair_loop.py`: **24/24 pass** — tile-rect parity (4K/1080p/remainder),
  planted-high-variance selector ranking, budget/per-frame-cap/floor/tie-break policy,
  structural reference-freedom (source-block grep + helper signatures + repair-before-
  grading ordering), feather alpha properties (a==1 on the whole graded tile, 0 outside
  the border, continuity <= 1/feather), seam-safe composite (finite, bounded jumps,
  neighbor tiles within noise epsilon), region merge/clamp/disjointness, border Y-flip
  round-trip + embedded-script formula lockstep, stubbed end-to-end accounting (T_stack
  equals the exact sum of every charged stage; selection charged at zero repairs;
  repaired tiles come from the planted hot set; post-repair tile SSIM improves; derived
  knobs resolve to 4x cap + thr/2), and the adapter repair path (real tile fractions, no
  double charge) + unchanged legacy path.
- Byte-identity proven against the actual pre-change file: the pre-change snapshot and
  the new runner, stub-run with identical deterministic fakes across three configs
  (kf=1 rerender, kf=2 nearest fully-measured, kf=2 rerender exercising the modeled
  crop), emit **byte-identical** metrics JSON with repair off. The committed test also
  pins the exact legacy key set and `{}` == `{"repair_enabled": false}`.
- Full spec-lab suite: no regressions (see the test run recorded with this wave).

## 7. Risks, named

- Shared OIDN bias invisible to the selector (measured post-hoc via `selector_recall`
  every run).
- Adaptive-threshold binding (mitigated by thr/2; the receipt's `repair_adaptive_threshold`
  echoes the effective value).
- OIDN border-context at crop edges (feather discounts exactly those pixels; margin 32 is
  the priced escalation).
- Selection-draft cost-model uncertainty (the conservative/central band brackets it; the
  real run measures it — every charged second is wall-clock, so the receipt cannot
  flatter the model).
- The divergence -> post-repair-SSIM mapping is unproven until the next cloud run; the
  strict gate stays untouched, so a miss self-prunes honestly.

No gate, money-safety path (`require_gpu`, functional GPU probe, final CPU gate), or
grading path was weakened; the new renders flow through `run_blender_frame`, so every
guard applies to them automatically. Writes limited to `scripts/spec-lab/` and
`docs/research/`.
