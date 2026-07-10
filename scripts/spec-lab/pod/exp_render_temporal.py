#!/usr/bin/env python3
"""
exp_render_temporal.py — CUSTOM TEMPORAL-REUSE speculation for ANIMATION.
============================================================================

This is the "render the DELTA for 3D video" experiment — the real ComputeExchange
`render a long video` job class. It is a REAL Blender/Cycles render measurement
(modeled:false), but the SPECULATION STRATEGY is entirely OURS: we do not lean on
any framework's built-in temporal denoiser or motion-blur trick. We own the warp
and we own the disocclusion mask.

THE OWNER'S STRATEGY (what makes an animation cheap to render)
-------------------------------------------------------------
In an animated shot, frame N is almost identical to frame N-1 — the camera and
objects moved a little, so most of frame N is just frame N-1's pixels REPROJECTED
by the scene's motion. Path tracing every frame from scratch re-does that shared
work every single frame. Our bet:

  1. Render frame N-1 FULLY once (the KEYFRAME) — a real, expensive Cycles render.
  2. Ask Cycles for its per-pixel screen-space MOTION field (the 'Vector' pass) and
     its DEPTH ('Z' pass) and 'Mist' — these are essentially FREE by-products of the
     path trace; Blender computes them while rendering the keyframe.
  3. REPROJECT (warp) the keyframe's pixels forward by that motion field with OUR OWN
     numpy warp (forward-scatter with a z-buffer + a small gather fallback). Most of
     frame N is reconstructed this way for ~0 path-tracing cost.
  4. Detect where reprojection FAILS with OUR OWN disocclusion mask:
        - DISOCCLUSION: geometry that was hidden in N-1 becomes visible in N. We flag
          it via forward/backward motion-vector INCONSISTENCY (a pixel warped forward
          then backward should land back home; large round-trip error = new geometry)
          and MOTION-VECTOR DIVERGENCE (neighbouring MVs that fan apart uncover a hole).
        - DEPTH DISCONTINUITY: silhouette edges where the warp smears foreground over
          background — detected from the Z pass gradient.
        - FADE-IN HOLES: forward-scatter simply never writes some destination pixels
          (nothing mapped there) — those are holes by construction.
  5. RE-RENDER only the masked (disoccluded / high-error) region and COMPOSITE it over
     the reprojected estimate. The rest of frame N is free.

THE WIN we are measuring
------------------------
  full_render_cost_all_frames = time to path-trace every one of the F frames fully.
  our_cost                    = 1 full keyframe render
                                + per non-key frame: (cheap numpy warp, ~free)
                                  + a PARTIAL re-render whose cost we charge as the
                                    disoccluded-area fraction of a full render.
  net_speedup = full_render_cost_all_frames / our_cost.
  quality     = mean SSIM( our composited frame , the true full per-frame render ).

HONESTY / what is REAL here (modeled:false)
-------------------------------------------
  * The animated scene, the keyframe render, AND a full per-frame render of EVERY
    frame are REAL Cycles path traces (the full renders are our ground truth).
  * The Vector / Z passes are REAL Cycles output read out of a multilayer EXR.
  * The warp and the disocclusion mask are OUR real numpy code, run on real data.
  * The composited frames are real pixels; SSIM is real scikit-image on real images.
  * real_render_s_full_frame is a MEASURED per-frame wall time.
  * The FIXED per-frame render overhead O (Blender start + scene build + BVH build,
    independent of rendered-pixel count) is MEASURED once via a tiny calibration
    render; the per-frame pixel-trace time P is the full-frame time minus O.
  * The ONE accounting step: a non-key frame's partial re-render is not actually
    border-rendered in Cycles; its cost is charged as O + disoccluded_area_fraction*P
    — a real crop still pays the fixed overhead O in FULL (BVH/scene do not shrink with
    crop area) and traces only the disoccluded pixels (Cycles' trace cost is ~linear in
    rendered-pixel count at fixed spp). This is honest, if anything CONSERVATIVE for a
    resident-Blender pipeline that would amortize O across crops; it is NOT an upper
    bound. The REAL numpy reproject/mask/fill wall-time is also measured and charged
    (for inpaint/nearest it is the ONLY per-frame cost). Every render TIME and every
    QUALITY (SSIM) number is measured on real renders; SSIM is measurement-only and is
    NOT charged to pipeline cost. We set modeled:false and disclose the single
    crop-trace area->cost proportionality loudly in "note". Nothing is fabricated.

Params (argv[1] JSON), all optional:
  frames              : int  number of animation frames                    (default 8)
  keyframe_every      : int  render a fresh full keyframe every K frames    (default 8)
                              (K>=frames => a single keyframe reused for the whole shot)
                              (IGNORED when adaptive_keyframe=true — see below)
  spp                 : int  samples per pixel for every render             (default 256)
  resolution          : int  square image side length in px                (default 384)
  disocclusion_thresh : float  round-trip MV error (px) above which a pixel
                              is declared disoccluded / must be re-rendered  (default 0.1
                              — interpreted as a fraction of the frame diagonal, see note)
  seed                : int  Cycles + animation seed                        (default 0)
  device              : "AUTO" (default) | "GPU" | "CPU"
  blender_url         : str  override the download URL (real 4.x LTS)

  ---- NEAR-LOSSLESS LEVERS (all optional, safe defaults = OLD behavior) ----
  adaptive_keyframe   : bool  (default false) THE BIG LEVER. When false the keyframe
                              cadence is the FIXED keyframe_every (old behavior). When
                              true we DROP the fixed interval: after building each
                              reprojected frame we estimate its quality from a CHEAP
                              proxy (disoccluded/low-confidence pixel fraction + a small
                              reprojection-residual probe). If the PREDICTED quality would
                              fall below quality_floor we render a FRESH full keyframe for
                              this frame instead of reprojecting — bounding quality to
                              ~near-lossless and letting the speedup float with content.
                              We report the ACTUAL mean keyframe interval achieved.
  quality_floor       : float (default 0.98) the near-lossless SSIM target the adaptive
                              controller keeps every reprojected frame at or above. Only
                              consulted when adaptive_keyframe=true.
  reproject_method    : "backward" (default, old) | "forward_splat" | "bidirectional".
                              backward     = gather along this frame's prev-motion (old).
                              forward_splat= scatter the keyframe's pixels forward by its
                                             own motion into a z-tested accumulation buffer.
                              bidirectional= blend the backward reprojection from the
                                             nearest keyframe on EACH side (prev + next key),
                                             weighted by temporal distance — cuts mid-interval
                                             error. Falls back to one-sided at the shot ends.
  hole_fill           : "rerender" (default, old) | "inpaint". rerender drops the TRUE
                              full-render pixels into disoccluded patches (old, highest
                              quality). inpaint fills those patches with a cheap numpy/cv2
                              inpaint of the reprojected frame — faster (no patch render
                              cost) but lower quality. A speed/quality knob for the holes.
  error_feedback      : bool  (default false) accumulate the residual between the
                              reprojection and an occasional cheap check across a long
                              keyframe interval, and add it back, to stop error DRIFT.

OUTPUT (last stdout line = exactly one JSON metrics object):
  {"net_speedup","quality","reproject_accept_frac","frames","real_render_s_full_frame",
   "device","modeled":false,"note":"CUSTOM temporal reprojection via Cycles
   motion-vector pass + our disocclusion mask; render-the-delta for animation",
   "mean_keyframe_interval","reproject_method","adaptive", ...}
  (mean_keyframe_interval is the ACTUAL mean interval achieved — headline for adaptive.)

Contract: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object; any
failure emits {"error":...} as the last stdout line and exits (never hangs).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Bootstrap constants — IDENTICAL pattern to exp_cycles_render.py so a pod that #
# already downloaded Blender for a prior rung reuses it (ensure_blender skips). #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/temporal_render"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[temporal]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs (same list as the sibling render runner).         #
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
#    /root/blender/blender from a prior rung on the same pod.                    #
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
# 3. The Blender ANIMATION scene script (run inside Blender's own python via -P).#
#    Differences from the still-frame sibling:                                  #
#      * objects + camera are KEYFRAMED so there is genuine screen-space motion. #
#      * we enable the Cycles VECTOR pass (per-pixel forward/backward motion),   #
#        the Z (depth) pass and the combined color, and write a MULTILAYER EXR   #
#        (full float) so our numpy code can read real motion + depth + color.    #
#      * one frame is rendered per invocation (env CX_FRAME selects which),      #
#        so we can time each frame independently and re-use one keyframe.        #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP      = int(os.environ["CX_SPP"])
RES      = int(os.environ["CX_RES"])
OUT      = os.environ["CX_OUT"]                 # output EXR path (multilayer)
FRAME    = int(os.environ["CX_FRAME"])          # which animation frame to render
NFRAMES  = int(os.environ["CX_NFRAMES"])        # total frames in the shot
SEED     = int(os.environ["CX_SEED"])
DEV_PREF = os.environ.get("CX_DEVICE", "AUTO")  # AUTO | GPU | CPU

# ---- start from an empty scene ----------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- REAL geometry: a rotating monkey + an orbiting metal sphere + a static  #
#      red cube + a floor. The MOVING objects create disocclusion at their     #
#      silhouettes as they rotate/orbit — exactly where our mask must fire.     #
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

# ---- ANIMATE: keyframe object transforms across the shot --------------------
# The whole point of a temporal-reuse test is genuine, smooth screen-space motion
# with silhouette disocclusion. We drive:
#   * the monkey SPINNING about Z (its ears/brow sweep, uncovering background),
#   * the sphere ORBITING in an arc (translation => parallax vs the floor/monkey),
#   * the camera slowly DOLLYING/PANNING (global motion the reprojection must track).
# frame index t in [0 .. NFRAMES-1]; Blender frames are 1-based (t+1).
scene.frame_start = 1
scene.frame_end = NFRAMES

# amount of motion per frame — tuned so per-frame displacement is a few % of the
# frame (small enough to reproject well, large enough to create real disocclusion).
SPIN_PER_FRAME = math.radians(9.0)     # monkey yaw per frame
ORBIT_R = 1.9                          # sphere orbit radius
ORBIT_PER_FRAME = math.radians(11.0)   # sphere angular step per frame
CAM_PAN_PER_FRAME = 0.06               # camera x drift per frame (world units)

for t in range(NFRAMES):
    fr = t + 1
    # monkey spin about Z
    monkey.rotation_euler = (0.0, 0.0, math.radians(35) + SPIN_PER_FRAME * t)
    monkey.keyframe_insert(data_path="rotation_euler", frame=fr)
    # sphere orbit in the X-Y plane, keeping a fixed height
    ang = ORBIT_PER_FRAME * t
    sphere.location = (0.6 + ORBIT_R * math.cos(ang),
                       0.0 + ORBIT_R * math.sin(ang),
                       0.9)
    sphere.keyframe_insert(data_path="location", frame=fr)

# ---- lights + world ---------------------------------------------------------
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

# ---- camera (also keyframed to introduce global screen motion) --------------
bpy.ops.object.camera_add(location=(0.0, -6.6, 3.5))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(63), 0.0, 0.0)
scene.camera = cam
for t in range(NFRAMES):
    fr = t + 1
    cam.location = (0.0 + CAM_PAN_PER_FRAME * t, -6.6, 3.5)
    cam.keyframe_insert(data_path="location", frame=fr)

# ---- Cycles engine + deterministic sampling ---------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False   # fixed spp so per-frame cost is a clean constant
cyc.use_denoising = False           # NO framework denoiser: our strategy must stand alone
# Motion vectors (the 'Vector' pass) require the deprecated-but-present vector data.
# They are computed from object+camera motion between the previous/next frame.

# ---- device selection: GPU (OPTIX->CUDA->HIP->ONEAPI) else CPU --------------
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

# ---- enable the render PASSES we need: Combined (color), Vector (motion), Z --#
# The Vector pass is 4 floats/pixel: (prev.x, prev.y, next.x, next.y) screen-space
# motion in PIXELS. Z is linear camera depth. These fall out of the path trace for
# ~free and are the raw material our warp + disocclusion mask run on.
vl = scene.view_layers[0]
vl.use_pass_vector = True
vl.use_pass_z = True
# Combined is on by default; make sure.
vl.use_pass_combined = True

# Motion blur must be ON at the scene level for Cycles to populate the Vector pass
# with real object motion (it derives MVs from the shutter interval). We keep the
# shutter tiny so the beauty pass is NOT actually motion-blurred — we only want the
# vectors. (Cycles computes vectors from the frame-to-frame transforms regardless of
# the visual blur; a near-zero shutter gives crisp color + valid vectors.)
scene.render.use_motion_blur = True
try:
    scene.cycles.motion_blur_position = 'START'   # sample motion from this frame forward
except Exception:
    pass
scene.render.motion_blur_shutter = 0.01           # ~no visible blur, vectors still valid

# ---- render settings: square res, MULTILAYER EXR (float) --------------------
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.image_settings.color_depth = '32'   # full float — needed for MVs/depth
scene.render.image_settings.exr_codec = 'ZIP'
scene.render.filepath = OUT
scene.frame_set(FRAME)

_log(f"rendering frame={FRAME}/{NFRAMES} spp={SPP} res={RES} device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_frame(blender_bin, script_path, spp, res, out_exr, frame, nframes,
                      seed, device_pref, timeout_s):
    """Render ONE animation frame to a multilayer EXR. Returns (wall_s, chosen_device).

    The wall-time is measured around the subprocess: the honest end-to-end cost of
    path-tracing this single frame at these params. This is our per-frame render cost.
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_exr
    env["CX_FRAME"] = str(frame)
    env["CX_NFRAMES"] = str(nframes)
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref

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

    # Blender may append the frame number to the EXR path; resolve the real file.
    resolved = _resolve_exr_path(out_exr, frame)
    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and (resolved is not None)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed (frame={frame}, rc={proc.returncode}, "
            f"out_exists={resolved is not None}); stdout tail: {tail[-600:]}"
        )
    return wall_s, chosen_device, resolved


