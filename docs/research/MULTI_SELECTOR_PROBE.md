# Multi-Selector Reference-Free Tile-Repair Probe (2026-07-10)

Status: BUILT + LOCALLY VERIFIED (unit tests + py_compile + driver `--dry-run`). Cloud
run PENDING (one money-safe driver, fired separately — LOCAL ONLY here, no pod
provisioned). This doc is the design of record for the third — and intentionally
exhaustive — reference-free tile-selector experiment.

## Why this exists

The banked product number is **RUN 3: 5.561x @ global 0.9854 / worst-tile 0.9095** at
4K (classroom, 4 frames, ref 4096 / draft 512, keyframe_every=1). It clears the
historically-published 0.95 tier (g≥0.95, wt≥0.90) but **misses the STRICT delivery
gate** (g≥0.98 ✓, wt≥0.95 ✗). The whole question is whether a **reference-free**
per-tile selector can point the repair loop at the worst-tile so a cheap targeted
re-render lifts 0.91 → 0.95.

Two reference-free selectors are already **measured-dead** against that gap:

| selector | result | what it proved |
|----------|--------|----------------|
| two-draft VARIANCE divergence (RUN 4) | `selector_recall = 0.0` | the failing tiles are NOT variance-limited; two seeds share the denoiser's bias, so seed-to-seed divergence is blind to it |
| cross-denoiser DISAGREEMENT (OIDN vs OptiX, probe 2) | recall@12 ≈ 0.083, Spearman ≈ **−0.02** | the denoisers AGREE (max D 0.007 vs worst error 0.091) and are both ~equally wrong; the miss is a **SHARED denoiser bias** on hard motion-reveal edge/corner content, not denoiser-specific |

Both point to the same diagnosis: the strict-gate miss is a **shared denoiser bias on
hard edge/corner content**, concentrated at motion-reveal frame edges/corners — not
variance, not a denoiser-specific artifact. This probe scores the remaining
**principled** reference-free signals side-by-side, from ONE anchor render, to find
which (if any) localizes that bias — or to close the question honestly.

## The four candidate selectors (all reference-free; all HIGH = candidate failing tile)

| id | signal | source | hypothesis |
|----|--------|--------|-----------|
| **S1** SAMPLE_COUNT | per-tile mean of the Cycles **adaptive per-pixel sample-count** pass of the delivered anchor | `view_layer.cycles.use_pass_debug_sample_count` (defensive; may be UNAVAILABLE) | the renderer's OWN convergence-difficulty map. If the corners are under-converged, it localizes them; if converged-but-biased, it CONFIRMS the ceiling (the strongest untested candidate) |
| **S2** DENOISER_RESIDUAL | per-tile mean `|tone(noisy) − tone(oidn)|` (tonemapped space) | an extra denoiser-OFF same-seed render | where the denoiser did the MOST work is where it is most likely to have erred |
| **S3** CONTENT_GRADIENT | per-tile mean gradient magnitude of the tonemapped delivered frame luma | the delivered anchor color | the shared bias lives on hard high-frequency edge content |
| **S4** AOV_EDGE | per-tile mean normal-gradient magnitude from the Normal AOV | `view_layer.use_pass_normal` | geometric complexity / silhouette-edge density (the motion-reveal edges are geometric) |

None of the four reads the reference. The reference (4096 spp) is used **only** to
MEASURE the true per-tile error `E_oidn = 1 − SSIM(oidn, ref)` that the selectors are
scored against.

## Design — one frame, THREE real renders (same count/cost as probe 2, ~$1.0–1.6)

