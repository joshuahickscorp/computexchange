#!/usr/bin/env python3
"""
exp_cross_denoiser_probe.py — CROSS-DENOISER BIAS-DETECTOR PROBE (REAL, one frame).
================================================================================

WHY THIS EXISTS (RUN 4 decisive negative, 2026-07-09 staged table):
The integrated 4K receipt's strict-gate gap is worst-tile 0.9095 vs the 0.95 bar.
RUN 4 proved the two-draft-divergence repair selector repairs the WRONG tiles:
two independent drafts diverge where VARIANCE is high, but the true worst tiles
are DENOISER-BIAS-limited (OIDN's systematic deviation is identical across seeds,
so seed-to-seed divergence is blind to it — measured selector_recall = 0.0).

HYPOTHESIS UNDER TEST: OIDN and OptiX have DIFFERENT biases, so per-tile
disagreement between the two denoised outputs of the SAME underlying render
should localize the bias-limited tiles that two-draft variance divergence missed.

DESIGN (one frame, three renders, same box):
  (a) ANCHOR/OIDN : the exact RUN 3 anchor config — draft 512 spp cap, adaptive
      threshold 0.02 (min 16), OIDN + albedo/normal prefiltered guides +
      light-tree — frame 1 of the SAME deterministic 4-frame camera path
      exp_render_stack.py animates (we import its scene/camera machinery).
  (b) ANCHOR/OPTIX: IDENTICAL render with denoiser=OPTIX and the SAME SEED.
      Deterministic same-seed Cycles => the same underlying noisy estimate, so
      the two outputs differ only by the denoiser. (Any residual GPU trace
      nondeterminism would add variance-LIKE disagreement to D; it cannot
      erase a bias signal.)
  (c) REFERENCE   : ref_spp (4096) fixed samples, adaptive OFF, denoise OFF —
      the same ground-truth recipe as the integrated runner.

METRICS on the 8x8 grading grid (EXACTLY the compute_ssim_global_and_tiles
tiling: same _tone(), same _tile_rects(), via per_tile_ssim_map):
  D[tile]       = 1 - SSIM(oidn, optix)   the reference-free selector signal
  E_oidn[tile]  = 1 - SSIM(oidn, ref)     the true error of the delivered output
  E_optix[tile] = 1 - SSIM(optix, ref)
  recall@k (k=1/4/12): |top-k by D  ∩  top-k true-worst by E_oidn| / k
  Spearman rank correlation of D vs E_oidn (and vs E_optix) over the 64 tiles.

HONESTY CONTRACT (same as exp_render_stack.py):
  * Human logs -> STDERR; the LAST stdout line is exactly ONE JSON object.
  * Any failure emits {"error": ...} as the last stdout line and exits 0 —
    we NEVER fabricate a number.
  * require_gpu is fail-loud: the functional 64x64@1spp GPU probe (CPU devices
    disabled) gates the run, and every render refuses CPU fallback — all
    reused verbatim from exp_render_stack.py (run_blender_frame keeps its
    denoiser-unavailable and CX_DEVICE_ERROR guards, so a pod without the
    OptiX denoiser FAILS before the expensive reference render is paid for).
  * D / E / recall / Spearman / wall times are MEASURED. The one MODELED claim
    (named in the note): a production selector would denoise ONE noisy render
    twice (near-zero extra render cost) instead of re-rendering same-seed as
    this probe must, because the runner denoises in-pipeline.

CONFIG (argv[1] JSON, all optional; defaults = the RUN 3 anchor config):
  scene="classroom", resolution="3840x2160", frame=1, nframes=4 (camera-path
  length, matching the RUN 3 shot), draft_spp=512, ref_spp=4096,
  adaptive_threshold=0.02, adaptive_min_samples=16, denoise_guides=true,
  light_tree=true, bounces=12, cam_motion=1.0, seed=0, device="AUTO",
  require_gpu=false (driver passes true), gpu_probe_timeout_s=300 (driver
  passes 1500 for sm_90 first-render JIT headroom), blender_url=<4.2 LTS>.
"""

import json
import math
import os
import sys
import time

# The stack runner lives in this same pod/ directory (the driver scp's the whole
# directory); we REUSE its Blender bootstrap, scene cache, deterministic camera
# path, money-safety guards, EXR reader and grading-grid tiling verbatim.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exp_render_stack as ers  # noqa: E402

