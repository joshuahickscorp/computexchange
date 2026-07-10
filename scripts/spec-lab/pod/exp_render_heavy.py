#!/usr/bin/env python3
"""
exp_render_heavy.py — Track C (REAL, DECISIVE): render speculation in its EXPENSIVE regime.

================================================================================
WHY THIS RUNNER EXISTS (the finding it is built to test)
================================================================================
Every prior spec-lab render experiment LOST. Not because the strategy was wrong,
but because the TEST SCENE was too cheap: on a datacenter GPU those scenes rendered
in 1-7 seconds, and speculation carries a fixed overhead (a second render, a mask
pass, a composite). When the base render is 3 seconds, saving 40% of it (~1.2s) is
swamped by the overhead — net_speedup < 1. That is a property of the *benchmark*,
not of the *method*.

The finding those failures pointed at: render speculation can only pay when the
base render is genuinely SLOW — minutes, not seconds — and that happens when GLOBAL
ILLUMINATION dominates the cost. Direct light is cheap (one shadow ray). Deep
indirect light — many diffuse bounces carrying color between surfaces — is where a
path tracer spends its minutes. So the decisive test must (a) build a scene where GI
genuinely dominates, and (b) make the reference render slow enough (~60-180s) that
the speculation overhead is a rounding error. Then, and only then, do we learn
whether the bounce-hybrid strategy actually wins in its real regime.

================================================================================
THE SCENE: a GI-DOMINATED CORNELL BOX (this is the whole point)
================================================================================
A Cornell box is the canonical hard case for global illumination: an ENCLOSED room
so light bounces many times before it dies (nothing escapes to a black void), with a
SATURATED RED left wall and SATURATED GREEN right wall so those bounces carry strong
COLOR BLEED — pure indirect light tinting the white surfaces near them. Add occluders
(a tall block + a sphere) that cast soft *indirect* shadows filled only by bounced
light, and a single emissive ceiling panel. Enclosed + colored + occluded means the
image is DOMINATED by indirect bounces, so the full high-bounce render is genuinely
expensive — exactly the regime the prior tests never reached.

We deliberately size resolution/spp so the FULL (high-bounce) reference lands around
target_ref_seconds (~120s) on a datacenter GPU. That slowness is a FEATURE: it makes
the fixed speculation overhead negligible, so the measured net_speedup reflects the
method, not the benchmark's per-render tax.

================================================================================
THE STRATEGY UNDER TEST: bounce-hybrid (cheap direct draft + full-GI where it matters)
================================================================================
  * DRAFT   = render at draft_bounces (default 1): direct light + one bounce. FAST,
              because it skips the deep GI recursion — but it MISSES color bleed and
              the deep indirect fill in shadowed pockets.
  * REFERENCE = render at ref_bounces (default 24): full, deep GI. SLOW. Ground truth.
  * HYBRID  = draft everywhere EXCEPT the GI-heavy regions, where we splice in the
              full-GI reference pixels (feathered seam). GI-poor pixels ship the cheap
              draft; GI-heavy pixels get the expensive full-GI path.

THE CUSTOM PART — how we choose the GI-heavy region WITHOUT knowing the reference
--------------------------------------------------------------------------------
In production you cannot peek at the reference to decide where the draft is wrong —
that would defeat the purpose (you'd have already paid for the reference). So we
derive the "needs full GI" mask FROM THE CHEAP DRAFT ALONE. The physics tells us
where the cheap direct-only draft is most likely to be WRONG:

  1. SHADOWED / low-direct-light regions. In a Cornell box the only light that fills
     a shadow is BOUNCED light — precisely what draft_bounces=1 under-computes. So
     dark regions of the draft are where deep GI matters most. We measure this from
     the draft's own luma: darker => more GI-dependent.

  2. COLOR-BLEED zones near strongly-colored surfaces. Where a pixel is strongly
     tinted red or green (high color saturation away from neutral grey), it is
     receiving colored *bounce* light off the red/green walls — again pure indirect
     light the cheap draft under-renders. We measure per-pixel chroma (distance from
     the grey axis) from the draft.

  We combine (shadow importance) + (chroma importance) into a single per-pixel
  GI-importance score computed ENTIRELY from the draft, then select the top
  gi_region_frac of pixels by that score as the GI-heavy region. This selection is
  OUR logic and — critically — it is causal: it uses only information available at
  draft time, so the measured speedup is one a real production renderer could achieve.

  (For CONTEXT we also compute an oracle-style beauty-difference view — where the
  draft and reference actually diverge — purely as a diagnostic to show how well the
  draft-only mask localized the true GI error. It does NOT feed the shipped hybrid.)

HONEST COST MODEL for the hybrid render time
--------------------------------------------------------------------------------
A production renderer renders the cheap draft over the WHOLE frame, then re-path-
traces ONLY the GI-heavy region at full bounce depth. Cycles supports exactly this
via a render BORDER (render_border_min/max + use_border), so we actually DO a third
render: the full-GI pass restricted to the bounding box of the GI-heavy mask. We
measure its real wall-time (gi_region_render_s). The hybrid time is then the sum of
three REAL measured times:

    t_hybrid = draft_time + gi_region_render_time + composite_time
    net_speedup = ref_time / t_hybrid

Everything in that ratio is a real perf_counter measurement around a real render or a
real numpy composite — no modeled fudge. (The border render's cost is a real upper
bound on a tile-exact re-render; a perfect per-pixel scheduler would be at most this
expensive, so this is the conservative, honest number.)

================================================================================
WHAT IS REAL (honesty ledger) — modeled:false
================================================================================
  REAL: three genuine Cycles path traces (draft@draft_bounces full frame,
    reference@ref_bounces full frame, GI-region@ref_bounces border-only); all three
    wall-times measured with perf_counter around the real subprocesses; the GI mask
    computed from the real draft PNG; the composite done in real numpy with a measured
    time; SSIM(hybrid, reference) and SSIM(draft, reference) both real scikit-image on
    the real PNGs. gi_cost_ratio = ref_time/draft_time is the real measured "how much
    more expensive is full GI" — the premise of the whole test.
  NOT MODELED: nothing in the shipped metrics is modeled. If the calibration shows GI
    did NOT actually get expensive (gi_cost_ratio ~ 1-2x), we SAY SO in the note and
    call the test inconclusive rather than declaring victory.

================================================================================
THE DECISIVE QUESTION
================================================================================
On a GI-dominated EXPENSIVE render, does net_speedup exceed ~1.5 at quality >= 0.9?
  * YES  => 3D render speculation is proven for its real (expensive) regime.
  * gi_cost_ratio only ~1-2x => GI wasn't actually expensive; scene wasn't heavy
    enough; test inconclusive (said honestly in the note).

Params (argv[1] JSON), all optional. "res" is accepted as an alias for "resolution":
  resolution        : int   square image side length in px        (default 512)
  spp               : int   samples per pixel (SAME for all three) (default 512)
  draft_bounces     : int   cheap draft total bounces             (default 1)
  ref_bounces       : int   expensive reference total bounces     (default 24)
  gi_region_frac    : float fraction of frame treated as GI-heavy (default 0.4)
  target_ref_seconds: float wall-time we want the ref to take     (default 120)
  seed              : int   Cycles + scene seed (determinism)     (default 1)
  blender_url       : str   override Blender download URL         (default 4.2 LTS)
  device            : "AUTO"|"GPU"|"CPU"                          (default AUTO)

Emits ONE json line on stdout (the metrics); human logs -> stderr; any failure emits
{"error":...} as the last stdout line and exits (never hangs).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Constants: REUSED verbatim from exp_cycles_render.py / exp_render_gbuffer.py #
# — same Blender 4.2 LTS bootstrap location + URL so a prior rung's download   #
# is reused (idempotent: ensure_blender skips if BLENDER_BIN already exists).  #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/render_heavy"

# Hard cap on any single render subprocess so a pathological deep-GI render can
# never hang the harness. The reference at 512/512/24 should land ~120s; 800s is
# generous headroom, after which we abort that render rather than block forever.
MAX_RENDER_S = 800


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_heavy]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs — REUSED from exp_cycles_render.py. Blender needs #
#    a few X/GL shared libs even for a headless -b render. Idempotent, never   #
#    fatal (|| true style).                                                    #
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
# 2. Self-bootstrap Blender — REUSED verbatim from exp_cycles_render.py.       #
#    Idempotent: if BLENDER_BIN already exists (a prior rung downloaded it) we  #
#    skip straight to using it, per the task note.                             #
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

    # Extract with system tar (handles .xz); strip the top-level versioned dir so
    # the binary lands exactly at BLENDER_DIR/blender.
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
# 3. The Blender scene+render script (run inside Blender's own python via -P).  #
#    Builds the GI-DOMINATED Cornell box, drives BOUNCE DEPTH from CX_BOUNCES   #
#    (spp held constant so the noise floor is identical across all renders),    #
#    optionally restricts the render to a BORDER (for the GI-region-only pass), #
#    and picks GPU via the OPTIX->CUDA->HIP->ONEAPI->CPU ladder.                #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                 # output PNG path
BOUNCES    = int(os.environ["CX_BOUNCES"])        # total light bounce depth (the lever)
SEED       = int(os.environ["CX_SEED"])
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")  # AUTO | GPU | CPU
# Optional render border (for the GI-region-only hybrid pass). Fractions in [0,1]
# of the frame; empty/unset => render the full frame.
BORDER     = os.environ.get("CX_BORDER", "")      # "xmin,xmax,ymin,ymax" or ""

random.seed(SEED)

# ---- start from an empty scene (delete the default cube etc.) ---------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# =============================================================================
# THE GI-DOMINATED CORNELL BOX
# An ENCLOSED room: floor, ceiling, back wall, saturated-RED left wall,
# saturated-GREEN right wall, white front-ish walls. Enclosed => light bounces
# many times; colored side walls => strong COLOR BLEED (pure indirect light);
# occluders inside => soft indirect shadows filled only by bounced light. This
# makes global illumination DOMINATE the image and the render cost.
#
# Geometry convention: the box spans roughly x,z in [-2,2] and depth y in
# [-2,2]. The camera looks down +y from -y (into the open front). We build each
# wall as a plane, positioned + rotated to face inward.
# =============================================================================

def make_mat(name, rgba, rough=0.75, metal=0.0, emit_strength=0.0):
    """Diffuse-ish material (high roughness => diffuse bounces => GI cost)."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = rgba
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = rough
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = metal
        # Emission drives the ceiling light panel. Input naming differs across
        # 4.x ("Emission" vs "Emission Color"); set defensively.
        if emit_strength > 0.0:
            for ename in ("Emission Color", "Emission"):
                if ename in bsdf.inputs:
                    bsdf.inputs[ename].default_value = (1.0, 0.98, 0.95, 1.0)
                    break
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = emit_strength
    return m