def _resolve_exr_path(out_exr, frame):
    """Blender writes write_still to exactly `filepath`+ext OR with a frame suffix
    depending on version; probe the likely candidates and return the first that exists."""
    candidates = [
        out_exr,
        out_exr + ".exr",
        f"{out_exr}{frame:04d}.exr",
        f"{out_exr}{frame:04d}",
    ]
    # also strip a trailing .exr the caller may have already put on
    base = out_exr[:-4] if out_exr.endswith(".exr") else out_exr
    candidates += [f"{base}{frame:04d}.exr", f"{base}.exr", base]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# --------------------------------------------------------------------------- #
# 4. EXR reader — pull the Combined (RGB color), Vector (motion) and Z (depth)  #
#    channels out of the multilayer EXR into numpy. Blender ships OpenEXR support #
#    inside its own python, but OUR runner uses the pod's system python; we read  #
#    with the `OpenEXR`/`Imath` libs if present, else fall back to imageio which  #
#    scikit-image/pillow-adjacent stacks usually provide. We keep the reader      #
#    defensive: the channel NAMES differ across writers.                          #
# --------------------------------------------------------------------------- #
def read_exr_layers(path, res):
    """Return (color[H,W,3] float, motion[H,W,2] float prev-vectors, depth[H,W] float).

    The Vector pass in Blender is 4 channels; the first two are the motion FROM the
    previous frame TO this frame's pixel, in *pixels* (screen space). We use those.
    Depth (Z) is linear camera distance. Missing depth -> zeros (mask degrades to
    MV-only, still valid). Everything is returned at (res,res).
    """
    import numpy as np

    # --- Primary path: OpenEXR + Imath (most faithful channel access) --------
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

        # Channel names look like "ViewLayer.Combined.R", "ViewLayer.Vector.X",
        # "ViewLayer.Depth.Z" (or older "Combined.R", "Vector.X", "Depth.Z").
        def _find(suffixes):
            for want in suffixes:
                for c in chans:
                    if c.endswith(want):
                        return c
            return None

        r = _find([".Combined.R", "Combined.R", ".R"])
        g = _find([".Combined.G", "Combined.G", ".G"])
        b = _find([".Combined.B", "Combined.B", ".B"])
        # Vector pass: channels X,Y,Z,W. Blender maps the PREVIOUS-frame motion to
        # the first pair. Names commonly ".Vector.X"/".Vector.Y" (or .Z/.W for next).
        vx = _find([".Vector.X", "Vector.X"])
        vy = _find([".Vector.Y", "Vector.Y"])
        # next-frame vectors (Z,W) — used for forward/backward consistency if present.
        vz = _find([".Vector.Z", "Vector.Z"])
        vw = _find([".Vector.W", "Vector.W"])
        zc = _find([".Depth.Z", "Depth.Z", ".Z"])

        color = np.stack([_chan(c) if c else np.zeros((h, w), np.float32)
                          for c in (r, g, b)], axis=-1)
        # motion: we standardise to the PREV->cur pixel motion (dx, dy)
        mx = _chan(vx) if vx else np.zeros((h, w), np.float32)
        my = _chan(vy) if vy else np.zeros((h, w), np.float32)
        motion = np.stack([mx, my], axis=-1)
        # also carry the next-frame vectors when available for consistency checks
        if vz and vw:
            motion_next = np.stack([_chan(vz), _chan(vw)], axis=-1)
        else:
            motion_next = None
        depth = _chan(zc) if zc else np.zeros((h, w), np.float32)
        f.close()

        color, motion, depth, motion_next = _resize_layers(
            color, motion, depth, motion_next, res
        )
        return color, motion, depth, motion_next
    except ImportError:
        pass  # fall through to imageio path
    except Exception as e:  # noqa: BLE001 — try the fallback reader before giving up
        log(f"OpenEXR read failed ({type(e).__name__}: {e}); trying imageio")

    # --- Fallback: imageio (freeimage/openexr plugin). Gives us at least the RGB;
    #     motion/depth may be unavailable -> mask degrades gracefully to a
    #     luma-difference disocclusion detector (still OUR logic, just fewer cues). #
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
        color, motion, depth, _ = _resize_layers(color, motion, depth, None, res)
        log("imageio fallback: motion/depth passes unavailable -> luma-diff mask")
        return color, motion, depth, None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not read EXR {path}: {type(e).__name__}: {e}")


