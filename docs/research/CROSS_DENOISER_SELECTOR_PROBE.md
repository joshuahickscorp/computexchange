# Cross-Denoiser Bias-Detector Probe — selector candidate #2 for the repair loop (2026-07-10)

Status: BUILT + LOCALLY VERIFIED (design + local build; zero cloud, zero money).
The cloud run is fired separately by the orchestrator — ONE driver at a time,
per the standing `.tracked_pods.json` rule. Nothing in this doc is a measured
cloud result yet; every number below is labeled.

Code:    `scripts/spec-lab/pod/exp_cross_denoiser_probe.py` (pod runner)
Driver:  `scripts/spec-lab/run_cross_denoiser_probe.py` (money-safe, `--dry-run` supported)
Tests:   `scripts/spec-lab/test_cross_denoiser_probe.py` (30 tests, all local/synthetic)
Ledger:  `docs/speed-lane-reports/spec-lab/cross_denoiser_probe_ledger.jsonl`

## 1. Why this probe exists (the RUN 4 decisive negative)

The integrated 4K receipt (RUN 3, A100 SECURE) measured **5.561x @ global 0.9854 /
worst-tile 0.9095** — pruned under the strict delivery gate (worst-tile >= 0.95).
RUN 4 ($3.90, H100 SECURE) ran the repair loop with the v1 **two-draft-divergence**
selector and produced a decisive negative: the machinery worked perfectly (12 tiles
selected, re-rendered at 2048spp, feather-composited, every second charged) but
**`selector_recall = 0.0`** — worst-tile **unchanged to 4 decimals** (0.9095). The
diagnosis, from the RUN 4 receipt itself:

- Two independent drafts diverge where **VARIANCE** is high. Those tiles were
  already fine (pre-repair 0.969-0.980).
- The TRUE worst tiles are **DENOISER-BIAS-limited**: OIDN's systematic deviation is
  identical across seeds, so seed-to-seed divergence is structurally blind to it.
  Repaired tiles saturated ~0.98 even at 2048spp+OIDN — same denoiser, same bias.

Two-draft divergence: measured, kill-rule applied. This probe tests the named next
candidate.

## 2. Hypothesis under test

**OIDN and OptiX have DIFFERENT biases.** Per-tile disagreement between the two
denoised outputs of the SAME underlying render should therefore localize the
bias-limited tiles that variance divergence missed: where both denoisers agree, the
output is (with high probability) either converged or share-limited; where they
disagree, at least one denoiser is deviating from the signal.

Falsifiable prediction: `recall@k(D, E_oidn)` for k=1/4/12 lands materially above
RUN 4's 0.0, with positive Spearman(D, E_oidn). If OIDN and OptiX biases COINCIDE
on the hard tiles, recall stays ~0 — an honest negative that kills this selector
too (the blind spot is reproduced synthetically in
`TestConstructedBiasCaseEndToEnd.test_variance_blind_spot_reproduced`).

## 3. Design (one frame, three real renders, same box)

Scene/camera: classroom @ 3840x2160, **frame 1** of the SAME deterministic 4-frame
camera path as `exp_render_stack.py` (the probe imports that runner's scene/camera/
bootstrap machinery — same `BLENDER_SCENE_SCRIPT`, same keyframe math — so the frame
is pixel-comparable to the RUN 3/4 receipts, whose per-frame worst tile is frame 1).

