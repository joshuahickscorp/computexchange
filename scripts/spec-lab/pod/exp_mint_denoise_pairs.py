#!/usr/bin/env python3
"""exp_mint_denoise_pairs.py — Track 2 M0 data harness: mint Noise2Noise training
pairs (+ held-out eval frames with a true reference) from our own Cycles pipeline.

WHY: the moat for the owned denoiser is the TRAINING DISTRIBUTION, not the net — an
unlimited supply of pairs from OUR exact renderer (same sampler, same light-tree noise,
same AOVs) is the one lever a general tool like OIDN structurally can't pull. This runner
produces that data on demand.

WHAT it renders, per frame (FULL frame, then crops in post — never render tiny crops
directly; launch + .blend load + BVH build dominate a small render, so one full frame
amortizes across hundreds of patches):
  * noisy_a : low-spp Cycles render, seed A          (independent noise realization)
  * noisy_b : low-spp Cycles render, seed B          (the N2N target — different seed)
  * albedo  : Denoising Albedo AOV  (noise-free guide, extracted from render A's EXR)
  * normal  : Denoising Normal AOV
  * depth   : Z / Depth AOV
  * true_reference : high-spp render — EVAL FRAMES ONLY, never used for training

Train frames -> N2N crop patches (noisy_a, noisy_b, albedo, normal, depth), no reference.
Eval  frames -> one full-frame sample (noisy_a, albedo, normal, depth, true_reference),
so eval grades whole-frame quality against ground truth on the shared SSIM harness.

Honesty contract (identical to every sibling runner): exactly ONE final JSON line on
stdout, error-key JSON on any failure, `modeled: false`, never a fabricated number.

Config (JSON arg): {scene, resolution, spp, ref_spp, frames, eval_fraction, n_crops,
                    crop_size, seed, out_dir, blender_url, device}
"""

import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Reuse the PROVEN Blender bootstrap + scene fetch verbatim from the prod runner
# (import is side-effect-free: it only defines functions/constants at module level).
import exp_cycles_render_prod as prod  # noqa: E402


def log(*a):
    print("[mint_denoise]", *a, file=sys.stderr, flush=True)


def emit(obj):
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# The Blender render script: opens the production .blend, overrides ONLY the    #
# render controls, and writes a multilayer EXR carrying the combined radiance   #
# plus the Denoising Albedo/Normal + Depth AOVs. Seed + spp come from env so a   #
# second call with a different seed gives an INDEPENDENT noise realization.      #
# (Compositor OutputFile multilayer pattern reused from exp_render_gbuffer.py.)  #
# --------------------------------------------------------------------------- #
BLENDER_MINT_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[bpy-mint]", *a, file=sys.stderr, flush=True)

BLEND    = os.environ["CX_BLEND"]
EXR_DIR  = os.environ["CX_EXR_DIR"]          # dir the multilayer EXR is written into
STEM     = os.environ["CX_EXR_STEM"]         # basename stem for the EXR
RES_X    = int(os.environ["CX_RES_X"])
RES_Y    = int(os.environ["CX_RES_Y"])
SPP      = int(os.environ["CX_SPP"])
SEED     = int(os.environ["CX_SEED"])
IS_REF   = os.environ["CX_IS_REF"] == "1"    # high-spp ground-truth render?
BOUNCES  = int(os.environ.get("CX_BOUNCES", "12"))
DEV_PREF = os.environ.get("CX_DEVICE", "AUTO")
FRAME    = int(os.environ.get("CX_FRAME", "1"))

bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
cyc = scene.cycles

# Fixed spp with adaptive OFF: we WANT the raw Monte-Carlo noise (two independent seeds
# must be genuinely independent realizations, not adaptively-equalized ones).
cyc.samples = SPP
cyc.use_adaptive_sampling = False
cyc.use_denoising = False                     # NEVER Cycles-denoise: our net does that
cyc.seed = SEED

cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

try:
    scene.frame_set(FRAME)
except Exception as e:
    _log("frame_set failed:", e)

# ---- AOV passes: Denoising Albedo/Normal (noise-free guides) + Z depth --------
# PROVEN PATTERN (2026-07-07): write the multilayer EXR directly via
# scene.render.image_settings, the SAME approach exp_render_stack.py /
# exp_render_temporal.py / exp_cycles_render_prod.py all use successfully on real
# hardware this session. An earlier version of this script used a manual compositor
# CompositorNodeOutputFile node graph (copied from exp_render_gbuffer.py) -- that
# pattern had NEVER been run successfully on real hardware (no ledger entries) and
# failed its first real test (rc=0, exr_found=False -- the compositor never wrote a
# file at all, only the main PNG still-image saved). Replaced with the proven direct
# approach: enabling view-layer pass flags + OPEN_EXR_MULTILAYER on the MAIN render
# output bundles Combined + all enabled passes into ONE file, no compositor needed.
vl = scene.view_layers[0]
vl.use_pass_combined = True
try:
    vl.use_pass_z = True