# Wall materials: WHITE-ish neutral surfaces + saturated colored side walls.
mat_white = make_mat("white", (0.80, 0.80, 0.80, 1), rough=0.85)
mat_red   = make_mat("red",   (0.85, 0.05, 0.05, 1), rough=0.85)  # left wall
mat_green = make_mat("green", (0.05, 0.75, 0.10, 1), rough=0.85)  # right wall
mat_light = make_mat("light", (1.0, 1.0, 1.0, 1), rough=0.9, emit_strength=28.0)

def add_wall(name, size, location, rotation_euler, material):
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.rotation_euler = rotation_euler
    obj.data.materials.append(material)
    return obj

WALL = 4.0  # side length of each wall plane (box is ~4 units across)

# Floor (z=-2, faces up) and ceiling (z=+2, faces down): white.
add_wall("floor",   WALL, (0.0, 0.0, -2.0), (0.0, 0.0, 0.0), mat_white)
add_wall("ceiling", WALL, (0.0, 0.0,  2.0), (math.radians(180), 0.0, 0.0), mat_white)
# Back wall (y=+2, faces -y toward camera): white.
add_wall("back",    WALL, (0.0, 2.0, 0.0), (math.radians(90), 0.0, 0.0), mat_white)
# Left wall (x=-2, faces +x): SATURATED RED.
add_wall("left",    WALL, (-2.0, 0.0, 0.0), (0.0, math.radians(90), 0.0), mat_red)
# Right wall (x=+2, faces -x): SATURATED GREEN.
add_wall("right",   WALL, (2.0, 0.0, 0.0), (0.0, math.radians(-90), 0.0), mat_green)

