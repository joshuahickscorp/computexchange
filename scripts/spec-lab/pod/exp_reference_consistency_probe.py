#!/usr/bin/env python3
"""exp_reference_consistency_probe.py — REFERENCE SELF-CONSISTENCY PROBE (REAL, one frame).
================================================================================

WHY THIS EXISTS (the 2026-07-10 decisive edge-tile finding):
The strict delivery gate is worst-tile SSIM >= 0.95 (cx_integrated_speculation.py
DELIVERY_WORST_TILE). Tonight's result: repairing the failing corner tiles at raw
4096 spp (denoiser OFF = the reference's OWN config) only reached SSIM ~0.914 vs the
reference — i.e. two reference-quality renders of the SAME tile disagree by ~0.086.

HYPOTHESIS UNDER TEST: the 4096-spp reference itself is NOT converged at these
high-variance frame-edge tiles, so worst-tile 0.95 is UNREACHABLE BY CONSTRUCTION
there (you would be scoring a delivery render against a still-noisy target — noise vs
noise). This probe measures that self-disagreement directly.

DESIGN (one frame, TWO renders, one box — identical config, seed the ONLY difference):
  reference_A : classroom FRAME 1 at 3840x2160, 4096 spp, adaptive OFF, denoiser OFF,
                guides OFF, light-tree left at the scene's .blend default — the EXACT
                reference recipe exp_render_stack.py renders (run_blender_frame with
                is_ref=True; the scene script FORCES use_adaptive_sampling=False and
                use_denoising=False, and its anchor-only light-tree block is gated
                `if (not IS_REF)`, so the reference never touches use_light_tree).
                Rendered with seed=seed_a (default 0).
  reference_B : BYTE-IDENTICAL config, seed=seed_b (default 12345). Deterministic Cycles
                means seed is the ONLY thing that changes the Monte-Carlo noise
                realization; every other knob (spp, adaptive, denoiser, guides,
                light-tree, camera keyframes, resolution, frame, bounces) is identical.
                The code builds ONE shared kwargs dict and asserts the two render configs
                differ in NOTHING but `seed` before spending a cent.

METRICS on the 8x8 grading grid (EXACTLY compute_ssim_global_and_tiles' tiling — same
_tone(), same _tile_rects(), via per_tile_ssim_map / compute_ssim_global_and_tiles):
  per-tile SSIM(reference_A, reference_B)   the reference's SELF-CONSISTENCY, 64 tiles
  global SSIM, worst-tile SSIM, p5-tile SSIM between A and B
  the identity of the WORST self-consistency tile (lowest A-vs-B SSIM)
  gate_reachable = (worst_tile_ref_vs_ref >= DELIVERY_WORST_TILE)  + honest interpretation

HONESTY CONTRACT (same as exp_render_stack.py / exp_cross_denoiser_probe.py):
  * Human logs -> STDERR; the LAST stdout line is exactly ONE JSON object.
  * Any failure emits {"error": ...} as the last stdout line and exits 0 — never
    fabricate a number.
  * require_gpu is fail-loud: the functional 64x64@1spp GPU probe (CPU devices disabled)
    gates the run, and every render refuses CPU fallback — all reused verbatim from
    exp_render_stack.py (run_blender_frame keeps its CX_DEVICE_ERROR / require_gpu
    guards). A CPU/unknown device NEVER reaches a receipt.
  * Every SSIM / worst-tile / wall value is MEASURED on real pixels. modeled=false.
  * The comparison is meaningful ONLY because the two renders share an IDENTICAL config
    and differ ONLY in seed; this is asserted in code (assert_seed_only_diff) AND
    guaranteed structurally by is_ref=True (which forces adaptive/denoiser off and skips
    the light-tree lever for BOTH renders).

CONFIG (argv[1] JSON, all optional; defaults = the exp_render_stack.py reference recipe):
  scene="classroom", resolution="3840x2160", frame=1, nframes=4 (camera-path length,
  matching the RUN 3 shot so frame 1 is pixel-comparable to the integrated receipts),
  ref_spp=4096, bounces=12, cam_motion=1.0, seed_a=0, seed_b=12345, device="AUTO",
  require_gpu=false (driver passes true), gpu_probe_timeout_s=300 (driver passes 1500 for
  sm_90 first-render JIT headroom), blender_url=<4.2 LTS>.
"""