except Exception as e:
    _log("use_pass_z set failed:", e)
if not IS_REF:
    # Guides skipped on the reference render -- we only need its combined radiance.
    try:
        vl.cycles.denoising_store_passes = True
    except Exception as e:
        _log("denoising_store_passes set failed:", e)

# ---- Device ladder: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU ------------------
chosen_device = "CPU"
if DEV_PREF in ("AUTO", "GPU"):
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        picked = None
        for backend in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
            try:
                prefs.compute_device_type = backend
            except Exception:
                continue
            try:
                prefs.get_devices()
            except Exception:
                pass
            gpu_devs = [d for d in prefs.devices if getattr(d, "type", "CPU") != "CPU"]
            if gpu_devs:
                for d in prefs.devices:
                    d.use = (getattr(d, "type", "CPU") != "CPU")
                picked = backend
                break
        if picked:
            scene.cycles.device = 'GPU'
            chosen_device = f"GPU/{picked}"
        else:
            scene.cycles.device = 'CPU'
    except Exception as e:
        _log("GPU setup failed, using CPU:", e)
        scene.cycles.device = 'CPU'
        chosen_device = "CPU(gpu-setup-failed)"
else:
    scene.cycles.device = 'CPU'

scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
# The MAIN render output IS the multilayer EXR (proven pattern -- see note above).
# Blender appends the frame number to this path (e.g. STEM0001.exr); the driver-side
# run_blender() probes candidates for this exactly like exp_render_stack.py does.
scene.render.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
scene.render.image_settings.color_depth = '32'
scene.render.filepath = os.path.join(EXR_DIR, STEM)

_log(f"render {'REF' if IS_REF else 'NOISY'} spp={SPP} seed={SEED} "
     f"res={RES_X}x{RES_Y} bounces={BOUNCES} device={chosen_device}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)
bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def _resolve_exr_path(out_exr, frame):
    """Blender may append the frame number to the EXR path; probe the candidates.
    Identical logic to exp_render_stack.py's _resolve_exr_path (the proven helper) --
    kept as a local copy since this file imports exp_cycles_render_prod, not
    exp_render_stack, and duplicating one small pure function is simpler/safer than
    cross-importing between sibling pod runners."""
    candidates = [
        out_exr,
        out_exr + ".exr",
        f"{out_exr}{frame:04d}.exr",
        f"{out_exr}{frame:04d}",
    ]
    base = out_exr[:-4] if out_exr.endswith(".exr") else out_exr
    candidates += [f"{base}{frame:04d}.exr", f"{base}.exr", base]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def run_blender(blender_bin, script_path, *, blend, exr_dir, stem, res_x, res_y,
                spp, seed, is_ref, frame, bounces, device_pref, timeout_s):
    """Render ONE full frame into a multilayer EXR. Returns (wall_s, chosen_device,
    exr_path). Raises RuntimeError on non-zero exit / missing EXR."""
    import subprocess

    os.makedirs(exr_dir, exist_ok=True)
    out_exr = os.path.join(exr_dir, stem)
    env = dict(os.environ)
    env.update({
        "CX_BLEND": blend, "CX_EXR_DIR": exr_dir, "CX_EXR_STEM": stem,
        "CX_RES_X": str(res_x), "CX_RES_Y": str(res_y), "CX_SPP": str(spp),
        "CX_SEED": str(seed), "CX_IS_REF": "1" if is_ref else "0",
        "CX_FRAME": str(frame), "CX_BOUNCES": str(bounces), "CX_DEVICE": device_pref,
    })
    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s)
    wall_s = time.perf_counter() - t0

    chosen = "unknown"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen = line.split("=", 1)[1].strip()
    if (proc.stderr or "").strip():
        log("blender stderr tail:\n" + (proc.stderr or "")[-1200:])

    resolved = _resolve_exr_path(out_exr, frame)
    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and (resolved is not None)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender mint render failed (rc={proc.returncode}, "
            f"exr_found={resolved is not None}); stdout tail: {(proc.stdout or '')[-500:]}"
        )
    return wall_s, chosen, resolved