# Emissive CEILING LIGHT PANEL: a smaller bright plane just under the ceiling,
# facing down. This is the ONLY light — so everything not in its direct line is
# lit purely by bounces (maximizing the indirect fraction).
add_wall("light_panel", 1.6, (0.0, 0.0, 1.98), (math.radians(180), 0.0, 0.0),
         mat_light)

# ---- OCCLUDERS inside the box: a tall block + a sphere ----------------------
# These cast soft INDIRECT shadows (filled only by bounced light) and pick up
# color bleed from the red/green walls — real, localizable GI.
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-0.75, 0.6, -1.0))
block = bpy.context.active_object
block.scale = (0.65, 0.65, 1.6)          # tall block
block.rotation_euler = (0.0, 0.0, math.radians(20))
block.data.materials.append(make_mat("block_white", (0.78, 0.78, 0.80, 1), rough=0.85))

bpy.ops.mesh.primitive_uv_sphere_add(radius=0.6, location=(0.85, -0.2, -1.4),
                                     segments=48, ring_count=24)
sphere = bpy.context.active_object
for p in sphere.data.polygons:
    p.use_smooth = True
sphere.data.materials.append(make_mat("sphere_white", (0.80, 0.80, 0.82, 1), rough=0.8))

# ---- world: BLACK (no ambient) so ALL fill light is genuine bounced light ---
world = bpy.data.worlds.new("cx_world")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    bg.inputs["Strength"].default_value = 0.0

