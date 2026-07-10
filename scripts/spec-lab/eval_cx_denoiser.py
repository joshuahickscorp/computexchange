#!/usr/bin/env python3
"""eval_cx_denoiser.py — Track 2 M1 go/no-go: grade the trained cx denoiser against
OIDN on the SHARED worst-tile SSIM harness, on held-out eval frames with a true
high-spp reference.

The decision rule (from the plan): cx must MATCH/BEAT OIDN on OUR worst-tile SSIM. Both
denoisers run on the IDENTICAL noisy frames + guides and are graded by the byte-identical
compute_ssim_global_and_tiles() copied verbatim from exp_render_stack_analytical.py, so
the numbers compose directly with everything else measured this session.

OIDN is given the SAME albedo+normal guides the cx net gets (the fair, strong OIDN
baseline — not a strawman), run via Blender's own bundled Cycles/compositor Denoise
node (not the unreliable standalone 'oidn' pip binding). If Blender/the node isn't
resolvable, we DO NOT substitute a weaker filter and pretend it's OIDN: we report the
cx numbers and verdict "oidn_unavailable" honestly.

Emits ONE JSON line: cx {quality, worst_tile, p5_tile}, oidn {…}, and a `verdict`
(cx_beats_oidn | oidn_wins | tie | oidn_unavailable).

CLI:
  python3 eval_cx_denoiser.py --ckpt cx_denoiser.pt --data-dir MINT_DIR \
        [--device auto|cuda|cpu] [--margin 0.002] [--input noisy_a]
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "pod"))

import numpy as np  # noqa: E402
import torch  # noqa: E402

import cx_denoiser_model as m  # noqa: E402


def log(*a):
    print("[eval_cx]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# SSIM harness — VERBATIM from exp_render_stack_analytical.py so cx/oidn numbers #
# are directly comparable to every banked result this session.                  #
# --------------------------------------------------------------------------- #
def _tone(x):
    x = np.clip(x, 0.0, None)
    return np.clip(x / (1.0 + x), 0.0, 1.0)


def compute_ssim_global_and_tiles(delivered_rgb, true_rgb, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim) between two [H,W,3] linear-HDR
    frames. Tonemapped to [0,1] first."""
    from skimage.metrics import structural_similarity as ssim

    A = _tone(delivered_rgb)
    B = _tone(true_rgb)
    if A.shape != B.shape:
        raise RuntimeError(f"delivered/true shape mismatch {A.shape} vs {B.shape}")

    global_ssim = float(ssim(B, A, channel_axis=-1, data_range=1.0))

    h, w = B.shape[:2]
    ty = max(1, h // grid)
    tx = max(1, w // grid)
    tile_scores = []
    for gy in range(grid):
        y0 = gy * ty
        y1 = h if gy == grid - 1 else (gy + 1) * ty
        for gx in range(grid):
            x0 = gx * tx
            x1 = w if gx == grid - 1 else (gx + 1) * tx
            rt = B[y0:y1, x0:x1]
            dt = A[y0:y1, x0:x1]
            if min(rt.shape[0], rt.shape[1]) < 7:
                continue
            tile_scores.append(float(ssim(rt, dt, channel_axis=-1, data_range=1.0)))

    if tile_scores:
        arr = np.asarray(tile_scores, dtype=np.float64)
        worst = float(arr.min())
        p5 = float(np.percentile(arr, 5))
    else:
        worst = global_ssim
        p5 = global_ssim
    return global_ssim, worst, p5


# --------------------------------------------------------------------------- #
# OIDN — the fair, guided baseline (albedo+normal), via Blender's Cycles          #
# compositor Denoise node. Every real, banked OIDN number this session went      #
# through Blender's bundled OIDN library (via Cycles/the compositor), never a    #
# standalone 'oidn' pip binding -- that package proved unreliable (import        #
# failures) and is what caused the original oidn_unavailable result. The exact   #
# compositor wiring below (Image -> Denoise -> OutputFile, fed by EXR guides) is #
# new this pass and NOT yet proven on real hardware -- the underlying Denoise    #
# node itself is a standard, well-documented Blender feature, but this specific  #
# script has not executed end-to-end on a pod yet. Loads the already-rendered    #
# noisy/guide arrays as EXR images into a throwaway headless Blender scene,      #
# runs them through a CompositorNodeDenoise node, and reads the result back --   #
# no path tracing, no re-render, just the denoise pass.                         #
# --------------------------------------------------------------------------- #
def _write_exr_rgb(path, arr):
    """Write an (H,W,3) float32 array as a single-part linear EXR via OpenEXR."""
    import OpenEXR
    import Imath
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    h, w, _ = arr.shape
    hdr = OpenEXR.Header(w, h)
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    hdr["channels"] = {c: Imath.Channel(pt) for c in ("R", "G", "B")}
    out = OpenEXR.OutputFile(path, hdr)
    out.writePixels({
        "R": arr[..., 0].tobytes(), "G": arr[..., 1].tobytes(), "B": arr[..., 2].tobytes(),
    })
    out.close()


def _read_exr_rgb(path):
    import OpenEXR
    import Imath
    f = OpenEXR.InputFile(path)
    dw = f.header()["dataWindow"]
    w = dw.max.x - dw.min.x + 1
    h = dw.max.y - dw.min.y + 1
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    chans = {}
    for c in ("R", "G", "B"):
        raw = f.channel(c, pt)
        chans[c] = np.frombuffer(raw, dtype=np.float32).reshape(h, w)
    f.close()
    return np.stack([chans["R"], chans["G"], chans["B"]], axis=-1).astype(np.float32)


_BLENDER_DENOISE_SCRIPT = r'''
import bpy, os

COLOR_PATH  = os.environ["CX_OIDN_COLOR"]
ALBEDO_PATH = os.environ.get("CX_OIDN_ALBEDO", "")
NORMAL_PATH = os.environ.get("CX_OIDN_NORMAL", "")
OUT_STEM    = os.environ["CX_OIDN_OUT_STEM"]
RES_X       = int(os.environ["CX_OIDN_W"])
RES_Y       = int(os.environ["CX_OIDN_H"])

scene = bpy.context.scene
scene.render.engine = 'CYCLES'  # Cycles bundles the OIDN library the Denoise node uses
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100

# bpy.ops.render.render() requires an active camera even though this render never
# path-traces anything -- every compositor input below comes from loaded Image
# nodes, not the (trivial, empty) render layer.
cam_data = bpy.data.cameras.new("cx_dummy_cam")
cam_obj = bpy.data.objects.new("cx_dummy_cam", cam_data)
scene.collection.objects.link(cam_obj)
scene.camera = cam_obj

scene.use_nodes = True
nt = scene.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)

img_color = bpy.data.images.load(COLOR_PATH)
n_color = nt.nodes.new('CompositorNodeImage')
n_color.image = img_color

n_denoise = nt.nodes.new('CompositorNodeDenoise')
nt.links.new(n_color.outputs['Image'], n_denoise.inputs['Image'])

if ALBEDO_PATH:
    img_albedo = bpy.data.images.load(ALBEDO_PATH)
    n_albedo = nt.nodes.new('CompositorNodeImage')
    n_albedo.image = img_albedo
    nt.links.new(n_albedo.outputs['Image'], n_denoise.inputs['Albedo'])

if NORMAL_PATH:
    img_normal = bpy.data.images.load(NORMAL_PATH)
    n_normal = nt.nodes.new('CompositorNodeImage')
    n_normal.image = img_normal
    nt.links.new(n_normal.outputs['Image'], n_denoise.inputs['Normal'])

n_out = nt.nodes.new('CompositorNodeOutputFile')
n_out.base_path = os.path.dirname(OUT_STEM)
n_out.format.file_format = 'OPEN_EXR'
n_out.format.color_depth = '32'
n_out.file_slots.clear()
n_out.file_slots.new(os.path.basename(OUT_STEM))
nt.links.new(n_denoise.outputs['Image'], n_out.inputs[0])

bpy.ops.render.render(write_still=False)
print("CX_OIDN_DENOISE_DONE", flush=True)
'''


def denoise_oidn(color, albedo=None, normal=None, blender_bin=None, timeout_s=180):
    """Real Intel OIDN via Blender's Cycles compositor Denoise node. Raises
    RuntimeError if Blender/the node is unavailable (caller reports honestly, never
    substitutes a weaker filter)."""
    import subprocess
    import tempfile

    if not blender_bin or not os.path.isfile(blender_bin):
        raise RuntimeError(f"blender binary not found: {blender_bin!r}")

    h, w, _ = color.shape
    work = tempfile.mkdtemp(prefix="cx_oidn_")
    color_path = os.path.join(work, "color.exr")
    _write_exr_rgb(color_path, color)
    albedo_path = ""
    if albedo is not None:
        albedo_path = os.path.join(work, "albedo.exr")
        _write_exr_rgb(albedo_path, np.clip(albedo, 0.0, 1.0))
    normal_path = ""
    if normal is not None:
        normal_path = os.path.join(work, "normal.exr")
        # Blender's Denoise node Normal input expects the raw Denoising Normal AOV
        # (native [-1,1] components, no display remap) -- same convention this
        # pipeline already uses everywhere else (see cx_denoiser_model.prepare_guides
        # and exp_mint_denoise_pairs' EXR channel reader).
        _write_exr_rgb(normal_path, np.clip(normal, -1.0, 1.0))

    out_stem = os.path.join(work, "denoised")
    script_path = os.path.join(work, "denoise.py")
    with open(script_path, "w") as f:
        f.write(_BLENDER_DENOISE_SCRIPT)

    env = dict(os.environ)
    env.update({
        "CX_OIDN_COLOR": color_path, "CX_OIDN_ALBEDO": albedo_path,
        "CX_OIDN_NORMAL": normal_path, "CX_OIDN_OUT_STEM": out_stem,
        "CX_OIDN_W": str(w), "CX_OIDN_H": str(h),
    })
    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s)
    if "CX_OIDN_DENOISE_DONE" not in (proc.stdout or ""):
        raise RuntimeError(
            f"blender denoise failed (rc={proc.returncode}); "
            f"stderr tail: {(proc.stderr or '')[-800:]}"
        )
    # CompositorNodeOutputFile in single-image mode appends the frame number; probe
    # the candidates the same way every other pod runner's _resolve_exr_path does.
    candidates = [out_stem + ".exr", out_stem + "0001.exr", out_stem + "_0001.exr"]
    resolved = next((c for c in candidates if os.path.isfile(c)), None)
    if resolved is None:
        raise RuntimeError(f"denoised EXR not found; tried {candidates}")
    return _read_exr_rgb(resolved)


# --------------------------------------------------------------------------- #
# cx denoiser — the owned pipeline, run whole-frame.                             #
# --------------------------------------------------------------------------- #
def _chw(a):
    return torch.from_numpy(np.ascontiguousarray(np.transpose(a, (2, 0, 1)))[None]).float()


def cx_denoise_frame(model, noisy, albedo, normal, depth, device):
    if depth.ndim == 2:
        depth = depth[..., None]
    with torch.no_grad():
        out = m.denoise(
            model,
            _chw(noisy).to(device), _chw(albedo).to(device),
            _chw(normal).to(device), _chw(depth[..., :1]).to(device),
        )
    return np.transpose(out[0].cpu().numpy(), (1, 2, 0)).astype(np.float32)


def load_model(ckpt_path, device):
    state = torch.load(ckpt_path, map_location="cpu")
    arch = state.get("arch", {"base": 48, "kernel": 5})
    model = m.KPCNDenoiser(base=arch.get("base", 48), kernel_size=arch.get("kernel", 5))
    model.load_state_dict(state["state_dict"])
    model.to(device).eval()
    return model, state.get("params", m.count_params(model))


def pick_device(pref):
    if pref == "cpu":
        return torch.device("cpu")
    if (pref in ("auto", "cuda")) and torch.cuda.is_available():
        return torch.device("cuda")
    if pref == "auto" and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--data-dir", required=True, help="mint out_dir (contains eval_frames/)")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    ap.add_argument("--margin", type=float, default=0.002,
                    help="worst-tile SSIM margin that counts as a real win")
    ap.add_argument("--input", default="noisy_a", help="which noisy field to denoise")
    args = ap.parse_args()

    eval_dir = os.path.join(args.data_dir, "eval_frames")
    files = sorted(glob.glob(os.path.join(eval_dir, "*.npz")))
    if not files:
        raise SystemExit(f"no eval frames in {eval_dir} "
                         f"(mint with eval_fraction>0 first)")

    device = pick_device(args.device)
    model, n_params = load_model(args.ckpt, device)
    log(f"device={device} eval_frames={len(files)} params={n_params:,}")

    cx_g, cx_w, cx_p = [], [], []
    oidn_g, oidn_w, oidn_p = [], [], []
    oidn_available = True
    oidn_err = ""
    blender_bin = None
    try:
        import exp_cycles_render_prod as prod
        blender_bin = prod.ensure_blender(prod.DEFAULT_BLENDER_URL)
    except Exception as e:  # noqa: BLE001
        oidn_available = False
        oidn_err = f"blender resolve failed: {type(e).__name__}: {e}"
        log(f"OIDN unavailable ({oidn_err}); reporting cx-only, honest verdict")

    for fp in files:
        d = np.load(fp)
        noisy = d[args.input]
        albedo, normal, depth = d["albedo"], d["normal"], d["depth"]
        ref = d["true_reference"]

        cx_out = cx_denoise_frame(model, noisy, albedo, normal, depth, device)
        g, w, p = compute_ssim_global_and_tiles(cx_out, ref)
        cx_g.append(g); cx_w.append(w); cx_p.append(p)
        log(f"{os.path.basename(fp)} cx: global={g:.4f} worst={w:.4f} p5={p:.4f}")

        if oidn_available:
            try:
                od = denoise_oidn(noisy, albedo=albedo, normal=normal, blender_bin=blender_bin)
                g2, w2, p2 = compute_ssim_global_and_tiles(od, ref)
                oidn_g.append(g2); oidn_w.append(w2); oidn_p.append(p2)
                log(f"{os.path.basename(fp)} oidn: global={g2:.4f} worst={w2:.4f} p5={p2:.4f}")
            except Exception as e:  # noqa: BLE001
                oidn_available = False
                oidn_err = f"{type(e).__name__}: {e}"
                log(f"OIDN unavailable ({oidn_err}); reporting cx-only, honest verdict")

    def _mean(xs):
        return round(float(np.mean(xs)), 4) if xs else None

    cx = {"quality": _mean(cx_g), "worst_tile": _mean(cx_w), "p5_tile": _mean(cx_p)}

    if oidn_available and oidn_w:
        oidn = {"quality": _mean(oidn_g), "worst_tile": _mean(oidn_w),
                "p5_tile": _mean(oidn_p)}
        dw = cx["worst_tile"] - oidn["worst_tile"]  # the GATING metric
        if dw > args.margin:
            verdict = "cx_beats_oidn"
        elif dw < -args.margin:
            verdict = "oidn_wins"
        else:
            verdict = "tie"
    else:
        oidn = {"quality": None, "worst_tile": None, "p5_tile": None}
        verdict = "oidn_unavailable"

    out = {
        "ok": True,
        "verdict": verdict,
        "gating_metric": "worst_tile_ssim",
        "margin": args.margin,
        "cx": cx,
        "oidn": oidn,
        "worst_tile_delta_cx_minus_oidn": (
            round(cx["worst_tile"] - oidn["worst_tile"], 4)
            if oidn["worst_tile"] is not None else None),
        "n_eval_frames": len(files),
        "params": n_params,
        "device": str(device),
        "oidn_guided": True,
        "oidn_error": oidn_err,
        "modeled": False,
        "note": (
            "cx and OIDN denoise the IDENTICAL noisy frames + albedo/normal guides; both "
            "graded by the verbatim compute_ssim_global_and_tiles (8x8 tiles) vs the "
            "high-spp reference. Verdict decided on worst_tile SSIM (the gating metric)."
        ),
    }
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)