# --------------------------------------------------------------------------- #
# Read the multilayer EXR back into numpy arrays. Primary path: the OpenEXR      #
# python binding the driver installs (OpenEXR + Imath); OpenImageIO fallback.    #
# Channels are named "<Slot>.R" etc.; we suffix-match so a view-layer prefix is  #
# tolerated (same matching discipline as exp_render_gbuffer.load_exr_passes).    #
# --------------------------------------------------------------------------- #
def _match_stack(chan_names, arr_by_name, stem):
    """Return an (H,W,C) array for a slot `stem` from a name->2Darray map, or None.

    Handles both color-style (R/G/B) and vector-style (X/Y/Z) 3-channel passes — a
    Cycles "Denoising Normal" AOV routed through a multilayer slot commonly names its
    channels 'Normal.X/Y/Z', while 'Denoising Albedo' uses 'Albedo.R/G/B' — plus a
    single-channel depth ('Depth.V' / 'Depth.Z' / bare 'Depth'). Suffix-matched so a
    view-layer prefix ('ViewLayer.Depth.V') still resolves."""
    import numpy as np
    stem = stem.lower()
    comp = {}
    for cn, a in arr_by_name.items():
        low = cn.lower()
        for c in ("r", "g", "b", "a", "x", "y", "z", "v"):
            if low.endswith(f"{stem}.{c}"):
                comp[c] = a
        if low == stem:  # a bare single-channel slot with no component suffix
            comp["_single"] = a
    if {"r", "g", "b"} <= set(comp):          # color-style 3-vector
        return np.stack([comp["r"], comp["g"], comp["b"]], axis=-1).astype(np.float32)
    if {"x", "y", "z"} <= set(comp):          # normal/vector-style 3-vector
        return np.stack([comp["x"], comp["y"], comp["z"]], axis=-1).astype(np.float32)
    for single in ("v", "z", "y", "_single", "r"):  # depth-style single channel
        if single in comp:
            return comp[single][..., None].astype(np.float32)
    return None