# ---- camera looking into the open front of the box (down +y) ----------------
bpy.ops.object.camera_add(location=(0.0, -6.2, 0.0))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(90), 0.0, 0.0)   # look along +y
cam.data.lens = 42.0                                 # frame the box interior
scene.camera = cam

# ---- Cycles engine + deterministic sampling ---------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP                    # SAME spp for every render (noise floor fixed)
cyc.seed = SEED
cyc.use_adaptive_sampling = False    # fixed spp => bounce depth is the ONLY lever
cyc.use_denoising = False            # no denoiser: same spp => no draft/ref noise gap

# ===== THE LEVER WE PULL: light bounce depth =================================
# draft_bounces (e.g. 1) = direct + one bounce (misses deep GI / color bleed);
# ref_bounces (e.g. 24) = full deep GI (expensive). diffuse_bounces high is what
# makes an enclosed colored box expensive, so we drive every sub-limit from it.
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = BOUNCES
cyc.glossy_bounces       = BOUNCES
cyc.transmission_bounces = BOUNCES
cyc.volume_bounces       = max(0, min(BOUNCES, 2))   # no volumes here; keep small
cyc.transparent_max_bounces = max(BOUNCES, 4)        # alpha edges, not a GI lever
_log(f"bounce depth set to {BOUNCES} (max/diffuse/glossy/transmission)")

# ---- device selection: GPU (OPTIX->CUDA->HIP->ONEAPI) else CPU --------------
# REUSED ladder from exp_cycles_render.py / exp_render_gbuffer.py.
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
scene.frame_set(1)

# ===== OPTIONAL RENDER BORDER (the GI-region-only hybrid pass) ================
# When CX_BORDER is set we render ONLY the bounding box of the GI-heavy mask at
# full bounce depth. use_crop_to_border=False keeps the OUTPUT the full RES with
# the un-rendered area left black, so the caller can composite it pixel-aligned
# against the full-frame draft. The wall-time of THIS render is the real cost of
# re-tracing just the GI-heavy region deep — the honest hybrid "extra" cost.
if BORDER:
    try:
        xmin, xmax, ymin, ymax = (float(v) for v in BORDER.split(","))
        scene.render.use_border = True
        scene.render.use_crop_to_border = False   # full-size output, border filled
        scene.render.border_min_x = max(0.0, min(1.0, xmin))
        scene.render.border_max_x = max(0.0, min(1.0, xmax))
        scene.render.border_min_y = max(0.0, min(1.0, ymin))
        scene.render.border_max_y = max(0.0, min(1.0, ymax))
        _log(f"render border ON: x[{xmin:.3f},{xmax:.3f}] y[{ymin:.3f},{ymax:.3f}]")
    except Exception as e:
        _log("bad CX_BORDER, rendering full frame:", e)
        scene.render.use_border = False

