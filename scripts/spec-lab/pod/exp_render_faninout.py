#!/usr/bin/env python3
"""
exp_render_faninout.py — the DISTRIBUTION / WALL-CLOCK-ENVELOPE lever.

================================================================================
WHAT THIS TESTS (the thesis under the microscope)
================================================================================
A single production frame is embarrassingly parallel in SPACE: split it into N
disjoint rectangular tiles, hand each tile to a DIFFERENT GPU, and the frame's
wall-clock collapses from "sum of all tile times" toward "the SLOWEST single
tile time". That is the fan-out ceiling — the best a perfect N-way scatter/gather
could ever do. This runner MEASURES it honestly on ONE box:

  * We render EACH tile as its OWN real Blender/Cycles subprocess (a real
    render_border/region render at the SAME full-production quality — this is a
    LOSSLESS spatial split, NOT the adaptive sample-routing trick of
    exp_render_tiles.py). We time every tile's whole subprocess with
    time.perf_counter().
  * T_serial          = SUM of the N tile times = what ONE GPU pays to render the
                        whole frame tile-by-tile (a real measured number).
  * T_parallel_ideal  = MAX of the N tile times = the wall-clock envelope of a
                        PERFECT N-GPU fan-out where all tiles overlap (a real
                        measured number — the slowest tile is genuinely rendered).
  * load_balance_eff  = mean(tile) / max(tile) = how evenly the frame's cost
                        spreads across the N workers (1.0 = perfectly balanced;
                        low = one hot tile bottlenecks the whole fan-out).
  * net_speedup       = T_serial / T_parallel_ideal  — the IDEAL fan-out ceiling,
                        clearly labelled IDEAL. See HONESTY below for why this is
                        an UPPER bound, not an achievable-in-production number.

Then we REASSEMBLE the N tiles into one composite frame and SSIM it against a
SINGLE full-frame render of the same scene at the same spp. Because the tiling
is lossless, that SSIM MUST be ~1.0. If it is NOT, that is a real bug in the
split/reassemble (a border rounding error, a seam) — we REPORT it, we do not hide
it. We report GLOBAL SSIM plus per-8x8-tile worst + 5th-percentile tile SSIM so a
subtle seam in a small region can't hide behind a high global average.

================================================================================
HONESTY (contract — we just fixed 4 speedup-inflation bugs; this adds NONE)
================================================================================
  * modeled:false. EVERY time is a real time.perf_counter() wall-clock around the
    WHOLE Blender subprocess (process launch + .blend load + BVH build + border
    path trace + PNG encode — NOTHING excluded). T_serial, T_parallel_ideal, and
    every per-tile time are real measurements on the SAME box.
  * quality is REAL skimage SSIM on real rendered/decoded pixels: the reassembled
    composite vs a true single full-frame render at the SAME spp. GLOBAL + per-tile
    (8x8) worst + 5th-percentile.
  * net_speedup = T_serial / T_parallel_ideal — the IDEAL fan-out ceiling. This is
    an UPPER BOUND on the real distribution speedup, and we say so explicitly:
    real cross-pod fan-out ALSO pays network scatter (ship the .blend / scene to N
    pods), network gather (pull N tile PNGs back), and scheduler/queue overhead —
    NONE of which is measured here (this is one box). T_parallel_ideal assumes
    every tile starts at t=0 on its own idle GPU with zero comms. Production will
    be SLOWER than this ceiling. The honest single-box cost of the whole split is
    ALSO reported (T_serial) — that is what ONE GPU actually pays.
  * The reference (single full-frame render) is a real render on the SAME box, at
    the SAME spp/bounces as the tiles, CACHED by (scene,res,spp,bounces,seed) so a
    repeat sweep reuses it (its time is a real measured time, just not re-paid).
  * On ANY failure (download, unzip, Blender non-zero exit, SSIM shape mismatch,
    missing dep) we emit {"error":"<type>: <msg>"} and exit 0 — we NEVER fabricate
    a number and NEVER silently substitute a fallback that mislabels success.

================================================================================
CONFIG (argv[1] JSON, all optional; defaults in parens)
================================================================================
  tiles       : 8 (default)              number of tiles to fan out (e.g. 4/8/16/32).
                                          Factored into a rows x cols grid as square
                                          as possible; the ACTUAL count is reported.
  scene       : "classroom" (default) | "bmw27" | <direct .blend/.zip URL>
  resolution  : "1920x1080" (default)    parsed WxH
  spp         : 4096 (default)           samples/pixel for BOTH tiles and the
                                          full-frame reference (lossless split ->
                                          same quality; NOT a sample-savings lever)
  bounces     : 12 (default)             total light bounces (same for tiles + ref)
  seed        : 0 (default)              Cycles seed (same for tiles + ref so the
                                          composite matches the full frame pixel-wise)
  device      : "AUTO" (default) | "GPU" | "CPU"   OPTIX->CUDA->HIP->ONEAPI->CPU
  blender_url : override the 4.2 LTS tarball URL

Emits ONE json line on stdout via emit() (last line):
  {"net_speedup","quality","worst_tile_ssim","p5_tile_ssim","tiles","grid_rows",
   "grid_cols","T_serial","T_parallel_ideal","load_balance_efficiency",
   "tile_secs","ref_render_s","full_frame_render_s","resolution","spp","bounces",
   "scene","device","modeled":false,"note":...}

Contract: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object;
any failure emits {"error":...} as the last stdout line and exits 0 (never hangs).
"""

