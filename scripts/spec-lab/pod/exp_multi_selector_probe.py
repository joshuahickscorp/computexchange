#!/usr/bin/env python3
"""
exp_multi_selector_probe.py — EXHAUSTIVE REFERENCE-FREE TILE-SELECTOR PROBE (REAL).
================================================================================

WHY THIS EXISTS (two decisive selector negatives before it, 2026-07-10 staged table):
The integrated 4K receipt's strict-gate gap is worst-tile 0.9095 vs the 0.95 bar.
TWO reference-free tile-repair selectors are already MEASURED-DEAD against that gap:
  (1) two-draft VARIANCE divergence (RUN 4): selector_recall = 0.0 — the failing
      tiles are NOT variance-limited (two seeds share the denoiser's bias).
  (2) cross-denoiser DISAGREEMENT (OIDN vs OptiX, probe 2): recall ~0.08, Spearman
      -0.02 — the denoisers AGREE and are both ~equally wrong; the miss is a SHARED
      denoiser bias on hard motion-reveal edge/corner content, not denoiser-specific.

This probe scores, from ONE anchor render, the remaining PRINCIPLED reference-free
signals side-by-side so we learn which (if any) localizes the shared-bias failing
tiles. If NONE clears a useful recall, the honest conclusion is definitive:
reference-free tile-repair cannot reach strict delivery on this failure mode, and
5.56x @ global 0.985 / worst-tile 0.91 (which clears the published 0.95-tier) is the
honest ceiling of this lever.

DESIGN (one frame, THREE real renders on the SAME box — same count/cost as probe 2):
  (a) ANCHOR/OIDN (delivered): the exact RUN 3 anchor stack — draft 512 spp cap,
      adaptive threshold 0.02 (min 16), OIDN + albedo/normal prefiltered guides +
      light-tree — frame 1 of the SAME deterministic camera path exp_render_stack.py
      animates. WITH two extra AOV passes riding in the same multilayer EXR at ~zero
      cost: the Cycles Debug Sample Count pass (S1) and the Normal pass (S4).
  (b) NOISY (denoiser OFF): IDENTICAL sampling to the anchor (same seed/spp/adaptive/
      light-tree) with denoiser OFF. Deterministic same-seed Cycles => the same
      underlying noisy estimate as the anchor's pre-denoise buffer, so |noisy - oidn|
      is the amount of work the denoiser did (S2). (Any residual GPU-trace
      nondeterminism only adds variance-like residual; it cannot erase the signal.)
  (c) REFERENCE (ground truth): ref_spp (4096) fixed, adaptive OFF, denoise OFF — the
      integrated runner's exact recipe, used ONLY to MEASURE the true per-tile error.

THE FOUR REFERENCE-FREE SELECTOR SIGNALS (none reads the reference; all HIGH=candidate):
  S1 SAMPLE_COUNT   — per-tile mean of the Cycles adaptive per-pixel sample-count pass
                      of the delivered anchor. HIGH count = hard-to-converge region =
                      candidate failing tile. Enabled DEFENSIVELY
                      (view_layer.cycles.use_pass_debug_sample_count via hasattr/try);
                      if the pass cannot be produced/written, S1 is reported UNAVAILABLE
                      HONESTLY (never faked).
  S2 DENOISER_RESID — per-tile mean |tone(noisy) - tone(oidn)| (tonemapped space).
                      HIGH residual = denoiser did the most work = candidate.
  S3 CONTENT_GRAD   — per-tile mean gradient magnitude of the delivered (tonemapped)
                      oidn frame — high-frequency content energy.
  S4 AOV_EDGE       — per-tile mean normal-gradient magnitude from the Normal AOV —
                      geometric complexity / silhouette edge density.

METRICS on the 8x8 grading grid (EXACTLY compute_ssim_global_and_tiles' tiling —
same _tone(), same _tile_rects()):
  E_oidn[tile] = 1 - SSIM(oidn, ref)   the TRUE error of the delivered output
  For EACH available selector S: recall@1/@4/@12 (top-k by S vs top-k true-worst by
  E_oidn) + Spearman rank correlation of S vs E_oidn over the 64 tiles.

HONESTY CONTRACT (same as exp_cross_denoiser_probe.py / exp_render_stack.py):
  * Human logs -> STDERR; the LAST stdout line is exactly ONE JSON object.
  * Any failure emits {"error": ...} as the last stdout line and exits 0 — never
    fabricate a number. An UNAVAILABLE selector is reported with available=false and a
    reason, its metrics null — never silently 0.
  * require_gpu is fail-loud (the reused run_blender_frame keeps every device /
    denoiser-unavailable / CPU-refusal guard). All E / selector / recall / Spearman /
    wall values are MEASURED on real pixels. The ONE named assumption (not a "modeled"
    crop term): S2's noisy render is a SEPARATE same-seed render standing in for the
    anchor's pre-denoise buffer (deterministic Cycles) — so modeled=false throughout.

CONFIG (argv[1] JSON, all optional; defaults = the RUN 3 anchor config): scene,
resolution=3840x2160, frame=1, nframes=4, draft_spp=512, ref_spp=4096,
adaptive_threshold=0.02, adaptive_min_samples=16, denoise_guides=true, light_tree=true,
bounces=12, cam_motion=1.0, seed=0, device="AUTO", require_gpu=false (driver passes
true), gpu_probe_timeout_s=300 (driver passes 1500), blender_url=<4.2 LTS>.
"""