_log(f"rendering spp={SPP} res={RES} bounces={BOUNCES} border={bool(BORDER)} "
     f"device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, bounces, out_png,
                       seed, device_pref, timeout_s, border=None):
    """Invoke Blender headless to render ONE frame at a given BOUNCE DEPTH.

    Returns (wall_seconds, chosen_device). The wall-time is measured around the
    subprocess — the honest end-to-end cost of producing this frame at this bounce
    depth (and, when `border` is given, over only that fraction of the frame). The
    timeout is capped so a pathological deep-GI render can never hang the harness.
    (REUSES the subprocess/timeout/device-parse pattern from exp_cycles_render.py;
    the new axes are CX_BOUNCES + the optional CX_BORDER.)
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_OUT"] = out_png
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    env["CX_BORDER"] = border if border else ""

    # -b (background/headless), -noaudio, --factory-startup so user prefs never
    # leak in; the script itself also read_factory_settings.
    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    log(f"render start: spp={spp} res={res} bounces={bounces} "
        f"border={border or 'full'} -> {out_png}")
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
# 4. THE GI-IMPORTANCE MASK, COMPUTED FROM THE DRAFT ALONE (our custom logic). #
#    In production you cannot peek at the reference, so we derive "where does   #
#    deep GI matter" causally from the cheap draft's own pixels:               #
#      * SHADOW importance  = darkness (low luma). In a Cornell box the only    #
#        thing that fills a shadow is bounced light, which draft_bounces=1      #
#        under-computes; so dark pixels are the most GI-dependent.              #
#      * CHROMA importance   = distance from the grey axis. Strong red/green    #
#        tint away from neutral is colored BOUNCE light off the walls — pure    #
#        indirect the draft misses; so saturated pixels are GI-dependent.       #
#    We combine them, then select the top gi_region_frac of pixels as GI-heavy. #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def _luma(rgb):
    """Rec.709 luma of an (H,W,3) array."""
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def _normalize01(a):
    """Robustly rescale a 2-D array to [0,1] using a small percentile clamp so a
    few outlier pixels don't crush the rest of the signal into a corner."""
    import numpy as np
    lo = float(np.quantile(a, 0.02))
    hi = float(np.quantile(a, 0.98))
    if hi - lo < 1e-9:
        return np.zeros_like(a)
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def gi_importance_mask_from_draft(draft_png, res, gi_region_frac):
    """Build the per-pixel GI-heavy boolean mask FROM THE DRAFT ONLY.

    score = shadow_importance + chroma_importance, both in [0,1]:
      shadow_importance = 1 - normalize(luma)   (dark => needs bounced fill)
      chroma_importance = normalize(chroma)      (colored tint => color bleed)

    Select the top gi_region_frac of pixels by score as GI-heavy. Using a quantile
    threshold makes the selected AREA equal gi_region_frac by construction, which is
    exactly the fraction our border render re-traces deep. Returns (mask, stats).
    """
    import numpy as np
    img = _load_png_float(draft_png)
    if img.shape[0] != res or img.shape[1] != res:
        # PNGs are RESxRES by construction, but guard anyway.
        img = img[:res, :res, :]

    luma = _luma(img)
    # chroma = distance of the pixel from its own grey (max-min across channels is
    # a cheap, robust saturation proxy that fires on red/green bleed but not on
    # neutral white walls or grey occluders).
    chroma = img.max(axis=-1) - img.min(axis=-1)

    shadow_imp = 1.0 - _normalize01(luma)     # dark => high
    chroma_imp = _normalize01(chroma)          # saturated => high

    # Combine. Weight shadow a touch higher: in a black-world Cornell box the
    # deep-GI error is dominated by shadow fill, with color bleed as the second
    # signal. (Both are causal — computed from the draft, never the reference.)
    score = 0.6 * shadow_imp + 0.4 * chroma_imp

    frac = min(max(gi_region_frac, 0.01), 0.99)
    thresh = float(np.quantile(score, 1.0 - frac))
    mask = score >= thresh

    stats = {
        "score_mean": float(score.mean()),
        "shadow_imp_mean": float(shadow_imp.mean()),
        "chroma_imp_mean": float(chroma_imp.mean()),
        "mask_frac_actual": float(mask.mean()),
        "mask_source": "draft_shadow+chroma",
    }
    return mask, stats


def mask_bounding_box_frac(mask):
    """Bounding box of the True pixels as (xmin,xmax,ymin,ymax) fractions in [0,1].

    Cycles' render border is a rectangle, so a scattered per-pixel mask is
    re-rendered by its bounding box. IMPORTANT: Blender's border Y origin is the
    BOTTOM of the image, while numpy row 0 is the TOP — so we flip Y here. Returns
    None if the mask is empty. The returned box is what we hand CX_BORDER.
    """
    import numpy as np
    ys, xs = np.where(mask)
    if ys.size == 0:
        return None
    h, w = mask.shape
    xmin = xs.min() / float(w)
    xmax = (xs.max() + 1) / float(w)
    # numpy row 0 = top; Blender border y=0 = bottom => flip.
    ymin = 1.0 - (ys.max() + 1) / float(h)
    ymax = 1.0 - ys.min() / float(h)
    # small pad so feathering at the box edge has real rendered pixels to blend
    pad = 1.0 / float(max(w, h))
    return (max(0.0, xmin - pad), min(1.0, xmax + pad),
            max(0.0, ymin - pad), min(1.0, ymax + pad))


# --------------------------------------------------------------------------- #
# 5. Composite: splice the GI-region full-GI render into the draft over the     #
#    feathered mask, then SSIM the hybrid against the full reference.           #
# --------------------------------------------------------------------------- #
def _feather_mask(mask, radius=2):
    """Soften a boolean mask into [0,1] weights via a tiny separable box blur (no
    scipy). A hard edge would leave a visible seam between cheap-draft and full-GI
    pixels; feathering blends the transition so the composite reads as one image."""
    import numpy as np
    m = mask.astype(np.float32)
    if radius <= 0:
        return m
    k = 2 * radius + 1
    pad = np.pad(m, radius, mode="edge")
    cs = np.cumsum(pad, axis=1)
    horiz = (cs[:, k - 1:] - np.pad(cs[:, :-k], ((0, 0), (1, 0)))) / k
    horiz = horiz[radius:-radius, :]
    pad2 = np.pad(horiz, ((radius, radius), (0, 0)), mode="edge")
    cs2 = np.cumsum(pad2, axis=0)
    vert = (cs2[k - 1:, :] - np.pad(cs2[:-k, :], ((1, 0), (0, 0)))) / k
    return np.clip(vert, 0.0, 1.0)