WORK_DIR = "/tmp/cross_denoiser_probe"
PROBE_KS = (1, 4, 12)


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the JSON line)."""
    print("[cross_denoiser_probe]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Pure ranking/correlation math (unit-tested locally; no numpy/skimage needed). #
# --------------------------------------------------------------------------- #
def _is_finite(v):
    if v is None:
        return False
    f = float(v)
    return not (math.isnan(f) or math.isinf(f))


def _finite_indices(values):
    return [i for i, v in enumerate(values) if _is_finite(v)]


def topk_indices(values, k):
    """Indices of the k LARGEST finite values, descending; deterministic tie-break
    (equal values -> lower index first, i.e. row-major tile order). None/NaN/inf
    entries are skipped. k is clamped to the number of finite values."""
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    idx = _finite_indices(values)
    idx.sort(key=lambda i: (-float(values[i]), i))
    return idx[: min(k, len(idx))]


def recall_at_k(pred, truth, k):
    """recall@k = |top-k(pred) ∩ top-k(truth)| / k_eff, ranking ONLY the indices
    finite in BOTH lists (so pred and truth rank the same tile universe).
    k_eff = min(k, #shared finite indices). Returns (recall, k_eff); recall is
    None when k_eff == 0 (nothing rankable — never fabricated as 0 or 1)."""
    if len(pred) != len(truth):
        raise ValueError(f"length mismatch: {len(pred)} vs {len(truth)}")
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    shared = set(_finite_indices(pred)) & set(_finite_indices(truth))
    k_eff = min(int(k), len(shared))
    if k_eff == 0:
        return None, 0
    pred_m = [v if i in shared else None for i, v in enumerate(pred)]
    truth_m = [v if i in shared else None for i, v in enumerate(truth)]
    top_pred = set(topk_indices(pred_m, k_eff))
    top_truth = set(topk_indices(truth_m, k_eff))
    return len(top_pred & top_truth) / float(k_eff), k_eff


def _average_ranks(values):
    """1-based ranks with ties assigned the AVERAGE of their positions (the
    standard Spearman tie treatment)."""
    order = sorted(range(len(values)), key=lambda i: float(values[i]))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and float(values[order[j + 1]]) == float(values[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for m in range(i, j + 1):
            ranks[order[m]] = avg
        i = j + 1
    return ranks


def spearman_rank_corr(a, b):
    """Spearman rho = Pearson correlation of average ranks, over the pairs where
    BOTH entries are finite. Returns None when fewer than 2 usable pairs or when
    either side has zero rank variance (rho undefined — never fabricated)."""
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    pairs = [(float(x), float(y)) for x, y in zip(a, b)
             if _is_finite(x) and _is_finite(y)]
    if len(pairs) < 2:
        return None
    ra = _average_ranks([p[0] for p in pairs])
    rb = _average_ranks([p[1] for p in pairs])
    ma = sum(ra) / len(ra)
    mb = sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 0.0 or vb <= 0.0:
        return None
    return cov / math.sqrt(va * vb)


def flatten_tiles(mat):
    """Row-major flatten of a [grid][grid] matrix to a list, mapping NaN/inf to
    None (JSON-safe; unscored tiles stay visibly unscored, never 0.0)."""
    flat = []
    for row in mat:
        for v in row:
            f = float(v)
            flat.append(None if (math.isnan(f) or math.isinf(f)) else f)
    return flat


def _tile_entry(flat, idx, grid, key):
    return {"tile": [int(idx // grid), int(idx % grid)], key: float(flat[idx])}


def probe_metrics(D_mat, E_oidn_mat, E_optix_mat, *, grid, params_echo, walls,
                  device, quality_context=None, note="", ks=PROBE_KS):
    """PURE assembly of the final JSON object from the three [grid][grid] tile
    matrices (NaN allowed for unscored tiles) + measured wall times + device.
    No I/O, no rendering — unit-tested locally on synthetic matrices."""
    D = flatten_tiles(D_mat)
    E_oidn = flatten_tiles(E_oidn_mat)
    E_optix = flatten_tiles(E_optix_mat)
    n_tiles = grid * grid
    for name, flat in (("D", D), ("E_oidn", E_oidn), ("E_optix", E_optix)):
        if len(flat) != n_tiles:
            raise ValueError(f"{name} has {len(flat)} tiles, expected {n_tiles}")

    recalls = {}
    k_effs = {}
    for k in ks:
        r, k_eff = recall_at_k(D, E_oidn, k)
        recalls[f"recall_at_{k}"] = (round(r, 4) if r is not None else None)
        k_effs[str(k)] = k_eff

    def _top1(flat, key):
        top = topk_indices(flat, 1)
        return _tile_entry(flat, top[0], grid, key) if top else None

    def _round_or_none(v, places=4):
        return round(v, places) if v is not None else None

    metrics = {
        "probe": "cross_denoiser_selector",
        "label": "MEASURED",
        "hypothesis": (
            "strict-gate-failing tiles are DENOISER-BIAS-limited; OIDN-vs-OptiX "
            "per-tile disagreement D on the SAME same-seed render localizes them "
            "(two-draft variance divergence scored selector_recall=0.0 in RUN 4)"
        ),
        "grid": int(grid),
        "n_tiles": int(n_tiles),
        "n_valid_tiles": len(set(_finite_indices(D)) & set(_finite_indices(E_oidn))),
        **recalls,
        "recall_k_eff": k_effs,
        "spearman_D_vs_E_oidn": _round_or_none(spearman_rank_corr(D, E_oidn)),
        "spearman_D_vs_E_optix": _round_or_none(spearman_rank_corr(D, E_optix)),
        "D_tiles": [round(v, 6) if v is not None else None for v in D],
        "E_oidn_tiles": [round(v, 6) if v is not None else None for v in E_oidn],
        "E_optix_tiles": [round(v, 6) if v is not None else None for v in E_optix],
        "top_tile_by_D": _top1(D, "D"),
        "worst_tile_by_E_oidn": _top1(E_oidn, "E"),
        "worst_tile_by_E_optix": _top1(E_optix, "E"),
        "top12_by_D": [_tile_entry(D, i, grid, "D") for i in topk_indices(D, 12)],
        "top12_by_E_oidn": [_tile_entry(E_oidn, i, grid, "E")
                            for i in topk_indices(E_oidn, 12)],
        "wall_oidn_s": round(float(walls["oidn"]), 3),
        "wall_optix_s": round(float(walls["optix"]), 3),
        "wall_ref_s": round(float(walls["ref"]), 3),
        "device": device,
        "modeled": False,
        "note": note,
    }
    metrics.update(params_echo)
    if quality_context:
        metrics.update(quality_context)
    return metrics


def tile_dissimilarity(a_rgb, b_rgb, grid=ers.GRADING_TILE_GRID):
    """[grid,grid] of 1 - SSIM per grading tile between two [H,W,3] linear-HDR
    frames — EXACTLY the compute_ssim_global_and_tiles tiling (same _tone(), same
    _tile_rects()) via the stack runner's per_tile_ssim_map; NaN where a tile is
    too small to score (mirrors the grading skip)."""
    return 1.0 - ers.per_tile_ssim_map(a_rgb, b_rgb, grid=grid)


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    scene_arg = str(params.get("scene", "classroom"))
    resolution = str(params.get("resolution", "3840x2160"))
    frame = int(params.get("frame", 1))
    nframes = int(params.get("nframes", 4))       # RUN 3 shot length -> same camera path
    draft_spp = int(params.get("draft_spp", 512))
    ref_spp = int(params.get("ref_spp", 4096))
    adaptive_threshold = float(params.get("adaptive_threshold", 0.02))
    adaptive_min_samples = int(params.get("adaptive_min_samples", 16))
    denoise_guides = bool(params.get("denoise_guides", True))
    light_tree = bool(params.get("light_tree", True))
    bounces = int(params.get("bounces", 12))
    cam_motion = float(params.get("cam_motion", 1.0))
    seed = int(params.get("seed", 0))
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
    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    bounces = max(1, bounces)
    adaptive_min_samples = max(1, min(adaptive_min_samples, draft_spp))
    adaptive_threshold = max(0.0, adaptive_threshold)
    cam_motion = max(0.0, cam_motion)

    log(f"params: scene={scene_arg} res={res_x}x{res_y} frame={frame}/{nframes} "
        f"draft_spp={draft_spp} ref_spp={ref_spp} adaptive_thr={adaptive_threshold} "
        f"adaptive_min={adaptive_min_samples} guides={denoise_guides} "
        f"light_tree={light_tree} bounces={bounces} cam_motion={cam_motion} "
        f"seed={seed} device={device_pref} require_gpu={require_gpu}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) bootstrap: libs + Blender + functional GPU gate + scene ---------- #
    # All reused verbatim from the stack runner (same caches, same fail-loud
    # guards). The GPU probe runs BEFORE anything expensive.
    ers.ensure_system_libs()
    ers.ensure_pydeps()
    blender_bin = ers.ensure_blender(blender_url)
    if require_gpu:
        ers.require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)
    blend, scene_key, fallback_note = ers.resolve_scene(scene_arg)

    # The SAME embedded scene script as the stack runner => the SAME deterministic
    # camera keyframes (dolly+rise+yaw over nframes), so this probe's frame 1 is
    # pixel-comparable to the integrated runs' frame 1.
    script_path = os.path.join(WORK_DIR, "cx_probe_scene.py")
    with open(script_path, "w") as f:
        f.write(ers.BLENDER_SCENE_SCRIPT)

    anchor_timeout = 1800
    ref_timeout = 3600

    def anchor(denoiser_name, out_name):
        """One RUN 3-config anchor render, denoiser swapped, SAME seed."""
        return ers.run_blender_frame(
            blender_bin, script_path, blend=blend,
            out_exr=os.path.join(WORK_DIR, out_name),
            res_x=res_x, res_y=res_y, spp=draft_spp, is_ref=False,
            frame=frame, nframes=nframes, cam_motion=cam_motion, seed=seed,
            bounces=bounces, device_pref=device_pref, timeout_s=anchor_timeout,
            adaptive=True, adaptive_thr=adaptive_threshold,
            adaptive_min=adaptive_min_samples, denoiser=denoiser_name,
            guides=denoise_guides, light_tree=light_tree, require_gpu=require_gpu,
        )

    # ---- 1) the two cheap anchor renders FIRST (fail-fast ordering: a missing
    # OptiX denoiser or GPU problem aborts BEFORE the expensive reference is paid
    # for; run_blender_frame raises loudly on CX_DENOISER_UNAVAILABLE). ---------- #
    wall_oidn, dev_oidn, exr_oidn = anchor("oidn", "anchor_oidn.exr")
    wall_optix, dev_optix, exr_optix = anchor("optix", "anchor_optix.exr")

    # ---- 2) the reference (ground truth): ref_spp fixed, adaptive OFF,
    # denoise OFF — the integrated runner's exact recipe. ----------------------- #
    wall_ref, dev_ref, exr_ref = ers.run_blender_frame(
        blender_bin, script_path, blend=blend,
        out_exr=os.path.join(WORK_DIR, "ref.exr"),
        res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True,
        frame=frame, nframes=nframes, cam_motion=cam_motion, seed=seed,
        bounces=bounces, device_pref=device_pref, timeout_s=ref_timeout,
        require_gpu=require_gpu,
    )

    devices = sorted({dev_oidn, dev_optix, dev_ref})
    device = ",".join(devices)
    # Belt-and-suspenders (run_blender_frame already refuses per-render): a CPU
    # device string must never reach a GPU-required receipt.
    if require_gpu and any(d.startswith("CPU") or d == "unknown" for d in devices):
        raise RuntimeError(
            f"require_gpu set but render device set is {devices!r}; refusing a "
            f"CPU-fallback receipt")

    # ---- 3) read pixels + compute the tile matrices on the grading grid ------- #
    color_oidn = ers.read_exr_layers(exr_oidn, res_x, res_y)[0]
    color_optix = ers.read_exr_layers(exr_optix, res_x, res_y)[0]
    color_ref = ers.read_exr_layers(exr_ref, res_x, res_y)[0]

    t0 = time.perf_counter()
    D_mat = tile_dissimilarity(color_oidn, color_optix)
    E_oidn_mat = tile_dissimilarity(color_oidn, color_ref)
    E_optix_mat = tile_dissimilarity(color_optix, color_ref)
    g_oidn, wt_oidn, p5_oidn = ers.compute_ssim_global_and_tiles(color_oidn, color_ref)
    g_optix, wt_optix, p5_optix = ers.compute_ssim_global_and_tiles(color_optix, color_ref)
    scoring_s = time.perf_counter() - t0
    log(f"scoring done in {scoring_s:.1f}s; oidn global={g_oidn:.4f} "
        f"worst_tile={wt_oidn:.4f}; optix global={g_optix:.4f} worst_tile={wt_optix:.4f}")

    note = (
        f"CROSS-DENOISER bias-detector probe on '{scene_key}' frame {frame}/{nframes} "
        f"({res_x}x{res_y}), the SAME deterministic camera path as exp_render_stack.py. "
        f"THREE real renders on the same box: (a) the exact RUN 3 anchor stack "
        f"[adaptive thr={adaptive_threshold} min={adaptive_min_samples} + OIDN"
        f"{' + albedo/normal prefiltered guides' if denoise_guides else ''}"
        f"{' + light-tree' if light_tree else ''}, draft_spp={draft_spp}, seed={seed}], "
        f"(b) IDENTICAL render with denoiser=OPTIX and the SAME SEED — deterministic "
        f"same-seed Cycles means the same underlying noisy estimate, so the outputs "
        f"differ only by denoiser (any residual GPU trace nondeterminism would add "
        f"variance-like disagreement, it cannot erase a bias signal), and "
        f"(c) reference at {ref_spp} spp, adaptive OFF, denoise OFF. "
        f"D=1-SSIM(oidn,optix), E_oidn=1-SSIM(oidn,ref), E_optix=1-SSIM(optix,ref) "
        f"per tile on the 8x8 grading grid (same _tone/_tile_rects as "
        f"compute_ssim_global_and_tiles). recall@k asks whether the top-k tiles by D "
        f"contain the top-k TRUE-worst by E_oidn — the direct analogue of RUN 4's "
        f"selector_recall (which measured 0.0 for two-draft variance divergence). "
        f"All D/E/recall/Spearman values and wall times are MEASURED on real pixels. "
        f"ONE MODELED claim, not measured here: a production selector would denoise "
        f"ONE noisy render twice (OIDN+OptiX) at near-zero extra render cost; this "
        f"probe pays a second full anchor render ({wall_optix:.0f}s) only because the "
        f"runner denoises in-pipeline."
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."

    params_echo = {
        "scene": scene_key,
        "requested_scene": scene_arg,
        "resolution": f"{res_x}x{res_y}",
        "frame": int(frame),
        "nframes": int(nframes),
        "draft_spp": int(draft_spp),
        "ref_spp": int(ref_spp),
        "adaptive_threshold": float(adaptive_threshold),
        "adaptive_min_samples": int(adaptive_min_samples),
        "denoise_guides": bool(denoise_guides),
        "light_tree": bool(light_tree),
        "bounces": int(bounces),
        "cam_motion": float(cam_motion),
        "seed": int(seed),
    }
    quality_context = {
        "ssim_global_oidn_vs_ref": round(float(g_oidn), 4),
        "ssim_worst_tile_oidn_vs_ref": round(float(wt_oidn), 4),
        "ssim_p5_tile_oidn_vs_ref": round(float(p5_oidn), 4),
        "ssim_global_optix_vs_ref": round(float(g_optix), 4),
        "ssim_worst_tile_optix_vs_ref": round(float(wt_optix), 4),
        "ssim_p5_tile_optix_vs_ref": round(float(p5_optix), 4),
        "scoring_s": round(float(scoring_s), 3),
    }

    metrics = probe_metrics(
        D_mat, E_oidn_mat, E_optix_mat, grid=ers.GRADING_TILE_GRID,
        params_echo=params_echo, walls={"oidn": wall_oidn, "optix": wall_optix,
                                        "ref": wall_ref},
        device=device, quality_context=quality_context, note=note,
    )
    log(f"RESULT recall@1={metrics['recall_at_1']} recall@4={metrics['recall_at_4']} "
        f"recall@12={metrics['recall_at_12']} "
        f"spearman={metrics['spearman_D_vs_E_oidn']} device={device}")
    emit(metrics)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — the contract: error key, exit 0, never fabricate
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