import contextlib
import json
import os
import sys
import time

# Both siblings live in this pod/ directory (the driver scp's the whole directory); we
# REUSE exp_render_stack for the Blender bootstrap / scene cache / deterministic camera
# path / money-safe render driver / EXR reader / grading-grid tiling + SSIM, and
# exp_cross_denoiser_probe for the already-unit-tested ranking + correlation math and
# the 1-SSIM tile-dissimilarity — verbatim, so this probe adds no divergent copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import exp_render_stack as ers  # noqa: E402
import exp_cross_denoiser_probe as xdp  # noqa: E402

WORK_DIR = "/tmp/multi_selector_probe"
PROBE_KS = (1, 4, 12)

# Ordered selector identities (row-order in the emitted JSON + the note).
SELECTOR_ORDER = (
    "S1_sample_count",
    "S2_denoiser_residual",
    "S3_content_gradient",
    "S4_aov_edge",
)


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the JSON line)."""
    print("[multi_selector_probe]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Blender scene script — DERIVED from the shared exp_render_stack one with a     #
# SINGLE documented injection so it stays in lockstep (no 300-line fork): after  #
# the Combined/Vector/Z pass block we optionally enable the Normal AOV (S4) and  #
# the Cycles Debug Sample Count pass (S1) and print availability sentinels. With #
# CX_WANT_NORMAL / CX_WANT_SAMPLECOUNT unset the script is behavior-identical to  #
# the shared one (so the noisy + reference renders are unchanged).               #
# --------------------------------------------------------------------------- #
_INJECT_ANCHOR = "vl.use_pass_combined = True\n"
_INJECT_BLOCK = _INJECT_ANCHOR + r'''
# ---- MULTI-SELECTOR reference-free AOV passes (S1 sample-count, S4 normal) -----
# Both ride inside the SAME multilayer EXR as Combined at ~zero extra cost and are
# read back reference-free. Enabling is gated on env + guarded (hasattr/try) so a
# Blender build/device that cannot produce the debug sample-count pass simply omits
# the channel and the caller reports S1 UNAVAILABLE (never fabricated). Sentinels go
# to STDOUT (the caller's run_blender_frame does not surface them, so the DECISIVE
# availability gate is channel-presence in the EXR — this is belt-and-suspenders).
_WANT_NORMAL = os.environ.get("CX_WANT_NORMAL", "0") == "1"
_WANT_SAMPLECOUNT = os.environ.get("CX_WANT_SAMPLECOUNT", "0") == "1"
if _WANT_NORMAL:
    try:
        vl.use_pass_normal = True
        print("CX_NORMAL_PASS=1", flush=True)
    except Exception as _e:
        print("CX_NORMAL_PASS=0", flush=True)
        _log("normal pass unavailable:", _e)
if _WANT_SAMPLECOUNT:
    _ok_sc = False
    try:
        _vlc = getattr(vl, "cycles", None)
        if _vlc is not None and hasattr(_vlc, "use_pass_debug_sample_count"):
            _vlc.use_pass_debug_sample_count = True
            _ok_sc = True
        elif _vlc is not None and hasattr(_vlc, "pass_debug_sample_count"):
            _vlc.pass_debug_sample_count = True
            _ok_sc = True
    except Exception as _e:
        _log("sample-count pass set failed:", _e)
    print("CX_SAMPLECOUNT_PASS=%d" % (1 if _ok_sc else 0), flush=True)
'''

if _INJECT_ANCHOR not in ers.BLENDER_SCENE_SCRIPT:
    raise RuntimeError(
        "could not locate the pass-enable anchor line in the shared "
        "exp_render_stack.BLENDER_SCENE_SCRIPT; the AOV injection is out of lockstep")
BLENDER_SCENE_SCRIPT_PLUS = ers.BLENDER_SCENE_SCRIPT.replace(
    _INJECT_ANCHOR, _INJECT_BLOCK, 1)


@contextlib.contextmanager
def _extra_env(**kv):
    """Temporarily set env vars for a reused ers.run_blender_frame call (which copies
    os.environ), restoring the prior state afterward. Used to flip on the two AOV passes
    for the anchor render ONLY."""
    saved = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------- #
# Reference-free selector inputs — read the extra AOV passes from the anchor EXR #
# (the shared read_exr_layers only returns Combined/Vector/Z). Reuses the same    #
# OpenEXR channel-read pattern as exp_render_stack.read_exr_layers.               #
# --------------------------------------------------------------------------- #
def read_named_channels(path, res_x, res_y):
    """Return {"normal": [H,W,3]|None, "sample_count": [H,W]|None, "channels": [...],
    "reason": str|None} from the anchor EXR. A channel absent from the EXR (e.g. a
    Blender build/device that did not write the Debug Sample Count pass) yields None so
    the selector is reported UNAVAILABLE HONESTLY — never fabricated. Falls back to
    reason-only (both None) if OpenEXR is missing (imageio cannot read named passes)."""
    import numpy as np
    try:
        import OpenEXR  # type: ignore
        import Imath    # type: ignore
    except Exception:
        return {"normal": None, "sample_count": None, "channels": [],
                "reason": "OpenEXR module unavailable on pod (cannot read AOV passes)"}

    try:
        f = OpenEXR.InputFile(path)
        header = f.header()
        dw = header["dataWindow"]
        w = dw.max.x - dw.min.x + 1
        h = dw.max.y - dw.min.y + 1
        chans = list(header["channels"].keys())
        pt = Imath.PixelType(Imath.PixelType.FLOAT)

        def _chan(name):
            raw = f.channel(name, pt)
            return np.frombuffer(raw, dtype=np.float32).reshape(h, w).copy()

        def _find_suffix(suffixes):
            for want in suffixes:
                for c in chans:
                    if c.endswith(want):
                        return c
            return None

        def _find_contains(substrs):
            low = [(c, c.lower()) for c in chans]
            for sub in substrs:
                for c, cl in low:
                    if sub in cl:
                        return c
            return None

        nx = _find_suffix([".Normal.X", "Normal.X"])
        ny = _find_suffix([".Normal.Y", "Normal.Y"])
        nz = _find_suffix([".Normal.Z", "Normal.Z"])
        normal = None
        if nx and ny and nz:
            normal = np.stack([_chan(nx), _chan(ny), _chan(nz)], axis=-1)

        # The Cycles Debug Sample Count pass channel name varies across Blender builds;
        # match by content first ("... sample count"), then common single-channel forms.
        sc_name = _find_contains(["sample count"]) or _find_suffix(
            [".Debug Sample Count.X", "Debug Sample Count.X", ".Samples.X", "Samples.X",
             ".Samples", "Samples"])
        sample_count = _chan(sc_name) if sc_name else None
        f.close()
    except Exception as e:  # noqa: BLE001 — honest reason, both None
        return {"normal": None, "sample_count": None, "channels": [],
                "reason": f"EXR named-channel read failed: {type(e).__name__}: {e}"}

    def _rs(a):
        if a is None:
            return None
        if a.shape[:2] == (res_y, res_x):
            return a
        from skimage.transform import resize as sk_resize
        shape = (res_y, res_x) + tuple(a.shape[2:])
        return sk_resize(a, shape, order=1, preserve_range=True,
                         anti_aliasing=False).astype(np.float32)

    return {"normal": _rs(normal), "sample_count": _rs(sample_count),
            "channels": chans, "sample_count_channel": sc_name, "reason": None}


# --------------------------------------------------------------------------- #
# Per-pixel reference-free selector FIELDS + the shared tile reduction.          #
# All operate ONLY on delivered/noisy/AOV pixels — never the reference.          #
# --------------------------------------------------------------------------- #
def _reduce_field_to_tiles(field, grid=ers.GRADING_TILE_GRID):
    """[grid,grid] per-tile MEAN of a [H,W] scalar field on the SAME grading grid
    (ers._tile_rects), NaN where a tile is too small to score (< 7px — mirrors the
    SSIM grading skip so the selector ranks the SAME 64-tile universe as E_oidn)."""
    import numpy as np
    h, w = field.shape[:2]
    out = np.full((grid, grid), np.nan, dtype=np.float64)
    for gy, gx, y0, y1, x0, x1 in ers._tile_rects(h, w, grid):
        tile = field[y0:y1, x0:x1]
        if min(tile.shape[0], tile.shape[1]) < 7:
            continue
        out[gy, gx] = float(np.mean(tile))
    return out


def residual_field(noisy_rgb, oidn_rgb):
    """S2 pixel field: |tone(noisy) - tone(oidn)| averaged over RGB, in tonemapped
    space (the space SSIM grades in). HIGH = the denoiser moved the pixel the most."""
    import numpy as np
    a = ers._tone(np.asarray(noisy_rgb, dtype=np.float32))
    b = ers._tone(np.asarray(oidn_rgb, dtype=np.float32))
    if a.shape != b.shape:
        raise RuntimeError(f"noisy/oidn shape mismatch {a.shape} vs {b.shape}")
    return np.abs(a - b).mean(axis=-1)


def gradient_field(oidn_rgb):
    """S3 pixel field: gradient magnitude of the tonemapped delivered-frame luma
    (mean over channels) — a high-frequency / content-edge energy map."""
    import numpy as np
    tm = ers._tone(np.asarray(oidn_rgb, dtype=np.float32))
    luma = tm.mean(axis=-1)
    gy, gx = np.gradient(luma)
    return np.sqrt(gx * gx + gy * gy).astype(np.float32)


def normal_edge_field(normal_rgb):
    """S4 pixel field: gradient magnitude of the Normal AOV, summed over the three
    normal components — geometric-complexity / silhouette-edge density."""
    import numpy as np
    n = np.asarray(normal_rgb, dtype=np.float32)
    acc = None
    for c in range(n.shape[-1]):
        gy, gx = np.gradient(n[..., c])
        e = gx * gx + gy * gy
        acc = e if acc is None else acc + e
    return np.sqrt(acc).astype(np.float32)


# --------------------------------------------------------------------------- #
# Pure metric assembly (unit-tested locally on synthetic tile arrays). Reuses     #
# the ranking + correlation math from exp_cross_denoiser_probe verbatim.          #
# --------------------------------------------------------------------------- #
def _round_or_none(v, places=4):
    return round(v, places) if v is not None else None


def _selector_entry(name, sel_mat, available, reason, E_flat, *, grid, ks=PROBE_KS):
    """One selector's block of the JSON. When unavailable, every metric is null and a
    reason is carried (never a fabricated 0/1). When available, recall@k and Spearman
    are computed vs E_oidn over the shared finite tile universe (reused recall_at_k /
    spearman_rank_corr handle NaN tiles + k clamping)."""
    if (not available) or sel_mat is None:
        return {
            "selector": name,
            "available": False,
            "unavailable_reason": reason,
            **{f"recall_at_{k}": None for k in ks},
            "recall_k_eff": {str(k): 0 for k in ks},
            "spearman_vs_E_oidn": None,
            "n_valid_tiles": 0,
            "tiles": None,
            "top_tile": None,
            "top12": [],
        }
    sel_flat = xdp.flatten_tiles(sel_mat)
    if len(sel_flat) != len(E_flat):
        raise ValueError(
            f"selector {name} has {len(sel_flat)} tiles, expected {len(E_flat)}")
    recalls, k_effs = {}, {}
    for k in ks:
        r, k_eff = xdp.recall_at_k(sel_flat, E_flat, k)
        recalls[f"recall_at_{k}"] = _round_or_none(r)
        k_effs[str(k)] = k_eff
    top1 = xdp.topk_indices(sel_flat, 1)
    n_valid = len(set(xdp._finite_indices(sel_flat)) & set(xdp._finite_indices(E_flat)))
    return {
        "selector": name,
        "available": True,
        "unavailable_reason": None,
        **recalls,
        "recall_k_eff": k_effs,
        "spearman_vs_E_oidn": _round_or_none(xdp.spearman_rank_corr(sel_flat, E_flat)),
        "n_valid_tiles": n_valid,
        "tiles": [round(v, 6) if v is not None else None for v in sel_flat],
        "top_tile": (xdp._tile_entry(sel_flat, top1[0], grid, "score") if top1 else None),
        "top12": [xdp._tile_entry(sel_flat, i, grid, "score")
                  for i in xdp.topk_indices(sel_flat, 12)],
    }


def probe_metrics(E_oidn_mat, selectors, *, grid, params_echo, walls, device,
                  quality_context=None, note="", ks=PROBE_KS):
    """PURE assembly of the final JSON object. `selectors` is an ORDERED list of
    (name, sel_mat_or_None, available: bool, reason_or_None) — one per candidate signal.
    E_oidn_mat is the [grid,grid] true-error matrix (1 - SSIM(oidn, ref)). No I/O, no
    rendering — unit-tested locally on synthetic matrices."""
    E_oidn = xdp.flatten_tiles(E_oidn_mat)
    n_tiles = grid * grid
    if len(E_oidn) != n_tiles:
        raise ValueError(f"E_oidn has {len(E_oidn)} tiles, expected {n_tiles}")

    sel_blocks = {}
    available_names = []
    for name, sel_mat, available, reason in selectors:
        entry = _selector_entry(name, sel_mat, available, reason, E_oidn,
                                grid=grid, ks=ks)
        sel_blocks[name] = entry
        if entry["available"]:
            available_names.append(name)

    e_top1 = xdp.topk_indices(E_oidn, 1)
    metrics = {
        "probe": "multi_selector",
        "label": "MEASURED",
        "hypothesis": (
            "the strict-gate-failing tiles are a SHARED denoiser bias on hard "
            "motion-reveal edge/corner content (variance-divergence recall 0.0, "
            "cross-denoiser recall ~0.08). Score FOUR principled reference-free "
            "selectors (adaptive sample-count, denoiser residual, content gradient, "
            "normal-AOV edge) from ONE anchor render to find which — if any — "
            "localizes them; a uniformly weak recall is the honest ceiling verdict"
        ),
        "grid": int(grid),
        "n_tiles": int(n_tiles),
        "n_valid_E_tiles": len(xdp._finite_indices(E_oidn)),
        "E_oidn_tiles": [round(v, 6) if v is not None else None for v in E_oidn],
        "worst_tile_by_E_oidn": (
            xdp._tile_entry(E_oidn, e_top1[0], grid, "E") if e_top1 else None),
        "top12_by_E_oidn": [xdp._tile_entry(E_oidn, i, grid, "E")
                            for i in xdp.topk_indices(E_oidn, 12)],
        "selectors": sel_blocks,
        "selector_order": list(SELECTOR_ORDER),
        "available_selectors": available_names,
        "wall_anchor_oidn_s": round(float(walls["anchor_oidn"]), 3),
        "wall_noisy_s": round(float(walls["noisy"]), 3),
        "wall_ref_s": round(float(walls["ref"]), 3),
        "device": device,
        "modeled": False,
        "note": note,
    }
    metrics.update(params_echo)
    if quality_context:
        metrics.update(quality_context)
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
    # All reused verbatim from the stack runner (same caches, same fail-loud guards).
    # The GPU probe runs BEFORE anything expensive.
    ers.ensure_system_libs()
    ers.ensure_pydeps()
    blender_bin = ers.ensure_blender(blender_url)
    if require_gpu:
        ers.require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)
    blend, scene_key, fallback_note = ers.resolve_scene(scene_arg)

    # OUR derived scene script (shared script + the two AOV passes). Written once.
    script_path = os.path.join(WORK_DIR, "cx_multiselect_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT_PLUS)

    anchor_timeout = 1800
    ref_timeout = 3600

    # ---- 1) the two cheap anchor-config renders FIRST (fail-fast: a missing GPU/OIDN
    # aborts BEFORE the expensive reference is paid for). ---------------------- #
    # (a) delivered anchor + AOV passes (S1 sample-count, S4 normal).
    with _extra_env(CX_WANT_NORMAL="1", CX_WANT_SAMPLECOUNT="1"):
        wall_oidn, dev_oidn, exr_oidn = ers.run_blender_frame(
            blender_bin, script_path, blend=blend,
            out_exr=os.path.join(WORK_DIR, "anchor_oidn.exr"),
            res_x=res_x, res_y=res_y, spp=draft_spp, is_ref=False,
            frame=frame, nframes=nframes, cam_motion=cam_motion, seed=seed,
            bounces=bounces, device_pref=device_pref, timeout_s=anchor_timeout,
            adaptive=True, adaptive_thr=adaptive_threshold,
            adaptive_min=adaptive_min_samples, denoiser="oidn",
            guides=denoise_guides, light_tree=light_tree, require_gpu=require_gpu,
        )

    # (b) the NOISY render (S2): identical sampling, denoiser OFF, same seed. No AOV
    # passes needed here (CX_WANT_* unset -> script behaves like the shared one).
    wall_noisy, dev_noisy, exr_noisy = ers.run_blender_frame(
        blender_bin, script_path, blend=blend,
        out_exr=os.path.join(WORK_DIR, "anchor_noisy.exr"),
        res_x=res_x, res_y=res_y, spp=draft_spp, is_ref=False,
        frame=frame, nframes=nframes, cam_motion=cam_motion, seed=seed,
        bounces=bounces, device_pref=device_pref, timeout_s=anchor_timeout,
        adaptive=True, adaptive_thr=adaptive_threshold,
        adaptive_min=adaptive_min_samples, denoiser="none",
        guides=False, light_tree=light_tree, require_gpu=require_gpu,
    )

    # ---- 2) the reference (ground truth): ref_spp fixed, adaptive OFF, denoise OFF -- #
    wall_ref, dev_ref, exr_ref = ers.run_blender_frame(
        blender_bin, script_path, blend=blend,
        out_exr=os.path.join(WORK_DIR, "ref.exr"),
        res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True,
        frame=frame, nframes=nframes, cam_motion=cam_motion, seed=seed,
        bounces=bounces, device_pref=device_pref, timeout_s=ref_timeout,
        require_gpu=require_gpu,
    )

    devices = sorted({dev_oidn, dev_noisy, dev_ref})
    device = ",".join(devices)
    if require_gpu and any(d.startswith("CPU") or d == "unknown" for d in devices):
        raise RuntimeError(
            f"require_gpu set but render device set is {devices!r}; refusing a "
            f"CPU-fallback receipt")

    # ---- 3) read pixels + AOV passes ----------------------------------------- #
    color_oidn = ers.read_exr_layers(exr_oidn, res_x, res_y)[0]
    color_noisy = ers.read_exr_layers(exr_noisy, res_x, res_y)[0]
    color_ref = ers.read_exr_layers(exr_ref, res_x, res_y)[0]
    named = read_named_channels(exr_oidn, res_x, res_y)
    aov_reason = named.get("reason")

    t0 = time.perf_counter()
    # TRUE per-tile error of the delivered output (reuses the cross-denoiser probe's
    # 1 - per_tile_ssim_map, i.e. the exact grading tiling).
    E_oidn_mat = xdp.tile_dissimilarity(color_oidn, color_ref)
    g_oidn, wt_oidn, p5_oidn = ers.compute_ssim_global_and_tiles(color_oidn, color_ref)

    # ---- 4) score the four reference-free selectors -------------------------- #
    # S1 SAMPLE_COUNT (availability gated on the debug pass channel being present).
    sc = named.get("sample_count")
    if sc is not None:
        s1_mat = _reduce_field_to_tiles(sc)
        s1_avail, s1_reason = True, None
    else:
        s1_mat, s1_avail = None, False
        s1_reason = (aov_reason or "Cycles Debug Sample Count pass channel not present "
                     "in the anchor EXR (use_pass_debug_sample_count unsupported or not "
                     "written on this Blender build/device)")

    # S2 DENOISER_RESIDUAL (always available — derived from the two renders we produce).
    s2_mat = _reduce_field_to_tiles(residual_field(color_noisy, color_oidn))

    # S3 CONTENT_GRADIENT (always available — derived from the delivered frame).
    s3_mat = _reduce_field_to_tiles(gradient_field(color_oidn))

    # S4 AOV_EDGE (availability gated on the Normal pass being present).
    nrm = named.get("normal")
    if nrm is not None:
        s4_mat = _reduce_field_to_tiles(normal_edge_field(nrm))
        s4_avail, s4_reason = True, None
    else:
        s4_mat, s4_avail = None, False
        s4_reason = (aov_reason or "Normal AOV pass channels not present in the anchor "
                     "EXR (use_pass_normal unsupported or not written)")

    selectors = [
        ("S1_sample_count", s1_mat, s1_avail, s1_reason),
        ("S2_denoiser_residual", s2_mat, True, None),
        ("S3_content_gradient", s3_mat, True, None),
        ("S4_aov_edge", s4_mat, s4_avail, s4_reason),
    ]
    scoring_s = time.perf_counter() - t0
    log(f"scoring done in {scoring_s:.1f}s; oidn global={g_oidn:.4f} "
        f"worst_tile={wt_oidn:.4f}; S1_avail={s1_avail} S4_avail={s4_avail} "
        f"sc_channel={named.get('sample_count_channel')}")

    note = (
        f"MULTI-SELECTOR reference-free probe on '{scene_key}' frame {frame}/{nframes} "
        f"({res_x}x{res_y}), the SAME deterministic camera path as exp_render_stack.py. "
        f"THREE real renders on the same box: (a) the exact RUN 3 anchor stack "
        f"[adaptive thr={adaptive_threshold} min={adaptive_min_samples} + OIDN"
        f"{' + albedo/normal prefiltered guides' if denoise_guides else ''}"
        f"{' + light-tree' if light_tree else ''}, draft_spp={draft_spp}, seed={seed}] "
        f"carrying the Normal + Debug-Sample-Count AOV passes at ~zero extra cost, "
        f"(b) an IDENTICAL-sampling render with denoiser OFF and the SAME SEED "
        f"(deterministic same-seed Cycles => the anchor's underlying noisy estimate; any "
        f"residual GPU-trace nondeterminism only adds variance-like residual, it cannot "
        f"erase the signal), and (c) reference at {ref_spp} spp, adaptive OFF, denoise "
        f"OFF. E_oidn=1-SSIM(oidn,ref) per tile on the 8x8 grading grid (same "
        f"_tone/_tile_rects). Four reference-free selectors scored vs E_oidn (all "
        f"HIGH=candidate, none reads the reference): S1 per-tile mean adaptive "
        f"sample-count [avail={s1_avail}], S2 per-tile mean |noisy-oidn| in tonemapped "
        f"space, S3 per-tile mean content-gradient magnitude of the delivered frame, S4 "
        f"per-tile mean Normal-AOV gradient magnitude [avail={s4_avail}]. recall@k asks "
        f"whether the top-k tiles by each selector contain the top-k TRUE-worst by "
        f"E_oidn — the direct analogue of RUN 4's selector_recall (0.0) and probe 2's "
        f"cross-denoiser recall (~0.08). All E/selector/recall/Spearman/wall values are "
        f"MEASURED on real pixels; an UNAVAILABLE selector is reported honestly (null "
        f"metrics + reason), never fabricated."
    )
    if aov_reason:
        note += f" AOV NOTE: {aov_reason}."
    if fallback_note:
        note += " SCENE NOTE: " + fallback_note + "."

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
        "sample_count_channel": named.get("sample_count_channel"),
    }
    quality_context = {
        "ssim_global_oidn_vs_ref": round(float(g_oidn), 4),
        "ssim_worst_tile_oidn_vs_ref": round(float(wt_oidn), 4),
        "ssim_p5_tile_oidn_vs_ref": round(float(p5_oidn), 4),
        "scoring_s": round(float(scoring_s), 3),
    }

    metrics = probe_metrics(
        E_oidn_mat, selectors, grid=ers.GRADING_TILE_GRID,
        params_echo=params_echo,
        walls={"anchor_oidn": wall_oidn, "noisy": wall_noisy, "ref": wall_ref},
        device=device, quality_context=quality_context, note=note,
    )
    log("RESULT " + " | ".join(
        f"{n}: r@1={metrics['selectors'][n]['recall_at_1']} "
        f"r@4={metrics['selectors'][n]['recall_at_4']} "
        f"r@12={metrics['selectors'][n]['recall_at_12']} "
        f"rho={metrics['selectors'][n]['spearman_vs_E_oidn']} "
        f"avail={metrics['selectors'][n]['available']}"
        for n in SELECTOR_ORDER) + f" device={device}")
    emit(metrics)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — the contract: error key, exit 0, never fabricate
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