def composite_hybrid(draft_png, gi_region_png, mask, out_png, feather_radius=2):
    """hybrid = draft*(1-w) + gi_region*w, where w is the feathered GI-heavy mask.

    GI-poor pixels (w≈0) ship the cheap draft; GI-heavy pixels (w≈1) get the
    full-GI border render. We composite against the GI-REGION render (not the full
    reference), because in production that border render is all you'd have paid for.
    Returns (hybrid_array, composite_seconds) — the compose time is REAL and folds
    into the hybrid cost. (The border render leaves un-rendered pixels black; the
    feathered mask only pulls from pixels the border actually covered, so black
    fringe never leaks in.)
    """
    from PIL import Image
    import numpy as np
    t0 = time.perf_counter()
    draft = _load_png_float(draft_png)
    gi = _load_png_float(gi_region_png)
    if draft.shape != gi.shape:
        raise RuntimeError(f"draft/gi shape mismatch {draft.shape} vs {gi.shape}")
    w = _feather_mask(mask, radius=feather_radius)[..., None]  # (H,W,1)
    hybrid = draft * (1.0 - w) + gi * w
    hybrid8 = np.clip(hybrid * 255.0 + 0.5, 0, 255).astype("uint8")
    Image.fromarray(hybrid8, mode="RGB").save(out_png)
    compose_s = time.perf_counter() - t0
    return hybrid, compose_s


