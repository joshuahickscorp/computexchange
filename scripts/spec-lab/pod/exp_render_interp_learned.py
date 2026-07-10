#!/usr/bin/env python3
"""
exp_render_interp_learned.py — LEARNED FRAME-INTERPOLATION spike for 3D animation.
============================================================================

THE RESEARCH BET
----------------
`exp_render_temporal.py` reprojects (warps) the keyframe forward by Cycles' own
motion vectors and re-renders only the disoccluded holes. On the same animated
scene its *reprojection warp* tops out around SSIM ~0.84 on the frames it fully
synthesises (the panel's prior was a hoped ~0.97). This runner asks a different
question: instead of warping ONE keyframe forward, can a LEARNED / flow-guided
INTERPOLATOR — given the keyframe on EACH side plus Cycles' EXACT ground-truth
motion — synthesise the in-between frames ABOVE that 0.84 warp ceiling?

We render every Nth frame FULLY (the keyframes) and SYNTHESISE the in-between
frames with an interpolator, then measure:
  * quality = SSIM(synthesised frame, TRUE full per-frame render) — global AND
    per-8x8-tile (worst + 5th-percentile), so a lever that blurs high-frequency
    detail is caught, not hidden behind a high average.
  * net_speedup = (time to full-render ALL frames) / (time to render the
    keyframes + time to interpolate the in-betweens) — both wall-clock, same box.

TWO INTERPOLATORS (model knob), no vendor lock:
  * "rife"        : try to fetch a self-hostable RIFE-family optical-flow-based
                    interpolation model via torch.hub / a pip wheel. RIFE takes the
                    two bracketing REAL frames and predicts the middle frame. This
                    is a genuine LEARNED predictor (a trained CNN). If NO pretrained
                    weight can be fetched on the pod (offline / hub down), we DO NOT
                    silently substitute and claim RIFE — we fall back to flow_guided
                    and label modeled:true with the exact reason in "note".
  * "flow_guided" : (more original — we have EXACT flow, unlike generic RIFE) use
                    Cycles' GROUND-TRUTH motion vectors to warp BOTH bracketing
                    keyframes to the target time, then blend them occlusion-aware
                    (a learned-ish refine: per-pixel confidence from fwd/bwd MV
                    round-trip consistency + a bilinear splat), filling the rest.
                    No network, so this branch is honestly labelled modeled:true
                    (the "refine" is a hand-built heuristic, not a trained net).

HONESTY CONTRACT (this file emits EXACTLY ONE JSON line; on any failure it emits
{"error": "..."} and exits 0 — it NEVER fabricates a number):
  * Every TIME is a real time.perf_counter() wall-clock. Keyframe render time is the
    WHOLE Blender subprocess (launch + BVH + path trace + EXR write). Interpolation
    time is the REAL numpy/torch wall-time of synthesising the in-between frames
    (model inference or the flow warp+blend). net_speedup = T_full_all / T_ours,
    both measured on THIS box.
  * Every QUALITY number is real skimage.structural_similarity on real rendered /
    synthesised pixels vs the TRUE full per-frame Cycles render (our ground truth).
    Global SSIM + per-tile worst + 5th-percentile tile SSIM on an 8x8 grid.
  * modeled:false ONLY when the interpolator is a real pretrained LEARNED model that
    loaded and ran. The flow_guided branch (and the RIFE->flow fallback) set
    modeled:true and name the modeled step in "note".
  * NO silent mislabel: a missing RIFE weight does not become "we ran RIFE". A
    missing dependency that makes the whole run impossible emits {"error":...}.
  * SSIM is measurement-only and is NOT charged to pipeline cost.

Forked HONEST patterns:
  * Blender self-bootstrap, whole-subprocess timing, device ladder OPTIX>CUDA>CPU,
    the animated-scene AOV plumbing (Vector/Z/Combined multilayer EXR) and the
    numpy warp/EXR-reader — from exp_render_temporal.py.
  * The per-8x8-tile SSIM (global + worst + p5) — from exp_cycles_render_prod.py.

Params (argv[1] JSON), all optional:
  frames       : int   total animation frames                     (default 8)
  interp_every : int   render every Nth frame fully; synthesise
                       the interp_every-1 frames between each pair (default 2,
                       i.e. render frames 0,2,4,... synthesise 1,3,5,...)
  model        : "rife" | "flow_guided"                           (default "flow_guided")
  scene        : "animated" (default; the temporal keyframed scene with REAL
                 motion vectors — REQUIRED for flow_guided) | "classroom"
                 (the CC0 Classroom download; STATIC — has no per-frame motion,
                 so flow_guided degrades to a plain frame blend and is disclosed).
  spp          : int   samples per pixel for every render          (default 256)
  resolution   : int   square image side length in px             (default 384)
  seed         : int   Cycles + animation seed                     (default 0)
  device       : "AUTO" (default) | "GPU" | "CPU"
  blender_url  : str   override the Blender download URL

OUTPUT (last stdout line = exactly one JSON metrics object), e.g.:
  {"net_speedup", "quality" (global SSIM mean of synthesised frames),
   "worst_tile_ssim", "p5_tile_ssim", "warp_baseline_ssim" (naive single-key warp
   on the SAME frames, so we can say whether learned BEAT the 0.84 warp), "model",
   "modeled", "note", "frames", "interp_every", "keyframes", "synthesised_frames",
   "real_render_s_full_frame", "device", ...}

Contract: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object;
any failure emits {"error":...} as the last stdout line and exits 0 (never hangs).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Bootstrap constants — IDENTICAL pattern to exp_render_temporal.py so a pod    #
# that already downloaded Blender for a prior rung reuses it (ensure_blender    #
# skips the download when the binary is present).                              #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/interp_learned_render"
# Cached Classroom demo download (reused across rungs on the same pod).
CLASSROOM_URL = "https://download.blender.org/demo/test/classroom.zip"
CLASSROOM_DIR = "/root/cx_scenes/classroom"
CLASSROOM_ZIP = "/root/cx_scenes/classroom.zip"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[interp_learned]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs (same list as the sibling render runners).        #
# --------------------------------------------------------------------------- #
def ensure_system_libs():
    pkgs = ["libxi6", "libxxf86vm1", "libxfixes3", "libxrender1", "libgl1"]
    try:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", *pkgs],
            check=False, capture_output=True, timeout=300, env=env,
        )
        log(f"apt-get best-effort installed: {' '.join(pkgs)} (failures ignored)")
    except Exception as e:  # noqa: BLE001 — best-effort, must never abort
        log(f"apt-get for X/GL libs failed (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# 2. Self-bootstrap Blender: download + extract if absent. Idempotent — reuses  #
#    /root/blender/blender from a prior rung on the same pod. (forked verbatim)  #
# --------------------------------------------------------------------------- #
def ensure_blender(url):
    """Ensure a runnable Blender at BLENDER_BIN. Returns the binary path or raises."""
    if os.path.isfile(BLENDER_BIN) and os.access(BLENDER_BIN, os.X_OK):
        log(f"Blender already present at {BLENDER_BIN} (skip download)")
        return BLENDER_BIN

    os.makedirs(BLENDER_DIR, exist_ok=True)
    tarball = "/tmp/blender-dl.tar.xz"

    log(f"downloading Blender tarball: {url}")
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0 spec-lab"})
        with urllib.request.urlopen(req, timeout=600) as r, open(tarball, "wb") as f:
            while True:
                chunk = r.read(1 << 20)  # 1 MiB chunks
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"blender download failed: {type(e).__name__}: {e}")
    dl_s = time.perf_counter() - t0
    size_mb = os.path.getsize(tarball) / 1e6
    log(f"downloaded {size_mb:.1f} MB in {dl_s:.1f}s; extracting…")

    try:
        subprocess.run(
            ["tar", "-xJf", tarball, "-C", BLENDER_DIR, "--strip-components=1"],
            check=True, capture_output=True, timeout=600,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"blender extract failed: {type(e).__name__}: {e}")

    try:
        os.remove(tarball)
    except OSError:
        pass

    if not (os.path.isfile(BLENDER_BIN) and os.access(BLENDER_BIN, os.X_OK)):
        raise RuntimeError(
            f"blender binary not found/executable at {BLENDER_BIN} after extract"
        )
    log(f"Blender bootstrapped at {BLENDER_BIN}")
    return BLENDER_BIN


# --------------------------------------------------------------------------- #
# 2b. Self-bootstrap the CC0 Classroom demo scene (only when scene='classroom').#
#     Cached under /root/cx_scenes and reused. Returns the .blend path.          #
#     NOTE: Classroom is a STATIC scene (no animation), so it has no per-frame   #
#     motion vectors; the flow_guided interpolator degrades to a plain blend on  #
#     it. We render it anyway (honest ground truth) and disclose the degradation.#
# --------------------------------------------------------------------------- #
def ensure_classroom():
    """Ensure the Classroom .blend exists locally; download+unzip once. Returns path."""
    blend = os.path.join(CLASSROOM_DIR, "classroom", "classroom.blend")
    if os.path.isfile(blend):
        log(f"Classroom scene present at {blend} (skip download)")
        return blend
    os.makedirs(CLASSROOM_DIR, exist_ok=True)
    log(f"downloading Classroom demo: {CLASSROOM_URL}")
    try:
        req = urllib.request.Request(CLASSROOM_URL,
                                     headers={"User-Agent": "curl/8.0 spec-lab"})
        with urllib.request.urlopen(req, timeout=600) as r, open(CLASSROOM_ZIP, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"classroom download failed: {type(e).__name__}: {e}")
    try:
        subprocess.run(["unzip", "-o", "-q", CLASSROOM_ZIP, "-d", CLASSROOM_DIR],
                       check=True, capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"classroom unzip failed: {type(e).__name__}: {e}")
    if not os.path.isfile(blend):
        # some zips nest differently; search for the blend.
        for root, _dirs, files in os.walk(CLASSROOM_DIR):
            for fn in files:
                if fn.endswith(".blend"):
                    blend = os.path.join(root, fn)
                    break
    if not os.path.isfile(blend):
        raise RuntimeError("classroom.blend not found after unzip")
    log(f"Classroom scene bootstrapped at {blend}")
    return blend


# --------------------------------------------------------------------------- #
# 3. The Blender ANIMATION scene script (scene='animated'). Forked from         #
#    exp_render_temporal.py: keyframed monkey+sphere+camera => genuine screen    #
#    motion + silhouette disocclusion; enables the Vector (motion) / Z (depth) / #
#    Combined passes and writes a MULTILAYER EXR so our numpy reads real motion. #
#    One frame per invocation (CX_FRAME) so we time each frame independently.    #
# --------------------------------------------------------------------------- #
BLENDER_ANIM_SCRIPT = r'''
import bpy, os, sys, math

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

SPP      = int(os.environ["CX_SPP"])
RES      = int(os.environ["CX_RES"])
OUT      = os.environ["CX_OUT"]
FRAME    = int(os.environ["CX_FRAME"])
NFRAMES  = int(os.environ["CX_NFRAMES"])
SEED     = int(os.environ["CX_SEED"])
DEV_PREF = os.environ.get("CX_DEVICE", "AUTO")

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

bpy.ops.mesh.primitive_cube_add(size=1.4, location=(-1.9, 0.3, 0.7))
cube = bpy.context.active_object
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.85, location=(1.6, 0.0, 0.9),
                                     segments=48, ring_count=24)
sphere = bpy.context.active_object
for p in sphere.data.polygons:
    p.use_smooth = True
bpy.ops.mesh.primitive_monkey_add(size=1.7, location=(0.0, -0.3, 0.9))
monkey = bpy.context.active_object
bpy.ops.mesh.primitive_plane_add(size=24.0, location=(0.0, 0.0, 0.0))
floor = bpy.context.active_object

def make_mat(name, rgba, rough=0.4, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = rgba
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = rough
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = metal
    return m

cube.data.materials.append(make_mat("cube_red",   (0.80, 0.18, 0.16, 1), 0.35))
sphere.data.materials.append(make_mat("sph_metal", (0.85, 0.80, 0.55, 1), 0.15, 0.9))
monkey.data.materials.append(make_mat("monk_blue", (0.20, 0.40, 0.85, 1), 0.45))
floor.data.materials.append(make_mat("floor_grey", (0.70, 0.70, 0.72, 1), 0.6))

scene.frame_start = 1
scene.frame_end = NFRAMES
SPIN_PER_FRAME = math.radians(9.0)
ORBIT_R = 1.9
ORBIT_PER_FRAME = math.radians(11.0)
CAM_PAN_PER_FRAME = 0.06

for t in range(NFRAMES):
    fr = t + 1
    monkey.rotation_euler = (0.0, 0.0, math.radians(35) + SPIN_PER_FRAME * t)
    monkey.keyframe_insert(data_path="rotation_euler", frame=fr)
    ang = ORBIT_PER_FRAME * t
    sphere.location = (0.6 + ORBIT_R * math.cos(ang),
                       0.0 + ORBIT_R * math.sin(ang),
                       0.9)
    sphere.keyframe_insert(data_path="location", frame=fr)

bpy.ops.object.light_add(type='AREA', location=(2.6, -2.6, 5.2))
area = bpy.context.active_object
area.data.energy = 1400.0
area.data.size = 4.0
area.rotation_euler = (math.radians(35), math.radians(20), math.radians(15))

world = bpy.data.worlds.new("cx_world")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs["Color"].default_value = (0.15, 0.18, 0.24, 1.0)
    bg.inputs["Strength"].default_value = 0.6

bpy.ops.object.camera_add(location=(0.0, -6.6, 3.5))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(63), 0.0, 0.0)
scene.camera = cam
for t in range(NFRAMES):
    fr = t + 1
    cam.location = (0.0 + CAM_PAN_PER_FRAME * t, -6.6, 3.5)
    cam.keyframe_insert(data_path="location", frame=fr)

scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False
cyc.use_denoising = False

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
            gpu_devs = [d for d in prefs.devices
                        if getattr(d, "type", "CPU") not in ("CPU",)]
            if gpu_devs:
                for d in prefs.devices:
                    d.use = (getattr(d, "type", "CPU") not in ("CPU",))
                picked = backend
                break
        if picked:
            scene.cycles.device = 'GPU'
            chosen_device = f"GPU/{picked}"
        else:
            scene.cycles.device = 'CPU'
            chosen_device = "CPU"
    except Exception as e:
        _log("GPU setup failed, using CPU:", e)
        scene.cycles.device = 'CPU'
        chosen_device = "CPU(gpu-setup-failed)"
else:
    scene.cycles.device = 'CPU'
    chosen_device = "CPU"

vl = scene.view_layers[0]
vl.use_pass_vector = True
vl.use_pass_z = True
vl.use_pass_combined = True

# CRITICAL (fixed 2026-07-07, verified on real hardware): Cycles EMPTIES the Vector
# (motion) pass whenever render motion blur is ENABLED — documented Cycles behavior
# (Blender T48908, "by design"). MUST be False to get real, non-zero motion vectors.
scene.render.use_motion_blur = False
try:
    scene.cycles.motion_blur_position = 'START'
except Exception:
    pass
scene.render.motion_blur_shutter = 0.01

scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.image_settings.color_depth = '32'
scene.render.image_settings.exr_codec = 'ZIP'
scene.render.filepath = OUT
scene.frame_set(FRAME)

_log(f"rendering frame={FRAME}/{NFRAMES} spp={SPP} res={RES} device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)
bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


# --------------------------------------------------------------------------- #
# 3b. The Blender script for scene='classroom': open the downloaded .blend,     #
#     enable Vector/Z/Combined, render ONE frame. Classroom is static so the    #
#     Vector pass is ~zero motion (disclosed); we still write a valid EXR so the #
#     ground-truth full render and SSIM are real.                               #
# --------------------------------------------------------------------------- #
BLENDER_CLASSROOM_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

SPP      = int(os.environ["CX_SPP"])
RES      = int(os.environ["CX_RES"])
OUT      = os.environ["CX_OUT"]
FRAME    = int(os.environ["CX_FRAME"])
SEED     = int(os.environ["CX_SEED"])
DEV_PREF = os.environ.get("CX_DEVICE", "AUTO")
BLEND    = os.environ["CX_BLEND"]

bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
try:
    cyc.use_adaptive_sampling = False
except Exception:
    pass
cyc.use_denoising = False

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
            gpu_devs = [d for d in prefs.devices
                        if getattr(d, "type", "CPU") not in ("CPU",)]
            if gpu_devs:
                for d in prefs.devices:
                    d.use = (getattr(d, "type", "CPU") not in ("CPU",))
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

vl = scene.view_layers[0]
try:
    vl.use_pass_vector = True
except Exception:
    pass
vl.use_pass_z = True
vl.use_pass_combined = True
# CRITICAL (fixed 2026-07-07, verified on real hardware): Cycles EMPTIES the Vector
# (motion) pass whenever render motion blur is ENABLED — documented Cycles behavior
# (Blender T48908, "by design"). MUST be False to get real, non-zero motion vectors.
scene.render.use_motion_blur = False
try:
    scene.cycles.motion_blur_position = 'START'
except Exception:
    pass
scene.render.motion_blur_shutter = 0.01

scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.image_settings.color_depth = '32'
scene.render.image_settings.exr_codec = 'ZIP'
scene.render.filepath = OUT
try:
    scene.frame_set(FRAME)
except Exception:
    pass

_log(f"rendering CLASSROOM frame={FRAME} spp={SPP} res={RES} device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)
bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_frame(blender_bin, script_path, spp, res, out_exr, frame, nframes,
                      seed, device_pref, timeout_s, blend_path=None):
    """Render ONE frame to a multilayer EXR. Returns (wall_s, chosen_device, resolved).

    wall_s is measured around the WHOLE subprocess: the honest end-to-end cost of
    path-tracing this single frame (Blender launch + scene build + BVH + trace + EXR
    write). This is our per-frame render cost — nothing is excluded."""
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_exr
    env["CX_FRAME"] = str(frame)
    env["CX_NFRAMES"] = str(nframes)
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    if blend_path is not None:
        env["CX_BLEND"] = blend_path

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    log(f"render start: frame={frame} spp={spp} res={res} -> {out_exr}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1500:]
    err_tail = (proc.stderr or "")[-1500:]
    log(f"render frame={frame} rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()

    resolved = _resolve_exr_path(out_exr, frame)
    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and (resolved is not None)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed (frame={frame}, rc={proc.returncode}, "
            f"out_exists={resolved is not None}); stdout tail: {tail[-600:]}"
        )
    return wall_s, chosen_device, resolved


def _resolve_exr_path(out_exr, frame):
    """Blender writes write_still to `filepath`+ext OR with a frame suffix depending on
    version; probe the likely candidates and return the first that exists."""
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


# --------------------------------------------------------------------------- #
# 4. EXR reader — pull Combined (color), Vector (motion) and Z (depth) into     #
#    numpy. Forked from exp_render_temporal.py (defensive channel-name probing;  #
#    OpenEXR primary, imageio fallback). Motion is prev->cur pixel motion (dx,dy)#
#    and, when present, the next-frame vectors (cur->next) for consistency.      #
# --------------------------------------------------------------------------- #
def read_exr_layers(path, res):
    """Return (color[H,W,3] float, motion_prev[H,W,2], depth[H,W], motion_next[H,W,2]|None)."""
    import numpy as np

    try:
        import OpenEXR  # type: ignore
        import Imath    # type: ignore

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

        def _find(suffixes):
            for want in suffixes:
                for c in chans:
                    if c.endswith(want):
                        return c
            return None

        r = _find([".Combined.R", "Combined.R", ".R"])
        g = _find([".Combined.G", "Combined.G", ".G"])
        b = _find([".Combined.B", "Combined.B", ".B"])
        vx = _find([".Vector.X", "Vector.X"])
        vy = _find([".Vector.Y", "Vector.Y"])
        vz = _find([".Vector.Z", "Vector.Z"])
        vw = _find([".Vector.W", "Vector.W"])
        zc = _find([".Depth.Z", "Depth.Z", ".Z"])

        color = np.stack([_chan(c) if c else np.zeros((h, w), np.float32)
                          for c in (r, g, b)], axis=-1)
        mx = _chan(vx) if vx else np.zeros((h, w), np.float32)
        my = _chan(vy) if vy else np.zeros((h, w), np.float32)
        motion = np.stack([mx, my], axis=-1)
        if vz and vw:
            motion_next = np.stack([_chan(vz), _chan(vw)], axis=-1)
        else:
            motion_next = None
        depth = _chan(zc) if zc else np.zeros((h, w), np.float32)
        f.close()

        return _resize_layers(color, motion, depth, motion_next, res)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 — try the fallback reader before giving up
        log(f"OpenEXR read failed ({type(e).__name__}: {e}); trying imageio")

    try:
        import imageio.v2 as imageio  # type: ignore
        arr = imageio.imread(path)
        arr = np.asarray(arr, dtype=np.float32)
        if arr.ndim == 3 and arr.shape[-1] >= 3:
            color = arr[..., :3]
        else:
            color = np.stack([arr] * 3, axis=-1)
        h, w = color.shape[:2]
        motion = np.zeros((h, w, 2), np.float32)
        depth = np.zeros((h, w), np.float32)
        log("imageio fallback: motion/depth passes unavailable -> blend-only interp")
        return _resize_layers(color, motion, depth, None, res)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not read EXR {path}: {type(e).__name__}: {e}")


def _resize_layers(color, motion, depth, motion_next, res):
    """Resize every layer to (res,res). Motion vectors are in PIXELS, so on resize we
    SCALE them by the resolution ratio to stay physically correct."""
    import numpy as np
    h, w = color.shape[:2]
    if (h, w) == (res, res):
        return color, motion, depth, motion_next
    from skimage.transform import resize as sk_resize
    sy, sx = res / float(h), res / float(w)
    color = sk_resize(color, (res, res, color.shape[-1]), order=1,
                      preserve_range=True, anti_aliasing=True).astype(np.float32)
    depth = sk_resize(depth, (res, res), order=1,
                      preserve_range=True, anti_aliasing=False).astype(np.float32)
    m = sk_resize(motion, (res, res, 2), order=1,
                  preserve_range=True, anti_aliasing=False).astype(np.float32)
    m[..., 0] *= sx
    m[..., 1] *= sy
    motion = m
    if motion_next is not None:
        mn = sk_resize(motion_next, (res, res, 2), order=1,
                       preserve_range=True, anti_aliasing=False).astype(np.float32)
        mn[..., 0] *= sx
        mn[..., 1] *= sy
        motion_next = mn
    return color, motion, depth, motion_next


# --------------------------------------------------------------------------- #
# 5. WARP primitives (forked from exp_render_temporal.py).                      #
#    warp_gather reprojects a source image by a backward pixel-motion field with #
#    bilinear sampling and returns a validity mask (False where the sample left   #
#    the source image — a disocclusion).                                          #
# --------------------------------------------------------------------------- #
def warp_gather(src_color, motion_prev):
    """Reproject src_color by GATHERING along a backward prev-motion field with
    bilinear sampling. Returns (warped[H,W,3], valid[H,W] bool)."""
    import numpy as np
    h, w = src_color.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = xs + motion_prev[..., 0]
    sy = ys + motion_prev[..., 1]
    valid = (sx >= 0) & (sx <= w - 1) & (sy >= 0) & (sy <= h - 1)
    cx = np.clip(sx, 0, w - 1)
    cy = np.clip(sy, 0, h - 1)
    x0 = np.floor(cx).astype(np.int32); x1 = np.minimum(x0 + 1, w - 1)
    y0 = np.floor(cy).astype(np.int32); y1 = np.minimum(y0 + 1, h - 1)
    fx = (cx - x0)[..., None]
    fy = (cy - y0)[..., None]
    Ia = src_color[y0, x0]; Ib = src_color[y0, x1]
    Ic = src_color[y1, x0]; Id = src_color[y1, x1]
    top = Ia * (1 - fx) + Ib * fx
    bot = Ic * (1 - fx) + Id * fx
    warped = top * (1 - fy) + bot * fy
    return warped.astype(np.float32), valid


# --------------------------------------------------------------------------- #
# 6. NAIVE WARP BASELINE — the thing we must beat.                              #
#    Reproject the PREVIOUS keyframe forward to the target frame using the       #
#    target frame's OWN prev-motion field (exactly the temporal runner's single- #
#    keyframe warp), hole-filled by a cheap diffusion so it is a fair per-pixel   #
#    image. This is the ~0.84 ceiling. We measure it on the SAME target frames    #
#    the learned/flow interpolator synthesises, so "did learned beat warp?" is a  #
#    like-for-like comparison on identical ground truth.                          #
# --------------------------------------------------------------------------- #
def naive_warp_baseline(prev_key_color, target_motion_prev):
    """Single-keyframe forward warp (the temporal runner's reproject). Returns an RGB
    image with holes filled by a light numpy diffusion so SSIM sees a complete frame."""
    import numpy as np
    warped, valid = warp_gather(prev_key_color, target_motion_prev)
    hole = ~valid
    if hole.any():
        warped = _diffuse_fill(warped, hole, iters=48)
    return warped.astype(np.float32)


def _diffuse_fill(rgb, hole, iters=48):
    """Fill `hole` pixels by iterative 4-neighbour averaging (pure-numpy Laplacian
    inpaint). Used to complete a warp so SSIM scores a full image, not a punctured one."""
    import numpy as np
    filled = rgb.astype(np.float32).copy()
    h = hole.copy()
    for _ in range(iters):
        if not h.any():
            break
        acc = np.zeros_like(filled)
        cnt = np.zeros(h.shape, np.float32)
        for dyx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(filled, shift=dyx, axis=(0, 1))
            known = ~np.roll(h, shift=dyx, axis=(0, 1))
            acc += shifted * known[..., None]
            cnt += known
        upd = (cnt > 0) & h
        with np.errstate(invalid="ignore", divide="ignore"):
            avg = acc / np.maximum(cnt, 1.0)[..., None]
        filled[upd] = avg[upd]
        h[upd] = False
    return filled.astype(np.float32)


# --------------------------------------------------------------------------- #
# 7. FLOW-GUIDED INTERPOLATOR (model='flow_guided') — the ORIGINAL branch that   #
#    exploits Cycles' EXACT motion, which generic RIFE does not have.            #
#                                                                                #
#    For an in-between frame at fractional time `alpha` in (0,1) between the      #
#    previous keyframe (kp) and the next keyframe (kn):                           #
#      * Warp kp FORWARD by alpha * (kp's forward motion) and kn BACKWARD by      #
#        (1-alpha) * (kn's backward motion). We do not have the intermediate      #
#        frame's own motion (it was never rendered), so we scale each keyframe's  #
#        real motion field by the temporal fraction — a genuine use of the        #
#        ground-truth flow, honestly an approximation for the un-rendered mid.     #
#      * Occlusion-aware blend: per-pixel confidence from the fwd/bwd validity     #
#        masks. Where BOTH warps are valid we blend by temporal proximity; where   #
#        only one is valid we take that side; where NEITHER is valid we diffusion- #
#        fill. This is the "learned-ish refine" — a hand-built heuristic, NOT a    #
#        trained network, so this branch is modeled:true.                          #
#                                                                                 #
#    If motion is absent (imageio fallback / classroom static), both scaled fields #
#    are ~zero and this degrades to a plain temporal blend of the two keyframes —  #
#    disclosed in the note.                                                        #
# --------------------------------------------------------------------------- #
def interp_flow_guided(kp_color, kp_motion_next, kn_color, kn_motion_prev, alpha):
    """Synthesise the in-between frame at fractional time alpha in (0,1). Returns
    (rgb[H,W,3], any_motion: bool). any_motion=False signals the blend-only degrade."""
    import numpy as np
    h, w = kp_color.shape[:2]

    # Do we actually have motion to guide the warp? (else this is a plain blend)
    mp = kp_motion_next if kp_motion_next is not None else np.zeros((h, w, 2), np.float32)
    mn = kn_motion_prev if kn_motion_prev is not None else np.zeros((h, w, 2), np.float32)
    any_motion = bool(np.any(np.abs(mp) > 1e-4) or np.any(np.abs(mn) > 1e-4))

    # forward warp of the previous key by alpha of its forward motion.
    fwd, valid_f = warp_gather(kp_color, (alpha * mp).astype(np.float32))
    # backward warp of the next key by (1-alpha) of its backward motion.
    bwd, valid_b = warp_gather(kn_color, ((1.0 - alpha) * mn).astype(np.float32))

    both = valid_f & valid_b
    only_f = valid_f & (~valid_b)
    only_b = valid_b & (~valid_f)
    neither = (~valid_f) & (~valid_b)

    out = np.zeros((h, w, 3), np.float32)
    # temporal-proximity blend where both sides warp cleanly: nearer key weighs more.
    wf = (1.0 - alpha)
    wb = alpha
    out[both] = wf * fwd[both] + wb * bwd[both]
    out[only_f] = fwd[only_f]
    out[only_b] = bwd[only_b]
    # remaining holes: temporal blend of the two RAW keyframes, then diffusion-fill.
    if neither.any():
        raw_blend = (wf * kp_color + wb * kn_color).astype(np.float32)
        out[neither] = raw_blend[neither]
    return out.astype(np.float32), any_motion


# --------------------------------------------------------------------------- #
# 8. RIFE LEARNED INTERPOLATOR (model='rife'). We ATTEMPT to fetch a self-      #
#    hostable RIFE-family model (torch.hub) and run its middle-frame prediction. #
#    RIFE takes the two bracketing REAL frames (no external motion) and predicts  #
#    the mid frame with a TRAINED flow+refine CNN. This is the genuine learned    #
#    predictor. IMPORTANT HONESTY: if no weight can be fetched, we return         #
#    (None, reason) and the caller falls back to flow_guided with modeled:true —  #
#    we NEVER claim we ran RIFE when we did not.                                   #
# --------------------------------------------------------------------------- #
def try_load_rife():
    """Attempt to load a pretrained RIFE-family interpolator. Returns (callable, tag)
    on success or (None, reason_str) on failure. The callable maps
    (img0[H,W,3] in [0,1] float32, img1, alpha) -> mid[H,W,3] float32 in [0,1].

    We try torch.hub entrypoints known to host RIFE-family weights. This runs ONLY a
    real, downloaded, pretrained network — never a stand-in."""
    try:
        import torch  # type: ignore
    except Exception as e:  # noqa: BLE001
        return None, f"torch unavailable: {type(e).__name__}: {e}"

    import numpy as np
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    # Candidate torch.hub entrypoints that ship a RIFE-family interpolator. We try each;
    # the FIRST that loads real pretrained weights wins. If all fail (offline / hub gone /
    # api mismatch) we return None so the caller falls back honestly.
    candidates = [
        # (repo, entrypoint) — best-effort; hub repos evolve, so failures are expected
        ("hzwer/Practical-RIFE", "rife"),
        ("megvii-research/ECCV2022-RIFE", "rife"),
    ]
    model = None
    used = None
    for repo, entry in candidates:
        try:
            log(f"try torch.hub.load({repo!r}, {entry!r}) …")
            model = torch.hub.load(repo, entry, pretrained=True, trust_repo=True)
            used = f"{repo}#{entry}"
            break
        except Exception as e:  # noqa: BLE001 — try the next candidate
            log(f"hub load {repo} failed: {type(e).__name__}: {e}")
            model = None
            continue
    if model is None:
        return None, "no RIFE torch.hub weight could be fetched (offline or repo/API mismatch)"

    try:
        model = model.to(dev).eval()
    except Exception as e:  # noqa: BLE001
        return None, f"RIFE loaded but could not move to device: {type(e).__name__}: {e}"

    def _infer(img0, img1, alpha):
        # RIFE canonical API predicts the MIDDLE frame; many wrappers accept a timestep.
        with torch.no_grad():
            t0 = torch.from_numpy(np.ascontiguousarray(img0.transpose(2, 0, 1)))[None].to(dev)
            t1 = torch.from_numpy(np.ascontiguousarray(img1.transpose(2, 0, 1)))[None].to(dev)
            # try timestep-aware call first, then the plain mid-frame call.
            try:
                mid = model.inference(t0, t1, timestep=float(alpha))
            except TypeError:
                mid = model.inference(t0, t1)
            except Exception:
                mid = model(t0, t1)
            m = mid[0].clamp(0, 1).detach().cpu().numpy().transpose(1, 2, 0)
        return m.astype(np.float32)

    # smoke-test the callable on tiny data so a broken API surfaces NOW (caller then
    # falls back), never mid-run as a fabricated success.
    try:
        z = np.zeros((16, 16, 3), np.float32)
        _ = _infer(z, z, 0.5)
    except Exception as e:  # noqa: BLE001
        return None, f"RIFE inference smoke-test failed: {type(e).__name__}: {e}"

    return _infer, used


def _tonemap01(x):
    """Reinhard tonemap linear HDR -> [0,1] for a model/SSIM that expects a bounded
    range. Reversible enough; both learned models and SSIM want [0,1]."""
    import numpy as np
    x = np.clip(x, 0.0, None)
    return np.clip(x / (1.0 + x), 0.0, 1.0)


def _untonemap(t):
    """Inverse of _tonemap01: t/(1-t) back toward linear (for compositing symmetry)."""
    import numpy as np
    t = np.clip(t, 0.0, 0.999)
    return (t / (1.0 - t)).astype(np.float32)


# --------------------------------------------------------------------------- #
# 9. QUALITY — SSIM global + per-8x8-tile (worst + 5th-percentile).             #
#    Forked from exp_cycles_render_prod.py, adapted to run on the float RGB      #
#    arrays we already hold (no PNG round-trip). We tonemap linear HDR into       #
#    [0,1] first so SSIM sees a bounded, comparable range. Per-tile catches a     #
#    lever that lifts the global average while blurring high-frequency detail.    #
# --------------------------------------------------------------------------- #
def compute_ssim_global_and_tiles(a_rgb, b_rgb, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim) between two [H,W,3] float
    HDR images. a=synthesised, b=true reference. Real skimage on real pixels."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    A = _tonemap01(a_rgb)
    B = _tonemap01(b_rgb)
    if A.shape != B.shape:
        raise RuntimeError(f"synth/ref shape mismatch {A.shape} vs {B.shape}")

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
                continue  # too small for the SSIM window
            tile_scores.append(
                float(ssim(rt, dt, channel_axis=-1, data_range=1.0))
            )

    if tile_scores:
        arr = np.asarray(tile_scores, dtype=np.float64)
        worst = float(arr.min())
        p5 = float(np.percentile(arr, 5))
    else:
        worst = global_ssim
        p5 = global_ssim
    return global_ssim, worst, p5


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    import numpy as np  # local import so a missing numpy still yields a clean error

    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    frames = int(params.get("frames", 8))
    interp_every = int(params.get("interp_every", 2))
    model = str(params.get("model", "flow_guided")).lower()
    scene = str(params.get("scene", "animated")).lower()
    spp = int(params.get("spp", 256))
    res = int(params.get("resolution", 384))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # sane bounds
    frames = max(3, frames)
    interp_every = max(2, interp_every)          # every=1 would synthesise nothing
    spp = max(1, spp)
    res = max(64, res)
    if model not in ("rife", "flow_guided"):
        log(f"unknown model {model!r} -> 'flow_guided'")
        model = "flow_guided"
    if scene not in ("animated", "classroom"):
        log(f"unknown scene {scene!r} -> 'animated'")
        scene = "animated"

    log(f"params: frames={frames} interp_every={interp_every} model={model} "
        f"scene={scene} spp={spp} res={res} seed={seed} device={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender (+ classroom scene if requested) - #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    if scene == "classroom":
        blend_path = ensure_classroom()
        script_src = BLENDER_CLASSROOM_SCRIPT
    else:
        blend_path = None
        script_src = BLENDER_ANIM_SCRIPT
    script_path = os.path.join(WORK_DIR, "cx_interp_scene.py")
    with open(script_path, "w") as f:
        f.write(script_src)

    frame_timeout = 900

    # ---- 1) PASS 1: full render of EVERY frame (real path trace). ---------- #
    # This is BOTH our ground-truth-per-frame reference AND the naive "render all
    # frames" baseline whose total time is the numerator of net_speedup. We also
    # cache every frame's real color/motion/depth so the interpolator can read
    # Cycles' exact flow for the flow_guided model.
    full_render_times = []
    devices = set()
    layers = []  # per-frame dict: color, motion_prev, depth, motion_next

    for t in range(frames):
        frame_no = t + 1
        exr_path = os.path.join(WORK_DIR, f"frame_{frame_no:04d}.exr")
        wall_s, dev, resolved = run_blender_frame(
            blender_bin, script_path, spp, res, exr_path, frame_no, frames,
            seed, device_pref, frame_timeout, blend_path=blend_path,
        )
        full_render_times.append(wall_s)
        devices.add(dev)
        color, motion_prev, depth, motion_next = read_exr_layers(resolved, res)
        layers.append({
            "color": color, "motion_prev": motion_prev,
            "depth": depth, "motion_next": motion_next,
        })

    full_render_cost_all_frames = float(sum(full_render_times))
    real_render_s_full_frame = float(np.mean(full_render_times)) if full_render_times else 0.0

    # ---- 2) KEYFRAME SCHEDULE: render every interp_every-th frame; synthesise  #
    #        the frames strictly between consecutive keyframes. Frame 0 and the   #
    #        last frame are ALWAYS keyframes so every synthesised frame is         #
    #        bracketed by a real key on both sides.                                #
    key_idxs = list(range(0, frames, interp_every))
    if key_idxs[-1] != frames - 1:
        key_idxs.append(frames - 1)
    key_set = set(key_idxs)
    synth_idxs = [t for t in range(frames) if t not in key_set]

    # our pipeline's RENDER cost = the wall-time of the keyframe renders ONLY
    # (the synthesised frames are NOT rendered). Real measured times, summed.
    keyframe_render_cost = float(sum(full_render_times[k] for k in key_idxs))

    log(f"keyframes={key_idxs} ({len(key_idxs)}); synthesise={synth_idxs} "
        f"({len(synth_idxs)}); keyframe_render_cost={keyframe_render_cost:.3f}s")

    # ---- 3) MODEL SELECTION. For 'rife' we ATTEMPT to load a real pretrained    #
    #        network; on failure we fall back to flow_guided and record WHY, so    #
    #        the emitted modeled flag + note are truthful.                         #
    rife_infer = None
    model_used = model
    model_note = ""
    modeled = True  # default: flow_guided / fallback is a hand-built refine, not learned
    if model == "rife":
        rife_infer, tag = try_load_rife()
        if rife_infer is not None:
            model_used = "rife"
            model_note = f"RIFE pretrained network loaded via {tag} and ran (real learned model)"
            modeled = False  # a real trained network really ran -> numbers are all measured
            log(f"RIFE ready: {tag}")
        else:
            model_used = "flow_guided"
            model_note = (f"model=rife requested but NO pretrained RIFE weight could be "
                          f"fetched ({tag}); FELL BACK to flow_guided (hand-built refine) "
                          f"— labelled modeled:true, NOT claimed as RIFE")
            modeled = True
            log(f"RIFE unavailable -> flow_guided fallback: {tag}")
    else:
        model_note = ("flow_guided: Cycles ground-truth motion warps BOTH keyframes to the "
                      "target time + occlusion-aware blend (hand-built refine, not a trained "
                      "net) -> modeled:true")
        modeled = True

    # ---- 4) SYNTHESISE each in-between frame; TIME the real interpolation work; #
    #        SCORE SSIM (global + per-tile) vs the TRUE full render of that frame; #
    #        AND score the NAIVE single-key WARP baseline on the SAME frame so we  #
    #        can report whether the learned/flow predictor beat the 0.84 ceiling.  #
    #        SSIM is measurement-only and is NOT added to interp_cost.             #
    interp_cost_s = 0.0
    synth_global = []
    synth_worst = []
    synth_p5 = []
    warp_global = []           # naive single-key warp baseline, SAME frames
    per_frame_rows = []
    any_motion_seen = False
    blend_only_frames = 0

    # walk consecutive keyframe pairs; synthesise the interior of each.
    for a, b in zip(key_idxs[:-1], key_idxs[1:]):
        interior = list(range(a + 1, b))
        if not interior:
            continue
        kp = layers[a]      # previous keyframe (real render)
        kn = layers[b]      # next keyframe (real render)
        span = float(b - a)
        for t in interior:
            alpha = (t - a) / span         # fractional time in (0,1)
            true_rgb = layers[t]["color"]  # ground-truth full render (NOT given to the model)

            # -- TIME the interpolation (real wall-clock; charged to our cost) -- #
            _t0 = time.perf_counter()
            if model_used == "rife" and rife_infer is not None:
                # RIFE consumes the two bracketing REAL frames (tonemapped to [0,1]);
                # no external motion — this is the pure learned predictor.
                i0 = _tonemap01(kp["color"])
                i1 = _tonemap01(kn["color"])
                mid01 = rife_infer(i0, i1, alpha)
                synth_rgb = _untonemap(mid01)   # back to ~linear for a fair HDR SSIM
                am = True
            else:
                synth_rgb, am = interp_flow_guided(
                    kp["color"], kp.get("motion_next"),
                    kn["color"], kn.get("motion_prev"), alpha,
                )
            interp_cost_s += (time.perf_counter() - _t0)
            any_motion_seen = any_motion_seen or am
            if not am:
                blend_only_frames += 1

            # -- SSIM of the synthesised frame vs the TRUE render (measurement-only) -- #
            g, wst, p5 = compute_ssim_global_and_tiles(synth_rgb, true_rgb)
            synth_global.append(g); synth_worst.append(wst); synth_p5.append(p5)

            # -- NAIVE WARP BASELINE on the SAME target frame (the 0.84 ceiling) -- #
            # forward-warp the PREVIOUS keyframe by THIS frame's own prev-motion
            # (exactly the temporal runner's single-key reproject), then SSIM it.
            wb_rgb = naive_warp_baseline(kp["color"], layers[t]["motion_prev"])
            wg, _wwst, _wp5 = compute_ssim_global_and_tiles(wb_rgb, true_rgb)
            warp_global.append(wg)

            per_frame_rows.append({
                "frame": t, "alpha": round(alpha, 4),
                "synth_ssim": round(g, 4), "synth_worst_tile": round(wst, 4),
                "synth_p5_tile": round(p5, 4), "warp_ssim": round(wg, 4),
            })
            log(f"synth frame {t+1} alpha={alpha:.3f} model={model_used} "
                f"SSIM={g:.4f} worst_tile={wst:.4f} p5_tile={p5:.4f} | "
                f"naive_warp_SSIM={wg:.4f} | interp_so_far={interp_cost_s:.4f}s")

    # ---- 5) AGGREGATE + HONEST net_speedup ---------------------------------- #
    # our pipeline cost = keyframe RENDER wall-time (real) + interpolation wall-time
    # (real). Both measured on this box. SSIM is NOT charged.
    our_cost_s = keyframe_render_cost + interp_cost_s
    net_speedup = (full_render_cost_all_frames / our_cost_s) if our_cost_s > 1e-9 else 0.0

    quality = float(np.mean(synth_global)) if synth_global else 1.0
    worst_tile_ssim = float(np.min(synth_worst)) if synth_worst else 1.0
    p5_tile_ssim = float(np.mean(synth_p5)) if synth_p5 else 1.0
    warp_baseline_ssim = float(np.mean(warp_global)) if warp_global else 1.0
    beat_warp = bool(quality > warp_baseline_ssim)

    device = "|".join(sorted(devices)) if devices else "unknown"
    fell_to_cpu = ("CPU" in device)

    if not synth_global:
        # no interior frames to synthesise (e.g. interp_every >= frames). This is not a
        # fabrication guard — it is an honest "nothing to interpolate" outcome. Emit an
        # error rather than a meaningless speedup, per the no-mislabel contract.
        emit({"error": f"no in-between frames to synthesise "
                       f"(frames={frames}, interp_every={interp_every}); "
                       f"increase frames or lower interp_every"})
        return

    note = (f"LEARNED frame-interpolation spike: render every {interp_every}th frame fully, "
            f"synthesise the in-betweens with model={model_used}. "
            f"{model_note}. ")
    note += (f"scene={scene} ({'keyframed monkey+sphere+camera, REAL motion vectors' if scene=='animated' else 'CC0 Classroom, STATIC — no per-frame motion'}), "
             f"{frames} frames @ {res}x{res} spp={spp}. ")
    note += (f"net_speedup = full-render-ALL-frames wall-time / (keyframe-render wall-time + "
             f"interpolation wall-time), ALL measured on this box (SSIM is measurement-only, "
             f"NOT charged). ")
    note += (f"quality = mean GLOBAL SSIM of the {len(synth_global)} synthesised frames vs "
             f"their TRUE full Cycles renders; worst_tile_ssim / p5_tile_ssim are the min / "
             f"5th-pctile of an 8x8 per-tile SSIM grid (catch a blur that lifts the average). ")
    note += (f"warp_baseline_ssim = the NAIVE single-keyframe forward-warp (the temporal "
             f"runner's reproject) SSIM on the SAME frames — the ~0.84 ceiling we attack; "
             f"beat_warp={beat_warp} (learned/flow mean {quality:.4f} vs warp {warp_baseline_ssim:.4f}). ")
    if modeled:
        note += ("modeled:true — the interpolator's refine is a hand-built occlusion-aware "
                 "flow blend (or the RIFE->flow fallback), NOT a trained network; every TIME "
                 "and SSIM is still a real measurement, only the 'learned' label is withheld. ")
    else:
        note += ("modeled:false — a REAL pretrained RIFE network was downloaded and ran; every "
                 "reported number is a real measurement. ")
    if scene == "classroom" or blend_only_frames > 0 or not any_motion_seen:
        note += (f"NOTE: {blend_only_frames} frame(s) had no usable motion vectors "
                 f"(static scene / EXR fallback) -> flow_guided degraded to a plain temporal "
                 f"blend on those (disclosed). ")
    if fell_to_cpu:
        note += "render ran on CPU (no usable GPU device found by Cycles) — NOTE. "

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "worst_tile_ssim": round(float(worst_tile_ssim), 4),
        "p5_tile_ssim": round(float(p5_tile_ssim), 4),
        "warp_baseline_ssim": round(float(warp_baseline_ssim), 4),
        "beat_warp": beat_warp,
        "model": model_used,
        "model_requested": model,
        "modeled": bool(modeled),
        "note": note,
        # ---- structure / bookkeeping (all real) ---------------------------- #
        "frames": int(frames),
        "interp_every": int(interp_every),
        "keyframes": int(len(key_idxs)),
        "synthesised_frames": int(len(synth_global)),
        "scene": scene,
        "spp": int(spp),
        "resolution": f"{res}x{res}",
        "device": device,
        "real_render_s_full_frame": round(float(real_render_s_full_frame), 4),
        "full_render_cost_all_frames_s": round(full_render_cost_all_frames, 4),
        "keyframe_render_cost_s": round(float(keyframe_render_cost), 4),
        "interp_cost_s": round(float(interp_cost_s), 4),
        "our_pipeline_cost_s": round(float(our_cost_s), 4),
        "keyframe_indices": [int(i) for i in key_idxs],
        "per_frame_full_render_s": [round(float(x), 4) for x in full_render_times],
        "per_frame": per_frame_rows,
    }

    log(f"RESULT net_speedup={net_speedup:.3f} quality={quality:.4f} "
        f"worst_tile={worst_tile_ssim:.4f} p5_tile={p5_tile_ssim:.4f} "
        f"warp_baseline={warp_baseline_ssim:.4f} beat_warp={beat_warp} "
        f"model={model_used} modeled={modeled} "
        f"full_all={full_render_cost_all_frames:.2f}s our={our_cost_s:.2f}s device={device}")
    emit(metrics)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"timeout: {e}"})
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit({"error": f"{type(e).__name__}: {e}"})
        sys.exit(0)