import json
import os
import sys
import time

# The stack runner + the cross-denoiser probe both live in this same pod/ directory (the
# driver scp's the whole directory); we REUSE exp_render_stack for the Blender bootstrap /
# scene cache / deterministic camera path / money-safe render driver / EXR reader /
# grading-grid tiling + SSIM, and exp_cross_denoiser_probe for the already-unit-tested
# ranking + tile-flattening math — verbatim, so this probe adds no divergent copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exp_render_stack as ers  # noqa: E402
import exp_cross_denoiser_probe as xdp  # noqa: E402

WORK_DIR = "/tmp/reference_consistency_probe"

# The strict delivery gate this probe tests reachability of. The SINGLE source of truth
# is cx_integrated_speculation.DELIVERY_WORST_TILE (= 0.95); that module lives one
# directory up (scripts/spec-lab/) and is NOT scp'd into pod/, so we mirror the literal
# here and the LOCAL unit test asserts this mirror still equals the real constant.
DELIVERY_WORST_TILE = 0.95


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the JSON line)."""
    print("[reference_consistency_probe]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested locally; no numpy/skimage/Blender needed).          #
# --------------------------------------------------------------------------- #
def assert_seed_only_diff(cfg_a, cfg_b):
    """The comparison is meaningless unless A and B are identical except for the seed.
    Raise if the two render kwargs dicts differ in ANY key other than 'seed' (or if the
    seeds are equal — then A and B would be the SAME render and SSIM would be a trivial
    ~1.0 by construction). Returns the sorted list of differing keys on success."""
    keys = set(cfg_a) | set(cfg_b)
    diffs = sorted(k for k in keys if cfg_a.get(k) != cfg_b.get(k))
    if diffs != ["seed"]:
        raise RuntimeError(
            f"reference_A and reference_B must differ ONLY in 'seed'; differing keys are "
            f"{diffs} (a divergent config would make the self-consistency SSIM meaningless)")
    if cfg_a.get("seed") == cfg_b.get("seed"):
        raise RuntimeError(
            f"seed_a and seed_b are both {cfg_a.get('seed')}; identical seeds render the "
            f"SAME image and SSIM would be a trivial 1.0 — pick two distinct seeds")
    return diffs


def interpret_gate(worst_tile, gate=DELIVERY_WORST_TILE):
    """Return (gate_reachable: bool, interpretation: str) from the measured worst-tile
    A-vs-B self-consistency SSIM. gate_reachable = worst_tile >= gate. PURE — no I/O."""
    reachable = bool(worst_tile >= gate)
    if reachable:
        interpretation = (
            f"REFERENCE IS SELF-CONSISTENT AT THE GATE: the worst-tile SSIM between two "
            f"4096-spp renders of the SAME config (seeds differ only) is {worst_tile:.4f} "
            f">= {gate:.2f}. The reference agrees with itself to within the delivery "
            f"tolerance even at its noisiest tile, so residual reference Monte-Carlo noise "
            f"does NOT rule out the worst-tile {gate:.2f} gate — a delivery render that "
            f"truly matched the reference could in principle clear it; the strict-gate gap "
            f"is a real delivery deficit, not a noisy-target artifact.")
    else:
        interpretation = (
            f"REFERENCE IS NOT CONVERGED AT ITS WORST TILE: two 4096-spp renders of the "
            f"SAME config (seeds differ only) disagree there by 1-SSIM = {1.0 - worst_tile:.4f} "
            f"(worst-tile SSIM {worst_tile:.4f} < {gate:.2f}). The reference does not agree "
            f"with itself to within the delivery tolerance, so the strict worst-tile "
            f"{gate:.2f} gate is UNREACHABLE BY CONSTRUCTION on this tile: a delivery render "
            f"would be scored against a still-noisy target (noise vs noise). This "
            f"corroborates the ~0.086 self-disagreement measured when repairing the failing "
            f"corner tiles at raw 4096 spp — that gap is the reference's own noise floor, "
            f"not a deficit a better delivery render can close.")
    return reachable, interpretation


def probe_metrics(ssim_mat, global_ssim, worst_tile, p5_tile, *, grid, seeds, walls,
                  device, params_echo, note, gate=DELIVERY_WORST_TILE):
    """PURE assembly of the final JSON object from the [grid,grid] per-tile SSIM(A,B)
    matrix (NaN allowed for unscored tiles) + the global/worst/p5 SSIM(A,B) scalars +
    measured wall times + device + seeds. No I/O, no rendering — unit-tested locally on
    synthetic matrices. Reuses xdp.flatten_tiles / xdp.topk_indices / xdp._finite_indices
    (the cross-denoiser probe's already-tested tile math)."""
    ssim_flat = xdp.flatten_tiles(ssim_mat)   # NaN -> None, JSON-safe 64-list
    n_tiles = grid * grid
    if len(ssim_flat) != n_tiles:
        raise ValueError(f"ssim map has {len(ssim_flat)} tiles, expected {n_tiles}")
    finite = xdp._finite_indices(ssim_flat)

    # Worst self-consistency tile = LOWEST SSIM = HIGHEST dissimilarity. Rank on
    # dissimilarity so the reused topk_indices (largest-first, NaN-skipping,
    # deterministic tie-break) yields the argmin-SSIM tile.
    dissim = [(1.0 - v) if v is not None else None for v in ssim_flat]
    top = xdp.topk_indices(dissim, 1)
    worst_self_consistency_tile = (
        {"tile": [int(top[0] // grid), int(top[0] % grid)],
         "ssim": round(float(ssim_flat[top[0]]), 6)}
        if top else None)

    gate_reachable, interpretation = interpret_gate(worst_tile, gate)

    seed_a, seed_b = int(seeds[0]), int(seeds[1])
    metrics = {
        "probe": "reference_self_consistency",
        "label": "MEASURED",
        "hypothesis": (
            "the 4096-spp reference is NOT converged at high-variance frame-edge tiles, "
            "so the strict worst-tile 0.95 gate is unreachable by construction there "
            "(a delivery render is scored against a still-noisy target). Two same-config "
            "reference renders (seeds differ only) measure that self-disagreement directly."
        ),
        "grid": int(grid),
        "n_tiles": int(n_tiles),
        "n_valid_tiles": len(finite),
        "delivery_worst_tile_gate": float(gate),
        # The headline self-consistency numbers (A vs B), all MEASURED.
        "global_ref_vs_ref": round(float(global_ssim), 6),
        "worst_tile_ref_vs_ref": round(float(worst_tile), 6),
        "p5_tile_ref_vs_ref": round(float(p5_tile), 6),
        "ref_vs_ref_tiles": [round(v, 6) if v is not None else None for v in ssim_flat],
        "worst_self_consistency_tile": worst_self_consistency_tile,
        "gate_reachable": bool(gate_reachable),
        "interpretation": interpretation,
        "seed_a": seed_a,
        "seed_b": seed_b,
        "wall_ref_a_s": round(float(walls["ref_a"]), 3),
        "wall_ref_b_s": round(float(walls["ref_b"]), 3),
        "device": device,
        "modeled": False,
        "note": note,
    }
    metrics.update(params_echo)
    return metrics


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    scene_arg = str(params.get("scene", "classroom"))
    resolution = str(params.get("resolution", "3840x2160"))
    frame = int(params.get("frame", 1))
    nframes = int(params.get("nframes", 4))       # RUN 3 shot length -> same camera path
    ref_spp = int(params.get("ref_spp", 4096))
    bounces = int(params.get("bounces", 12))
    cam_motion = float(params.get("cam_motion", 1.0))
    seed_a = int(params.get("seed_a", 0))
    seed_b = int(params.get("seed_b", 12345))
    device_pref = str(params.get("device", "AUTO")).upper()
    require_gpu = bool(params.get("require_gpu", False))
    gpu_probe_timeout_s = int(params.get(
        "gpu_probe_timeout_s", os.environ.get("CX_GPU_PROBE_TIMEOUT_S", 300)))
    blender_url = str(params.get("blender_url", ers.DEFAULT_BLENDER_URL))

    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 3840x2160")

    nframes = max(1, nframes)
    if not (1 <= frame <= nframes):
        raise RuntimeError(f"frame must be in [1, nframes={nframes}], got {frame}")
    ref_spp = max(1, ref_spp)
    bounces = max(1, bounces)
    cam_motion = max(0.0, cam_motion)
    if seed_a == seed_b:
        raise RuntimeError(
            f"seed_a and seed_b must differ (both {seed_a}); identical seeds render the "
            f"SAME image and the self-consistency SSIM would be a trivial 1.0")

    log(f"params: scene={scene_arg} res={res_x}x{res_y} frame={frame}/{nframes} "
        f"ref_spp={ref_spp} bounces={bounces} cam_motion={cam_motion} "
        f"seed_a={seed_a} seed_b={seed_b} device={device_pref} require_gpu={require_gpu}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) bootstrap: libs + Blender + functional GPU gate + scene ---------- #
    # All reused verbatim from the stack runner (same caches, same fail-loud guards).
    # The GPU probe runs BEFORE anything expensive.
    ers.ensure_system_libs()
    ers.ensure_pydeps()
    blender_bin = ers.ensure_blender(blender_url)
    if require_gpu:
        ers.require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)
    blend, scene_key, fallback_note = ers.resolve_scene(scene_arg)

    # The SAME embedded scene script as the stack runner => the SAME deterministic camera
    # keyframes (dolly+rise+yaw over nframes), so this probe's frame 1 is pixel-comparable
    # to the integrated runs' frame 1.
    script_path = os.path.join(WORK_DIR, "cx_refconsistency_scene.py")
    with open(script_path, "w") as f:
        f.write(ers.BLENDER_SCENE_SCRIPT)

    ref_timeout = 3600

    # ---- 1) build ONE shared reference kwargs dict; the two renders differ ONLY in
    # seed. is_ref=True makes the scene script FORCE adaptive OFF + denoiser OFF and SKIP
    # the anchor-only light-tree lever for BOTH renders (the light-tree block is gated
    # `if (not IS_REF)`), so this is byte-for-byte exp_render_stack.py's reference recipe.
    # We DON'T pass adaptive/denoiser/guides/light_tree at all — exactly as the stack
    # runner's reference call omits them — so the run_blender_frame defaults apply and
    # is_ref ignores them regardless. The assert below is belt-and-suspenders. ---------- #
    shared_ref_kwargs = dict(
        blend=blend, res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True,
        frame=frame, nframes=nframes, cam_motion=cam_motion, bounces=bounces,
        device_pref=device_pref, timeout_s=ref_timeout, require_gpu=require_gpu,
    )
    cfg_a = dict(shared_ref_kwargs, seed=seed_a)
    cfg_b = dict(shared_ref_kwargs, seed=seed_b)
    # HONESTY GATE: refuse to spend if the two configs differ in anything but the seed.
    diffs = assert_seed_only_diff(cfg_a, cfg_b)
    log(f"config parity confirmed: reference_A and reference_B differ ONLY in {diffs}")

    # ---- 2) the two reference renders (identical config, seed the only difference) ---- #
    wall_a, dev_a, exr_a = ers.run_blender_frame(
        blender_bin, script_path, out_exr=os.path.join(WORK_DIR, "ref_a.exr"), **cfg_a)
    wall_b, dev_b, exr_b = ers.run_blender_frame(
        blender_bin, script_path, out_exr=os.path.join(WORK_DIR, "ref_b.exr"), **cfg_b)

    devices = sorted({dev_a, dev_b})
    device = ",".join(devices)
    # Belt-and-suspenders (run_blender_frame already refuses per-render): a CPU device
    # string must never reach a GPU-required receipt.
    if require_gpu and any(d.startswith("CPU") or d == "unknown" for d in devices):
        raise RuntimeError(
            f"require_gpu set but render device set is {devices!r}; refusing a "
            f"CPU-fallback receipt")

    # ---- 3) read pixels + compute the self-consistency SSIM on the grading grid ------- #
    color_a = ers.read_exr_layers(exr_a, res_x, res_y)[0]
    color_b = ers.read_exr_layers(exr_b, res_x, res_y)[0]

    t0 = time.perf_counter()
    # per-tile SSIM(A, B) and the global/worst/p5 SSIM(A, B) — the SAME grading tiling
    # (same _tone(), same _tile_rects()) the delivery gate scores in.
    ssim_mat = ers.per_tile_ssim_map(color_a, color_b)
    global_ssim, worst_tile, p5_tile = ers.compute_ssim_global_and_tiles(color_a, color_b)
    scoring_s = time.perf_counter() - t0
    log(f"scoring done in {scoring_s:.1f}s; A-vs-B global={global_ssim:.4f} "
        f"worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f}")

    note = (
        f"REFERENCE SELF-CONSISTENCY probe on '{scene_key}' frame {frame}/{nframes} "
        f"({res_x}x{res_y}), the SAME deterministic camera path as exp_render_stack.py. "
        f"TWO real renders on the same box, BYTE-IDENTICAL config differing ONLY in seed "
        f"(A seed={seed_a}, B seed={seed_b}): {ref_spp} spp fixed, adaptive OFF, denoiser "
        f"OFF, guides OFF, light-tree left at the scene's .blend default — the EXACT recipe "
        f"exp_render_stack.py renders its reference with (run_blender_frame is_ref=True, "
        f"which FORCES use_adaptive_sampling=False and use_denoising=False and SKIPS the "
        f"anchor-only light-tree lever for BOTH renders). per-tile SSIM(A,B) on the 8x8 "
        f"grading grid (same _tone/_tile_rects as compute_ssim_global_and_tiles) measures "
        f"the reference's SELF-CONSISTENCY: worst_tile_ref_vs_ref is how much two "
        f"reference-quality renders of the SAME tile disagree at the noisiest tile. "
        f"gate_reachable = (worst_tile_ref_vs_ref >= {DELIVERY_WORST_TILE}); when False the "
        f"strict worst-tile gate is unreachable by construction (noise vs noise). All SSIM "
        f"and wall values are MEASURED on real pixels; the comparison is valid ONLY because "
        f"the two configs are identical except the seed, asserted in code before any spend."
    )
    if fallback_note:
        note += " SCENE NOTE: " + fallback_note + "."

    params_echo = {
        "scene": scene_key,
        "requested_scene": scene_arg,
        "resolution": f"{res_x}x{res_y}",
        "frame": int(frame),
        "nframes": int(nframes),
        "ref_spp": int(ref_spp),
        "bounces": int(bounces),
        "cam_motion": float(cam_motion),
        "scoring_s": round(float(scoring_s), 3),
    }

    metrics = probe_metrics(
        ssim_mat, global_ssim, worst_tile, p5_tile,
        grid=ers.GRADING_TILE_GRID, seeds=(seed_a, seed_b),
        walls={"ref_a": wall_a, "ref_b": wall_b}, device=device,
        params_echo=params_echo, note=note,
    )
    log(f"RESULT global_ref_vs_ref={metrics['global_ref_vs_ref']} "
        f"worst_tile_ref_vs_ref={metrics['worst_tile_ref_vs_ref']} "
        f"p5_tile_ref_vs_ref={metrics['p5_tile_ref_vs_ref']} "
        f"gate_reachable={metrics['gate_reachable']} device={device}")
    emit(metrics)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — the contract: error key, exit 0, never fabricate
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