def _ssim(a_arr_or_png, ref_png):
    """SSIM(image, reference PNG). `a_arr_or_png` may be a float array or a path."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim
    a = a_arr_or_png if isinstance(a_arr_or_png, np.ndarray) else _load_png_float(a_arr_or_png)
    b = _load_png_float(ref_png)
    return float(ssim(b, a.astype("float32"), channel_axis=-1, data_range=1.0))


# --------------------------------------------------------------------------- #
# 6. Calibration: a quick low-res probe render to estimate whether the full     #
#    reference will land near target_ref_seconds, and scale spp/res toward it   #
#    if the estimate is wildly off. This keeps the reference genuinely SLOW     #
#    (the whole point) without blindly overshooting the 800s cap.               #
# --------------------------------------------------------------------------- #
def calibrate_reference_cost(blender_bin, script_path, res, spp, ref_bounces,
                             seed, device_pref, target_ref_seconds):
    """Render a small calibration frame at full ref_bounces and extrapolate.

    Cost of a fixed-spp path trace scales ~linearly in pixel count (res^2) and
    ~linearly in spp. We render at a downscaled probe resolution, measure its
    wall-time, subtract a fixed-overhead estimate, and extrapolate to (res, spp).
    Then we nudge spp toward target_ref_seconds if the projection is way off.
    Returns (spp_out, res_out, projection_note). Best-effort: on any failure we
    return the requested (spp, res) unchanged so calibration can never block the run.
    """
    probe_res = max(96, res // 4)          # small probe (~1/16 the pixels)
    probe_spp = max(16, min(spp, 128))     # cheap spp for a fast probe
    probe_png = os.path.join(WORK_DIR, "calib.png")
    try:
        # generous but bounded probe timeout; if even the probe is slow that is
        # itself the signal that the full render would blow the cap.
        probe_s, _dev = run_blender_render(
            blender_bin, script_path, probe_spp, probe_res, ref_bounces, probe_png,
            seed=seed, device_pref=device_pref, timeout_s=min(MAX_RENDER_S, 300),
        )
    except Exception as e:  # noqa: BLE001 — calibration is best-effort
        log(f"calibration probe failed (non-fatal): {e}; using requested spp/res")
        return spp, res, f"calibration skipped ({type(e).__name__})"

    # Estimate fixed per-render overhead (scene build + Blender startup) so we
    # extrapolate only the render-scaling portion. ~4s is a conservative floor.
    overhead = 4.0
    render_only = max(probe_s - overhead, 0.05)
    pixel_scale = (res * res) / float(probe_res * probe_res)
    spp_scale = spp / float(probe_spp)
    projected_ref_s = overhead + render_only * pixel_scale * spp_scale
    log(f"calibration: probe {probe_res}px/{probe_spp}spp took {probe_s:.2f}s "
        f"=> projected full ref ({res}px/{spp}spp) ~ {projected_ref_s:.1f}s "
        f"(target {target_ref_seconds:.0f}s)")

    note = (f"calib: probe {probe_res}px/{probe_spp}spp={probe_s:.1f}s -> "
            f"projected ref {projected_ref_s:.0f}s vs target {target_ref_seconds:.0f}s")

    spp_out = spp
    # If projection is far off target, scale spp (cheapest, cleanest lever — it
    # doesn't change composition/mask geometry) toward the target. Only act on
    # gross mismatches (>2x either way) so we respect the caller's intent otherwise.
    if projected_ref_s > 1e-3:
        ratio = target_ref_seconds / projected_ref_s
        if ratio > 2.0 or ratio < 0.5:
            spp_out = int(max(64, min(4096, round(spp * ratio))))
            note += f"; scaled spp {spp}->{spp_out} to approach target"
            log(f"calibration: scaling spp {spp} -> {spp_out} (ratio {ratio:.2f})")
    return spp_out, res, note


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    # "res" is an accepted alias for "resolution".
    resolution = int(params.get("resolution", params.get("res", 512)))
    spp = int(params.get("spp", 512))
    draft_bounces = int(params.get("draft_bounces", 1))
    ref_bounces = int(params.get("ref_bounces", 24))
    gi_region_frac = float(params.get("gi_region_frac", 0.4))
    target_ref_seconds = float(params.get("target_ref_seconds", 120))
    seed = int(params.get("seed", 1))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))
    device_pref = str(params.get("device", "AUTO")).upper()

    # sanity clamps
    resolution = max(64, resolution)
    spp = max(16, spp)
    draft_bounces = max(0, draft_bounces)
    ref_bounces = max(draft_bounces + 1, ref_bounces)
    gi_region_frac = min(max(gi_region_frac, 0.01), 0.99)

    log(f"params: resolution={resolution} spp={spp} draft_bounces={draft_bounces} "
        f"ref_bounces={ref_bounces} gi_region_frac={gi_region_frac} "
        f"target_ref_seconds={target_ref_seconds} seed={seed} device_pref={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender (reused, idempotent) ----------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_scene_heavy.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    draft_png = os.path.join(WORK_DIR, "draft.png")
    ref_png = os.path.join(WORK_DIR, "reference.png")
    gi_region_png = os.path.join(WORK_DIR, "gi_region.png")
    hybrid_png = os.path.join(WORK_DIR, "hybrid.png")

    # ---- 0b) CALIBRATION: keep the reference genuinely slow (~target_ref_s) - #
    # so the fixed speculation overhead is negligible — the entire premise. Best
    # effort; on any failure we proceed with the requested spp/res unchanged.
    spp, resolution, calib_note = calibrate_reference_cost(
        blender_bin, script_path, resolution, spp, ref_bounces,
        seed, device_pref, target_ref_seconds,
    )

    # per-render timeouts, all capped at MAX_RENDER_S so nothing can hang.
    draft_timeout = min(MAX_RENDER_S, 400)
    ref_timeout = MAX_RENDER_S
    region_timeout = MAX_RENDER_S

    # ---- 1) DRAFT: full frame at draft_bounces (cheap direct-light draft) --- #
    # This is the fast render that misses color bleed + deep indirect fill. It is
    # ALSO the only thing our GI mask is allowed to look at (production-causal).
    real_render_s_draft, dev_draft = run_blender_render(
        blender_bin, script_path, spp, resolution, draft_bounces, draft_png,
        seed=seed, device_pref=device_pref, timeout_s=draft_timeout,
    )

    # ---- 2) REFERENCE: full frame at ref_bounces (expensive full GI) ------- #
    real_render_s_ref, dev_ref = run_blender_render(
        blender_bin, script_path, spp, resolution, ref_bounces, ref_png,
        seed=seed, device_pref=device_pref, timeout_s=ref_timeout,
    )

    # gi_cost_ratio — how many x more expensive full GI is than the cheap draft.
    # This is the PREMISE of the whole test: if it's only ~1-2x, GI never got
    # expensive and the result is inconclusive (we say so below).
    gi_cost_ratio = real_render_s_ref / max(real_render_s_draft, 1e-9)
    log(f"gi_cost_ratio = ref/draft = {real_render_s_ref:.2f}/"
        f"{real_render_s_draft:.2f} = {gi_cost_ratio:.2f}x")

    # ---- 3) GI-HEAVY MASK from the DRAFT ALONE (our causal region selection) - #
    mask, mask_stats = gi_importance_mask_from_draft(draft_png, resolution, gi_region_frac)
    log(f"GI mask (draft-only): {mask_stats}")
    bbox = mask_bounding_box_frac(mask)
    if bbox is None:
        # degenerate: no GI-heavy pixels selected — fall back to full-frame border
        bbox = (0.0, 1.0, 0.0, 1.0)
    border_str = ",".join(f"{v:.5f}" for v in bbox)

    # ---- 4) GI-REGION render: full GI, but ONLY over the mask bbox (border) - #
    # This is the REAL "re-trace just the GI-heavy region deep" cost — the honest
    # hybrid extra. Its wall-time (gi_region_render_s) is measured, not modeled.
    gi_region_render_s, dev_region = run_blender_render(
        blender_bin, script_path, spp, resolution, ref_bounces, gi_region_png,
        seed=seed, device_pref=device_pref, timeout_s=region_timeout,
        border=border_str,
    )

    device = dev_ref if (dev_ref == dev_draft == dev_region) else \
        f"{dev_draft}|{dev_ref}|{dev_region}"
    fell_to_cpu = "CPU" in device

    # ---- 5) COMPOSITE the hybrid + measure real quality -------------------- #
    hybrid_arr, composite_s = composite_hybrid(
        draft_png, gi_region_png, mask, hybrid_png, feather_radius=2
    )
    quality = _ssim(hybrid_arr, ref_png)              # hybrid vs full-GI reference
    quality_draft_only = _ssim(draft_png, ref_png)    # cheap draft ALONE vs reference
    log(f"SSIM(hybrid, ref) = {quality:.4f} | "
        f"SSIM(draft_only, ref) = {quality_draft_only:.4f}")

    # ---- 6) net_speedup from THREE REAL times + the real composite --------- #
    # t_hybrid = draft render + GI-region render + composite. Every term is a real
    # measurement (perf_counter around a real render / a real numpy composite).
    t_hybrid = real_render_s_draft + gi_region_render_s + composite_s
    net_speedup = real_render_s_ref / max(t_hybrid, 1e-9)
    log(f"t_hybrid = {real_render_s_draft:.2f}(draft) + "
        f"{gi_region_render_s:.2f}(gi-region) + {composite_s:.3f}(composite) = "
        f"{t_hybrid:.2f}s -> net_speedup = {net_speedup:.3f}")

    # ---- 7) verdict wording keyed to the premise (honesty is load-bearing) - #
    verdict = ""
    if gi_cost_ratio < 2.0:
        verdict = (" INCONCLUSIVE: gi_cost_ratio only "
                   f"{gi_cost_ratio:.2f}x — full GI did NOT get expensive on this "
                   "hardware, so speculation overhead is not negligible; scene "
                   "wasn't heavy enough (raise spp/ref_bounces/resolution).")
    elif net_speedup >= 1.5 and quality >= 0.9:
        verdict = (f" WIN: net_speedup {net_speedup:.2f}x at SSIM {quality:.3f} on a "
                   f"genuinely expensive render (gi_cost_ratio {gi_cost_ratio:.1f}x) — "
                   "render speculation PROVEN for its real (GI-dominated) regime.")
    else:
        verdict = (f" NO WIN: net_speedup {net_speedup:.2f}x at SSIM {quality:.3f} "
                   f"despite gi_cost_ratio {gi_cost_ratio:.1f}x — GI was expensive but "
                   "the hybrid did not clear 1.5x @ 0.9 SSIM.")

    note = (
        "GI-dominated Cornell-box scene; full render intentionally slow so "
        "speculation overhead is negligible; bounce-hybrid = cheap direct draft "
        "(max_bounces=%d) + full-GI (max_bounces=%d) only in GI-heavy regions, "
        "selected from the DRAFT alone (shadow darkness + color-bleed chroma, no "
        "peek at the reference); times+SSIM real (three real renders: draft, ref, "
        "GI-region border; composite in numpy). %s%s"
        % (draft_bounces, ref_bounces, calib_note, verdict)
    )
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "real_render_s_draft": round(float(real_render_s_draft), 4),
        "real_render_s_ref": round(float(real_render_s_ref), 4),
        "gi_cost_ratio": round(float(gi_cost_ratio), 4),
        "draft_bounces": int(draft_bounces),
        "ref_bounces": int(ref_bounces),
        "gi_region_frac": round(float(mask_stats.get("mask_frac_actual", gi_region_frac)), 4),
        "quality_draft_only": round(float(quality_draft_only), 4),
        "resolution": int(resolution),
        "spp": int(spp),
        "device": device,
        "modeled": False,
        "note": note,
        # ---- strategy-specific real diagnostics ---------------------------- #
        "gi_region_render_s": round(float(gi_region_render_s), 4),
        "composite_s": round(float(composite_s), 4),
        "t_hybrid_s": round(float(t_hybrid), 4),
        "quality_gain_vs_draft_only": round(float(quality - quality_draft_only), 4),
        "mask_source": mask_stats.get("mask_source", "draft_shadow+chroma"),
        "mask_bbox_frac": [round(float(v), 4) for v in bbox],
        "shadow_imp_mean": round(float(mask_stats.get("shadow_imp_mean", 0.0)), 4),
        "chroma_imp_mean": round(float(mask_stats.get("chroma_imp_mean", 0.0)), 4),
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