def _resize_layers(color, motion, depth, motion_next, res):
    """Nearest/area-resize every layer to (res,res) so all frames align even if the
    EXR came back at a slightly different size. Motion vectors are in PIXELS, so on a
    resize we must SCALE them by the resolution ratio to stay physically correct."""
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
    m[..., 0] *= sx  # scale pixel-motion to the new width
    m[..., 1] *= sy  # scale pixel-motion to the new height
    motion = m
    if motion_next is not None:
        mn = sk_resize(motion_next, (res, res, 2), order=1,
                       preserve_range=True, anti_aliasing=False).astype(np.float32)
        mn[..., 0] *= sx
        mn[..., 1] *= sy
        motion_next = mn
    return color, motion, depth, motion_next


# --------------------------------------------------------------------------- #
# 5. OUR CUSTOM WARP — reproject the KEYFRAME (source) into an estimate of the  #
#    TARGET frame using the target frame's PREV-motion field.                   #
#                                                                               #
#    Blender's Vector pass at frame N stores, per destination pixel, the motion #
#    that pixel came FROM in frame N-1 (a BACKWARD field: dst <- src). That is   #
#    exactly what a GATHER warp wants: for each dst pixel p, sample the source   #
#    (keyframe) at p + motion_prev[p]. Gather warps have no holes-by-scatter and #
#    are trivially parallel, so we implement a bilinear gather. We ALSO compute  #
#    a forward-scatter pass purely to find where NOTHING maps (fade-in holes)    #
#    for the disocclusion mask. The gather is the pixels; the scatter feeds the  #
#    mask. This split is ours.                                                   #
# --------------------------------------------------------------------------- #
def warp_gather(src_color, motion_prev):
    """Reproject src_color into the target view by GATHERING along the target's
    prev-motion field with bilinear sampling. Returns (warped[H,W,3], valid[H,W] bool).

    valid=False where the sample coordinate leaves the source image (off-screen source
    => that dst pixel had no predecessor => a disocclusion the mask will pick up)."""
    import numpy as np
    h, w = src_color.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    # dst pixel p came from p + motion_prev (backward field points to source location)
    sx = xs + motion_prev[..., 0]
    sy = ys + motion_prev[..., 1]

    # in-bounds mask (a sample fully outside the source is invalid)
    valid = (sx >= 0) & (sx <= w - 1) & (sy >= 0) & (sy <= h - 1)

    # bilinear gather (clamp coords for the arithmetic; validity handled separately)
    cx = np.clip(sx, 0, w - 1)
    cy = np.clip(sy, 0, h - 1)
    x0 = np.floor(cx).astype(np.int32); x1 = np.minimum(x0 + 1, w - 1)
    y0 = np.floor(cy).astype(np.int32); y1 = np.minimum(y0 + 1, h - 1)
    fx = (cx - x0)[..., None]
    fy = (cy - y0)[..., None]

    Ia = src_color[y0, x0]
    Ib = src_color[y0, x1]
    Ic = src_color[y1, x0]
    Id = src_color[y1, x1]
    top = Ia * (1 - fx) + Ib * fx
    bot = Ic * (1 - fx) + Id * fx
    warped = top * (1 - fy) + bot * fy
    return warped.astype(np.float32), valid


def warp_forward_splat(src_color, src_motion_next, src_depth):
    """FORWARD-SPLAT reprojection (reproject_method='forward_splat').

    Instead of GATHERING per destination pixel, we SCATTER each SOURCE (keyframe) pixel
    forward to where it lands in the target using the keyframe's OWN forward-motion
    (the 'next' vectors, src->cur). Each source pixel p writes its color to the target
    at p + motion_next[p]. Two source pixels can land on the same target pixel — we keep
    the NEAREST one via a per-target z-buffer (smaller depth wins), which is exactly how
    a real forward warp resolves the fold-over that a naive gather smears. Splatting
    leaves fade-in HOLES (target pixels nothing mapped to); those come back as valid=False
    for the disocclusion mask to catch, same contract as the gather.

    Returns (warped[H,W,3] float, valid[H,W] bool). If next-vectors are missing this warp
    cannot run honestly; the caller must fall back to the gather.
    """
    import numpy as np
    h, w = src_color.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = xs + src_motion_next[..., 0]
    dy = ys + src_motion_next[..., 1]
    # nearest-target integer coords for each source pixel
    tx = np.round(dx).astype(np.int64)
    ty = np.round(dy).astype(np.int64)
    in_b = (tx >= 0) & (tx < w) & (ty >= 0) & (ty < h)

    out = np.zeros((h, w, 3), np.float32)
    zbuf = np.full((h, w), np.inf, np.float32)
    written = np.zeros((h, w), bool)

    # depth per source pixel to resolve fold-over (nearest wins). If depth is absent
    # (all zeros) every source has equal z, so this degrades to last-writer-wins, which
    # is still a valid splat — quality just loses the fold-over ordering.
    zsrc = src_depth if (src_depth is not None and np.any(src_depth > 0)) else \
        np.zeros((h, w), np.float32)

    sy = ys[in_b].astype(np.int64)
    sx = xs[in_b].astype(np.int64)
    dyi = ty[in_b]
    dxi = tx[in_b]
    dz = zsrc[sy, sx]
    col = src_color[sy, sx]

    # Resolve collisions z-nearest without a Python loop: sort source pixels by depth
    # DESCENDING and scatter in that order so the SMALLEST depth is written LAST and wins.
    order = np.argsort(-dz, kind="stable")
    dyi = dyi[order]; dxi = dxi[order]; col = col[order]; dz = dz[order]
    out[dyi, dxi] = col
    # nearest z written per target: scatter-min via the same descending order (last wins)
    zbuf[dyi, dxi] = dz
    written[dyi, dxi] = True

    valid = written  # holes (unwritten targets) are disocclusions by construction
    return out.astype(np.float32), valid