import glob
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
import zipfile

# --------------------------------------------------------------------------- #
# Bootstrap constants — IDENTICAL Blender location/URL to the sibling render    #
# runners so a pod that already downloaded Blender for a prior rung reuses it.  #
# (Forked verbatim from exp_cycles_render.py / exp_cycles_render_prod.py.)      #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/render_faninout"
# Persistent scene + reference cache. Prefer the big /models volume (survives
# between runners in one pod session); fall back to /root if /models is absent.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")
REF_CACHE_DIR = os.path.join(_CACHE_ROOT, "faninout_ref_cache")

# Known production scenes on Blender's public demo server (VERIFIED reachable).
# (Forked from exp_cycles_render_prod.py.) The DEFAULT is 'classroom'.
SCENE_SOURCES = {
    "classroom": {
        "url": "https://download.blender.org/demo/test/classroom.zip",
        "is_zip": True,
        "blend_relpaths": ["classroom/classroom.blend"],
    },
    "bmw27": {
        "url": "https://download.blender.org/demo/test/BMW27_2.blend.zip",
        "is_zip": True,
        "blend_relpaths": ["bmw27/bmw27_gpu.blend", "bmw27/bmw27_cpu.blend"],
    },
}


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_faninout]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs + imaging deps (idempotent, never fatal).         #
#    Same list as the sibling render runners; Blender needs a few X/GL libs.    #
# --------------------------------------------------------------------------- #
def ensure_system_libs():
    pkgs = ["libxi6", "libxxf86vm1", "libxfixes3", "libxrender1", "libgl1", "unzip"]
    try:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", *pkgs],
            check=False, capture_output=True, timeout=300, env=env,
        )
        log(f"apt-get best-effort installed: {' '.join(pkgs)} (failures ignored)")
    except Exception as e:  # noqa: BLE001 — best-effort, must never abort
        log(f"apt-get for X/GL libs failed (non-fatal): {e}")


def ensure_pydeps():
    """Best-effort ensure numpy/pillow/scikit-image exist. setup_base.sh installs
    them; this is belt-and-suspenders for a pod that skipped it. Never fatal here —
    the real check is the import at SSIM time (which errors CLEANLY, not silently)."""
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
        import skimage  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages",
             "--no-cache-dir", "numpy", "pillow", "scikit-image"],
            check=False, capture_output=True, timeout=600,
        )
        log("best-effort pip-installed numpy/pillow/scikit-image")
    except Exception as e:  # noqa: BLE001
        log(f"pip ensure of imaging deps failed (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# 2. Self-bootstrap Blender: download + extract if absent. Idempotent — reuses  #
#    /root/blender/blender from a prior rung. (Verbatim honest pattern.)        #
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
# 3. Fetch + cache the production scene (forked from exp_cycles_render_prod.py). #
#    Idempotent: a scene already downloaded + extracted is reused.               #
# --------------------------------------------------------------------------- #
def _download(url, dest):
    """Stream a URL to dest with a real UA. Raises RuntimeError on failure."""
    log(f"downloading scene: {url}")
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0 spec-lab"})
        with urllib.request.urlopen(req, timeout=1200) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"scene download failed: {type(e).__name__}: {e}")
    dl_s = time.perf_counter() - t0
    size_mb = os.path.getsize(dest) / 1e6
    log(f"downloaded {size_mb:.1f} MB in {dl_s:.1f}s")


def _find_blend(extract_dir, preferred_relpaths):
    """Locate the main .blend in an extracted scene tree.

    First try the KNOWN top-level main-file relative paths (so we don't accidentally
    open an asset sub-blend). If none match (unknown direct-URL zip), fall back to the
    largest .blend in the tree — the main scene file is essentially always the biggest.
    """
    for rp in preferred_relpaths:
        cand = os.path.join(extract_dir, rp)
        if os.path.isfile(cand):
            return cand
    blends = glob.glob(os.path.join(extract_dir, "**", "*.blend"), recursive=True)
    if not blends:
        return None
    blends.sort(key=lambda p: os.path.getsize(p), reverse=True)
    log(f"no known main .blend matched; picking largest: {blends[0]}")
    return blends[0]