def read_multilayer_exr(path, stems):
    """Return {stem: (H,W,C) float32}. Raises if the beauty layer can't be read."""
    import numpy as np

    # ---- primary: OpenEXR + Imath (installed by the driver's pip line) ----------
    try:
        import OpenEXR
        import Imath
        f = OpenEXR.InputFile(path)
        hdr = f.header()
        dw = hdr["dataWindow"]
        w = dw.max.x - dw.min.x + 1
        h = dw.max.y - dw.min.y + 1
        pt = Imath.PixelType(Imath.PixelType.FLOAT)
        arr_by_name = {}
        for cn in hdr["channels"].keys():
            raw = f.channel(cn, pt)
            arr_by_name[cn] = np.frombuffer(raw, dtype=np.float32).reshape(h, w)
        f.close()
        out = {}
        for stem in stems:
            a = _match_stack(list(arr_by_name.keys()), arr_by_name, stem)
            if a is not None:
                out[stem] = a
        if out:
            return out
    except ImportError:
        pass

    # ---- fallback: OpenImageIO (ships alongside Blender, sometimes pip-present) --
    try:
        import OpenImageIO as oiio  # type: ignore
        inp = oiio.ImageInput.open(path)
        if inp is not None:
            spec = inp.spec()
            chan_names = list(spec.channelnames)
            pixels = np.asarray(inp.read_image(format="float"), dtype=np.float32)
            inp.close()
            pixels = pixels.reshape(spec.height, spec.width, spec.nchannels)
            arr_by_name = {cn: pixels[..., i] for i, cn in enumerate(chan_names)}
            out = {}
            for stem in stems:
                a = _match_stack(chan_names, arr_by_name, stem)
                if a is not None:
                    out[stem] = a
            if out:
                return out
    except ImportError:
        pass

    raise RuntimeError(
        "could not read multilayer EXR (need OpenEXR+Imath or OpenImageIO); "
        f"path={path}"
    )


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
def main():
    import numpy as np

    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    scene_arg = str(params.get("scene", "classroom"))
    resolution = str(params.get("resolution", "960x540"))
    spp = int(params.get("spp", 32))
    ref_spp = int(params.get("ref_spp", 2048))
    frames = int(params.get("frames", 5))
    eval_fraction = float(params.get("eval_fraction", 0.2))
    n_crops = int(params.get("n_crops", 128))
    crop_size = int(params.get("crop_size", 128))
    base_seed = int(params.get("seed", 0))
    bounces = int(params.get("bounces", 12))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", prod.DEFAULT_BLENDER_URL))
    out_dir = str(params.get("out_dir", "/root/spec-lab/denoise_data"))

    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(64, int(rx)), max(64, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 960x540")
    if crop_size > min(res_x, res_y):
        raise RuntimeError(f"crop_size {crop_size} exceeds frame {res_x}x{res_y}")

    t_start = time.time()
    prod.ensure_system_libs()
    blender_bin = prod.ensure_blender(blender_url)
    blend, scene_key, fallback_note = prod.resolve_scene(scene_arg)

    patches_dir = os.path.join(out_dir, "patches")
    evalframes_dir = os.path.join(out_dir, "eval_frames")
    exr_scratch = os.path.join(out_dir, "_exr")
    for d in (patches_dir, evalframes_dir, exr_scratch):
        os.makedirs(d, exist_ok=True)

    # ---- train / eval frame split (eval frames are the LAST n_eval, held out) ---
    n_eval = int(round(eval_fraction * frames))
    if eval_fraction > 0:
        n_eval = max(1, min(frames, n_eval))
    n_eval = min(n_eval, frames)
    eval_frames = set(range(frames - n_eval, frames))
    train_frames = [f for f in range(frames) if f not in eval_frames]

    rng = random.Random(base_seed)
    render_noisy_s = 0.0
    render_ref_s = 0.0
    chosen_device = "unknown"
    patch_records = []
    eval_records = []
    # generous per-render timeout: a high-spp reference at 1080p is heavy on CPU.
    per_render_timeout = int(params.get("render_timeout_s", 2400))

    def _crop(a, y, x):
        return np.ascontiguousarray(a[y:y + crop_size, x:x + crop_size])

    # ---------------- TRAIN frames: N2N pairs cropped from full renders ----------
    for fidx in train_frames:
        seed_a = base_seed + 2 * fidx + 1
        seed_b = base_seed + 2 * fidx + 2
        stem_a = f"train_f{fidx}_a"
        stem_b = f"train_f{fidx}_b"
        wa, dev, exr_a = run_blender(
            blender_bin, _script_path(), blend=blend, exr_dir=exr_scratch, stem=stem_a,
            res_x=res_x, res_y=res_y, spp=spp, seed=seed_a, is_ref=False,
            frame=fidx + 1, bounces=bounces, device_pref=device_pref,
            timeout_s=per_render_timeout)
        wb, _, exr_b = run_blender(
            blender_bin, _script_path(), blend=blend, exr_dir=exr_scratch, stem=stem_b,
            res_x=res_x, res_y=res_y, spp=spp, seed=seed_b, is_ref=False,
            frame=fidx + 1, bounces=bounces, device_pref=device_pref,
            timeout_s=per_render_timeout)
        render_noisy_s += wa + wb
        chosen_device = dev

        a = read_multilayer_exr(exr_a, ["Combined", "Albedo", "Normal", "Depth"])
        b = read_multilayer_exr(exr_b, ["Combined"])
        noisy_a, noisy_b = a["Combined"], b["Combined"]
        albedo = a.get("Albedo", np.ones_like(noisy_a))
        normal = a.get("Normal", np.zeros_like(noisy_a))
        depth = a.get("Depth", np.zeros((noisy_a.shape[0], noisy_a.shape[1], 1),
                                        dtype=np.float32))
        H, W = noisy_a.shape[:2]
        if noisy_b.shape[:2] != (H, W):
            raise RuntimeError(f"noisy_a/noisy_b size mismatch {(H, W)} vs {noisy_b.shape[:2]}")

        for _ in range(n_crops):
            y = rng.randint(0, H - crop_size)
            x = rng.randint(0, W - crop_size)
            fn = os.path.join(patches_dir, f"patch_{len(patch_records):06d}.npz")
            np.savez_compressed(
                fn, noisy_a=_crop(noisy_a, y, x), noisy_b=_crop(noisy_b, y, x),
                albedo=_crop(albedo, y, x), normal=_crop(normal, y, x),
                depth=_crop(depth, y, x))
            patch_records.append({"file": os.path.basename(fn), "split": "train",
                                  "frame": fidx, "y": y, "x": x})
        for p in (exr_a, exr_b):
            try:
                os.remove(p)
            except OSError:
                pass
        log(f"train frame {fidx}: +{n_crops} patches (render {wa:.1f}+{wb:.1f}s)")

    # ---------------- EVAL frames: full-frame sample + true reference ------------
    for fidx in sorted(eval_frames):
        seed_a = base_seed + 2 * fidx + 1
        wa, dev, exr_a = run_blender(
            blender_bin, _script_path(), blend=blend, exr_dir=exr_scratch,
            stem=f"eval_f{fidx}_a", res_x=res_x, res_y=res_y, spp=spp, seed=seed_a,
            is_ref=False, frame=fidx + 1, bounces=bounces, device_pref=device_pref,
            timeout_s=per_render_timeout)
        wr, _, exr_r = run_blender(
            blender_bin, _script_path(), blend=blend, exr_dir=exr_scratch,
            stem=f"eval_f{fidx}_ref", res_x=res_x, res_y=res_y, spp=ref_spp,
            seed=base_seed + 9999, is_ref=True, frame=fidx + 1, bounces=bounces,
            device_pref=device_pref, timeout_s=per_render_timeout)
        render_noisy_s += wa
        render_ref_s += wr
        chosen_device = dev

        a = read_multilayer_exr(exr_a, ["Combined", "Albedo", "Normal", "Depth"])
        r = read_multilayer_exr(exr_r, ["Combined"])
        noisy_a = a["Combined"]
        albedo = a.get("Albedo", np.ones_like(noisy_a))
        normal = a.get("Normal", np.zeros_like(noisy_a))
        depth = a.get("Depth", np.zeros((noisy_a.shape[0], noisy_a.shape[1], 1),
                                        dtype=np.float32))
        true_reference = r["Combined"]
        if true_reference.shape[:2] != noisy_a.shape[:2]:
            raise RuntimeError("eval noisy/reference size mismatch")

        fn = os.path.join(evalframes_dir, f"eval_{len(eval_records):04d}.npz")
        np.savez_compressed(
            fn, noisy_a=noisy_a, albedo=albedo, normal=normal, depth=depth,
            true_reference=true_reference)
        eval_records.append({"file": os.path.basename(fn), "split": "eval",
                             "frame": fidx, "H": int(noisy_a.shape[0]),
                             "W": int(noisy_a.shape[1])})
        for p in (exr_a, exr_r):
            try:
                os.remove(p)
            except OSError:
                pass
        log(f"eval frame {fidx}: full-frame sample + reference "
            f"(noisy {wa:.1f}s, ref@{ref_spp}spp {wr:.1f}s)")

    manifest = {
        "scene": scene_arg, "scene_key": scene_key, "resolution": resolution,
        "res_x": res_x, "res_y": res_y, "spp": spp, "ref_spp": ref_spp,
        "frames": frames, "eval_fraction": eval_fraction,
        "train_frames": train_frames, "eval_frames": sorted(eval_frames),
        "n_crops_per_frame": n_crops, "crop_size": crop_size, "bounces": bounces,
        "base_seed": base_seed, "device": chosen_device,
        "patches": patch_records, "eval_samples": eval_records,
        "n_patches": len(patch_records), "n_eval_frames": len(eval_records),
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh)

    note = (
        f"Noise2Noise data from OUR Cycles pipeline: {len(patch_records)} train patches "
        f"({crop_size}x{crop_size}, cropped from {len(train_frames)} full {res_x}x{res_y} "
        f"renders @ {spp}spp) + {len(eval_records)} held-out eval frames with a "
        f"{ref_spp}spp reference. Albedo/Normal are Cycles denoising-data AOVs; Depth is "
        f"the Z pass. Guides extracted once per frame (noise-free). No clean target used "
        f"for training (N2N)."
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if "CPU" in chosen_device:
        note += " NOTE: ran on CPU (no usable Cycles GPU device found)."

    emit({
        "ok": True,
        "out_dir": out_dir,
        "manifest_path": manifest_path,
        "n_patches": len(patch_records),
        "n_train_frames": len(train_frames),
        "n_eval_frames": len(eval_records),
        "scene": scene_arg, "scene_key": scene_key, "resolution": resolution,
        "spp": spp, "ref_spp": ref_spp, "crop_size": crop_size, "n_crops": n_crops,
        "eval_fraction": eval_fraction, "device": chosen_device,
        "render_noisy_s": round(render_noisy_s, 1),
        "render_ref_s": round(render_ref_s, 1),
        "total_s": round(time.time() - t_start, 1),
        "modeled": False,
        "note": note,
    })


# The Blender script is written to a temp file once and reused across renders.
_SCRIPT_PATH = None


def _script_path():
    global _SCRIPT_PATH
    if _SCRIPT_PATH is None:
        import tempfile
        fd, p = tempfile.mkstemp(suffix="_mint.py", prefix="cx_")
        with os.fdopen(fd, "w") as fh:
            fh.write(BLENDER_MINT_SCRIPT)
        _SCRIPT_PATH = p
    return _SCRIPT_PATH


if __name__ == "__main__":
    try:
        prod.ensure_pydeps()
        main()
    except Exception as e:  # noqa: BLE001 — one honest error line, never a fake number
        import traceback
        traceback.print_exc()
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(1)
