#!/usr/bin/env python3
"""
exp_render_tiles.py — CUSTOM SPATIAL-TILE speculation ("render as image parts").

============================================================================
WHY THIS IS OURS (not a Blender built-in)
============================================================================
Cycles ships two things that superficially resemble what we do but are NOT it:
  * adaptive sampling — a PER-PIXEL noise threshold Cycles evaluates INTERNALLY
    while it renders; it is a black box, it does not hand us a per-tile budget,
    it is not steerable by our own signal, and it is not distributable across a
    fleet.
  * render borders / regions — the ABILITY to render a rectangular sub-window.

We use the render-border *mechanism* as a dumb pipe. Everything that decides
WHERE the expensive samples go is OUR code:

  1. DRAFT PASS. Render ONE cheap full-frame image at draft_spp. This is the
     speculative "guess" of the whole frame — fast, noisy, but complete.

  2. OUR HARDNESS CLASSIFIER (the IP). Slice the draft into a grid of tiles and
     score each tile with a hardness signal WE define — a blend of:
        - local luminance VARIANCE          (Monte-Carlo noise + texture busy-ness)
        - EDGE density via a Sobel gradient  (silhouettes, contact shadows, specular
                                              edges — exactly where low-spp path
                                              tracing leaves fireflies and aliasing)
     normalized to [0,1] and combined into ONE per-tile hardness map. This map
     is our speculation's confidence signal: high hardness == "the cheap draft
     is probably WRONG here, re-render this tile at full quality."

  3. OUR NON-UNIFORM BUDGET ROUTING (the IP). We do NOT re-render everything.
     We pick the top `hard_frac` fraction of tiles by hardness and re-render ONLY
     those, at hard_spp, using a real Cycles render_border around each tile. The
     flat/cheap tiles keep their draft pixels for free. This is speculative
     execution: draft = speculate cheaply everywhere, then spend the real compute
     only on the tiles our classifier flags as low-confidence.

  4. COMPOSITE + HONEST SCORING. Stitch the final image: draft pixels for flat
     tiles, high-spp pixels for hard tiles. Compare that composite against a
     UNIFORM high-spp reference render (the ground truth) with real SSIM, and
     compare their real wall-clock costs. The WIN we are hunting: match the
     reference's quality at a FRACTION of its samples because we only paid full
     price where our classifier said it mattered.

  5. NATURALLY DISTRIBUTABLE. Each hard tile is an independent render_border job
     -> different tiles fan out to different fleet nodes. The draft is one small
     job; the reference never needs to exist in production (it is only rendered
     HERE to score honesty). We surface a `distributed_speedup` that models the
     fleet-parallel wall-clock (draft + slowest single hard tile) alongside the
     honest single-box `net_speedup`.

============================================================================
HONESTY (contract)
============================================================================
  * modeled:false for the core result. real_render_s_ref and
    real_composite_render_s are REAL perf_counter wall-times around REAL Blender
    Cycles subprocess renders (the draft render + every hard-tile border render).
  * quality is a REAL skimage SSIM between the stitched composite PNG and the
    uniform high-spp reference PNG — both genuine path-traced images.
  * net_speedup = real_render_s_ref / real_composite_render_s  (honest single-box
    wall-clock: what the whole adaptive pipeline actually cost vs the reference).
  * The ONLY modeled number is `distributed_speedup` (a fleet-parallel projection);
    it is clearly labelled and never overrides net_speedup. Everything else is measured.

Time budget ~18 min. We size the REFERENCE render to land in the ~20-90s regime
(res=384, ref_spp=512, a 4-mesh GI scene) so that real sample savings actually
show up — the tiny 256x256/1s cases failed because fixed overhead dominated.

Params (argv[1] JSON), all optional:
  grid        : int   tiles per side (grid=4 -> 16 tiles)              (default 4)
  draft_spp   : int   cheap full-frame draft samples                  (default 32)
  hard_spp    : int   high samples for re-rendered hard tiles          (default 512)
  ref_spp     : int   uniform reference samples (ground truth)         (default 512)
  hard_frac   : float fraction of tiles to re-render at hard_spp       (default 0.3)
  resolution  : int   square image side length in px                  (default 384)
  classifier  : "variance" | "edge" | "blend"  hardness signal         (default "blend")
  seed        : int   Cycles/scene determinism seed                    (default 0)
  device      : "AUTO" | "GPU" | "CPU"  compute preference             (default "AUTO")
  blender_url : str   override the 4.2 LTS tarball URL

Emits ONE json object as the LAST stdout line (metrics). On any failure the last
stdout line is {"error":"..."} and we exit 0 (never hang).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Constants: REUSED VERBATIM from exp_cycles_render.py so the pod's already-   #
# downloaded Blender (from a prior rung) is picked up and NOT re-downloaded.   #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/render_tiles"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_tiles]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs (idempotent, never fatal) — SAME pattern as the  #
#    flagship runner. Blender needs a few X/GL shared libs even for -b renders.#
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
# 2. Self-bootstrap Blender — SAME idempotent pattern as exp_cycles_render.py. #
#    If /root/blender/blender already exists (likely, from a prior rung), we   #
#    skip the download entirely.                                              #
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
# 3. The Blender scene+render script (run inside Blender's own python via -P). #
#    Scene geometry + camera + lights + GPU-selection are REUSED from the      #
#    flagship runner (so both runners trace the SAME scene and results are     #
#    comparable). The ONE new capability is the render_border: when the caller #
#    passes CX_BORDER we render ONLY that normalized sub-rectangle — this is    #
#    how a single hard TILE gets re-rendered at high spp without touching the   #
#    rest of the frame. That border feature is the pluggable Cycles pipe; the  #
#    DECISION of which borders to render is entirely our numpy classifier.      #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                 # output PNG path
SEED       = int(os.environ["CX_SEED"])
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")  # AUTO | GPU | CPU
# Optional render border (normalized 0..1, origin bottom-left in Blender):
#   CX_BORDER = "min_x,max_x,min_y,max_y". Absent -> full frame.
BORDER     = os.environ.get("CX_BORDER", "").strip()

random.seed(SEED)

# ---- start from an empty scene (delete the default cube etc.) ---------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- REAL geometry: a cube, a UV sphere, and Suzanne the monkey -------------
# Identical scene to exp_cycles_render.py so cross-runner numbers are comparable.
bpy.ops.mesh.primitive_cube_add(size=1.4, location=(-1.6, 0.0, 0.7))
cube = bpy.context.active_object

bpy.ops.mesh.primitive_uv_sphere_add(radius=0.9, location=(1.5, 0.4, 0.9),
                                     segments=48, ring_count=24)
sphere = bpy.context.active_object
for p in sphere.data.polygons:
    p.use_smooth = True

bpy.ops.mesh.primitive_monkey_add(size=1.6, location=(0.0, -0.3, 0.8))
monkey = bpy.context.active_object
monkey.rotation_euler = (0.0, 0.0, math.radians(35))

bpy.ops.mesh.primitive_plane_add(size=20.0, location=(0.0, 0.0, 0.0))
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

# ---- lights: a real area light + a lit world (ambient) ----------------------
bpy.ops.object.light_add(type='AREA', location=(2.5, -2.5, 5.0))
area = bpy.context.active_object
area.data.energy = 1200.0
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
bpy.ops.object.camera_add(location=(0.0, -6.5, 3.4))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(63), 0.0, 0.0)
scene.camera = cam

# ---- Cycles engine + deterministic FIXED sampling ---------------------------
# IMPORTANT: adaptive sampling stays OFF. We want a clean, fixed-spp cost so our
# OWN tile router is the only thing steering where samples go — not Cycles'
# internal per-pixel heuristic. This is what keeps the strategy provably ours.
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False
cyc.use_denoising = False   # no denoiser: honest spp-vs-spp comparison

# ---- device selection: try GPU (OPTIX -> CUDA -> HIP -> ONEAPI), else CPU ----
# REUSED VERBATIM from exp_cycles_render.py.
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

# ---- OPTIONAL render border: re-render ONLY one tile's sub-rectangle --------
# This is the Cycles "pluggable pipe" our router drives. When CX_BORDER is set
# we enable the border and crop the output to exactly that window, so the saved
# PNG is JUST the tile (WxH of the tile) — cheap to composite back in numpy.
if BORDER:
    try:
        mnx, mxx, mny, mxy = (float(v) for v in BORDER.split(","))
        scene.render.use_border = True
        scene.render.use_crop_to_border = True   # output PNG == the border only
        scene.render.border_min_x = mnx
        scene.render.border_max_x = mxx
        scene.render.border_min_y = mny
        scene.render.border_max_y = mxy
        _log(f"render border ON: x[{mnx:.4f},{mxx:.4f}] y[{mny:.4f},{mxy:.4f}]")
    except Exception as e:
        _log("failed to parse/set CX_BORDER, rendering full frame:", e)
else:
    scene.render.use_border = False
    scene.render.use_crop_to_border = False

_log(f"rendering spp={SPP} res={RES} device={chosen_device} border={bool(BORDER)} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, out_png,
                       seed, device_pref, timeout_s, border=None):
    """Invoke Blender headless to render ONE frame (or ONE tile if `border` set).

    Returns (wall_seconds, chosen_device). Wall-time is measured around the
    subprocess — the honest, end-to-end cost of producing this image at these
    params. `border` is a normalized (min_x, max_x, min_y, max_y) tuple in
    Blender's bottom-left origin, or None for a full frame.
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_png
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    if border is not None:
        env["CX_BORDER"] = ",".join(f"{v:.6f}" for v in border)
    else:
        env.pop("CX_BORDER", None)

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1200:]
    err_tail = (proc.stderr or "")[-800:]
    log(f"  render rc={proc.returncode} spp={spp} border={border is not None} "
        f"wall={wall_s:.3f}s")
    if proc.returncode != 0 and err_tail.strip():
        log("  blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed (rc={proc.returncode}, "
            f"out_exists={os.path.isfile(out_png)}); stdout tail: {tail[-500:]}"
        )
    return wall_s, chosen_device