All on the SAME box, the SAME deterministic camera path `exp_render_stack.py` animates,
classroom **frame 1** (where RUN 3/4's worst tiles live) at **3840×2160**:

1. **ANCHOR / OIDN (delivered)** — the exact RUN 3 anchor stack: draft 512 spp cap,
   adaptive threshold 0.02 (min 16), OIDN + albedo/normal prefiltered guides +
   light-tree. Carries the **Normal (S4)** and **Debug Sample Count (S1)** AOV passes in
   the same multilayer EXR at ~zero extra cost.
2. **NOISY (denoiser OFF)** — identical sampling (same seed / spp / adaptive threshold /
   light-tree) with the denoiser OFF. Deterministic same-seed Cycles ⇒ the same
   underlying noisy estimate as the anchor's pre-denoise buffer, so `|noisy − oidn|` is
   the denoiser's per-pixel work (S2).
3. **REFERENCE (ground truth)** — 4096 spp fixed, adaptive OFF, denoise OFF — the
   integrated runner's exact recipe. Used ONLY to measure `E_oidn`.

Cheap renders run FIRST (fail-fast: a missing GPU / OIDN / debug pass aborts before the
expensive reference is paid for).

### Metrics (on the 8×8 grading grid — same `_tone()` / `_tile_rects()` as delivery grading)

For each **available** selector S, over the 64 tiles:
- `recall@k` for k ∈ {1, 4, 12} = `|top-k(S) ∩ top-k-true-worst(E_oidn)| / k_eff` — the
  direct analogue of RUN 4's `selector_recall`.
- `spearman(S, E_oidn)` — rank correlation over the tiles.

## Honesty contract

- Human logs → STDERR; the LAST stdout line is exactly ONE JSON object.
- Any failure emits `{"error": ...}` as the last stdout line and exits 0 — never a
  fabricated number.
- An **UNAVAILABLE** selector (e.g. the Blender build/device does not write the debug
  sample-count pass, or OpenEXR is missing so named passes can't be read) is reported
  with `available: false`, a `unavailable_reason`, and **null** metrics — never silently
  0 or 1. Availability is gated on the pass **channel actually being present in the
  EXR**, not merely on the attribute being settable.
- Everything reported — E, every selector value, recall, Spearman, wall times — is
  MEASURED on real pixels. `modeled: false`. The ONE named assumption (not a "modeled"
  crop term): S2's noisy render is a SEPARATE same-seed render standing in for the
  anchor's pre-denoise buffer (deterministic Cycles). Any residual GPU-trace
  nondeterminism only adds variance-like residual; it cannot erase the signal.
- All GPU / denoiser / CPU-fallback fail-loud guards are **reused verbatim** from
  `exp_render_stack.run_blender_frame` / `require_gpu_probe` (no CPU-fallback receipt).

## Emitted JSON (last stdout line)

```jsonc
{
  "probe": "multi_selector", "label": "MEASURED", "modeled": false,
  "grid": 8, "n_tiles": 64, "n_valid_E_tiles": 64,
  "E_oidn_tiles": [ ...64 floats/null... ],
  "worst_tile_by_E_oidn": {"tile": [gy, gx], "E": 0.09...},
  "top12_by_E_oidn": [ {"tile": [..], "E": ..}, ... ],
  "selector_order": ["S1_sample_count","S2_denoiser_residual","S3_content_gradient","S4_aov_edge"],
  "available_selectors": ["S2_denoiser_residual", ...],
  "selectors": {
    "S1_sample_count": {
      "available": true|false, "unavailable_reason": null|"...",
      "recall_at_1": .., "recall_at_4": .., "recall_at_12": ..,
      "recall_k_eff": {"1": .., "4": .., "12": ..},
      "spearman_vs_E_oidn": ..,
      "n_valid_tiles": 64,
      "tiles": [ ...64 floats/null... ],
      "top_tile": {"tile": [gy, gx], "score": ..},
      "top12": [ {"tile": [..], "score": ..}, ... ]
    },
    "S2_denoiser_residual": { ... }, "S3_content_gradient": { ... }, "S4_aov_edge": { ... }
  },
  "wall_anchor_oidn_s": .., "wall_noisy_s": .., "wall_ref_s": ..,
  "device": "GPU/CUDA", "sample_count_channel": "...|null",
  "ssim_global_oidn_vs_ref": .., "ssim_worst_tile_oidn_vs_ref": .., "ssim_p5_tile_oidn_vs_ref": ..,
  "scoring_s": .., "note": "...", /* + params echo */
}
```

## Decision rule (what the number means)

- A selector is a **useful localizer** only if it materially beats the two dead
  baselines — concretely a `recall@12` well above the cross-denoiser 0.083 **and** a
  positive Spearman (the sign matters: −0.02 was orthogonal). recall@4/@1 lifting off 0
  is the strong signal (repair budgets are small: RUN 4 repaired top-12 shot-wide).
- **If S1 (sample-count) localizes** the worst tiles → the corners are *under-converged*
  and a higher-sample anchor on exactly those tiles is the honest repair lever (costs
  some multiplier, but targeted). This is the outcome that could still reach strict
  delivery reference-free.
- **If S1 is high on the worst tiles but the residual/gradient say "converged"**, or if
  every selector's recall is uniformly weak → the diagnosis is confirmed: the miss is a
  **converged-but-biased** shared denoiser deviation, and the honest conclusion is
  **definitive**: reference-free tile-repair cannot reach strict delivery on this failure
  mode. Then **5.56x @ global 0.985 / worst-tile 0.91 (clears the published 0.95 tier) is
  the honest ceiling of this lever**, and strict delivery would require either a **better
  denoiser** (re-opening the owned-denoiser thread with a far sharper target: beat OIDN
  on its shared-bias edge tiles ONLY) or a **higher-sample anchor** (which costs the
  multiplier). The product number stands either way — a real negative beats a massaged
  positive.

## Files

- `scripts/spec-lab/pod/exp_multi_selector_probe.py` — the pod runner. Reuses
  `exp_render_stack` (Blender bootstrap / scene cache / deterministic camera / money-safe
  `run_blender_frame` / EXR reader / grading tiling + SSIM) and `exp_cross_denoiser_probe`
  (the unit-tested ranking + correlation math and `tile_dissimilarity`). Its Blender
  scene script is **derived** from the shared one by a single documented injection (the
  two AOV passes + sentinels) so it stays in lockstep; with the AOV env flags unset it is
  behavior-identical to the shared script (so the noisy + reference renders are
  unchanged).
- `scripts/spec-lab/run_multi_selector_probe.py` — the money-safe driver
  (`register_cleanup` → preflight refuses tracked/live pods + balance floor → provision
  on the policy ladder A100 C/S → H100 C/S → H200 C/S → `arm_remote_watchdog` FIRST →
  detached render → teardown in `finally` + tracked-empty assertion). `--dry-run` prints
  the manifest without touching the API. Ledger:
  `docs/speed-lane-reports/spec-lab/multi_selector_probe_ledger.jsonl`.
- `scripts/spec-lab/test_multi_selector_probe.py` — 27 local/synthetic unit tests
  (recall@k + Spearman per selector, the sample-count-first case, recall edge cases,
  unavailable-selector handling, the per-pixel selector fields localizing planted tiles,
  the full JSON contract, and the derived-script superset/lockstep guard).

## How to run (LOCAL verification — no cloud)

```
cd scripts/spec-lab
python3 -m py_compile pod/exp_multi_selector_probe.py run_multi_selector_probe.py test_multi_selector_probe.py
python3 test_multi_selector_probe.py            # 27 tests
python3 run_multi_selector_probe.py --dry-run   # manifest only; no pod
```

The cloud run is fired separately by the orchestrator (ONE money-safe driver at a time —
concurrent drivers share `.tracked_pods.json` and would kill each other's pods).