def resolve_scene(scene_arg):
    """Fetch/cache the scene and return (blend_path, scene_key, fallback_note).

    scene_arg is one of the named scenes, or a direct .blend/.zip URL. Everything is
    cached under SCENES_DIR and reused on repeat invocations (idempotent). The download
    is CACHED on the pod and reused — never re-downloaded on a cache hit.
    """
    os.makedirs(SCENES_DIR, exist_ok=True)
    fallback_note = ""

    key = scene_arg.strip()
    is_url = key.startswith("http://") or key.startswith("https://")

    # junkshop has no verified stable CC0 direct URL on the demo server -> fall back.
    if key == "junkshop":
        fallback_note = (
            "requested scene 'junkshop' has no verified stable CC0 direct URL on "
            "download.blender.org/demo/test/; FELL BACK to 'classroom'"
        )
        log("WARN: " + fallback_note)
        key = "classroom"

    if not is_url and key not in SCENE_SOURCES:
        raise RuntimeError(
            f"unknown scene {scene_arg!r}; expected one of "
            f"{list(SCENE_SOURCES) + ['<direct .blend/.zip URL>']}"
        )

    if is_url:
        url = key
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        scene_key = f"url-{h}"
        is_zip = url.lower().split("?")[0].endswith(".zip")
        preferred = []  # unknown layout -> _find_blend uses the largest-.blend fallback
    else:
        src = SCENE_SOURCES[key]
        url = src["url"]
        scene_key = key
        is_zip = src["is_zip"]
        preferred = src["blend_relpaths"]

    scene_root = os.path.join(SCENES_DIR, scene_key)
    ready_marker = os.path.join(scene_root, ".ready")
    blend_ptr = os.path.join(scene_root, ".blendpath")

    # ---- CACHE HIT: already extracted + we recorded the main .blend path -------
    if os.path.isfile(ready_marker) and os.path.isfile(blend_ptr):
        with open(blend_ptr) as f:
            cached_blend = f.read().strip()
        if cached_blend and os.path.isfile(cached_blend):
            log(f"scene '{scene_key}' already cached -> {cached_blend}")
            return cached_blend, scene_key, fallback_note
        log(f"scene '{scene_key}' cache marker present but .blend missing; refetching")

    os.makedirs(scene_root, exist_ok=True)

    if is_zip:
        zpath = os.path.join(scene_root, "download.zip")
        if not os.path.isfile(zpath):
            _download(url, zpath)
        extract_dir = os.path.join(scene_root, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        log(f"extracting {zpath} -> {extract_dir}")
        try:
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(extract_dir)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"scene unzip failed: {type(e).__name__}: {e}")
        blend = _find_blend(extract_dir, preferred)
        if blend is None:
            raise RuntimeError(f"no .blend found in extracted scene {scene_key}")
    else:
        blend = os.path.join(scene_root, "scene.blend")
        if not os.path.isfile(blend):
            _download(url, blend)

    with open(blend_ptr, "w") as f:
        f.write(blend)
    with open(ready_marker, "w") as f:
        f.write("ok\n")
    log(f"scene '{scene_key}' ready -> {blend}")
    return blend, scene_key, fallback_note


# --------------------------------------------------------------------------- #
# 4. The Blender render script (run inside Blender's own python via -P). It      #
#    OPENS the production .blend and OVERRIDES only the render controls; it      #
#    never touches geometry/lights/camera. The ONE lever it exposes is an        #
#    OPTIONAL render border (Cycles border/region render): when CX_BORDER is set  #
#    we render ONLY that normalized sub-rectangle at the SAME spp/bounces as the  #
#    full frame — a LOSSLESS spatial split. Adaptive sampling + denoise stay OFF  #
#    so the composited tiles equal the full frame pixel-for-pixel (SSIM ~= 1.0).  #
#    Prints CX_CHOSEN_DEVICE + CX_RENDER_DONE sentinels like the siblings.        #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
BLEND      = os.environ["CX_BLEND"]                 # production .blend to open
OUT        = os.environ["CX_OUT"]                   # output PNG path
RES_X      = int(os.environ["CX_RES_X"])
RES_Y      = int(os.environ["CX_RES_Y"])
SPP        = int(os.environ["CX_SPP"])              # fixed samples/pixel
SEED       = int(os.environ["CX_SEED"])
BOUNCES    = int(os.environ["CX_BOUNCES"])          # total light bounces
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")    # AUTO | GPU | CPU
# Optional render border (normalized 0..1, origin BOTTOM-LEFT in Blender):
#   CX_BORDER = "min_x,max_x,min_y,max_y". Absent -> full frame.
BORDER     = os.environ.get("CX_BORDER", "").strip()

# ---- open the REAL production scene -----------------------------------------
# open_mainfile loads the .blend's own geometry, materials, lights, camera. We
# only override render controls below; we do NOT rebuild the scene.
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