# =========================================================================== #
#  OUR IP #1 — THE PER-TILE HARDNESS CLASSIFIER                                #
#  Pure numpy on the DRAFT image. No Blender knowledge; no framework built-in. #
# =========================================================================== #
def _luma(img):
    """Rec.709 luminance of an (H,W,3) float image -> (H,W) float."""
    import numpy as np
    return (0.2126 * img[..., 0] +
            0.7152 * img[..., 1] +
            0.0722 * img[..., 2]).astype(np.float64)


def _sobel_edge_density(gray):
    """Edge-magnitude map via a hand-rolled Sobel gradient (no scipy dependency).

    Path tracers at low spp leave their worst error exactly on EDGES — object
    silhouettes, contact shadows, specular boundaries — where a handful of samples
    can't resolve the sharp transition. So edge density is a strong 'this tile is
    hard, re-render it' signal. We compute |grad| with the classic 3x3 Sobel
    kernels using numpy slicing (reflect-padded so it's the full image size).
    """
    import numpy as np
    g = np.pad(gray, 1, mode="reflect")
    # Sobel X / Y via shifted-slice arithmetic on the padded array.
    gx = (
        (g[:-2, 2:] + 2.0 * g[1:-1, 2:] + g[2:, 2:]) -
        (g[:-2, :-2] + 2.0 * g[1:-1, :-2] + g[2:, :-2])
    )
    gy = (
        (g[2:, :-2] + 2.0 * g[2:, 1:-1] + g[2:, 2:]) -
        (g[:-2, :-2] + 2.0 * g[:-2, 1:-1] + g[:-2, 2:])
    )
    return np.sqrt(gx * gx + gy * gy)