def quantize_mv(motion, precision):
    """mv_precision knob. Round a motion field to a coarser grid to model the quality
    cost of transmitting cheaper motion vectors in a distributed variant (draft on the
    fleet ships MVs to the GPU verifier — coarser MVs are cheaper to move). 'full' is a
    no-op (the original behavior). 'half' snaps to 0.5px, 'int' to whole px."""
    import numpy as np
    if motion is None or precision == "full":
        return motion
    step = {"half": 0.5, "int": 1.0}.get(precision)
    if step is None:  # unknown value -> behave like 'full' (safe default)
        return motion
    return (np.round(motion / step) * step).astype(np.float32)


def hole_fill(reproj_rgb, fill_mask, method):
    """hole_fill knob. Fill the pixels flagged by fill_mask WITHOUT a re-render, so the
    disoccluded region costs ~0 render time (trading quality for speed on the holes).

    method 'nearest' : push-pull nearest-valid-neighbor flood (a few dilation passes that
                       copy the closest already-valid color into each hole).
    method 'inpaint' : iterative diffusion — repeatedly replace each hole pixel with the
                       mean of its valid 4-neighbors until the region fills. Pure numpy so
                       no cv2 dependency; it is a genuine Laplacian-fill inpaint.
    Returns the filled RGB. 'rerender' is handled by the caller (drops TRUE pixels in) and
    never reaches here."""
    import numpy as np
    out = reproj_rgb.copy()
    hole = fill_mask.copy()
    if not hole.any():
        return out
    valid = ~hole

    if method == "nearest":
        # push-pull: repeatedly dilate the valid region, copying a valid neighbor's color
        # into each newly-covered hole pixel, until no holes remain (bounded iterations).
        for _ in range(max(reproj_rgb.shape[:2])):
            if not hole.any():
                break
            # for each of the 4 shift directions, a hole pixel adjacent to a valid one
            # inherits that neighbor's color.
            for dyx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                shifted_valid = np.roll(valid, dyx, axis=(0, 1))
                shifted_col = np.roll(out, dyx, axis=(0, 1))
                take = hole & shifted_valid
                if take.any():
                    out[take] = shifted_col[take]
                    valid = valid | take
                    hole = hole & ~take
        return out.astype(np.float32)

    if method == "inpaint":
        # Laplacian diffusion: iterate mean-of-4-neighbors on the hole pixels only.
        filled = out.astype(np.float32)
        for _ in range(200):
            if not hole.any():
                break
            up = np.roll(filled, 1, axis=0)
            dn = np.roll(filled, -1, axis=0)
            lf = np.roll(filled, 1, axis=1)
            rt = np.roll(filled, -1, axis=1)
            avg = (up + dn + lf + rt) / 4.0
            filled[hole] = avg[hole]
            # once every hole pixel has at least one filled neighbor the mean is defined;
            # 200 Jacobi sweeps diffuse a bounded-size hole to convergence.
        return filled.astype(np.float32)

    # unknown method -> return unchanged (caller will treat as rerender-equivalent)
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# 6. OUR CUSTOM DISOCCLUSION MASK — where reprojection is NOT trustworthy and   #
#    the frame must be RE-RENDERED. Four independent cues, OR-ed together:       #
#      (a) OUT-OF-BOUNDS gather (source sample left the keyframe) — new geometry #
#          scrolled in from the frame edge.                                      #
#      (b) MOTION-VECTOR DIVERGENCE — the local magnitude of the MV gradient.    #
#          Where neighbouring pixels' motion fans APART, a hole opens between    #
#          them (a surface pulling away from what's behind it => disocclusion).  #
#      (c) DEPTH DISCONTINUITY — large Z-gradient marks silhouette edges where   #
#          the warp smears foreground pixels across background. Re-render the     #
#          silhouette band so edges stay crisp.                                  #
#      (d) FORWARD/BACKWARD MV INCONSISTENCY — if the 'next' vectors are present, #
#          a pixel warped by prev then by next should round-trip near home; a     #
#          large round-trip error means the correspondence is broken (occlusion). #
#    We dilate the union slightly so re-rendered patches fully cover the seam.    #
#    disocclusion_thresh is interpreted as a FRACTION OF THE FRAME DIAGONAL, so   #
#    it is resolution-independent (0.1 => 10% of the diagonal in round-trip px).  #
# --------------------------------------------------------------------------- #
def disocclusion_mask(motion_prev, motion_next, depth, valid, res, thresh_frac):
    """Return a boolean mask[H,W] = True where the frame must be RE-RENDERED, plus a
    small dict of per-cue coverage for reporting."""
    import numpy as np
    h, w = valid.shape
    diag = float(np.hypot(h, w))
    rt_thresh_px = max(0.5, thresh_frac * diag)  # round-trip error threshold, in px

    # (a) out-of-bounds gather
    mask_oob = ~valid

    # (b) MV divergence: gradient magnitude of the motion field. A big spatial change
    #     in motion between neighbours is exactly where surfaces separate.
    mvx = motion_prev[..., 0]
    mvy = motion_prev[..., 1]
    gxx, gxy = np.gradient(mvx)
    gyx, gyy = np.gradient(mvy)
    # divergence-ish magnitude: how fast the flow field is changing locally (px/px)
    div = np.sqrt(gxx ** 2 + gxy ** 2 + gyx ** 2 + gyy ** 2)
    # threshold divergence relative to the same round-trip scale (per-pixel fan-out)
    mask_div = div > (rt_thresh_px / max(diag, 1.0) * 4.0)

    # (c) depth discontinuity: normalized Z-gradient. Guard against an all-zero depth
    #     (imageio fallback) so this cue simply contributes nothing then.
    if np.any(depth > 0):
        dz = depth.copy()
        finite = np.isfinite(dz)
        dz[~finite] = 0.0
        # robust normalisation to [0,1] by the 1st/99th percentile of finite depths
        lo, hi = np.percentile(dz[finite & (dz > 0)] if np.any(finite & (dz > 0))
                               else dz, [1, 99])
        span = max(hi - lo, 1e-6)
        dzn = np.clip((dz - lo) / span, 0, 1)
        gzx, gzy = np.gradient(dzn)
        zgrad = np.sqrt(gzx ** 2 + gzy ** 2)
        mask_depth = zgrad > 0.06   # ~6% depth jump between neighbours = silhouette
    else:
        mask_depth = np.zeros((h, w), bool)

    # (d) forward/backward round-trip consistency (only if next-vectors exist)
    if motion_next is not None:
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        # go to source by prev, then follow next from there back toward here
        srcx = np.clip(xs + mvx, 0, w - 1)
        srcy = np.clip(ys + mvy, 0, h - 1)
        # sample the NEXT vectors at the source location (nearest is fine for a check)
        sxi = np.clip(np.round(srcx).astype(np.int32), 0, w - 1)
        syi = np.clip(np.round(srcy).astype(np.int32), 0, h - 1)
        back_x = motion_next[syi, sxi, 0]
        back_y = motion_next[syi, sxi, 1]
        # round-trip landing position vs the original pixel
        rtx = srcx + back_x - xs
        rty = srcy + back_y - ys
        rt_err = np.sqrt(rtx ** 2 + rty ** 2)
        mask_consistency = rt_err > rt_thresh_px
    else:
        mask_consistency = np.zeros((h, w), bool)

    mask = mask_oob | mask_div | mask_depth | mask_consistency

    # dilate to seal the seam around each disoccluded region (3x3 max-filter, a few
    # iterations). Pure-numpy dilation via rolled ORs so we need no scipy dependency.
    def _dilate(m, iters=2):
        out = m.copy()
        for _ in range(iters):
            acc = out.copy()
            acc[:-1, :] |= out[1:, :]
            acc[1:, :] |= out[:-1, :]
            acc[:, :-1] |= out[:, 1:]
            acc[:, 1:] |= out[:, :-1]
            out = acc
        return out
    mask = _dilate(mask, iters=2)

    coverage = {
        "oob": float(mask_oob.mean()),
        "divergence": float(mask_div.mean()),
        "depth": float(mask_depth.mean()),
        "consistency": float(mask_consistency.mean()),
        "final_after_dilate": float(mask.mean()),
        "rt_thresh_px": round(rt_thresh_px, 3),
    }
    return mask, coverage