# Force the Cycles engine (the demo scenes ship as Cycles, but be explicit).
scene.render.engine = 'CYCLES'
cyc = scene.cycles

# ===== SAMPLING =============================================================
# FIXED spp, adaptive OFF, denoise OFF for BOTH the tiles and the full-frame
# reference. This is a LOSSLESS spatial split — a tile rendered at spp S is
# pixel-for-pixel identical to that region of the full frame rendered at spp S
# with the same seed. Adaptive/denoise would break that equivalence, so they
# are off: the ONLY thing under test here is WHERE the pixels are computed, not
# HOW MANY samples they get. (Contrast exp_render_tiles.py, which deliberately
# routes DIFFERENT sample budgets per tile — a different, lossy experiment.)
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False
cyc.use_denoising = False

# ===== BOUNCES (production light depth) =====================================
# Same bounces for tiles AND reference so bounce depth is NOT a hidden lever.
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ===== DEVICE LADDER: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU ===============
# (Same honest pattern as the sibling render runners.) OPTIX first per the
# task's AUTO preference; enabling only GPU devices avoids a slow CPU+GPU hybrid.
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

# ===== OUTPUT: full resolution, 8-bit RGB PNG, single frame =================
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT

# ===== OPTIONAL render border: render ONLY one tile's sub-rectangle =========
# use_crop_to_border makes the saved PNG EXACTLY the tile window (its own WxH),
# so reassembly is a straight numpy paste with no resampling. Absent -> full frame.
if BORDER:
    try:
        mnx, mxx, mny, mxy = (float(v) for v in BORDER.split(","))
        scene.render.use_border = True
        scene.render.use_crop_to_border = True   # output PNG == the border only
        scene.render.border_min_x = mnx
        scene.render.border_max_x = mxx
        scene.render.border_min_y = mny
        scene.render.border_max_y = mxy
        _log(f"render border ON: x[{mnx:.5f},{mxx:.5f}] y[{mny:.5f},{mxy:.5f}]")
    except Exception as e:
        # A malformed border must NOT silently become a full-frame render that we
        # then paste as if it were a tile — surface it on a parseable line so the
        # caller can error cleanly instead of mislabeling.
        print(f"CX_BORDER_BAD={type(e).__name__}: {e}", flush=True)
        scene.render.use_border = False
        scene.render.use_crop_to_border = False
else:
    scene.render.use_border = False
    scene.render.use_crop_to_border = False

_log(f"rendering spp={SPP} res={RES_X}x{RES_Y} seed={SEED} bounces={BOUNCES} "
     f"device={chosen_device} border={bool(BORDER)} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, *, blend, out_png, res_x, res_y,
                       spp, seed, bounces, device_pref, timeout_s, border=None):
    """Invoke Blender headless to render ONE frame (or ONE tile if `border` set).

    Returns (wall_seconds, chosen_device). wall_seconds is time.perf_counter()
    around the WHOLE subprocess — launch + .blend load + BVH + border path trace +
    PNG encode. NOTHING excluded. `border` is a normalized (min_x, max_x, min_y,
    max_y) tuple in Blender's BOTTOM-LEFT origin, or None for a full frame.

    Raises RuntimeError on non-zero exit, missing PNG, or a malformed border (we
    refuse to silently render a full frame and paste it as if it were a tile).
    """
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_png
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
    env["CX_SEED"] = str(seed)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_DEVICE"] = device_pref
    if border is not None:
        env["CX_BORDER"] = ",".join(f"{v:.6f}" for v in border)
    else:
        env.pop("CX_BORDER", None)

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    kind = "TILE" if border is not None else "FULL"
    log(f"render start [{kind}]: spp={spp} res={res_x}x{res_y} "
        f"border={border} -> {out_png}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1800:]
    err_tail = (proc.stderr or "")[-1500:]
    log(f"render [{kind}] rc={proc.returncode} wall={wall_s:.3f}s")
    if proc.returncode != 0 and err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    border_bad = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()
        elif line.startswith("CX_BORDER_BAD="):
            border_bad = line.split("=", 1)[1].strip()

    # HONESTY: a border we asked for but couldn't set means this "tile" would be a
    # full-frame render mislabeled as a tile — FAIL instead of pasting it.
    if border is not None and border_bad is not None:
        raise RuntimeError(
            f"tile border {border} was malformed inside Blender ({border_bad}); "
            f"refusing to paste a full-frame render as a tile"
        )

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [{kind}] (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_png)}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device