def _tile_bounds_px(res, grid):
    """Yield (row, col, y0, y1, x0, x1) integer pixel bounds for every tile.

    Rows/cols use even splits with the remainder folded into the last tile so
    every pixel belongs to exactly one tile even when res % grid != 0.
    """
    step = res // grid
    for row in range(grid):
        y0 = row * step
        y1 = res if row == grid - 1 else (row + 1) * step
        for col in range(grid):
            x0 = col * step
            x1 = res if col == grid - 1 else (col + 1) * step
            yield row, col, y0, y1, x0, x1


def classify_tiles(draft_png, res, grid, classifier):
    """OUR hardness classifier. Returns a list of per-tile dicts:
        {row,col,y0,y1,x0,x1, hardness (0..1)}

    hardness blends two signals WE chose, each min-max normalized across tiles
    so hard_frac is a meaningful percentile cut:
       * var_score  = mean local luminance variance in the tile
                      (Monte-Carlo noise + busy texture -> the draft is unreliable)
       * edge_score = mean Sobel edge magnitude in the tile
                      (silhouettes / shadow & specular boundaries -> low-spp aliasing)

    classifier:
       "variance" -> hardness = var_score
       "edge"     -> hardness = edge_score
       "blend"    -> hardness = 0.5*var_score + 0.5*edge_score   (our default)
    """
    import numpy as np
    img = load_png_float(draft_png)
    # Guard: a cropped/odd draft — resize-agnostic, just use its actual shape.
    if img.shape[0] != res or img.shape[1] != res:
        res = min(img.shape[0], img.shape[1])
    gray = _luma(img)
    edge = _sobel_edge_density(gray)

    raw = []
    for row, col, y0, y1, x0, x1 in _tile_bounds_px(res, grid):
        tile_gray = gray[y0:y1, x0:x1]
        tile_edge = edge[y0:y1, x0:x1]
        var_score = float(tile_gray.var())
        edge_score = float(tile_edge.mean())
        raw.append({
            "row": row, "col": col,
            "y0": y0, "y1": y1, "x0": x0, "x1": x1,
            "var_score": var_score, "edge_score": edge_score,
        })

    # min-max normalize each signal across tiles into [0,1] so the two signals
    # are commensurate before we blend them.
    def _norm(vals):
        vals = np.asarray(vals, dtype=np.float64)
        lo, hi = float(vals.min()), float(vals.max())
        if hi - lo < 1e-12:
            return np.zeros_like(vals)  # degenerate flat frame -> all equally easy
        return (vals - lo) / (hi - lo)

    var_n = _norm([t["var_score"] for t in raw])
    edge_n = _norm([t["edge_score"] for t in raw])

    for i, t in enumerate(raw):
        vn, en = float(var_n[i]), float(edge_n[i])
        if classifier == "variance":
            h = vn
        elif classifier == "edge":
            h = en
        else:  # "blend" (default) — our combined signal
            h = 0.5 * vn + 0.5 * en
        t["var_norm"] = round(vn, 4)
        t["edge_norm"] = round(en, 4)
        t["hardness"] = round(float(h), 4)
    return raw


