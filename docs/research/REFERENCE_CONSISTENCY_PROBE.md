# Reference self-consistency probe

**Question it answers:** is the strict worst-tile delivery gate (`worst-tile SSIM >= 0.95`,
`cx_integrated_speculation.DELIVERY_WORST_TILE`) *reachable at all*, or is it unreachable
by construction because the 4096-spp reference is not converged at its worst tiles?

## Why

The 2026-07-10 decisive finding: repairing the failing corner tiles at **raw 4096 spp**
(denoiser OFF — the reference's own config) only reached SSIM **~0.914** vs the reference.
That means two reference-quality renders of the same tile disagree by **~0.086**.

**Hypothesis:** the 4096-spp reference itself is *not converged* at these high-variance
frame-edge tiles. If a delivery render is graded against a still-noisy target, then
`worst-tile 0.95` is unreachable *by construction* on that tile — you are measuring noise
against noise, and the ~0.086 gap is the reference's own Monte-Carlo noise floor rather
than a deficit a better delivery render could close.

This probe measures that self-disagreement **directly**.

## What it does

Two real renders of **classroom frame 1** at **3840x2160**, on one GPU, with a
**byte-identical reference config** that differs **only in seed**:

| knob | value | source |
|---|---|---|
| samples per pixel | 4096 (fixed) | `ref_spp` |
| adaptive sampling | **OFF** | forced by `is_ref=True` (`cyc.use_adaptive_sampling = False`) |
| denoiser | **OFF** | forced by `is_ref=True` (`cyc.use_denoising = False`) |
| guides (albedo/normal) | **OFF** | denoiser block is `if (not IS_REF)` — never runs for a reference |
| light-tree | **scene .blend default (untouched)** | light-tree block is `if (not IS_REF) and LIGHTTREE` — skipped for a reference |
| camera path | dolly+rise+yaw over `nframes=4` | the shared `BLENDER_SCENE_SCRIPT` |
| resolution / frame / bounces / cam_motion | 3840x2160 / 1 / 12 / 1.0 | config |
| seed | **A=0, B=12345** (the ONLY difference) | `seed_a`, `seed_b` |

It then scores, on the **same 8x8 grading grid** the delivery gate uses
(`compute_ssim_global_and_tiles` / `per_tile_ssim_map`, same `_tone()` / `_tile_rects()`):

- **per-tile SSIM(reference_A, reference_B)** — the reference's self-consistency, all 64 tiles
- **global / worst-tile / p5-tile SSIM(A, B)**
- the identity of the **worst self-consistency tile** (lowest A-vs-B SSIM)
- **`gate_reachable = (worst_tile_ref_vs_ref >= 0.95)`** + an honest interpretation string

### The exact reference config matches `exp_render_stack.py`

The probe renders each frame with `ers.run_blender_frame(..., is_ref=True)` and, exactly as
the stack runner's reference call (`exp_render_stack.py` lines ~1838-1844) does, **omits**
the `adaptive`/`denoiser`/`guides`/`light_tree` knobs so the defaults apply. With
`is_ref=True` the embedded scene script:

- forces `use_adaptive_sampling = False` and `use_denoising = False`, and
- **never touches `use_light_tree`** (its anchor-only enable is gated `if (not IS_REF)`).

So reference_A and reference_B are byte-for-byte the stack runner's reference recipe, and
they leave `use_light_tree` at the scene's `.blend` default **for both** — identical, which
is all a self-consistency comparison requires. The probe builds **one** shared kwargs dict
and calls `assert_seed_only_diff(cfg_a, cfg_b)` before spending anything; it raises if the
two configs differ in any key but `seed`, or if the seeds are equal (which would trivially
score SSIM ~1.0).

## How to read the result

- **`gate_reachable = false`** (worst-tile A-vs-B SSIM `< 0.95`): the reference disagrees
  with itself beyond the delivery tolerance at its worst tile → the strict `0.95` worst-tile
  gate is **unreachable by construction** there. This corroborates the ~0.086 raw-4096-spp
  self-disagreement — the gap is the reference's noise floor, not a closeable deficit.
- **`gate_reachable = true`**: the reference is self-consistent to within the gate even at
  its noisiest tile → residual reference noise does **not** rule out the gate; the
  strict-gate gap is a real delivery deficit, and a delivery render that truly matched the
  reference could in principle clear it.

The `p5_tile_ref_vs_ref` (5th-percentile tile) shows whether a below-gate reading is one
pathological corner or a broad edge-tile band.

## Files

- `scripts/spec-lab/pod/exp_reference_consistency_probe.py` — pod runner. Reuses
  `exp_render_stack` (Blender bootstrap, scene cache, deterministic camera path, fail-loud
  `require_gpu_probe`, `run_blender_frame`, EXR reader, grading tiling + SSIM) and
  `exp_cross_denoiser_probe` (tile-flatten / top-k math) verbatim.
- `scripts/spec-lab/run_reference_consistency_probe.py` — money-safe driver.
- `scripts/spec-lab/test_reference_consistency_probe.py` — local, synthetic, no-GPU unit
  tests (seed-only-diff guard, gate logic + single-source gate literal, grading-grid
  self-consistency, JSON contract, end-to-end constructed below-gate case).
- Ledger: `docs/speed-lane-reports/spec-lab/reference_consistency_ledger.jsonl`.

## Money-safety

Same adversarially-verified standard as `run_integrated_production_benchmark.py` /
`run_cross_denoiser_probe.py`: one driver at a time (preflight refuses any tracked/live
pod), `register_cleanup()` before any provision, `arm_remote_watchdog()` as the first pod
action, the policy GPU ladder **A100 C/S → H100 C/S → H200 C/S** (upgrade-never-downgrade,
Blackwell rejected — no Blender 4.2 kernel), the render detached-on-pod
(`runpod.ssh_detached`) so a peer reset can't kill it, `gpu_probe_timeout_s=1500` for sm_90
first-render JIT headroom, teardown in `finally` + a post-teardown tracked-pods-empty
assertion, and `--dry-run`. Honest error contract throughout: any failure emits a single
`{"error": ...}` JSON line and exits 0 — never a fabricated number.

Estimated spend: **~$0.75–1.20** (about 35–45 GPU-minutes on an A100 for two reference
renders; more if the H100 rung pays the one-time sm_90 probe JIT).

## Fire command

```
python3 scripts/spec-lab/run_reference_consistency_probe.py
```

Inspect first without touching the API:

```
python3 scripts/spec-lab/run_reference_consistency_probe.py --dry-run
```