# =========================================================================== #
#  TILE GEOMETRY — factor N tiles into a rows x cols grid and give each tile    #
#  its integer pixel bounds AND its normalized Blender render border.           #
# =========================================================================== #
def factor_tiles(n_tiles, res_x, res_y):
    """Factor n_tiles into (rows, cols) as square-in-ASPECT as possible.

    We want tile aspect ~1 so a hot region doesn't dump all its cost into one long
    thin tile. Among all (rows, cols) with rows*cols == n_tiles we pick the pair
    whose tile aspect (tile_w/tile_h) is closest to 1. If n_tiles is prime and >
    ~a strip, that forces a 1xN or Nx1 strip — that's fine, it's still N real tiles.
    Returns (rows, cols).
    """
    n = max(1, int(n_tiles))
    best = (1, n)
    best_score = None
    for cols in range(1, n + 1):
        if n % cols != 0:
            continue
        rows = n // cols
        tile_w = res_x / cols
        tile_h = res_y / rows
        # aspect distance from 1.0 (log so 2x and 0.5x are penalized equally)
        aspect = tile_w / tile_h if tile_h > 0 else 1e9
        score = abs(math.log(aspect)) if aspect > 0 else 1e9
        if best_score is None or score < best_score:
            best_score = score
            best = (rows, cols)
    return best


def tile_pixel_bounds(rows, cols, res_x, res_y):
    """Yield per-tile integer pixel bounds (top-left origin, y downward):
        (r, c, y0, y1, x0, x1)
    Even splits with the remainder folded into the last row/col so every pixel
    belongs to exactly one tile even when res is not divisible by rows/cols.
    """
    step_y = res_y // rows
    step_x = res_x // cols
    for r in range(rows):
        y0 = r * step_y
        y1 = res_y if r == rows - 1 else (r + 1) * step_y
        for c in range(cols):
            x0 = c * step_x
            x1 = res_x if c == cols - 1 else (c + 1) * step_x
            yield r, c, y0, y1, x0, x1


def tile_border_norm(y0, y1, x0, x1, res_x, res_y):
    """Convert a tile's INTEGER pixel bounds (top-left origin, y downward) into a
    Blender render border (NORMALIZED, BOTTOM-left origin, y upward).

    numpy/PNG rows count from the TOP; Blender's border y counts from the BOTTOM.
    So we flip: blender_min_y = 1 - (y1/res_y), blender_max_y = 1 - (y0/res_y).
    """
    min_x = x0 / res_x
    max_x = x1 / res_x
    min_y = 1.0 - (y1 / res_y)
    max_y = 1.0 - (y0 / res_y)
    clamp = lambda v: max(0.0, min(1.0, v))
    return (clamp(min_x), clamp(max_x), clamp(min_y), clamp(max_y))


# --------------------------------------------------------------------------- #
#  Image IO + SSIM (REAL, via pillow + scikit-image). Forked from the honest    #
#  render runners.                                                              #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def _save_png_float(arr, path):
    """Save an (H,W,3) float [0,1] array as an 8-bit PNG via pillow."""
    from PIL import Image
    import numpy as np
    a = np.clip(arr, 0.0, 1.0)
    Image.fromarray((a * 255.0 + 0.5).astype(np.uint8), "RGB").save(path)


def build_composite(tile_renders, res_x, res_y, out_png):
    """Reassemble the N cropped tile PNGs into one full frame.

    tile_renders is a list of (bounds_dict, tile_png_path). Each tile PNG was
    rendered with use_crop_to_border so its pixels are EXACTLY the tile window and
    drop straight into place — no resampling. Returns the composite float array
    (also written to out_png). We start from zeros and paste every tile; if any
    pixel is left unwritten that is a real coverage bug the SSIM will catch.
    """
    import numpy as np
    comp = np.zeros((res_y, res_x, 3), dtype=np.float32)
    covered = np.zeros((res_y, res_x), dtype=bool)
    for t, tile_png in tile_renders:
        tile = _load_png_float(tile_png)
        y0, y1, x0, x1 = t["y0"], t["y1"], t["x0"], t["x1"]
        th, tw = (y1 - y0), (x1 - x0)
        # Defensive: if the border render came back a pixel off (rounding), clip
        # to the smaller overlapping region rather than crashing.
        use_h = min(th, tile.shape[0], res_y - y0)
        use_w = min(tw, tile.shape[1], res_x - x0)
        if use_h <= 0 or use_w <= 0:
            log(f"  WARN tile r{t['r']}c{t['c']} empty overlap; skipped")
            continue
        comp[y0:y0 + use_h, x0:x0 + use_w, :] = tile[:use_h, :use_w, :]
        covered[y0:y0 + use_h, x0:x0 + use_w] = True
    coverage = float(covered.mean())
    _save_png_float(comp, out_png)
    return comp, coverage