# =========================================================================== #
#  OUR IP #2 — NON-UNIFORM SAMPLE-BUDGET ROUTING                               #
#  Given hardness scores + a budget fraction, choose WHICH tiles get the       #
#  expensive high-spp re-render. Everything else keeps the free draft pixels.  #
# =========================================================================== #
def route_budget(tiles, hard_frac):
    """Mark the top `hard_frac` fraction of tiles (by hardness) as 'hard'.

    Returns (hard_tiles, total_tiles). At least one tile is always re-rendered
    when hard_frac>0 so the composite is never a pure draft (unless hard_frac==0,
    a valid 'draft-only' ablation). We use a strict percentile cut on hardness:
    the fleet spends real samples only on this minority of the frame.
    """
    total = len(tiles)
    if total == 0:
        return [], 0
    n_hard = int(round(hard_frac * total))
    if hard_frac > 0.0:
        n_hard = max(1, n_hard)
    n_hard = min(n_hard, total)
    ranked = sorted(tiles, key=lambda t: t["hardness"], reverse=True)
    for i, t in enumerate(ranked):
        t["is_hard"] = i < n_hard
    hard = [t for t in ranked if t["is_hard"]]
    return hard, total


def tile_border_norm(t, res):
    """Convert a tile's INTEGER pixel bounds (top-left origin, y downward) into a
    Blender render border (NORMALIZED, bottom-left origin, y upward).

    numpy/PNG rows count from the TOP; Blender's border y counts from the BOTTOM.
    So we flip: blender_min_y = 1 - (y1/res),  blender_max_y = 1 - (y0/res).
    """
    min_x = t["x0"] / res
    max_x = t["x1"] / res
    min_y = 1.0 - (t["y1"] / res)
    max_y = 1.0 - (t["y0"] / res)
    # clamp to [0,1] against float dust
    clamp = lambda v: max(0.0, min(1.0, v))
    return (clamp(min_x), clamp(max_x), clamp(min_y), clamp(max_y))


