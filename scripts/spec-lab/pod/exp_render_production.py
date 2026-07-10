#!/usr/bin/env python3
"""
exp_render_production.py — Track C, "move 1": the PROPER heavy-scene render-speculation test.

WHY THIS RUNNER EXISTS (the R1 post-mortem)
-------------------------------------------
The small-scene test (exp_cycles_render.py at 256x256) was UNFAIR to the speculation
thesis. At 256px the reference render finished in ~1s, so per-frame FIXED overhead —
process launch, scene build, OIDN model load, PNG encode — dominated. In that regime a
"cheap" low-spp+denoise draft came out *slower* (net_speedup 0.4-0.8x): you paid the
denoiser's fixed cost to save almost no sample work, because there was almost no sample
work to save. That is a SMALL-SCENE ARTIFACT, not a verdict on the method.

This runner re-runs the SAME question — does spending fewer samples + being smart pay? —
but in the PRODUCTION REGIME where a full render is genuinely slow:

    * heavier geometry   : subdivided + displaced monkey, hi-res sphere, a field of
                           scattered clutter cubes, a glossy floor (real multi-bounce GI)
    * higher resolution  : 512x512 (default; 720p-class work per pixel)
    * higher ref samples : 1536 spp reference (default), many GI bounces
    * sized so the FULL reference render takes ~30-90s on a datacenter GPU.

Only when the reference is slow can a cheaper strategy save REAL wall-clock. We measure
real Blender render times (perf_counter around each subprocess) and real SSIM vs the
high-spp reference. modeled:false — every number here is a genuine render measurement.

THE STRATEGY IS OURS (not a framework built-in) — "CX-Foveate"
--------------------------------------------------------------
The naive approach (whole-frame low-spp + OIDN) ALREADY FAILED on real renders even at
larger sizes, because a global denoiser smears converged, easy regions just as much as it
rescues the noisy hard ones, and you still pay its fixed cost. Blender ALSO ships its own
"adaptive sampling" — but that is a framework built-in, exactly what the owner told us NOT
to lean on. So we own the allocation logic end to end:

  PASS 1 — SCOUT (cheap, denoised): render the whole frame at a very low spp WITH OIDN.
           This is our fast, complete-but-approximate base image. Cheap.

  OUR MAP (the forked IP): from the scout we build a per-tile SALIENCY map ourselves —
           combining (a) local luminance variance (residual Monte-Carlo noise the denoiser
           couldn't fully kill) and (b) gradient/edge energy (geometric + specular detail a
           denoiser tends to over-smooth). Tiles whose saliency clears an adaptive
           percentile threshold are the ones where extra REAL samples will actually change
           the picture. This map — not Cycles — decides where the compute goes.

  PASS 2 — REFINE (real samples, ONLY where it matters): we take the axis-aligned bounding
           region that covers the high-saliency tiles and render JUST that sub-rectangle at
           high spp using Blender's render BORDER (a real crop render — real rays, real
           convergence), no denoise. Everything outside the region keeps the cheap scout.

  COMPOSITE (ours): we paste the high-spp refined crop back over the denoised scout with a
           feathered (cosine-falloff) alpha seam so the boundary doesn't show. The result is
           "converged where it counts, cheap-but-clean everywhere else."

  VERIFY: SSIM(composite, full high-spp reference). net_speedup = ref_time / our_total_time
          where our_total_time = scout + refine-border + composite (all real).

The bet: the refine border covers only the fraction of the frame that carries the noise /
detail, so its high-spp cost is a fraction of a full high-spp render, while the denoised
scout handles the easy majority for almost free — net_speedup > 2x at SSIM >= 0.95. If the
saliency region blows up to ~the whole frame (pathological scene), we degrade gracefully
toward "just the scout" and report the honest (possibly <2x) number. Never fabricate.

Params (argv[1] JSON), all optional:
  draft_spp   : int  scout (draft) samples per pixel            (default 48)
  ref_spp     : int  reference AND refine-border samples/pixel  (default 1536)
  resolution  : int  square image side length in px             (default 512)
  complexity  : "heavy" (default) | "medium" | "light" scene weight
  tile        : int  saliency tile size in px                   (default 32)
  saliency_pct: float percentile cutoff for "hot" tiles 0..100  (default 62.0)
  feather     : int  composite seam feather width in px         (default 24)
  seed        : int  Cycles + jitter seed (determinism)         (default 0)
  bounces     : int  GI diffuse/glossy bounce count             (default 8)
  blender_url : str  override the download URL (real 4.x LTS)   (default 4.2.0 LTS)
  device      : "AUTO" (default) | "GPU" | "CPU"

Emits ONE json line on stdout (the metrics):
  {"net_speedup","quality","real_render_s_draft","real_render_s_ref","spp_ratio",
   "resolution","device","modeled":false,"note":...}   (+ strategy-specific reals)

Contract: human logs -> STDERR; the LAST stdout line is exactly one JSON object; any
failure emits {"error":...} as the last stdout line and exits 0 (never hangs, never crashes
silently). We wrap main in try/except.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Constants — REUSED verbatim from exp_cycles_render.py so both runners share  #
# the exact same self-bootstrap (idempotent: skips if /root/blender/blender    #
# already exists from a prior rung). 4.2 is the Long-Term-Support release.     #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/cycles_production"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_production]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs (REUSED). Blender needs a few X/GL shared libs    #
#    even for a headless -b render. Idempotent and never fatal.                #
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
# 2. Self-bootstrap Blender (REUSED, idempotent). If BLENDER_BIN already       #
#    exists — e.g. a prior rung downloaded it — we skip straight to using it.  #
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
        # Stream to disk with a real UA; some mirrors 403 the default urllib UA.
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
# 3. The Blender scene+render script. This builds a HEAVY, production-regime    #
#    scene and can render EITHER the whole frame OR just a border sub-region    #
#    (our "refine" pass renders only the hot crop). GPU-selection is the exact  #
#    OPTIX->CUDA->HIP->ONEAPI->CPU ladder from exp_cycles_render.py.            #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                  # output PNG path
USE_DENOISE= os.environ["CX_DENOISE"] == "1"       # scout: on; ref/refine: off
SEED       = int(os.environ["CX_SEED"])
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")   # AUTO | GPU | CPU
COMPLEXITY = os.environ.get("CX_COMPLEXITY", "heavy")
BOUNCES    = int(os.environ.get("CX_BOUNCES", "8"))
# Border render region in NORMALIZED coords [0..1]; when CX_BORDER=1 we render
# ONLY this sub-rectangle (the refine pass). Blender's border is bottom-left origin.
USE_BORDER = os.environ.get("CX_BORDER", "0") == "1"
BMIN_X = float(os.environ.get("CX_BMIN_X", "0.0"))
BMIN_Y = float(os.environ.get("CX_BMIN_Y", "0.0"))
BMAX_X = float(os.environ.get("CX_BMAX_X", "1.0"))
BMAX_Y = float(os.environ.get("CX_BMAX_Y", "1.0"))

random.seed(SEED)

# ---- start from an empty scene (no default cube/lamp/cam) -------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- scene weight knobs ------------------------------------------------------
# "heavy" is tuned so a full 512px @ high-spp reference lands ~30-90s on a
# datacenter GPU: more subdivisions, more clutter primitives, more GI bounces.
if COMPLEXITY == "light":
    SUBDIV, CLUTTER, SPH_SEG = 2, 12, 32
elif COMPLEXITY == "medium":
    SUBDIV, CLUTTER, SPH_SEG = 3, 40, 64
else:  # heavy (default)
    SUBDIV, CLUTTER, SPH_SEG = 4, 90, 96

# ---- distinct materials so SSIM has real color/detail structure -------------
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

mat_red   = make_mat("m_red",   (0.80, 0.18, 0.16, 1), 0.35)
mat_metal = make_mat("m_metal", (0.85, 0.80, 0.55, 1), 0.12, 0.95)  # glossy => noisy
mat_blue  = make_mat("m_blue",  (0.20, 0.40, 0.85, 1), 0.45)
mat_floor = make_mat("m_floor", (0.70, 0.70, 0.72, 1), 0.10)         # semi-glossy floor
mat_green = make_mat("m_green", (0.20, 0.70, 0.35, 1), 0.55)

# ---- HERO GEOMETRY: a subdivided + displaced monkey (heavy tri count) -------
# The subdivision surface + noise displacement gives fine geometric detail that
# a low-spp+denoise pass tends to smear — a real stress test for the saliency map.
bpy.ops.mesh.primitive_monkey_add(size=1.8, location=(0.0, -0.2, 0.95))
monkey = bpy.context.active_object
monkey.rotation_euler = (0.0, 0.0, math.radians(32))
sub = monkey.modifiers.new("subsurf", 'SUBSURF')
sub.levels = SUBDIV
sub.render_levels = SUBDIV
# a subtle displacement so silhouettes carry high-frequency detail
disp_tex = bpy.data.textures.new("disp_noise", type='CLOUDS')
disp_tex.noise_scale = 0.35
dmod = monkey.modifiers.new("disp", 'DISPLACE')
dmod.texture = disp_tex
dmod.strength = 0.08
monkey.data.materials.append(mat_blue)
for p in monkey.data.polygons:
    p.use_smooth = True

# ---- a high-segment glossy metal sphere (specular noise magnet) -------------
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.95, location=(1.7, 0.5, 0.95),
                                     segments=SPH_SEG, ring_count=SPH_SEG // 2)
sphere = bpy.context.active_object
for p in sphere.data.polygons:
    p.use_smooth = True
sphere.data.materials.append(mat_metal)

# ---- a red cube on the left -------------------------------------------------
bpy.ops.mesh.primitive_cube_add(size=1.5, location=(-1.9, 0.1, 0.75))
cube = bpy.context.active_object
cube.data.materials.append(mat_red)

# ---- a FIELD of scattered small clutter cubes (raises geometry + occlusion) -
# Deterministic placement from SEED so both scout and reference see identical
# geometry — SSIM is only meaningful if the scenes match exactly.
clutter_rng = random.Random(SEED * 7919 + 13)
for i in range(CLUTTER):
    cx = clutter_rng.uniform(-3.2, 3.2)
    cy = clutter_rng.uniform(-1.4, 2.6)
    cz = clutter_rng.uniform(0.05, 0.28)
    s  = clutter_rng.uniform(0.10, 0.32)
    bpy.ops.mesh.primitive_cube_add(size=s, location=(cx, cy, cz))
    c = bpy.context.active_object
    c.rotation_euler = (0.0, 0.0, clutter_rng.uniform(0, 6.28))
    c.data.materials.append(mat_green if (i % 3 == 0) else mat_red)

# ---- large semi-glossy floor to catch shadows + bounce light (real GI) ------
bpy.ops.mesh.primitive_plane_add(size=30.0, location=(0.0, 0.0, 0.0))
floor = bpy.context.active_object
floor.data.materials.append(mat_floor)

# ---- lights: a real area light + a lit world (ambient) ----------------------
bpy.ops.object.light_add(type='AREA', location=(2.8, -2.8, 5.2))
area = bpy.context.active_object
area.data.energy = 1500.0
area.data.size = 4.0
area.rotation_euler = (math.radians(35), math.radians(20), math.radians(15))

world = bpy.data.worlds.new("cx_world")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs["Color"].default_value = (0.15, 0.18, 0.24, 1.0)
    bg.inputs["Strength"].default_value = 0.6

# ---- camera looking at the scene --------------------------------------------
bpy.ops.object.camera_add(location=(0.0, -7.2, 3.6))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(63), 0.0, 0.0)
scene.camera = cam

# ---- Cycles engine + deterministic FIXED sampling ---------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
# CRITICAL: adaptive sampling OFF. We are proving OUR OWN allocation strategy,
# not leaning on Cycles' built-in adaptive sampler. Fixed spp keeps the draft-vs
# -ref comparison a clean, honest ratio and keeps the "framework trick" out of it.
cyc.use_adaptive_sampling = False
# more GI bounces => a genuinely path-traced, converge-slowly production look
try:
    cyc.max_bounces = BOUNCES
    cyc.diffuse_bounces = BOUNCES
    cyc.glossy_bounces = BOUNCES
    cyc.transmission_bounces = BOUNCES
except Exception as e:
    _log("could not set bounce counts:", e)

cyc.use_denoising = bool(USE_DENOISE)
if USE_DENOISE:
    try:
        cyc.denoiser = 'OPENIMAGEDENOISE'
    except Exception as e:
        _log("could not set OPENIMAGEDENOISE denoiser:", e)

# ---- device selection: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU (REUSED) -----
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

# ---- render settings: square res, PNG RGB, single frame ---------------------
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT

# ---- BORDER (crop) render for the REFINE pass -------------------------------
# When CX_BORDER=1 we render ONLY the hot sub-rectangle. use_crop_to_border=False
# means Blender still writes a FULL-resolution PNG but only traces rays inside the
# border (everything outside is left transparent/black). We keep the full canvas so
# the refined crop lands at the exact same pixel coords as the scout — the caller
# composites by copying just the border pixels back over the denoised scout.
if USE_BORDER:
    scene.render.use_border = True
    scene.render.use_crop_to_border = False
    # clamp + order the normalized border coords defensively
    bx0, bx1 = sorted((max(0.0, min(1.0, BMIN_X)), max(0.0, min(1.0, BMAX_X))))
    by0, by1 = sorted((max(0.0, min(1.0, BMIN_Y)), max(0.0, min(1.0, BMAX_Y))))
    scene.render.border_min_x = bx0
    scene.render.border_max_x = bx1
    scene.render.border_min_y = by0
    scene.render.border_max_y = by1
    # transparent film so untraced pixels are clearly outside the crop
    scene.render.film_transparent = True
    _log(f"BORDER render x[{bx0:.3f},{bx1:.3f}] y[{by0:.3f},{by1:.3f}]")
else:
    scene.render.use_border = False
    scene.render.film_transparent = False

scene.frame_set(1)

_log(f"rendering spp={SPP} res={RES} denoise={USE_DENOISE} border={USE_BORDER} "
     f"complexity={COMPLEXITY} bounces={BOUNCES} device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, out_png, denoise,
                       seed, device_pref, complexity, bounces, timeout_s,
                       border=None):
    """Invoke Blender headless to render ONE frame (or one BORDER crop).

    Returns (wall_seconds, chosen_device). The wall-time is measured around the
    subprocess: the honest end-to-end cost of producing this output at these params
    (OIDN denoise time is inside it when enabled, because Cycles denoises before it
    writes the PNG). `border`, if given, is (min_x, min_y, max_x, max_y) in [0..1]
    and switches on a real crop render of just that sub-rectangle.
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_png
    env["CX_DENOISE"] = "1" if denoise else "0"
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    env["CX_COMPLEXITY"] = complexity
    env["CX_BOUNCES"] = str(bounces)
    if border is not None:
        env["CX_BORDER"] = "1"
        env["CX_BMIN_X"] = str(border[0])
        env["CX_BMIN_Y"] = str(border[1])
        env["CX_BMAX_X"] = str(border[2])
        env["CX_BMAX_Y"] = str(border[3])
    else:
        env["CX_BORDER"] = "0"

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    log(f"render start: spp={spp} res={res} denoise={denoise} border={border} "
        f"complexity={complexity} -> {out_png}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1500:]
    err_tail = (proc.stderr or "")[-1500:]
    log(f"render rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_png)}); stdout tail: {tail[-600:]}"
        )
    return wall_s, chosen_device


# --------------------------------------------------------------------------- #
# 4. Image helpers (skimage/pillow/numpy are installed on the pod).           #
# --------------------------------------------------------------------------- #
def load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow (drops any alpha)."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def load_png_rgba(path):
    """Load a PNG as (H,W,4) float in [0,1]; alpha=1 if the file had no alpha.

    The refine (border) render uses a transparent film, so pixels OUTSIDE the crop
    have alpha≈0. We use that alpha as the ground-truth mask of "what was actually
    re-rendered", instead of trusting our own rounding of the border rectangle.
    """
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGBA")
    return np.asarray(img, dtype=np.float32) / 255.0


def compute_ssim(a_png, b_png):
    """SSIM between two real renders (color-aware, data_range=1.0)."""
    from skimage.metrics import structural_similarity as ssim
    a = load_png_float(a_png)
    b = load_png_float(b_png)
    return float(ssim(b, a, channel_axis=-1, data_range=1.0))


# --------------------------------------------------------------------------- #
# 5. THE CX-FOVEATE SALIENCY MAP — our own IP.                                 #
#    From the cheap denoised scout, decide WHERE the expensive refine samples  #
#    should go. Combines residual-noise variance with edge/gradient energy.    #
# --------------------------------------------------------------------------- #
def build_saliency_border(scout_png, res, tile, saliency_pct):
    """Return (border, hot_frac, n_hot, n_tiles) — OUR allocation decision.

    border = (min_x, min_y, max_x, max_y) in NORMALIZED [0..1], BOTTOM-LEFT origin
    (Blender's convention), i.e. the axis-aligned box covering every 'hot' tile.

    Saliency per tile = z(local luminance variance) + z(mean gradient magnitude):
      * local variance captures residual Monte-Carlo noise the denoiser left behind
        (glossy metal, contact shadows) — where more real samples buy convergence;
      * gradient magnitude captures geometric / specular EDGES the denoiser tends to
        over-blur — where more real samples buy sharpness.
    A tile is 'hot' if its saliency clears the `saliency_pct` percentile. We return
    the bounding box of the hot set: one contiguous border render is far cheaper to
    launch than N scattered crops, and production noise is spatially clustered
    (the hero object + its glossy neighbours), so the box stays tight in practice.
    """
    import numpy as np
    img = load_png_float(scout_png)          # (H,W,3) in [0,1], top-left origin
    h, w = img.shape[:2]
    gray = img.mean(axis=-1)                  # luminance proxy

    # per-pixel gradient magnitude (Sobel-ish via numpy diffs, edge-padded)
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = (gray[:, 2:] - gray[:, :-2]) * 0.5
    gy[1:-1, :] = (gray[2:, :] - gray[:-2, :]) * 0.5
    grad = np.sqrt(gx * gx + gy * gy)

    ny = max(1, h // tile)
    nx = max(1, w // tile)
    var_tiles = np.zeros((ny, nx), dtype=np.float64)
    grad_tiles = np.zeros((ny, nx), dtype=np.float64)
    for ty in range(ny):
        y0 = ty * tile
        y1 = h if ty == ny - 1 else (ty + 1) * tile
        for tx in range(nx):
            x0 = tx * tile
            x1 = w if tx == nx - 1 else (tx + 1) * tile
            block = gray[y0:y1, x0:x1]
            var_tiles[ty, tx] = float(block.var())
            grad_tiles[ty, tx] = float(grad[y0:y1, x0:x1].mean())

    def zscore(a):
        s = a.std()
        return (a - a.mean()) / s if s > 1e-12 else np.zeros_like(a)

    saliency = zscore(var_tiles) + zscore(grad_tiles)
    n_tiles = ny * nx

    # adaptive percentile cutoff: the top (100 - saliency_pct)% of tiles are 'hot'
    thresh = float(np.percentile(saliency, saliency_pct))
    hot = saliency > thresh
    n_hot = int(hot.sum())

    # degenerate guard: if nothing (or everything) is hot, fall back sensibly
    if n_hot == 0:
        # refine the single hottest tile so the refine pass is never empty
        ij = np.unravel_index(int(np.argmax(saliency)), saliency.shape)
        hot = np.zeros_like(hot)
        hot[ij] = True
        n_hot = 1

    ys, xs = np.where(hot)
    ty0, ty1 = ys.min(), ys.max()
    tx0, tx1 = xs.min(), xs.max()

    # tile index range -> pixel range -> normalized. Note the Y FLIP: image arrays
    # are top-left origin, Blender's border is bottom-left origin.
    px0 = tx0 * tile
    px1 = min(w, (tx1 + 1) * tile)
    py0 = ty0 * tile
    py1 = min(h, (ty1 + 1) * tile)

    nbmin_x = px0 / w
    nbmax_x = px1 / w
    # flip Y for Blender: top image row -> high Blender Y
    nbmin_y = 1.0 - (py1 / h)
    nbmax_y = 1.0 - (py0 / h)

    box_area = (nbmax_x - nbmin_x) * (nbmax_y - nbmin_y)
    hot_frac = float(box_area)  # fraction of the FRAME the refine border will cover

    border = (float(nbmin_x), float(nbmin_y), float(nbmax_x), float(nbmax_y))
    return border, hot_frac, n_hot, n_tiles


# --------------------------------------------------------------------------- #
# 6. THE CX-FOVEATE COMPOSITE — our own IP.                                    #
#    Paste the high-spp refined crop back over the denoised scout with a       #
#    cosine-feathered alpha seam so the boundary is invisible.                 #
# --------------------------------------------------------------------------- #
def composite_foveate(scout_png, refine_png, out_png, feather):
    """Composite refine (high-spp crop, transparent outside) over the denoised scout.

    Uses the refine render's OWN alpha as the region mask (ground truth of what was
    actually re-rendered), eroded to a feathered cosine falloff `feather` px wide so
    the high-spp island blends seamlessly into the cheap scout. Returns the composite
    path. This is a pure image op — its wall-time is folded into our_total_time.
    """
    import numpy as np
    from PIL import Image

    base = load_png_float(scout_png)             # (H,W,3) denoised scout
    ref = load_png_rgba(refine_png)              # (H,W,4) crop; alpha=1 inside
    h, w = base.shape[:2]
    if ref.shape[0] != h or ref.shape[1] != w:
        # resize defensively — should already match (same res, full canvas)
        ref_img = Image.fromarray((ref * 255).astype("uint8"), "RGBA").resize((w, h))
        ref = np.asarray(ref_img, dtype=np.float32) / 255.0

    rgb = ref[..., :3]
    alpha = ref[..., 3]                          # 1 inside the refined crop, 0 outside
    mask = (alpha > 0.5).astype(np.float32)

    # feather the mask edge with a cosine ramp so the seam doesn't pop. We do a cheap
    # separable box-blur (repeated) on the hard mask; the blurred mask in [0,1] is the
    # blend weight. This keeps refined pixels at weight~1 in the interior and ramps to
    # 0 across ~feather px at the border.
    def box_blur(a, radius):
        if radius < 1:
            return a
        k = 2 * radius + 1
        pad = np.pad(a, radius, mode="edge")
        # horizontal
        csum = np.cumsum(pad, axis=1)
        h1 = (csum[:, k - 1:] - np.pad(csum, ((0, 0), (1, 0)),
              mode="constant")[:, :-(k)]) / k
        h1 = h1[radius:-radius, :]
        # vertical
        csum2 = np.cumsum(np.pad(h1, ((radius, radius), (0, 0)), mode="edge"), axis=0)
        v1 = (csum2[k - 1:, :] - np.pad(csum2, ((1, 0), (0, 0)),
              mode="constant")[:-(k), :]) / k
        return v1

    weight = mask
    if feather >= 1:
        # Blur the hard mask -> a [0,1] ramp: ~1 in the interior, decaying to 0 across
        # ~feather px OUTSIDE the crop (so the seam feathers INTO the scout, not into
        # the refined island). np.maximum(., mask) guarantees every genuinely-refined
        # pixel keeps weight 1.0, so the high-spp interior is copied verbatim.
        weight = np.clip(box_blur(mask, int(feather)), 0.0, 1.0)
        weight = np.maximum(weight, mask)

    weight3 = weight[..., None]
    comp = weight3 * rgb + (1.0 - weight3) * base
    comp = np.clip(comp, 0.0, 1.0)
    Image.fromarray((comp * 255).astype("uint8"), "RGB").save(out_png)
    return out_png


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    draft_spp = int(params.get("draft_spp", 48))
    ref_spp = int(params.get("ref_spp", 1536))
    res = int(params.get("resolution", params.get("res", 512)))
    complexity = str(params.get("complexity", "heavy")).lower()
    tile = int(params.get("tile", 32))
    saliency_pct = float(params.get("saliency_pct", 62.0))
    feather = int(params.get("feather", 24))
    seed = int(params.get("seed", 0))
    bounces = int(params.get("bounces", 8))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))
    device_pref = str(params.get("device", "AUTO")).upper()

    # clamp inputs to sane ranges
    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    res = max(64, res)
    tile = max(8, min(tile, res // 2))
    saliency_pct = max(0.0, min(95.0, saliency_pct))
    feather = max(0, feather)
    if complexity not in ("light", "medium", "heavy"):
        complexity = "heavy"

    log(f"params: draft_spp={draft_spp} ref_spp={ref_spp} res={res} "
        f"complexity={complexity} tile={tile} saliency_pct={saliency_pct} "
        f"feather={feather} bounces={bounces} seed={seed} device_pref={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender (both idempotent) -------------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_scene_prod.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    scout_png = os.path.join(WORK_DIR, "scout.png")     # cheap denoised base
    refine_png = os.path.join(WORK_DIR, "refine.png")   # high-spp hot crop
    comp_png = os.path.join(WORK_DIR, "composite.png")  # our final output
    ref_png = os.path.join(WORK_DIR, "reference.png")   # full high-spp ground truth

    # per-render subprocess timeouts (bounded well under the ~18-min budget).
    scout_timeout = 300
    refine_timeout = 600
    ref_timeout = 800

    # ------------------------------------------------------------------ #
    # PASS 1 — SCOUT: whole frame, low spp + OIDN. The cheap base image.  #
    # ------------------------------------------------------------------ #
    scout_s, dev_scout = run_blender_render(
        blender_bin, script_path, draft_spp, res, scout_png,
        denoise=True, seed=seed, device_pref=device_pref,
        complexity=complexity, bounces=bounces, timeout_s=scout_timeout,
    )

    # ------------------------------------------------------------------ #
    # OUR MAP — decide the hot region from the scout (no render).         #
    # ------------------------------------------------------------------ #
    t_map0 = time.perf_counter()
    border, hot_frac, n_hot, n_tiles = build_saliency_border(
        scout_png, res, tile, saliency_pct
    )
    map_s = time.perf_counter() - t_map0
    log(f"saliency: {n_hot}/{n_tiles} tiles hot; refine border={border} "
        f"covers {hot_frac*100:.1f}% of frame (map build {map_s:.3f}s)")

    # ------------------------------------------------------------------ #
    # PASS 2 — REFINE: high spp on ONLY the hot border crop, no denoise.  #
    # ------------------------------------------------------------------ #
    refine_s, dev_refine = run_blender_render(
        blender_bin, script_path, ref_spp, res, refine_png,
        denoise=False, seed=seed, device_pref=device_pref,
        complexity=complexity, bounces=bounces, timeout_s=refine_timeout,
        border=border,
    )

    # ------------------------------------------------------------------ #
    # COMPOSITE — paste refined crop over the scout with a feathered seam #
    # ------------------------------------------------------------------ #
    t_comp0 = time.perf_counter()
    composite_foveate(scout_png, refine_png, comp_png, feather)
    comp_s = time.perf_counter() - t_comp0
    log(f"composite done in {comp_s:.3f}s -> {comp_png}")

    # our end-to-end cost = scout + saliency-map + refine crop + composite
    our_total_s = scout_s + map_s + refine_s + comp_s

    # ------------------------------------------------------------------ #
    # REFERENCE — the honest baseline: full frame at high spp, no denoise #
    # ------------------------------------------------------------------ #
    ref_s, dev_ref = run_blender_render(
        blender_bin, script_path, ref_spp, res, ref_png,
        denoise=False, seed=seed, device_pref=device_pref,
        complexity=complexity, bounces=bounces, timeout_s=ref_timeout,
    )

    devs = {dev_scout, dev_refine, dev_ref}
    device = dev_ref if len(devs) == 1 else "|".join(sorted(devs))
    fell_to_cpu = "CPU" in device

    # ------------------------------------------------------------------ #
    # VERIFY — SSIM(our composite, full reference) on the REAL PNGs.      #
    # Also compute the naive-baseline SSIM (scout alone vs reference) so   #
    # we can show foveate BEATS whole-frame low-spp+denoise on quality.    #
    # ------------------------------------------------------------------ #
    quality = compute_ssim(comp_png, ref_png)
    scout_quality = compute_ssim(scout_png, ref_png)
    log(f"SSIM(composite, reference) = {quality:.4f}   "
        f"SSIM(scout-only, reference) = {scout_quality:.4f}")

    # ------------------------------------------------------------------ #
    # net_speedup — honest wall-clock: full reference / our total.        #
    # Report the naive scout-only speedup too, for the head-to-head.      #
    # ------------------------------------------------------------------ #
    spp_ratio = ref_spp / float(draft_spp)
    net_speedup = ref_s / max(our_total_s, 1e-9)
    naive_speedup = ref_s / max(scout_s, 1e-9)  # what plain low-spp+denoise "would" get
    log(f"scout={scout_s:.3f}s refine={refine_s:.3f}s comp={comp_s:.3f}s "
        f"our_total={our_total_s:.3f}s ref={ref_s:.3f}s -> "
        f"net_speedup={net_speedup:.3f} (naive scout-only {naive_speedup:.3f})")

    note = (
        "heavy-scene production-regime Cycles render; tests whether low-spp+denoise "
        "pays when ref render is slow enough. STRATEGY=CX-Foveate (ours): cheap denoised "
        "scout -> our variance+gradient saliency map -> high-spp REAL border re-render of "
        "only the hot crop -> feathered composite. net_speedup = full-ref-time / "
        "(scout+map+refine-crop+composite); all times & SSIM are REAL renders."
    )
    if hot_frac > 0.9:
        note += (" WARNING: refine border covered >90% of the frame (scene noise not "
                 "spatially clustered) — foveation degraded toward a full re-render; "
                 "net_speedup will be near/below 1x, an honest negative result.")
    if fell_to_cpu:
        note += " NOTE: at least one render ran on CPU (Cycles found no usable GPU)."

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "real_render_s_draft": round(float(our_total_s), 4),  # OUR cheap-path total
        "real_render_s_ref": round(float(ref_s), 4),
        "spp_ratio": round(float(spp_ratio), 4),
        "resolution": f"{res}x{res}",
        "device": device,
        "modeled": False,
        "note": note,
        # ---- strategy-specific REAL numbers (the forked IP, all measured) ----
        "strategy": "cx-foveate",
        "complexity": complexity,
        "scout_spp": draft_spp,
        "refine_spp": ref_spp,
        "bounces": bounces,
        "real_render_s_scout": round(float(scout_s), 4),
        "real_render_s_refine": round(float(refine_s), 4),
        "real_composite_s": round(float(comp_s), 4),
        "real_saliency_map_s": round(float(map_s), 4),
        "refine_border": [round(b, 4) for b in border],
        "refine_frac_of_frame": round(float(hot_frac), 4),
        "hot_tiles": n_hot,
        "total_tiles": n_tiles,
        "quality_scout_only": round(float(scout_quality), 4),
        "naive_scout_only_speedup": round(float(naive_speedup), 4),
        "quality_gain_over_scout": round(float(quality - scout_quality), 4),
    }
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