def compute_ssim_global_and_tiles(comp_png, ref_png, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim).

    GLOBAL: skimage SSIM over the whole reassembled composite vs the real full-frame
    reference PNG. PER-TILE: split into grid x grid, SSIM per tile, report the MIN
    tile and the 5th-percentile tile. For a LOSSLESS spatial split this should be
    ~1.0 everywhere; a low worst tile pinpoints a seam / border rounding bug (which
    we REPORT, not hide). The 8x8 grid here is INDEPENDENT of the fan-out tile count
    — it is the quality microscope, not the distribution unit.
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    comp = _load_png_float(comp_png)
    ref = _load_png_float(ref_png)
    if comp.shape != ref.shape:
        raise RuntimeError(
            f"composite/ref shape mismatch {comp.shape} vs {ref.shape}"
        )

    global_ssim = float(ssim(ref, comp, channel_axis=-1, data_range=1.0))

    h, w = ref.shape[:2]
    ty = max(1, h // grid)
    tx = max(1, w // grid)
    tile_scores = []
    for gy in range(grid):
        y0 = gy * ty
        y1 = h if gy == grid - 1 else (gy + 1) * ty
        for gx in range(grid):
            x0 = gx * tx
            x1 = w if gx == grid - 1 else (gx + 1) * tx
            rt = ref[y0:y1, x0:x1]
            ct = comp[y0:y1, x0:x1]
            if min(rt.shape[0], rt.shape[1]) < 7:
                continue  # too small for the default 7x7 SSIM window
            tile_scores.append(
                float(ssim(rt, ct, channel_axis=-1, data_range=1.0))
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
#  Full-frame reference cache: render the single full frame ONCE, keyed by      #
#  (scene, resolution, spp, bounces, seed). A repeat sweep (different tiles=)    #
#  reuses the cached PNG and reads full_frame_render_s from a sidecar json (so   #
#  the ref time is still a real measured time on this same box, just not re-paid).#
# --------------------------------------------------------------------------- #
def _ref_cache_key(scene_key, res_x, res_y, spp, bounces, seed):
    raw = f"{scene_key}|{res_x}x{res_y}|spp{spp}|b{bounces}|s{seed}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return h, raw


def get_or_render_full_frame(blender_bin, script_path, *, blend, scene_key, res_x,
                             res_y, spp, bounces, seed, device_pref, timeout_s):
    """Return (ref_png_path, full_frame_render_s, ref_device, cache_hit_bool).

    On a cache HIT the PNG + sidecar exist and are reused (the time comes from the
    sidecar — a real measured time on this box, not re-paid). On a MISS we render the
    single full frame once, time the whole subprocess, and persist PNG + sidecar.
    """
    os.makedirs(REF_CACHE_DIR, exist_ok=True)
    h, raw = _ref_cache_key(scene_key, res_x, res_y, spp, bounces, seed)
    ref_png = os.path.join(REF_CACHE_DIR, f"full_{h}.png")
    sidecar = os.path.join(REF_CACHE_DIR, f"full_{h}.json")

    if os.path.isfile(ref_png) and os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                meta = json.load(f)
            ref_s = float(meta["full_frame_render_s"])
            ref_dev = str(meta.get("device", "unknown"))
            log(f"FULL-FRAME cache HIT [{raw}] -> {ref_png} "
                f"(full_frame_render_s={ref_s:.3f}s, device={ref_dev}); NOT re-paying it")
            return ref_png, ref_s, ref_dev, True
        except Exception as e:  # noqa: BLE001 — corrupt sidecar -> re-render
            log(f"full-frame sidecar unreadable ({e}); re-rendering full frame")

    log(f"FULL-FRAME cache MISS [{raw}] -> rendering single full frame once")
    ref_s, ref_dev = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=ref_png,
        res_x=res_x, res_y=res_y, spp=spp, seed=seed, bounces=bounces,
        device_pref=device_pref, timeout_s=timeout_s, border=None,
    )
    try:
        with open(sidecar, "w") as f:
            json.dump({
                "full_frame_render_s": ref_s, "device": ref_dev, "key": raw,
                "scene": scene_key, "resolution": f"{res_x}x{res_y}",
                "spp": spp, "bounces": bounces, "seed": seed,
            }, f)
    except Exception as e:  # noqa: BLE001 — non-fatal; ref still usable this run
        log(f"could not write full-frame sidecar (non-fatal): {e}")
    return ref_png, ref_s, ref_dev, False


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    n_tiles = int(params.get("tiles", 8))
    scene_arg = str(params.get("scene", "classroom"))
    resolution = str(params.get("resolution", "1920x1080"))
    spp = int(params.get("spp", 4096))
    bounces = int(params.get("bounces", 12))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- parse + clamp ----------------------------------------------------- #
    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 1920x1080")

    n_tiles = max(1, min(n_tiles, 256))  # sane upper bound so we don't fork 1000 procs
    spp = max(1, spp)
    bounces = max(1, bounces)

    rows, cols = factor_tiles(n_tiles, res_x, res_y)
    actual_tiles = rows * cols

    log(f"params: tiles={n_tiles} -> grid {rows}x{cols} ({actual_tiles} tiles) "
        f"scene={scene_arg} res={res_x}x{res_y} spp={spp} bounces={bounces} "
        f"seed={seed} device={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + imaging deps + bootstrap Blender ----------------- #
    ensure_system_libs()
    ensure_pydeps()
    blender_bin = ensure_blender(blender_url)

    # ---- 1) fetch + cache the production scene ----------------------------- #
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    # write the render script Blender runs for tiles AND the full frame (same build)
    script_path = os.path.join(WORK_DIR, "cx_faninout_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # generous subprocess timeouts: a 1080p @ 4096-spp full frame is heavy on CPU;
    # a single tile is a fraction of that but keep headroom for a 1-tile config.
    full_timeout = 3600
    tile_timeout = 3600

    # ---- 2) FULL-FRAME reference: cached-or-render, real whole-subprocess time
    ref_png, full_frame_render_s, ref_device, ref_cache_hit = get_or_render_full_frame(
        blender_bin, script_path, blend=blend, scene_key=scene_key,
        res_x=res_x, res_y=res_y, spp=spp, bounces=bounces, seed=seed,
        device_pref=device_pref, timeout_s=full_timeout,
    )

    # ---- 3) FAN-OUT: render EVERY tile as its own real Blender subprocess ---
    # Each tile is a real Cycles render_border job at the SAME spp/bounces/seed.
    # We time every tile's whole subprocess. T_serial = sum (one GPU tile-by-tile),
    # T_parallel_ideal = max (perfect N-GPU overlap, the wall-clock envelope).
    log(f"FAN-OUT: rendering {actual_tiles} tiles (grid {rows}x{cols}) at spp={spp}")
    tile_renders = []
    tile_secs = []
    tile_devices = set()
    for r, c, y0, y1, x0, x1 in tile_pixel_bounds(rows, cols, res_x, res_y):
        border = tile_border_norm(y0, y1, x0, x1, res_x, res_y)
        tile_png = os.path.join(WORK_DIR, f"tile_r{r}c{c}.png")
        secs, dev = run_blender_render(
            blender_bin, script_path, blend=blend, out_png=tile_png,
            res_x=res_x, res_y=res_y, spp=spp, seed=seed, bounces=bounces,
            device_pref=device_pref, timeout_s=tile_timeout, border=border,
        )
        bounds = {"r": r, "c": c, "y0": y0, "y1": y1, "x0": x0, "x1": x1}
        tile_renders.append((bounds, tile_png))
        tile_secs.append(secs)
        tile_devices.add(dev)
        log(f"  tile r{r}c{c} [{x0}:{x1},{y0}:{y1}] rendered in {secs:.2f}s")

    if not tile_secs:
        raise RuntimeError("no tiles were rendered (empty grid)")

    # ---- 4) REASSEMBLE the tiles into one composite frame ------------------
    comp_png = os.path.join(WORK_DIR, "composite.png")
    _comp_arr, coverage = build_composite(tile_renders, res_x, res_y, comp_png)
    log(f"REASSEMBLE: composite coverage={coverage:.4%} of the frame")

    # ---- 5) VERIFY: GLOBAL + PER-TILE SSIM (composite vs full-frame ref) ---
    # Lossless split => this MUST be ~1.0. A low worst tile is a seam bug we REPORT.
    quality, worst_tile, p5_tile = compute_ssim_global_and_tiles(
        comp_png, ref_png, grid=8
    )
    log(f"SSIM(composite, full-frame) global={quality:.4f} "
        f"worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f}")

    # ---- 6) DISTRIBUTION ENVELOPE (all real measured times) ----------------
    T_serial = float(sum(tile_secs))          # one GPU pays this (real sum)
    T_parallel_ideal = float(max(tile_secs))  # perfect N-GPU overlap (real slowest tile)
    mean_tile = T_serial / len(tile_secs)
    load_balance_efficiency = mean_tile / max(T_parallel_ideal, 1e-9)
    # net_speedup = the IDEAL fan-out ceiling. Clearly labelled ideal; it is an
    # UPPER BOUND (no comms/scheduling measured on one box) — see the note.
    net_speedup = T_serial / max(T_parallel_ideal, 1e-9)

    log(f"DISTRIBUTION: T_serial={T_serial:.3f}s T_parallel_ideal={T_parallel_ideal:.3f}s "
        f"load_balance_eff={load_balance_efficiency:.3f} "
        f"net_speedup(IDEAL fan-out ceiling)={net_speedup:.3f}")
    # sanity cross-check for the reader: T_serial (sum of tiles) vs the single
    # full-frame time. They should be within tiling/BVH-rebuild overhead of each
    # other; a big gap means per-tile fixed overhead (process launch + .blend load
    # + BVH build PER TILE) dominates — a real cost of naive fan-out, reported.
    tile_overhead_ratio = T_serial / max(full_frame_render_s, 1e-9)
    log(f"  sum-of-tiles / full-frame = {tile_overhead_ratio:.3f} "
        f"(>1 == per-tile launch/.blend-load/BVH overhead of the split)")

    device = ref_device
    parts = set(tile_devices) | {ref_device}
    if len(parts) > 1:
        device = "|".join(sorted(parts))
    fell_to_cpu = "CPU" in device

    note = (
        f"DISTRIBUTION / wall-clock-envelope lever on a REAL production Cycles scene "
        f"'{scene_key}' ({res_x}x{res_y}, spp={spp}, bounces={bounces}). ONE frame split "
        f"into {actual_tiles} tiles (grid {rows}x{cols}); EACH tile is a real Cycles "
        f"render_border subprocess at the SAME spp/bounces/seed (adaptive+denoise OFF), "
        f"so the split is LOSSLESS. All times are real time.perf_counter() whole-subprocess "
        f"wall-clock (launch+.blend load+BVH+border trace+PNG encode, NO exclusions) on the "
        f"SAME box. T_serial={T_serial:.2f}s = SUM of tile times (what ONE GPU pays tile-by-"
        f"tile). T_parallel_ideal={T_parallel_ideal:.2f}s = MAX tile time (perfect N-GPU "
        f"fan-out envelope). load_balance_efficiency={load_balance_efficiency:.3f} = "
        f"mean/max tile time. net_speedup={net_speedup:.2f}x is the IDEAL FAN-OUT CEILING "
        f"(T_serial/T_parallel_ideal) — an UPPER BOUND, NOT an achievable production number: "
        f"real cross-pod fan-out ALSO pays network scatter (ship the scene to N pods), "
        f"network gather (pull N tile PNGs back), and scheduler/queue overhead, NONE of which "
        f"is measured here (single box, zero-comms assumption; every tile assumed to start at "
        f"t=0 on its own idle GPU). quality={quality:.4f} is REAL scikit-image SSIM of the "
        f"reassembled composite vs a single full-frame render at the SAME spp (global + per-"
        f"8x8-tile worst={worst_tile:.4f}/p5={p5_tile:.4f}); a lossless split should score "
        f"~1.0 and a low worst tile is a seam/border-rounding BUG we report, not hide. "
        f"Composite pixel coverage={coverage:.4f}. sum-of-tiles/full-frame="
        f"{tile_overhead_ratio:.3f} (>1 == real per-tile launch/.blend-load/BVH overhead of "
        f"the naive split). Full-frame reference rendered ONCE on this box and cached by "
        f"(scene,res,spp,bounces,seed); full_frame_render_s is that real measured time"
        + (" (reused from cache this run, not re-paid)" if ref_cache_hit else "")
        + "."
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."
    if quality < 0.98:
        note += (
            f" WARNING: composite SSIM {quality:.4f} < 0.98 — the lossless spatial split "
            f"did NOT reproduce the full frame; this is a real seam/border-rounding BUG "
            f"in the split/reassemble (worst tile {worst_tile:.4f}), reported not hidden."
        )

    metrics = {
        "net_speedup": round(float(net_speedup), 4),   # IDEAL fan-out ceiling (labelled)
        "net_speedup_label": "ideal_fanout_ceiling_upper_bound",
        "quality": round(float(quality), 4),           # GLOBAL SSIM (composite vs full frame)
        "worst_tile_ssim": round(float(worst_tile), 4),
        "p5_tile_ssim": round(float(p5_tile), 4),
        "tiles": int(actual_tiles),
        "requested_tiles": int(n_tiles),
        "grid_rows": int(rows),
        "grid_cols": int(cols),
        "T_serial": round(T_serial, 4),
        "T_parallel_ideal": round(T_parallel_ideal, 4),
        "load_balance_efficiency": round(float(load_balance_efficiency), 4),
        "tile_secs": [round(float(s), 4) for s in tile_secs],
        "mean_tile_s": round(float(mean_tile), 4),
        "full_frame_render_s": round(float(full_frame_render_s), 4),
        "ref_render_s": round(float(full_frame_render_s), 4),  # alias for ledger parity
        "sum_tiles_over_full_frame": round(float(tile_overhead_ratio), 4),
        "composite_coverage": round(float(coverage), 6),
        "resolution": f"{res_x}x{res_y}",
        "spp": int(spp),
        "bounces": int(bounces),
        "seed": int(seed),
        "scene": scene_key,
        "requested_scene": scene_arg,
        "device": device,
        "ref_cache_hit": bool(ref_cache_hit),
        "modeled": False,
        "note": note,
    }

    log(f"RESULT net_speedup(ideal fan-out)={net_speedup:.3f} quality={quality:.4f} "
        f"worst_tile={worst_tile:.4f} T_serial={T_serial:.2f}s "
        f"T_parallel_ideal={T_parallel_ideal:.2f}s eff={load_balance_efficiency:.3f} "
        f"device={device}")
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