| render | config |
|---|---|
| (a) anchor/OIDN | the exact RUN 3 anchor: draft 512spp cap, adaptive thr 0.02 (min 16), OIDN + albedo/normal prefiltered guides + light-tree, seed 0 |
| (b) anchor/OptiX | IDENTICAL to (a), denoiser=OPTIX, **SAME SEED** — deterministic same-seed Cycles ⇒ same underlying noisy estimate; outputs differ only by denoiser |
| (c) reference | 4096spp fixed, adaptive OFF, denoise OFF (the integrated runner's exact ground-truth recipe) |

Render order is (a), (b), (c) — fail-fast: a missing OptiX denoiser or GPU problem
aborts (loudly, via the stack runner's `CX_DENOISER_UNAVAILABLE` /
`CX_DEVICE_ERROR` guards) BEFORE the ~19-min reference is paid for.

Metrics on the 8x8 grading grid (**exactly** the `compute_ssim_global_and_tiles`
tiling — same `_tone()`, same `_tile_rects()`, computed via the stack runner's
`per_tile_ssim_map`):

```
D[tile]       = 1 - SSIM(oidn, optix)     # the reference-free selector signal
E_oidn[tile]  = 1 - SSIM(oidn, ref)       # the true error of the delivered output
E_optix[tile] = 1 - SSIM(optix, ref)
recall@k      = |top-k by D  ∩  top-k by E_oidn| / k        (k = 1, 4, 12)
spearman      = rank correlation (average-rank ties) of D vs E_oidn over 64 tiles
```

Final stdout line = ONE JSON object: `recall_at_1/4/12` (+ `recall_k_eff`),
`spearman_D_vs_E_oidn`, `spearman_D_vs_E_optix`, the full 64-tile `D_tiles` /
`E_oidn_tiles` / `E_optix_tiles` arrays (row-major, NaN→null), worst-tile
identities (`top_tile_by_D`, `worst_tile_by_E_oidn`, `worst_tile_by_E_optix`,
`top12_by_*`), per-render wall times, device string, global/worst/p5 SSIM context
for both denoisers, `label: "MEASURED"`, `modeled: false`, and a full note.
On ANY failure: `{"error": ...}` as the last stdout line, exit 0 — never fabricated.

## 4. Honesty ledger

- MEASURED (when the cloud run lands): D, E_oidn, E_optix, recall@k, Spearman, all
  wall times, all SSIM context. Real pixels, real scikit-image, real wall-clock.
- MODELED (named in the receipt note, NOT measured by this probe): the claim that a
  production selector gets D at ~zero extra render cost by denoising ONE noisy
  render twice (OIDN + OptiX). This probe pays a second full anchor render (~200s at
  4K) only because the runner denoises in-pipeline; the wall times honestly include it.
- Assumption, named: same-seed Cycles determinism makes render (b) the same noisy
  estimate as (a). Residual GPU trace nondeterminism, if any, adds variance-like
  disagreement to D — it can dilute but not erase a bias signal, and it would push
  recall DOWN, not up (conservative direction).
- Statistical honesty: `recall` is None (never 0 or 1) when nothing is rankable;
  Spearman is None on constant input; ranking is restricted to tiles finite in BOTH
  D and E (all unit-tested).

## 5. Money-safety (driver = the adversarially-verified integrated-driver standard)

- `register_cleanup()` before any provision; preflight REFUSES if any tracked or
  live pod exists (one driver at a time) or balance ≤ floor.
- `arm_remote_watchdog(pod, 10800)` is the FIRST action on the pod — a 3h
  self-terminate backstop that survives this process dying.
- GPU plan: the policy ladder `A100 PCIe C/S → H100 HBM3 C/S → H200 C/S`
  (upgrade-never-downgrade); Blackwell rejected via the integrated driver's
  `parse_gpu_plan` (imported, single source of truth for the marker list).
- `gpu_probe_timeout_s = 1500` (sm_90 first-render JIT headroom, same value and
  rationale as the integrated driver); the functional 64x64@1spp GPU probe with CPU
  devices disabled gates everything expensive.
- The probe command runs via `runpod.ssh_detached` (detached-on-pod + short
  reconnecting polls — the post-incident pattern; this run's ~35-45 min is exactly
  the band where a synchronous SSH already died once).
- Teardown in `finally` + a post-teardown `_load_tracked() == []` assertion; every
  outcome (preflight, provision-prune, result, error) is ledgered to
  `cross_denoiser_probe_ledger.jsonl` BEFORE any raise.
- `--dry-run` prints the exact manifest and exits without touching the API.

Estimated spend (MODELED from RUN 3 measured per-frame times): reference ~1137s +
two anchors ~2x203s + probe/setup ≈ 35-45 GPU-minutes ≈ **$0.75-1.20** on the A100
rung (more if an H100 rung pays the one-time JIT; watchdog caps exposure at 3h).

## 6. Decision rule for the cloud result (pre-registered)

- `recall@12 >= ~0.5` with positive Spearman → wire a `cross_denoiser` selector
  into the repair loop (PASS 3.5) as a `repair_selector` variant and re-run the
  RUN 4 config; production form = one noisy render denoised twice.
- `recall` ~0 again → OIDN and OptiX biases coincide on the hard tiles; this
  selector branch dies too (record the kill), and the honest next levers are
  reference-anchored selection (e.g. sparse high-spp probe tiles) or a higher
  draft-spp floor on the historically-bad tile band.
- Middle ground (recall@12 ~0.2-0.4) → selector is signal but not sufficient alone;
  consider D combined with the two-draft variance score (union budget), priced
  before any run.

## 7. Local validation (this wave — REAL outputs, $0)

- `python3 -m py_compile` passes on all three new files.
- `test_cross_denoiser_probe.py`: **30/30 pass** (`Ran 30 tests in 0.280s / OK`) —
  top-k determinism + NaN handling; recall math incl. a constructed bias tile
  ranked first by D (recall@1=1.0), the RUN 4 failure mode scoring exactly 0.0,
  partial overlap, k-clamping via `k_eff`, shared-finite-universe ranking, k<1 and
  length-mismatch raising; Spearman +1/-1/tie-average (hand-computed
  1.5/sqrt(3))/constant→None/NaN-drop; tile dissimilarity on the real SSIM path
  (identical→0, planted bias tile = argmax, grid = `_tile_rects`); the end-to-end
  constructed bias case AND the shared-bias blind spot; the full JSON contract
  (64-length arrays, NaN→null, no bare `NaN` tokens, worst-tile identities,
  MEASURED label, honest zero-recall carriage).
- Driver `--dry-run`: prints the exact manifest (RUN 3 anchor knobs, frame 1/4,
  policy GPU ladder, timeout 7200s, watchdog 10800s, ledger path) and exits;
  `.tracked_pods.json` remains `[]`.
- Pod runner error contract exercised locally: bad resolution and out-of-range
  frame both emit `{"error": ...}` as the final stdout line and exit 0, before any
  download/render.
- No regressions: `test_render_repair_loop.py` 24/24 OK, `test_runpod_safety.py`
  9/9 OK.

Writes limited to the assigned paths. No gate, money-safety guard, or grading path
was modified — the probe's renders flow through the stack runner's
`run_blender_frame`, so every existing guard applies to them automatically.