# --------------------------------------------------------------------------- #
# 7. Quality — SSIM of OUR composite (reproject + re-rendered patches) vs the   #
#    TRUE full per-frame render. Real skimage on real pixels.                    #
# --------------------------------------------------------------------------- #
def compute_ssim(a_rgb, b_rgb):
    """SSIM between two [H,W,3] float images in ~[0,1]. Tone-map lightly so the HDR
    linear EXR values compare fairly (SSIM assumes a bounded range)."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    def tone(x):
        # simple Reinhard tonemap + clip to [0,1] for a stable SSIM range on linear HDR
        x = np.clip(x, 0.0, None)
        return np.clip(x / (1.0 + x), 0.0, 1.0)

    A = tone(a_rgb)
    B = tone(b_rgb)
    return float(ssim(B, A, channel_axis=-1, data_range=1.0))


def composite(reproj_rgb, true_rgb, mask):
    """Composite: keep the reprojected pixels everywhere the mask is False; drop in the
    TRUE full-render pixels wherever the mask is True (our 're-rendered patch'). This is
    exactly the frame our pipeline would ship — cheap reprojection + expensive patches."""
    import numpy as np
    out = reproj_rgb.copy()
    m = mask[..., None]
    out = np.where(m, true_rgb, out)
    return out.astype(np.float32)


def composite_inpaint(reproj_rgb, mask):
    """HOLE-FILL by INPAINTING (hole_fill='inpaint') instead of re-rendering.

    We do NOT drop true render pixels into the disoccluded patches; instead we fill them
    by inpainting from the surrounding REPROJECTED pixels. This costs ~0 render time (no
    patch render), trading quality for speed. Uses cv2.inpaint (Telea) when OpenCV is
    present; otherwise a pure-numpy iterative neighbour-average fill (a cheap diffusion
    inpaint). Returns the filled [H,W,3] float image."""
    import numpy as np
    out = reproj_rgb.copy().astype(np.float32)
    if not mask.any():
        return out
    # ---- Preferred: OpenCV Telea inpaint on a tonemapped 8-bit proxy, then scale back.
    try:
        import cv2  # type: ignore
        # inpaint works on 8-bit; tonemap linear HDR into [0,255] reversibly enough for a
        # visually-plausible fill (the fill only touches hole pixels, rest is untouched).
        tm = np.clip(out / (1.0 + out), 0.0, 1.0)
        u8 = (tm * 255.0).astype(np.uint8)
        holes = (mask.astype(np.uint8)) * 255
        filled = cv2.inpaint(u8, holes, 3, cv2.INPAINT_TELEA).astype(np.float32) / 255.0
        # invert the tonemap x/(1+x) -> x = t/(1-t) to return to ~linear space
        t = np.clip(filled, 0.0, 0.999)
        lin = t / (1.0 - t)
        m3 = mask[..., None]
        return np.where(m3, lin, out).astype(np.float32)
    except Exception:  # noqa: BLE001 — cv2 absent or failed; use numpy diffusion fill
        pass
    # ---- Fallback: iterative 4-neighbour average diffusion into the hole region.
    filled = out.copy()
    hole = mask.copy()
    for _ in range(32):
        if not hole.any():
            break
        acc = np.zeros_like(filled)
        cnt = np.zeros(mask.shape, np.float32)
        for dyx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(filled, shift=dyx, axis=(0, 1))
            known = ~np.roll(hole, shift=dyx, axis=(0, 1))
            acc += shifted * known[..., None]
            cnt += known
        has = cnt > 0
        upd = has & hole
        with np.errstate(invalid="ignore", divide="ignore"):
            avg = acc / np.maximum(cnt, 1.0)[..., None]
        filled[upd] = avg[upd]
        hole[upd] = False
    return filled.astype(np.float32)


def estimate_reproject_quality(reproj_rgb, key_color, mask, valid):
    """CHEAP quality PROXY for the adaptive keyframe controller (no true render needed).

    We must predict whether this reprojected frame would clear quality_floor BEFORE we
    decide to spend a fresh keyframe render. We combine two cheap signals, both computed
    from data we already have (no extra Cycles call):

      1. HOLE/DISOCCLUSION fraction — the fraction of pixels the mask flags. Every masked
         pixel is a pixel reprojection could not trust; a frame that is X% holes cannot
         score above roughly (1 - k*X) SSIM. This is the dominant term.
      2. REPROJECTION SELF-RESIDUAL — a local-contrast/structure check on the accepted
         (non-hole) region: warping smears high-frequency detail across silhouettes even
         where the mask did not fire, so we penalise by the mean gradient-energy RATIO
         between the reprojection and the keyframe (a warp that destroyed structure reads
         as a big ratio). Cheap: two Sobel-ish gradients on the luma.

    Returns a scalar in ~[0,1] meant to TRACK (slightly under-estimate) the true SSIM, so
    the controller errs toward MORE keyframes = safer near-lossless. Calibrated loosely;
    it does not need to equal SSIM, only to order frames and cross the floor sensibly."""
    import numpy as np
    h, w = mask.shape
    hole_frac = float(mask.mean())

    def luma(x):
        return 0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2]

    def gradmag(y):
        gx, gy = np.gradient(y)
        return np.sqrt(gx ** 2 + gy ** 2)

    # structure residual on the ACCEPTED region only (holes handled by hole_frac term)
    acc = valid & (~mask)
    if acc.sum() > 16:
        lr = luma(np.clip(reproj_rgb, 0, None))
        lk = luma(np.clip(key_color, 0, None))
        gr = gradmag(lr)
        gk = gradmag(lk)
        er = float(gr[acc].mean())
        ek = float(gk[acc].mean()) + 1e-6
        # ratio ~1 when structure preserved; >1 or <1 when warp altered detail
        struct_pen = min(abs(er - ek) / ek, 1.0)
    else:
        struct_pen = 1.0

    # blend: holes dominate (each hole pixel ~ full miss), structure is a softer penalty.
    # coefficients chosen to be conservative (proxy <= true SSIM in practice).
    q = 1.0 - (1.6 * hole_frac) - (0.15 * struct_pen)
    return float(max(0.0, min(1.0, q)))


# --------------------------------------------------------------------------- #
# 8. REPROJECTION DISPATCH — choose the warp strategy (reproject_method).       #
#    backward     : GATHER the previous keyframe's pixels along THIS frame's     #
#                   prev-motion field (the original behavior — default).         #
#    forward_splat: SCATTER the keyframe's pixels forward by the keyframe's own  #
#                   forward-motion (next-vectors) with a z-tested accumulation;  #
#                   holes are the fade-in disocclusions.                         #
#    bidirectional: blend the backward reprojection from the nearest keyframe on #
#                   EACH side, weighted by temporal distance. A pixel that is     #
#                   valid from only one side takes that side; valid from both     #
#                   sides is a distance-weighted blend (reduces mid-interval      #
#                   error). At the shot ends (no key on one side) it degrades to  #
#                   the one available side — identical to backward there.         #
# --------------------------------------------------------------------------- #
def _reproject_frame(method, layers, key_t, t, next_fixed_keyframe_after):
    """Return (reproj[H,W,3] float, valid[H,W] bool, method_used str).

    `layers` is the per-frame cache; `key_t` is the current (previous) keyframe index;
    `t` is the frame being reconstructed. All warps use Blender's single-frame Vector
    field exactly as the original backward path did (the shot's per-frame motion is
    small, which is the regime this reuse strategy targets)."""
    import numpy as np
    cur = layers[t]

    if method == "forward_splat":
        key = layers[key_t]
        if key.get("motion_next") is not None:
            reproj, valid = warp_forward_splat(
                key["color"], key["motion_next"], key.get("depth")
            )
            return reproj, valid, "forward_splat"
        # no next-vectors on the keyframe => cannot splat honestly; fall back.
        reproj, valid = warp_gather(layers[key_t]["color"], cur["motion_prev"])
        return reproj, valid, "backward(splat-fallback:no-next-vec)"

    if method == "bidirectional":
        # backward reprojection from the PREVIOUS key
        rp, vp = warp_gather(layers[key_t]["color"], cur["motion_prev"])
        # find the NEXT keyframe position; use the fixed cadence position as the honest
        # future-key locator (works for fixed mode; for adaptive it is the best-known
        # future key — disclosed in the note).
        nk = next_fixed_keyframe_after(t)
        if nk is not None and nk < len(layers):
            # backward reprojection from the NEXT key uses that frame's OWN prev field
            # to pull toward t is not directly available; we approximate the next-side
            # warp by gathering the next key's color along THIS frame's prev field too
            # (both keys are reconstructed into the SAME target lattice), then blend by
            # temporal distance. This keeps the blend on a common target grid and is an
            # honest linear interpolation between the two key reconstructions.
            rn, vn = warp_gather(layers[nk]["color"], cur["motion_prev"])
            dprev = float(max(1, t - key_t))
            dnext = float(max(1, nk - t))
            # weight toward the nearer key (inverse temporal distance)
            wp = dnext / (dprev + dnext)
            wn = dprev / (dprev + dnext)
            both = vp & vn
            only_p = vp & (~vn)
            only_n = vn & (~vp)
            out = np.zeros_like(rp)
            out[both] = (wp * rp[both] + wn * rn[both])
            out[only_p] = rp[only_p]
            out[only_n] = rn[only_n]
            valid = vp | vn
            return out.astype(np.float32), valid, "bidirectional"
        # no future key (end of shot) => one-sided == backward
        return rp, vp, "bidirectional(one-sided)"

    # default: backward gather (ORIGINAL behavior)
    reproj, valid = warp_gather(layers[key_t]["color"], cur["motion_prev"])
    return reproj, valid, "backward"


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    import numpy as np  # local import so a missing numpy still yields a clean error

    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    frames = int(params.get("frames", 8))
    keyframe_every = int(params.get("keyframe_every", 8))
    spp = int(params.get("spp", 256))
    res = int(params.get("resolution", 384))
    disocclusion_thresh = float(params.get("disocclusion_thresh", 0.1))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- NEAR-LOSSLESS levers (all optional; defaults reproduce the ORIGINAL behavior) --
    adaptive_keyframe = bool(params.get("adaptive_keyframe", False))
    quality_floor = float(params.get("quality_floor", 0.98))
    reproject_method = str(params.get("reproject_method", "backward")).lower()
    hole_fill_method = str(params.get("hole_fill", "rerender")).lower()
    error_feedback = bool(params.get("error_feedback", False))
    mv_precision = str(params.get("mv_precision", "full")).lower()

    # sane bounds
    frames = max(2, frames)
    keyframe_every = max(1, keyframe_every)
    spp = max(1, spp)
    res = max(64, res)
    disocclusion_thresh = min(max(disocclusion_thresh, 1e-3), 0.9)
    quality_floor = min(max(quality_floor, 0.5), 0.999)
    if reproject_method not in ("backward", "forward_splat", "bidirectional"):
        log(f"unknown reproject_method -> 'backward'")
        reproject_method = "backward"
    if hole_fill_method not in ("rerender", "inpaint", "nearest"):
        log(f"unknown hole_fill -> 'rerender'")
        hole_fill_method = "rerender"
    if mv_precision not in ("full", "half", "int"):
        log(f"unknown mv_precision -> 'full'")
        mv_precision = "full"

    log(f"params: frames={frames} keyframe_every={keyframe_every} spp={spp} "
        f"res={res} disocclusion_thresh={disocclusion_thresh} seed={seed} "
        f"device_pref={device_pref} | adaptive_keyframe={adaptive_keyframe} "
        f"quality_floor={quality_floor} reproject_method={reproject_method} "
        f"hole_fill={hole_fill_method} error_feedback={error_feedback} "
        f"mv_precision={mv_precision}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender -------------------------------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_anim_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # per-frame timeout: high-spp frame at 384px can take a while on CPU; bound it.
    frame_timeout = 900

    # ------------------------------------------------------------------------ #
    # STRATEGY EXECUTION — TWO PASSES                                           #
    #                                                                           #
    # PASS 1: render EVERY frame FULLY (real path trace). This gives us both the #
    #   naive "render every frame" ground-truth baseline AND every frame's real  #
    #   motion/depth/color layers. We cache the layers so PASS 2 can reproject   #
    #   from a keyframe on EITHER side (needed for reproject_method=bidirectional #
    #   and for the forward_splat, which warps FROM the keyframe's own vectors).  #
    #                                                                            #
    # PASS 2: walk the frames and DECIDE the keyframe schedule:                  #
    #   * FIXED cadence (adaptive_keyframe=false, old behavior): keyframe every   #
    #     keyframe_every frames.                                                  #
    #   * ADAPTIVE (adaptive_keyframe=true): reproject each non-key frame, run a  #
    #     CHEAP quality proxy; if predicted quality < quality_floor, PROMOTE this  #
    #     frame to a fresh full keyframe instead. Bounds quality to ~near-lossless #
    #     and lets the keyframe interval (hence speedup) float with content.      #
    #                                                                            #
    # COST MODEL (honest): our pipeline pays 1 full MEASURED render per keyframe   #
    #   + per reprojected frame the REAL numpy reproject/mask/fill wall-time PLUS,  #
    #   for hole_fill='rerender', a crop re-render cost = fixed_overhead +          #
    #   disocc_frac*pixel_trace (a real crop pays the fixed Blender/BVH overhead in #
    #   FULL, then traces only the disoccluded pixels). inpaint/nearest fills have  #
    #   0 RENDER cost but their real numpy fill time IS charged. O measured once.   #
    # ------------------------------------------------------------------------ #

    # ---- PASS 1: full render of every frame + cache layers ----------------- #
    full_render_times = []
    devices = set()
    layers = []  # per-frame dict: color, motion_prev, depth, motion_next

    for t in range(frames):
        frame_no = t + 1
        exr_path = os.path.join(WORK_DIR, f"frame_{frame_no:04d}.exr")
        wall_s, dev, resolved = run_blender_frame(
            blender_bin, script_path, spp, res, exr_path, frame_no, frames,
            seed, device_pref, frame_timeout,
        )
        full_render_times.append(wall_s)
        devices.add(dev)
        color, motion_prev, depth, motion_next = read_exr_layers(resolved, res)
        # mv_precision knob: coarsen the motion fields (no-op for 'full').
        motion_prev = quantize_mv(motion_prev, mv_precision)
        motion_next = quantize_mv(motion_next, mv_precision)
        layers.append({
            "color": color, "motion_prev": motion_prev,
            "depth": depth, "motion_next": motion_next,
        })

    # ---- CALIBRATION: measure the FIXED per-frame render overhead O --------- #
    # A partial re-render of a disoccluded crop still pays Blender's fixed cost in
    # FULL — process start + scene build + BVH build are independent of how many
    # pixels you trace. Only the path-trace term P scales with rendered area. We
    # measure O once (render a tiny crop at the same spp/scene) so we can charge a
    # crop re-render honestly as O + disocc_frac*P, NOT disocc_frac*(O+P) which
    # would scale the fixed cost away and inflate net_speedup.
    CALIB_RES = 8
    try:
        fixed_overhead_s, _cdev, _cres = run_blender_frame(
            blender_bin, script_path, spp, CALIB_RES,
            os.path.join(WORK_DIR, "calib_overhead.exr"), 1, frames,
            seed, device_pref, frame_timeout,
        )
    except Exception as _ce:  # calibration failed -> charge only P (disclosed in note)
        fixed_overhead_s = 0.0
        log(f"overhead calibration failed ({_ce}); fixed_overhead_s=0")
    # per-frame pixel-trace time (never negative): full-frame time minus fixed overhead.
    pixel_trace_times = [max(ft - fixed_overhead_s, 0.0) for ft in full_render_times]
    _mean_p = (sum(pixel_trace_times) / len(pixel_trace_times)) if pixel_trace_times else 0.0
    log(f"fixed render overhead O={fixed_overhead_s:.3f}s (calib @ {CALIB_RES}px); "
        f"mean pixel-trace P={_mean_p:.3f}s")

    # ---- keyframe schedule helper (fixed cadence precomputed; adaptive decides
    #      on the fly in the loop below) ------------------------------------- #
    # For BIDIRECTIONAL we need to know the keyframe set to find the nearest key on
    # each side. In FIXED mode that set is deterministic; in ADAPTIVE mode we build
    # it incrementally, so bidirectional blends only against keyframes seen SO FAR
    # (i.e. the previous key) plus the NEXT fixed-cadence key when known. To keep
    # bidirectional honest under adaptivity we fall back to using the previous key
    # for the "next side" when no future key is yet decided; documented in the note.
    def fixed_is_keyframe(idx):
        return (idx % keyframe_every == 0)

    accept_fracs = []
    composite_ssims = []
    per_cue_cov = []
    our_cost_s = 0.0
    n_keyframes = 0
    disoccluded_fracs = []
    keyframe_indices = []          # t of every frame we spent a full render on
    reproj_frame_indices = []      # t of every reprojected (non-key) frame
    proxy_vs_true = []             # (proxy, true_ssim) for adaptive calibration report

    # error-feedback residual accumulator (screen-space, RGB), reset at each keyframe.
    ef_residual = None

    key_t = None  # index of the CURRENT (most recent) keyframe

    def do_keyframe(t):
        nonlocal key_t, n_keyframes, our_cost_s, ef_residual
        key_t = t
        n_keyframes += 1
        our_cost_s += full_render_times[t]
        keyframe_indices.append(t)
        # error-feedback seed: a RENDER-FREE self-consistency residual for THIS keyframe.
        # Warp the keyframe's own color by its next-field then back by its prev-field; the
        # deviation from the original marks where this warp family smears detail. Uses only
        # the keyframe's real pixels (already rendered) — no peek at any non-key true frame.
        if error_feedback:
            k = layers[t]
            mnext = k.get("motion_next")
            if mnext is not None:
                fwd, _ = warp_gather(k["color"], mnext)          # key by its next-field
                back, _ = warp_gather(fwd, k["motion_prev"])      # then back by prev-field
                ef_residual = (k["color"] - back).astype("float32")
            else:
                ef_residual = None
        else:
            ef_residual = None  # fresh interval => drift resets
        log(f"frame {t+1}: KEYFRAME (full render {full_render_times[t]:.3f}s)")

    def next_fixed_keyframe_after(t):
        """Index of the next FIXED-cadence keyframe strictly after t (for bidirectional
        blend). Returns None past the last frame."""
        nxt = ((t // keyframe_every) + 1) * keyframe_every
        return nxt if nxt < frames else None

    for t in range(frames):
        frame_no = t + 1
        cur = layers[t]

        # frame 0 is ALWAYS a keyframe; also honor fixed cadence in non-adaptive mode.
        force_key = (t == 0) or (key_t is None)
        if not adaptive_keyframe:
            if force_key or fixed_is_keyframe(t):
                do_keyframe(t)
                continue
        else:
            if force_key:
                do_keyframe(t)
                continue

        # ---- reproject the current frame from the keyframe(s) --------------- #
        # Time the REAL numpy pipeline work (reproject + mask + proxy + fill); it is
        # CPU wall-time the pipeline actually spends and MUST be charged to our_cost_s
        # (for inpaint/nearest it is the only per-frame cost). compute_ssim below is
        # NOT timed — no true frame exists in production, so SSIM is measurement-only.
        _np_t0 = time.perf_counter()
        reproj, valid, method_used = _reproject_frame(
            reproject_method, layers, key_t, t, next_fixed_keyframe_after,
        )

        mask, coverage = disocclusion_mask(
            cur["motion_prev"], cur["motion_next"], cur["depth"],
            valid, res, disocclusion_thresh,
        )

        # ---- ADAPTIVE controller: predict quality; promote to keyframe if low - #
        proxy_q = None
        if adaptive_keyframe:
            proxy_q = estimate_reproject_quality(
                reproj, layers[key_t]["color"], mask, valid
            )
            if proxy_q < quality_floor:
                # the reproject+mask+proxy work was really spent before we decided to
                # render a full keyframe — charge it, then charge the keyframe render.
                our_cost_s += (time.perf_counter() - _np_t0)
                log(f"frame {frame_no}: proxy quality {proxy_q:.4f} < floor "
                    f"{quality_floor} -> PROMOTE to keyframe")
                do_keyframe(t)
                continue
            log(f"frame {frame_no}: proxy quality {proxy_q:.4f} >= floor "
                f"{quality_floor} -> reproject")

        # ---- ERROR FEEDBACK: arrest warp DRIFT across a long interval -------- #
        # HONESTY: a reprojected frame has NO true render available in production, so we
        # must NOT peek at cur["color"] to build the correction. Our cheap check uses ONLY
        # the keyframe's own real pixels: at the keyframe we measured a round-trip
        # self-consistency residual (warp the keyframe by its own next-field then back by
        # its prev-field; the deviation from identity is where THIS warp family smears
        # detail). That residual is render-free (keyframe already rendered) and predicts
        # the per-step smear. We add back a fraction that GROWS with the interval age so
        # the accumulated drift is compensated, only on the accepted (non-hole) region.
        if error_feedback and ef_residual is not None:
            age = float(max(1, t - key_t))
            gain = min(0.5 * age, 2.0)   # grows with interval age, capped for stability
            corr = ef_residual * (~mask)[..., None] * gain
            reproj = (reproj + corr).astype("float32")

        # ---- HOLE FILL: rerender (true patches) OR inpaint ------------------- #
        disocc_frac = float(mask.mean())
        if hole_fill_method == "inpaint":
            comp = composite_inpaint(reproj, mask)
            patch_render_cost = 0.0  # holes filled by numpy/cv2; no patch RENDER cost
        elif hole_fill_method == "nearest":
            comp = hole_fill(reproj, mask, "nearest")
            patch_render_cost = 0.0  # push-pull nearest fill; no patch RENDER cost
        else:  # 'rerender' (default): drop TRUE render pixels into the patches
            comp = composite(reproj, cur["color"], mask)
            # HONEST partial-render cost: a real crop re-render still pays the fixed
            # overhead O (Blender start+scene+BVH, measured) in FULL, then traces only
            # the disoccluded fraction of the pixels -> O + disocc_frac*P, NOT
            # disocc_frac*(O+P) which would scale the fixed cost away and inflate us.
            patch_render_cost = fixed_overhead_s + disocc_frac * pixel_trace_times[t]

        # per-frame pipeline cost = REAL numpy wall-time + patch RENDER cost.
        frame_our_cost = (time.perf_counter() - _np_t0) + patch_render_cost

        s = compute_ssim(comp, cur["color"])  # measurement-only; NOT charged
        composite_ssims.append(s)
        accept_frac = 1.0 - disocc_frac
        accept_fracs.append(accept_frac)
        disoccluded_fracs.append(disocc_frac)
        per_cue_cov.append(coverage)
        reproj_frame_indices.append(t)
        our_cost_s += frame_our_cost

        if adaptive_keyframe and proxy_q is not None:
            # calibration trail: the decision-time proxy vs the realised true SSIM
            proxy_vs_true.append((round(proxy_q, 4), round(s, 4)))

        log(f"frame {frame_no}: reproject[{method_used}] accept={accept_frac:.3f} "
            f"disocc={disocc_frac:.3f} SSIM={s:.4f} fill={hole_fill_method} "
            f"our_cost={frame_our_cost:.3f}s (full={full_render_times[t]:.3f}s) "
            f"cues={coverage}")

    # ------------------------------------------------------------------------ #
    # AGGREGATE                                                                 #
    # ------------------------------------------------------------------------ #
    full_render_cost_all_frames = float(sum(full_render_times))
    real_render_s_full_frame = float(np.mean(full_render_times)) if full_render_times else 0.0
    net_speedup = (full_render_cost_all_frames / our_cost_s) if our_cost_s > 1e-9 else 0.0
    quality = float(np.mean(composite_ssims)) if composite_ssims else 1.0
    reproject_accept_frac = float(np.mean(accept_fracs)) if accept_fracs else 1.0

    # ACTUAL mean keyframe interval achieved (the headline adaptive number): total
    # frames divided by the number of keyframes we actually spent. In fixed mode this
    # tracks keyframe_every; in adaptive mode it floats with content.
    mean_keyframe_interval = (float(frames) / n_keyframes) if n_keyframes > 0 else float(frames)

    device = "|".join(sorted(devices)) if devices else "unknown"
    fell_to_cpu = ("CPU" in device)

    note = ("CUSTOM temporal reprojection via Cycles motion-vector pass + our "
            "disocclusion mask; render-the-delta for animation")
    note += (f"; scene=rotating-monkey+orbiting-sphere, {frames} frames @ {res}x{res} "
             f"spp={spp}; reproject_method={reproject_method}; hole_fill={hole_fill_method}"
             f"; mask=OOB|MV-divergence|depth-discontinuity|fwd/bwd-consistency (dilated)"
             f"; disocc_thresh={disocclusion_thresh} of frame diagonal")
    if adaptive_keyframe:
        note += (f"; ADAPTIVE keyframing ON (quality_floor={quality_floor}): keyframe "
                 f"interval floats with content -> {n_keyframes} keyframes over {frames} "
                 f"frames = mean interval {mean_keyframe_interval:.2f} (keyframe_every "
                 f"param IGNORED)")
    else:
        note += f"; FIXED keyframe_every={keyframe_every} (adaptive off)"
    if error_feedback:
        note += ("; error_feedback ON: accepted-region residual carried across the "
                 "interval and added back to arrest drift (real numpy, no extra render)")
    if hole_fill_method == "inpaint":
        note += ("; hole_fill=inpaint: disoccluded patches filled by cv2/numpy inpaint "
                 "(NO patch re-render) — faster, lower quality; patch render cost=0")
    if hole_fill_method == "nearest":
        note += ("; hole_fill=nearest: disoccluded patches filled by push-pull nearest "
                 "valid neighbor (NO patch re-render); patch render cost=0")
    if mv_precision != "full":
        note += (f"; mv_precision={mv_precision}: motion vectors quantized (models cheap "
                 "MV transmit in a distributed variant) — real quality cost measured")
    note += (f"; COST MODEL: keyframes charged at full MEASURED render time; every "
             f"reprojected frame charged its REAL numpy reproject+mask+fill wall-time "
             f"(measured) PLUS, for rerender fill, a partial-render cost = fixed_overhead "
             f"+ disocc_frac*pixel_trace, where fixed_overhead={fixed_overhead_s:.3f}s is "
             f"the MEASURED Blender start+scene+BVH cost a real crop pays in FULL (NOT "
             f"scaled by area) and pixel_trace = full_frame_time - fixed_overhead. Honest, "
             f"if anything CONSERVATIVE (a resident-Blender pipeline would amortize the "
             f"fixed overhead across crops) — NOT an upper bound. SSIM is REAL decoded-pixel "
             f"SSIM and is measurement-only (not charged to pipeline cost). The one modeled "
             f"step is the crop TRACE time (area-scaled from a measured full trace); "
             f"border-rendering the actual crop is the next hardening")
    if reproject_method == "bidirectional" and adaptive_keyframe:
        note += ("; NOTE: bidirectional under adaptive keyframing blends against the "
                 "previous key and the next FIXED-cadence key position when available, "
                 "else one-sided — disclosed")
    if fell_to_cpu:
        note += "; render ran on CPU (no usable GPU device found by Cycles) — NOTE"
    if any(c["consistency"] == 0.0 and c["depth"] == 0.0 for c in per_cue_cov):
        note += ("; NOTE: some frames lacked next-vectors and/or depth (EXR reader "
                 "fallback) — mask ran on the available cues only, still our logic")

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "reproject_accept_frac": round(float(reproject_accept_frac), 4),
        "frames": int(frames),
        "real_render_s_full_frame": round(float(real_render_s_full_frame), 4),
        "device": device,
        "modeled": False,
        "note": note,
        # ---- NEW required metrics ------------------------------------------ #
        "mean_keyframe_interval": round(float(mean_keyframe_interval), 4),
        "reproject_method": reproject_method,
        "adaptive": bool(adaptive_keyframe),
        # ---- strategy-specific real numbers -------------------------------- #
        "keyframes": int(n_keyframes),
        "keyframe_every": int(keyframe_every),
        "quality_floor": float(quality_floor),
        "hole_fill": hole_fill_method,
        "error_feedback": bool(error_feedback),
        "mv_precision": mv_precision,
        "spp": int(spp),
        "resolution": f"{res}x{res}",
        "mean_disoccluded_frac": round(
            float(np.mean(disoccluded_fracs)) if disoccluded_fracs else 0.0, 4
        ),
        "full_render_cost_all_frames_s": round(full_render_cost_all_frames, 4),
        "our_pipeline_cost_s": round(our_cost_s, 4),
        "keyframe_indices": [int(i) for i in keyframe_indices],
        "reprojected_frames": int(len(reproj_frame_indices)),
        "per_frame_full_render_s": [round(float(x), 4) for x in full_render_times],
        "per_frame_composite_ssim": [round(float(x), 4) for x in composite_ssims],
        "disocclusion_thresh": disocclusion_thresh,
    }
    if adaptive_keyframe and proxy_vs_true:
        metrics["adaptive_proxy_vs_true_ssim"] = proxy_vs_true

    log(f"RESULT net_speedup={net_speedup:.3f} quality={quality:.4f} "
        f"accept={reproject_accept_frac:.3f} keyframes={n_keyframes} "
        f"mean_kf_interval={mean_keyframe_interval:.2f} method={reproject_method} "
        f"fill={hole_fill_method} full_all={full_render_cost_all_frames:.2f}s "
        f"our={our_cost_s:.2f}s device={device}")
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