# --------------------------------------------------------------------------- #
#  Image IO + SSIM (REAL, via pillow + scikit-image).                          #
# --------------------------------------------------------------------------- #
def load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def save_png_float(arr, path):
    """Save an (H,W,3) float [0,1] array as an 8-bit PNG via pillow."""
    from PIL import Image
    import numpy as np
    a = np.clip(arr, 0.0, 1.0)
    Image.fromarray((a * 255.0 + 0.5).astype(np.uint8), "RGB").save(path)


def compute_ssim(a_png, b_png):
    """Color-aware SSIM between two real render PNGs (data_range=1.0)."""
    from skimage.metrics import structural_similarity as ssim
    a = load_png_float(a_png)
    b = load_png_float(b_png)
    return float(ssim(b, a, channel_axis=-1, data_range=1.0))


# --------------------------------------------------------------------------- #
#  COMPOSITE — stitch draft (flat tiles) + high-spp tile renders (hard tiles). #
# --------------------------------------------------------------------------- #
def build_composite(draft_png, hard_renders, res, out_png):
    """Start from the full draft image; paste each hard tile's high-spp render
    into its pixel window. hard_renders is a list of (tile_dict, tile_png_path).

    Returns the composite as a float array (also written to out_png). Each hard
    tile PNG was rendered with use_crop_to_border, so its pixels are EXACTLY the
    tile's window (x1-x0 by y1-y0) and drop straight in — no resampling.
    """
    import numpy as np
    comp = load_png_float(draft_png).copy()
    # The draft may be RESxRES; ensure our canvas matches res for clean indexing.
    H, W = comp.shape[:2]
    for t, tile_png in hard_renders:
        tile = load_png_float(tile_png)
        y0, y1, x0, x1 = t["y0"], t["y1"], t["x0"], t["x1"]
        th, tw = (y1 - y0), (x1 - x0)
        # Defensive: if the border render came back a pixel off (rounding), clip
        # to the smaller overlapping region rather than crashing.
        use_h = min(th, tile.shape[0], H - y0)
        use_w = min(tw, tile.shape[1], W - x0)
        if use_h <= 0 or use_w <= 0:
            log(f"  WARN tile r{t['row']}c{t['col']} empty overlap; skipped")
            continue
        comp[y0:y0 + use_h, x0:x0 + use_w, :] = tile[:use_h, :use_w, :]
    save_png_float(comp, out_png)
    return comp


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    grid = int(params.get("grid", 4))
    draft_spp = int(params.get("draft_spp", 32))
    hard_spp = int(params.get("hard_spp", 512))
    ref_spp = int(params.get("ref_spp", 512))
    hard_frac = float(params.get("hard_frac", 0.3))
    res = int(params.get("resolution", params.get("res", 384)))
    classifier = str(params.get("classifier", "blend")).lower()
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # sanity clamps
    grid = max(1, min(grid, 16))
    draft_spp = max(1, draft_spp)
    hard_spp = max(draft_spp, hard_spp)
    ref_spp = max(draft_spp, ref_spp)
    hard_frac = max(0.0, min(1.0, hard_frac))
    res = max(64, res)
    # Ensure res is a clean multiple of grid so tile borders land on pixel edges.
    if res % grid != 0:
        res = (res // grid) * grid
        log(f"adjusted resolution to {res} (nearest multiple of grid={grid})")
    if classifier not in ("variance", "edge", "blend"):
        classifier = "blend"

    log(f"params: grid={grid} draft_spp={draft_spp} hard_spp={hard_spp} "
        f"ref_spp={ref_spp} hard_frac={hard_frac} res={res} "
        f"classifier={classifier} seed={seed} device_pref={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender (idempotent) ------------------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_tile_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    draft_png = os.path.join(WORK_DIR, "draft.png")
    ref_png = os.path.join(WORK_DIR, "reference.png")
    comp_png = os.path.join(WORK_DIR, "composite.png")

    # subprocess timeouts (generous headroom under the ~18-min budget)
    draft_timeout = 300
    ref_timeout = 900
    tile_timeout = 300

    # ================= STEP 1: cheap full-frame DRAFT ====================== #
    log("STEP 1: full-frame DRAFT render (cheap, low spp)")
    real_draft_render_s, dev_draft = run_blender_render(
        blender_bin, script_path, draft_spp, res, draft_png,
        seed=seed, device_pref=device_pref, timeout_s=draft_timeout, border=None,
    )

    # ================= STEP 2: OUR classifier + budget router ============== #
    log("STEP 2: classify tiles (OUR hardness signal) + route sample budget")
    tiles = classify_tiles(draft_png, res, grid, classifier)
    hard_tiles, total_tiles = route_budget(tiles, hard_frac)
    log(f"  {total_tiles} tiles total; {len(hard_tiles)} flagged HARD "
        f"(top {hard_frac:.0%} by {classifier} hardness)")
    for t in hard_tiles:
        log(f"    HARD r{t['row']}c{t['col']} hardness={t['hardness']:.3f} "
            f"(var_n={t['var_norm']:.2f} edge_n={t['edge_norm']:.2f})")

    # ================= STEP 3: re-render ONLY hard tiles at high spp ======= #
    # Each is a real Cycles render_border job. We sum their wall-times to get the
    # single-box composite cost; we also track the SLOWEST tile for the fleet model.
    log(f"STEP 3: re-render {len(hard_tiles)} HARD tiles at hard_spp={hard_spp} "
        "(real render_border jobs)")
    hard_renders = []
    hard_tile_secs = []
    dev_tiles = dev_draft
    for i, t in enumerate(hard_tiles):
        border = tile_border_norm(t, res)
        tile_png = os.path.join(WORK_DIR, f"hard_r{t['row']}c{t['col']}.png")
        secs, dev_tiles = run_blender_render(
            blender_bin, script_path, hard_spp, res, tile_png,
            seed=seed, device_pref=device_pref, timeout_s=tile_timeout,
            border=border,
        )
        hard_renders.append((t, tile_png))
        hard_tile_secs.append(secs)
        log(f"    tile {i+1}/{len(hard_tiles)} r{t['row']}c{t['col']} "
            f"rendered in {secs:.2f}s")

    # ================= STEP 4: composite draft + hard tiles ================ #
    log("STEP 4: composite (draft flat tiles + high-spp hard tiles)")
    build_composite(draft_png, hard_renders, res, comp_png)

    # ================= STEP 5: uniform REFERENCE (ground truth) ============ #
    log(f"STEP 5: uniform REFERENCE render at ref_spp={ref_spp} (ground truth)")
    real_render_s_ref, dev_ref = run_blender_render(
        blender_bin, script_path, ref_spp, res, ref_png,
        seed=seed, device_pref=device_pref, timeout_s=ref_timeout, border=None,
    )

    # ================= STEP 6: HONEST scoring ============================== #
    # composite single-box cost = draft + sum of hard-tile renders (real waves).
    real_composite_render_s = real_draft_render_s + float(sum(hard_tile_secs))
    quality = compute_ssim(comp_png, ref_png)

    # Also SSIM the raw draft vs reference so we can PROVE our routing added value
    # (composite SSIM should beat draft SSIM by re-rendering the hard tiles).
    draft_quality = compute_ssim(draft_png, ref_png)

    net_speedup = real_render_s_ref / max(real_composite_render_s, 1e-9)

    # --- distributed (fleet) projection — the ONLY modeled number --------- #
    # If every hard tile renders on its OWN node in parallel, the fleet wall-clock
    # is: draft (one small job) + the SLOWEST single hard tile (they overlap).
    # This is a projection, clearly labelled modeled in the note; net_speedup above
    # remains the honest single-box measurement and is what we report as truth.
    slowest_hard = max(hard_tile_secs) if hard_tile_secs else 0.0
    distributed_composite_s = real_draft_render_s + slowest_hard
    distributed_speedup = real_render_s_ref / max(distributed_composite_s, 1e-9)

    # effective sample budget spent vs a uniform ref (informational, honest count)
    hard_px_frac = (
        sum((t["y1"] - t["y0"]) * (t["x1"] - t["x0"]) for t in hard_tiles)
        / float(res * res)
    ) if hard_tiles else 0.0
    # samples touched ~ draft everywhere + (hard_spp-draft_spp) on hard pixels
    effective_spp = draft_spp + (hard_spp - draft_spp) * hard_px_frac
    sample_budget_frac = effective_spp / float(ref_spp)

    device = dev_ref
    parts = {dev_draft, dev_tiles, dev_ref}
    if len(parts) > 1:
        device = "|".join(sorted(parts))
    fell_to_cpu = "CPU" in device

    log(f"RESULTS: net_speedup={net_speedup:.3f}  quality(SSIM)={quality:.4f}  "
        f"draft_quality={draft_quality:.4f}  hard_tiles={len(hard_tiles)}/{total_tiles}")
    log(f"  real_render_s_ref={real_render_s_ref:.3f}s  "
        f"real_composite_render_s={real_composite_render_s:.3f}s "
        f"(draft {real_draft_render_s:.2f}s + hard {sum(hard_tile_secs):.2f}s)")
    log(f"  distributed_speedup(modeled)={distributed_speedup:.3f} "
        f"(draft {real_draft_render_s:.2f}s + slowest tile {slowest_hard:.2f}s)")
    log(f"  effective_spp~{effective_spp:.1f} -> sample_budget_frac={sample_budget_frac:.3f} "
        f"(hard pixels cover {hard_px_frac:.1%} of frame)")

    note = (
        "CUSTOM per-tile adaptive sample routing; our variance/edge hardness "
        "classifier; distributable by tile. Draft=full-frame low-spp path trace; "
        "hard tiles re-rendered at high spp via real Cycles render_border; composite "
        "= draft(flat)+highspp(hard); quality=REAL SSIM vs uniform high-spp reference. "
        "net_speedup + all render times are REAL wall-clock (modeled:false). "
        "distributed_speedup is a fleet-parallel projection (modeled), not the headline."
    )
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "draft_quality": round(float(draft_quality), 4),
        "hard_tiles": int(len(hard_tiles)),
        "total_tiles": int(total_tiles),
        "classifier": classifier,
        "grid": grid,
        "hard_frac": round(float(hard_frac), 4),
        "hard_pixel_frac": round(float(hard_px_frac), 4),
        "effective_spp": round(float(effective_spp), 2),
        "sample_budget_frac": round(float(sample_budget_frac), 4),
        "real_render_s_ref": round(float(real_render_s_ref), 4),
        "real_composite_render_s": round(float(real_composite_render_s), 4),
        "real_draft_render_s": round(float(real_draft_render_s), 4),
        "hard_tile_secs": [round(float(s), 4) for s in hard_tile_secs],
        "distributed_speedup": round(float(distributed_speedup), 4),
        "resolution": f"{res}x{res}",
        "draft_spp": draft_spp,
        "hard_spp": hard_spp,
        "ref_spp": ref_spp,
        "device": device,
        "modeled": False,
        "note": note,
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
