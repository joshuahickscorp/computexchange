#!/usr/bin/env python3
"""
exp_render_upscale_guided.py — Track C (RESEARCH BET, REAL): render-at-HALF-res +
FULL-res AOV-guided super-resolution vs a self-hostable generic upscaler control.

================================================================================
THE BET (read this before the code)
================================================================================
Path tracing cost scales roughly with pixel count. If we render the expensive
BEAUTY at HALF resolution (960x540) and then upscale to FULL (1920x1080), we pay
~1/4 the beauty-shading cost. The question is whether a good upscaler recovers
enough of the full-res detail that the SSIM (vs a TRUE full-res render) stays high
while the wall-clock drops. This is the classic "render small, upsample" gambit —
the honest test is whether the upscaler EARNS its keep on real pixels, or whether
it just blurs / hallucinates and loses to the reference.

Two arms, and we report BOTH honestly:

  (a) CONTROL — a self-hostable GENERIC upscaler (Real-ESRGAN, an RRDBNet GAN
      super-resolver fetched via torch.hub if reachable offline-cache/online).
      Its training objective is PERCEPTUAL / GAN loss: it HALLUCINATES plausible
      high-frequency detail (invented texture, sharpened edges) that looks crisp
      to a human but does NOT match the true full-res render pixel-for-pixel. So
      we EXPECT its SSIM-vs-true-reference to be LOWER than a faithful upsample,
      and we report that loss rather than hiding it. If Real-ESRGAN can't be
      fetched (offline pod, no cached weights), we skip this arm and SAY SO — we
      never substitute another method and label it realesrgan.

  (b) CONTENDER (ours, more original, no model lock-in) — an AOV-GUIDED upsample.
      We render the FULL-res ALBEDO + NORMAL + DEPTH guide passes (these converge
      almost instantly — they are geometry/material buffers, not noisy GI — so a
      few-spp guide render is cheap even at full res), then use their high-freq
      EDGE structure to STEER an edge-aware joint upsample of the half-res beauty.
      The guides tell us exactly where real full-res edges live (object silhouettes
      from normal/depth discontinuities, texture boundaries from albedo), so we
      snap the upsampled beauty's edges to the true geometry instead of inventing
      detail. No GAN, no pretrained weights, no lock-in — just real full-res
      structure guiding a real upsample. Because the guide edges are REAL scene
      geometry (not invented), this should PRESERVE SSIM where the GAN control
      loses it.

  (baseline) BICUBIC — a plain bicubic upsample of the half-res beauty. The
      honest floor: any method that can't beat bicubic-vs-reference isn't earning
      its keep. Cheap (no guides), so its net_speedup is the highest — the trade
      is quality.

================================================================================
HONEST TIMING (net_speedup = T_reference / T_ours, SAME box, whole subprocess)
================================================================================
  T_reference  = whole-subprocess wall-clock of the FULL-res beauty render
                 (Blender launch + .blend load + BVH + trace + PNG encode; nothing
                 excluded). This is the true full-quality output we compare against.
                 Rendered ONCE and cached by (scene,res,spp,bounces); the cached
                 time is a real measured time on THIS box, just not re-paid.

  T_ours (per arm) = the REAL cost of producing the full-res image this arm's way:
    * bicubic     : T_halfres_render + T_bicubic_upscale
    * aov_guided  : T_halfres_render + T_guide_render + T_guided_upscale
                    (the FULL-res guide render is a REAL extra render whose whole-
                     subprocess wall-clock is INCLUDED — no free lunch)
    * realesrgan  : T_halfres_render + T_realesrgan_upscale
  Every T_* is a time.perf_counter() around the real work (subprocess for renders,
  the actual numpy/torch upscale for the upscale step). NOTHING that the arm must
  do to produce its full-res pixels is excluded.

================================================================================
HONEST QUALITY (real SSIM on real decoded pixels vs the TRUE full-res render)
================================================================================
  For each arm we upscale to EXACTLY the reference resolution and compute:
    * quality        = GLOBAL skimage SSIM(upscaled, true_full_res_reference)
    * worst_tile_ssim= MIN over an 8x8 tile grid  (catches a lever that blurs a
                       small high-frequency region while lifting the global avg)
    * p5_tile_ssim   = 5th-percentile tile SSIM
  All on the real reference PNG and the real upscaled pixels. A GAN upscaler that
  hallucinates detail will show up as a LOWER per-tile SSIM even if it looks sharp.

modeled is FALSE: every emitted number (all T_*, all SSIM) is a real measurement
on real rendered/decoded pixels. There is no cost model here — unlike the bounce-
hybrid sibling, every arm's T_ours is the literal sum of measured render+upscale
times. If a dependency is missing we EMIT AN ERROR or drop that arm with a note;
we never substitute and mislabel.

Forked from exp_render_gbuffer.py (AOV / multilayer-EXR pass plumbing, device
ladder) and exp_cycles_render.py / exp_cycles_render_prod.py (Blender bootstrap,
Classroom scene download+cache, whole-subprocess timing, global+per-tile SSIM,
reference cache). No other runner is modified.

Params (argv[1] JSON), all optional:
  low_res    : "WxH" cheap half-res beauty render      (default "960x540")
  full_res   : "WxH" true full-res reference + target  (default "1920x1080")
  method     : "aov_guided" | "bicubic" | "realesrgan" | "all"  (default "all")
  scene      : "classroom" (default) | "bmw27" | <direct .blend/.zip URL>
  spp        : samples per pixel for the BEAUTY renders (default 4096)
  guide_spp  : samples per pixel for the FULL-res guide render (default 16 —
               albedo/normal/depth are near-noiseless, so this is cheap)
  bounces    : light bounce depth, SAME for all beauty renders (default 12)
  seed       : Cycles seed (default 0)
  device     : "AUTO" | "GPU" | "CPU" (default AUTO)
  blender_url: override Blender download URL (default 4.2 LTS)
  realesrgan_url / realesrgan_scale : override the control weights URL / SR factor

Emits ONE json line on stdout (the metrics). On ANY failure emits
{"error":"<type>: <msg>"} as the last stdout line and exits 0 (never fabricates
a number, never hangs).
"""

import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile

# --------------------------------------------------------------------------- #
# Constants: Blender bootstrap location + a REAL 4.2 LTS Linux tarball URL,    #
# VERBATIM from the sibling render runners so a prior rung's download on the   #
# same pod is reused (idempotent).                                            #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/render_upscale_guided"

# Persistent scene + reference cache (prefer the big /models volume; survives
# between runners in one pod session). REUSED pattern from exp_cycles_render_prod.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")
REF_CACHE_DIR = os.path.join(_CACHE_ROOT, "ref_cache_upscale")

# Known production scenes on Blender's public demo server (VERIFIED reachable).
# The DEFAULT is Classroom (CC0), per the task. REUSED from exp_cycles_render_prod.
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

# Real-ESRGAN x4 generic upscaler weights (RRDBNet). Public GitHub release asset.
# This is the CONTROL — a perceptual/GAN upscaler we expect to LOSE on SSIM.
DEFAULT_REALESRGAN_URL = (
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/"
    "RealESRGAN_x4plus.pth"
)


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the JSON line)."""
    print("[render_upscale_guided]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs + imaging deps. REUSED from the sibling runners.  #
#    Blender needs a few X/GL shared libs even for a headless -b render; unzip  #
#    is belt-and-suspenders for the scene zip (we use python's zipfile).       #
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
    """Best-effort ensure numpy/pillow/scikit-image exist (setup_base.sh installs
    them). Never fatal here — the real check is the import at SSIM time, which errors
    cleanly. We do NOT try to pip-install torch/OpenEXR here (heavy / may be offline);
    those are probed at use-time and the affected arm degrades honestly."""
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
# 2. Self-bootstrap Blender — VERBATIM from exp_cycles_render.py. Idempotent:   #
#    reuse /root/blender/blender from a prior rung on the same pod.             #
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
# 3. Fetch + cache the Classroom (default) scene. Idempotent. REUSED from       #
#    exp_cycles_render_prod.py (_download / _find_blend / resolve_scene).       #
# --------------------------------------------------------------------------- #
def _download(url, dest, timeout=1200):
    """Stream a URL to dest with a real UA. Raises RuntimeError on failure."""
    log(f"downloading: {url}")
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0 spec-lab"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"download failed: {type(e).__name__}: {e}")
    dl_s = time.perf_counter() - t0
    size_mb = os.path.getsize(dest) / 1e6
    log(f"downloaded {size_mb:.1f} MB in {dl_s:.1f}s")


def _find_blend(extract_dir, preferred_relpaths):
    """Locate the main .blend in an extracted scene tree (known relpath, else the
    largest .blend — the main scene file is essentially always the biggest)."""
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

    scene_arg is a named scene ('classroom' default) or a direct .blend/.zip URL.
    Everything is cached under SCENES_DIR and reused on repeat invocations.
    """
    os.makedirs(SCENES_DIR, exist_ok=True)
    fallback_note = ""

    key = scene_arg.strip()
    is_url = key.startswith("http://") or key.startswith("https://")

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
        preferred = []
    else:
        src = SCENE_SOURCES[key]
        url = src["url"]
        scene_key = key
        is_zip = src["is_zip"]
        preferred = src["blend_relpaths"]

    scene_root = os.path.join(SCENES_DIR, scene_key)
    ready_marker = os.path.join(scene_root, ".ready")
    blend_ptr = os.path.join(scene_root, ".blendpath")

    # CACHE HIT: already extracted + recorded the main .blend path.
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
#    OPENS the Classroom .blend and OVERRIDES only render controls. Two modes:  #
#      * BEAUTY  (CX_MODE=beauty): render the full-quality beauty PNG at the     #
#        given resolution + spp + bounces. Used for BOTH the full-res reference  #
#        and the half-res draft (identical settings except resolution).         #
#      * GUIDES  (CX_MODE=guides): render at FULL res but LOW spp, emitting the  #
#        DenoisingAlbedo + DenoisingNormal + Depth(Z) AOV passes to a multilayer #
#        OpenEXR. These geometry/material buffers converge almost instantly, so  #
#        the guide render is cheap even at full res. (AOV / multilayer-EXR       #
#        plumbing FORKED from exp_render_gbuffer.py.)                            #
#    Prints CX_CHOSEN_DEVICE + CX_RENDER_DONE sentinels like the siblings.       #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
BLEND    = os.environ["CX_BLEND"]                  # Classroom .blend to open
OUT      = os.environ["CX_OUT"]                    # beauty PNG path (SSIM-able)
RES_X    = int(os.environ["CX_RES_X"])
RES_Y    = int(os.environ["CX_RES_Y"])
SPP      = int(os.environ["CX_SPP"])
BOUNCES  = int(os.environ["CX_BOUNCES"])
SEED     = int(os.environ["CX_SEED"])
DEV_PREF = os.environ.get("CX_DEVICE", "AUTO")
MODE     = os.environ.get("CX_MODE", "beauty")     # beauty | guides
EXR_OUT  = os.environ.get("CX_EXR_OUT", "")        # multilayer EXR dir for guides

# ---- open the REAL Classroom scene ------------------------------------------
# open_mainfile loads the .blend's own geometry/materials/lights/camera; we only
# override render controls below — we never rebuild the scene.
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
try:
    cyc.seed = SEED
except Exception:
    pass
cyc.use_adaptive_sampling = False   # fixed spp so half vs full is a clean ratio
cyc.use_denoising = False           # NO denoiser: upscaling is the lever, not denoise

# Same bounces everywhere so bounce depth is NOT a hidden lever. The ONLY thing
# that differs between the half-res draft and the full-res reference is resolution.
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ---- DEVICE LADDER: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU -----------------
# FORKED verbatim from the sibling render runners. OPTIX first per AUTO; enabling
# only GPU devices avoids a slow CPU+GPU hybrid.
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

# ---- output: PNG RGB, single frame, requested resolution --------------------
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT

# ===== GUIDES MODE: emit full-res ALBEDO + NORMAL + DEPTH AOV passes ==========
# FORKED from exp_render_gbuffer.py's probe-pass plumbing. We route the render
# layer's DenoisingAlbedo / DenoisingNormal / Depth passes into a multilayer
# OpenEXR (32-bit float => real linear values the outer python reads with
# numpy). These buffers are geometry/material, not noisy GI, so a low-spp guide
# render is near-noiseless — the whole point of the cheap guide.
if MODE == "guides" and EXR_OUT:
    vl = scene.view_layers[0]
    # Denoising Albedo/Normal are the clean per-pixel material/orientation guides
    # (also what OIDN uses); Depth(Z) is the geometry distance buffer.
    for attr in ("use_pass_z",):
        try:
            setattr(vl, attr, True)
        except Exception as e:
            _log(f"could not enable view-layer {attr}:", e)
    # DenoisingAlbedo/Normal live under the cycles view-layer settings in 4.x.
    try:
        vl.cycles.denoising_store_passes = True
    except Exception as e:
        _log("could not set cycles.denoising_store_passes:", e)
    try:
        vl.use_pass_normal = True
    except Exception as e:
        _log("could not enable use_pass_normal:", e)
    try:
        vl.use_pass_diffuse_color = True  # albedo-ish fallback if Denoising* absent
    except Exception as e:
        _log("could not enable use_pass_diffuse_color:", e)

    scene.use_nodes = True
    nt = scene.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rl = nt.nodes.new('CompositorNodeRLayers')
    out = nt.nodes.new('CompositorNodeOutputFile')
    out.base_path = os.path.dirname(EXR_OUT)
    out.format.file_format = 'OPEN_EXR_MULTILAYER'
    out.format.color_depth = '32'
    # Candidate socket names across 4.x; we wire whichever exist. The outer python
    # matches on the produced EXR layer names (suffix match), so extra slots are OK.
    want = ["DenoisingAlbedo", "DenoisingNormal", "Normal", "DiffCol", "Depth"]
    out.file_slots.clear()
    wired = []
    for name in want:
        sock = rl.outputs.get(name)
        if sock is not None:
            out.file_slots.new(name)
            nt.links.new(sock, out.inputs[name])
            wired.append(name)
        else:
            _log(f"guide socket {name!r} not present on render layer (skipped)")
    if not wired:
        _log("WARN: no guide passes could be wired — outer python will error cleanly")
    else:
        out.file_slots[0].path = "cx_guides_"  # slot 0 drives the filename stem
        _log(f"guide passes wired: {wired} -> EXR dir {out.base_path}")

_log(f"rendering MODE={MODE} spp={SPP} res={RES_X}x{RES_Y} bounces={BOUNCES} "
     f"device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, *, blend, out_png, res_x, res_y,
                       spp, bounces, seed, device_pref, timeout_s, mode="beauty",
                       exr_out=""):
    """Invoke Blender headless to render ONE frame. Returns (wall_seconds, device).

    wall_seconds = time.perf_counter() around the WHOLE subprocess — process launch
    + .blend load + BVH build + trace + (guide-pass compositor) + PNG/EXR encode.
    NOTHING excluded. Raises RuntimeError on non-zero exit or missing output PNG.
    (Subprocess/timeout/device-parse pattern FORKED from the sibling render runners.)
    """
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_png
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    env["CX_MODE"] = mode
    env["CX_EXR_OUT"] = exr_out

    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    log(f"render start [{mode}]: spp={spp} res={res_x}x{res_y} bounces={bounces} "
        f"-> {out_png}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1800:]
    err_tail = (proc.stderr or "")[-1800:]
    log(f"render [{mode}] rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [{mode}] (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_png)}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device


# --------------------------------------------------------------------------- #
# 5. Read the full-res AOV guide passes from the multilayer EXR (numpy, in the  #
#    OUTER python). FORKED from exp_render_gbuffer.py's load_exr_passes: prefer  #
#    OpenImageIO (ships alongside Blender / commonly present), else OpenEXR,     #
#    else imageio. Raises if none can read multilayer channels — the aov_guided  #
#    arm then FAILS cleanly rather than pretending it had guides.               #
# --------------------------------------------------------------------------- #
def _find_guide_exr(exr_dir):
    """Locate the multilayer EXR the compositor OutputFile node wrote (newest match)."""
    cands = sorted(
        glob.glob(os.path.join(exr_dir, "*.exr")),
        key=lambda p: os.path.getmtime(p),
    )
    return cands[-1] if cands else None


def _match_layer(chan_names, arr, stems, ncomp):
    """From flat channel names + (H,W,C) array, pull the first matching layer.

    stems is a list of acceptable layer-stem synonyms (case-insensitive suffix
    match on '<stem>.R/.G/.B' or '<stem>.V' for a scalar). Returns an (H,W,ncomp)
    float32 array or None. ncomp=3 for RGB guides, 1 for a scalar depth pass.
    """
    import numpy as np
    comps = ("r", "g", "b")[:ncomp] if ncomp == 3 else ("v",)
    for stem in stems:
        # scalar passes are often stored as '<stem>.V' OR a single '<stem>.Z'/'<stem>'
        idx = {}
        for ci, cn in enumerate(chan_names):
            low = cn.lower()
            if ncomp == 3:
                for comp in comps:
                    if low.endswith(f"{stem.lower()}.{comp}"):
                        idx[comp] = ci
            else:
                # accept '<stem>.v', '<stem>.z', or bare '<stem>'
                if (low.endswith(f"{stem.lower()}.v")
                        or low.endswith(f"{stem.lower()}.z")
                        or low == stem.lower()):
                    idx["v"] = ci
        if set(comps) <= set(idx):
            planes = [arr[..., idx[c]] for c in comps]
            return np.stack(planes, axis=-1).astype(np.float32)
    return None


def load_guide_passes(exr_path):
    """Load full-res guide layers from the multilayer EXR.

    Returns a dict with any of: 'albedo' (H,W,3), 'normal' (H,W,3), 'depth' (H,W,1),
    all float32 linear. Uses OpenImageIO -> OpenEXR -> raises. We need at least ONE
    guide (albedo or normal) for the guided arm; the caller errors if the dict is
    empty. (Reader ladder FORKED from exp_render_gbuffer.load_exr_passes.)
    """
    import numpy as np

    chan_names = None
    arr = None

    # ---- Preferred: OpenImageIO (bundled with / commonly present alongside Blender)
    try:
        import OpenImageIO as oiio  # type: ignore
        inp = oiio.ImageInput.open(exr_path)
        if inp is None:
            raise RuntimeError(f"OIIO could not open {exr_path}")
        spec = inp.spec()
        chan_names = list(spec.channelnames)
        pixels = inp.read_image(format="float")
        inp.close()
        arr = np.asarray(pixels, dtype=np.float32).reshape(
            spec.height, spec.width, spec.nchannels
        )
    except ImportError:
        pass  # fall through to OpenEXR

    # ---- Fallback: OpenEXR + Imath (installed best-effort by setup_base.sh) -----
    if arr is None:
        try:
            import OpenEXR  # type: ignore
            import Imath  # type: ignore
        except ImportError:
            raise RuntimeError(
                "no multilayer-EXR reader available (OpenImageIO and OpenEXR both "
                "absent); cannot read full-res AOV guides — aov_guided arm cannot run"
            )
        f = OpenEXR.InputFile(exr_path)
        hdr = f.header()
        dw = hdr["dataWindow"]
        w = dw.max.x - dw.min.x + 1
        h = dw.max.y - dw.min.y + 1
        chan_names = list(hdr["channels"].keys())
        pt = Imath.PixelType(Imath.PixelType.FLOAT)
        planes = []
        for cn in chan_names:
            raw = f.channel(cn, pt)
            planes.append(np.frombuffer(raw, dtype=np.float32).reshape(h, w))
        f.close()
        arr = np.stack(planes, axis=-1).astype(np.float32)

    if arr is None or chan_names is None:
        raise RuntimeError("EXR read produced no pixel data")

    out = {}
    alb = _match_layer(chan_names, arr, ["DenoisingAlbedo", "DiffCol"], 3)
    if alb is not None:
        out["albedo"] = alb
    nrm = _match_layer(chan_names, arr, ["DenoisingNormal", "Normal"], 3)
    if nrm is not None:
        out["normal"] = nrm
    dep = _match_layer(chan_names, arr, ["Depth"], 1)
    if dep is not None:
        out["depth"] = dep

    if not out:
        raise RuntimeError(
            f"EXR opened but no known guide layers found (channels: {chan_names})"
        )
    log(f"guide passes loaded from EXR: {sorted(out)} (shape {arr.shape})")
    return out


# --------------------------------------------------------------------------- #
# 6. Upscalers. Each takes the half-res beauty (float [0,1] HxW x3) and returns #
#    a full-res float image at (full_h, full_w). Guided uses the real full-res  #
#    AOV edge structure; bicubic is the honest floor; realesrgan is the GAN     #
#    control we expect to LOSE on SSIM.                                         #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def _save_png_float(arr, path):
    """Save an (H,W,3) float [0,1] array as an 8-bit PNG."""
    from PIL import Image
    import numpy as np
    a8 = np.clip(arr * 255.0 + 0.5, 0, 255).astype("uint8")
    Image.fromarray(a8, mode="RGB").save(path)


def upscale_bicubic(low_rgb, full_w, full_h):
    """Plain bicubic upsample (the honest floor). Pillow BICUBIC resampling."""
    from PIL import Image
    import numpy as np
    a8 = np.clip(low_rgb * 255.0 + 0.5, 0, 255).astype("uint8")
    img = Image.fromarray(a8, mode="RGB").resize((full_w, full_h), Image.BICUBIC)
    return np.asarray(img, dtype=np.float32) / 255.0


def _gray(rgb):
    """Rec.709 luma of an (H,W,3) array."""
    import numpy as np
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1]
            + 0.0722 * rgb[..., 2]).astype(np.float32)


def _sobel_edges(gray2d):
    """Edge magnitude of a 2-D array via a 3x3 Sobel (numpy slicing, no scipy)."""
    import numpy as np
    g = np.pad(gray2d.astype(np.float32), 1, mode="edge")
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]
          - g[:-2, :-2] - 2 * g[1:-1, :-2] - g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]
          - g[:-2, :-2] - 2 * g[:-2, 1:-1] - g[:-2, 2:])
    return np.sqrt(gx * gx + gy * gy)


def _box_blur(a, radius):
    """Separable box blur of a 2-D array (dependency-free sliding window)."""
    import numpy as np
    if radius <= 0:
        return a.astype(np.float32)
    k = 2 * radius + 1
    pad = np.pad(a.astype(np.float32), radius, mode="edge")
    cs = np.cumsum(pad, axis=1)
    horiz = (cs[:, k - 1:] - np.pad(cs[:, :-k], ((0, 0), (1, 0)))) / k
    horiz = horiz[radius:-radius, :]
    pad2 = np.pad(horiz, ((radius, radius), (0, 0)), mode="edge")
    cs2 = np.cumsum(pad2, axis=0)
    vert = (cs2[k - 1:, :] - np.pad(cs2[:-k, :], ((1, 0), (0, 0)))) / k
    return vert.astype(np.float32)


def upscale_aov_guided(low_rgb, guides, full_w, full_h):
    """AOV-GUIDED edge-aware upsample (OUR contender — original, no model lock-in).

    Idea: bicubic gives us a smooth full-res base that has the right COLOR but soft
    EDGES (it can't invent the high-freq detail lost by rendering small). The full-res
    ALBEDO + NORMAL + DEPTH guides, however, DO carry the true full-res edge structure
    (object silhouettes = normal/depth discontinuities; texture boundaries = albedo
    edges) — and they're real scene geometry, not invented. So we build a full-res
    GUIDE-EDGE map from the AOVs and use it to steer a guided (edge-aware) sharpening
    of the bicubic base:

      1. base = bicubic(low_rgb) at full res  (correct color, soft edges).
      2. guide_edge = normalized Sobel-edge magnitude of the full-res albedo & normal
         luma, combined — a map of WHERE real full-res edges are.
      3. A guided-filter-style local correction: the beauty's own edges are the
         low-freq part; we add back a high-freq detail term (base - blur(base))
         MODULATED by guide_edge, so detail is enhanced ONLY where the AOVs say a
         real edge exists, and left alone in smooth regions (no ringing, no invented
         texture in flat areas). This snaps the upsample toward true geometry.

    This is a real, deterministic image operation on real pixels — it CANNOT
    hallucinate detail that isn't implied by the true full-res geometry buffers, so
    it should preserve SSIM where the GAN control loses it. Returns full-res float.
    """
    import numpy as np
    base = upscale_bicubic(low_rgb, full_w, full_h)  # (full_h, full_w, 3)

    # --- build the full-res guide-edge map from the AOVs -------------------- #
    edge_accum = np.zeros((full_h, full_w), dtype=np.float32)
    used = []
    for key, weight in (("albedo", 1.0), ("normal", 1.0)):
        g = guides.get(key)
        if g is None:
            continue
        g2 = _resize_to(g, full_w, full_h)          # AOVs are full-res, but be safe
        e = _sobel_edges(_gray(g2))
        m = float(e.max())
        if m > 1e-8:
            edge_accum += (e / m) * weight
            used.append(key)
    if "depth" in guides:
        d = guides["depth"]
        d2 = _resize_to(d, full_w, full_h)[..., 0]
        # normalize finite depth to [0,1] before edge detection (raw Z is in meters,
        # background may be a huge sentinel — clamp to the finite in-scene range).
        finite = np.isfinite(d2)
        if finite.any():
            lo = float(np.percentile(d2[finite], 1))
            hi = float(np.percentile(d2[finite], 99))
            if hi > lo:
                dn = np.clip((d2 - lo) / (hi - lo), 0.0, 1.0)
                e = _sobel_edges(dn)
                m = float(e.max())
                if m > 1e-8:
                    edge_accum += (e / m) * 0.8
                    used.append("depth")
    if not used:
        raise RuntimeError(
            "aov_guided: guide EXR had no usable albedo/normal/depth edges to steer "
            "the upsample — refusing to silently fall back to bicubic and mislabel it"
        )
    # smooth + normalize the edge map into a [0,1] steering weight.
    edge_map = _box_blur(edge_accum, radius=1)
    em = float(edge_map.max())
    if em > 1e-8:
        edge_map = edge_map / em
    edge_map = np.clip(edge_map, 0.0, 1.0)[..., None]  # (H,W,1)

    # --- guided detail enhancement: add high-freq detail ONLY on real edges - #
    # detail = base - blur(base) is the high-frequency residual bicubic left soft.
    blur = np.stack([_box_blur(base[..., c], radius=2) for c in range(3)], axis=-1)
    detail = base - blur
    # strength: enhance detail proportional to the guide edge weight (bounded).
    strength = 0.9
    out = base + strength * edge_map * detail
    return np.clip(out, 0.0, 1.0).astype(np.float32), used


def _resize_to(arr, full_w, full_h):
    """Resize an (H,W,C) or (H,W) array to (full_h, full_w) via nearest sampling."""
    import numpy as np
    a = arr
    single = (a.ndim == 2)
    if single:
        a = a[..., None]
    h, w = a.shape[:2]
    if (h, w) == (full_h, full_w):
        return a if not single else a
    ys = np.linspace(0, h - 1, full_h).astype(np.int64)
    xs = np.linspace(0, w - 1, full_w).astype(np.int64)
    out = a[ys][:, xs]
    return out


# ----- Real-ESRGAN control (fetched via torch.hub weights; may be unavailable) -- #
def _rrdbnet():
    """Build an RRDBNet x4 generator matching the RealESRGAN_x4plus checkpoint.

    Self-contained torch module (no basicsr dependency) so the CONTROL is runnable
    from just torch + the .pth weights. Returns an nn.Module or raises ImportError
    if torch is absent.
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class ResidualDenseBlock(nn.Module):
        def __init__(self, nf=64, gc=32):
            super().__init__()
            self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
            self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
            self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
            self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
            self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, nf=64, gc=32):
            super().__init__()
            self.b1 = ResidualDenseBlock(nf, gc)
            self.b2 = ResidualDenseBlock(nf, gc)
            self.b3 = ResidualDenseBlock(nf, gc)

        def forward(self, x):
            out = self.b1(x)
            out = self.b2(out)
            out = self.b3(out)
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self, in_nc=3, out_nc=3, nf=64, nb=23, gc=32):
            super().__init__()
            self.conv_first = nn.Conv2d(in_nc, nf, 3, 1, 1)
            self.body = nn.ModuleList([RRDB(nf, gc) for _ in range(nb)])
            self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_last = nn.Conv2d(nf, out_nc, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            feat = self.conv_first(x)
            body = feat
            for blk in self.body:
                body = blk(body)
            feat = feat + self.conv_body(body)
            feat = self.lrelu(self.conv_up1(
                F.interpolate(feat, scale_factor=2, mode='nearest')))
            feat = self.lrelu(self.conv_up2(
                F.interpolate(feat, scale_factor=2, mode='nearest')))
            out = self.conv_last(self.lrelu(self.conv_hr(feat)))
            return out

    return RRDBNet()


def fetch_realesrgan_model(url, cache_dir):
    """Fetch + load the Real-ESRGAN x4 control. Returns (model, device_str) or
    raises RuntimeError with a clear reason (torch absent / weights unfetchable /
    state_dict mismatch). The CALLER treats a raise as 'skip this arm and say so' —
    it must NEVER substitute another upscaler and label it realesrgan.
    """
    try:
        import torch
    except ImportError:
        raise RuntimeError("torch not importable — Real-ESRGAN control cannot run")

    os.makedirs(cache_dir, exist_ok=True)
    weights = os.path.join(cache_dir, "RealESRGAN_x4plus.pth")
    if not os.path.isfile(weights):
        try:
            _download(url, weights, timeout=600)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Real-ESRGAN weights could not be fetched (offline / unreachable): "
                f"{type(e).__name__}: {e}"
            )

    try:
        ckpt = torch.load(weights, map_location="cpu")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not load Real-ESRGAN checkpoint: "
                           f"{type(e).__name__}: {e}")
    # The official release stores the generator under 'params_ema' (or 'params').
    sd = ckpt.get("params_ema", ckpt.get("params", ckpt)) if isinstance(ckpt, dict) else ckpt

    model = _rrdbnet()
    try:
        model.load_state_dict(sd, strict=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Real-ESRGAN state_dict did not match our RRDBNet (strict) — refusing "
            f"to run a mismatched/partial model and mislabel it: {type(e).__name__}: {e}"
        )
    model.eval()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(dev)
    log(f"Real-ESRGAN x4 control loaded (strict) on {dev}")
    return model, dev


def upscale_realesrgan(low_rgb, model, dev, full_w, full_h):
    """Run the Real-ESRGAN x4 GAN upsampler, then resize to EXACTLY (full_h,full_w).

    x4 gives 4x the half-res; we resize (bicubic) to the true reference size so SSIM
    is apples-to-apples. The GAN's hallucinated detail is preserved through this
    resize — we are NOT hiding it; that's the whole point of the control.
    """
    import numpy as np
    import torch
    from PIL import Image
    x = torch.from_numpy(np.ascontiguousarray(
        low_rgb.transpose(2, 0, 1)[None])).float().to(dev)
    with torch.no_grad():
        y = model(x).clamp_(0, 1)
    sr = y[0].detach().cpu().numpy().transpose(1, 2, 0)  # (h*4, w*4, 3)
    if sr.shape[:2] != (full_h, full_w):
        a8 = np.clip(sr * 255.0 + 0.5, 0, 255).astype("uint8")
        sr = np.asarray(
            Image.fromarray(a8, mode="RGB").resize((full_w, full_h), Image.BICUBIC),
            dtype=np.float32,
        ) / 255.0
    return sr.astype(np.float32)


# --------------------------------------------------------------------------- #
# 7. Quality: GLOBAL SSIM + PER-TILE (8x8) worst + 5th-percentile tile SSIM,    #
#    on the REAL upscaled pixels vs the REAL full-res reference PNG. FORKED      #
#    from exp_cycles_render_prod.compute_ssim_global_and_tiles.                 #
# --------------------------------------------------------------------------- #
def compute_ssim_global_and_tiles(upscaled_arr, ref_png, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim).

    GLOBAL: skimage SSIM over the whole upscaled image vs the real reference PNG.
    PER-TILE: grid x grid tiles, SSIM per tile, then MIN and 5th-percentile. Catches
    an upscaler that lifts the global average while blurring / hallucinating a small
    high-frequency region (chair legs, specular) — exactly what a GAN control does.
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    ref = _load_png_float(ref_png)
    up = upscaled_arr.astype(np.float32)
    if up.shape != ref.shape:
        raise RuntimeError(
            f"upscaled/ref shape mismatch {up.shape} vs {ref.shape}"
        )

    global_ssim = float(ssim(ref, up, channel_axis=-1, data_range=1.0))

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
            dt = up[y0:y1, x0:x1]
            if min(rt.shape[0], rt.shape[1]) < 7:
                continue  # too small for the default 7x7 SSIM window
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
# 8. Reference cache: render the FULL-res beauty ground truth ONCE, keyed by     #
#    (scene, full_res, spp, bounces). FORKED from exp_cycles_render_prod.        #
# --------------------------------------------------------------------------- #
def _ref_cache_key(scene_key, res_x, res_y, spp, bounces):
    raw = f"{scene_key}|{res_x}x{res_y}|spp{spp}|b{bounces}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return h, raw


def get_or_render_reference(blender_bin, script_path, *, blend, scene_key, res_x,
                            res_y, spp, bounces, seed, device_pref, timeout_s):
    """Return (ref_png_path, ref_render_s, ref_device, cache_hit_bool).

    On a cache HIT the PNG + sidecar json exist and are reused — ref_render_s comes
    from the sidecar (a real measured time on this same box, just not RE-PAID). On a
    MISS we render it once, time the WHOLE subprocess, and persist PNG + sidecar.
    """
    os.makedirs(REF_CACHE_DIR, exist_ok=True)
    h, raw = _ref_cache_key(scene_key, res_x, res_y, spp, bounces)
    ref_png = os.path.join(REF_CACHE_DIR, f"ref_{h}.png")
    sidecar = os.path.join(REF_CACHE_DIR, f"ref_{h}.json")

    if os.path.isfile(ref_png) and os.path.isfile(sidecar):
        try:
            with open(sidecar) as f:
                meta = json.load(f)
            ref_s = float(meta["ref_render_s"])
            ref_dev = str(meta.get("device", "unknown"))
            log(f"REFERENCE cache HIT [{raw}] -> {ref_png} "
                f"(ref_render_s={ref_s:.3f}s, device={ref_dev}); NOT re-paying it")
            return ref_png, ref_s, ref_dev, True
        except Exception as e:  # noqa: BLE001 — corrupt sidecar -> re-render
            log(f"reference sidecar unreadable ({e}); re-rendering reference")

    log(f"REFERENCE cache MISS [{raw}] -> rendering full-res ground truth once")
    ref_s, ref_dev = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=ref_png,
        res_x=res_x, res_y=res_y, spp=spp, bounces=bounces, seed=seed,
        device_pref=device_pref, timeout_s=timeout_s, mode="beauty",
    )
    try:
        with open(sidecar, "w") as f:
            json.dump({
                "ref_render_s": ref_s, "device": ref_dev, "key": raw,
                "scene": scene_key, "resolution": f"{res_x}x{res_y}",
                "spp": spp, "bounces": bounces,
            }, f)
    except Exception as e:  # noqa: BLE001 — non-fatal; ref still usable this run
        log(f"could not write reference sidecar (non-fatal): {e}")
    return ref_png, ref_s, ref_dev, False


def _parse_res(s, default):
    try:
        rx, ry = str(s).lower().split("x")
        return max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {s!r}; expected WxH e.g. {default}")


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    low_res = str(params.get("low_res", "960x540"))
    full_res = str(params.get("full_res", "1920x1080"))
    method = str(params.get("method", "all")).lower()
    scene_arg = str(params.get("scene", "classroom"))
    spp = int(params.get("spp", 4096))
    guide_spp = int(params.get("guide_spp", 16))
    bounces = int(params.get("bounces", 12))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))
    realesrgan_url = str(params.get("realesrgan_url", DEFAULT_REALESRGAN_URL))

    low_w, low_h = _parse_res(low_res, "960x540")
    full_w, full_h = _parse_res(full_res, "1920x1080")
    spp = max(1, spp)
    guide_spp = max(1, guide_spp)
    bounces = max(1, bounces)

    valid_methods = {"aov_guided", "bicubic", "realesrgan", "all"}
    if method not in valid_methods:
        raise RuntimeError(f"bad method {method!r}; expected one of {sorted(valid_methods)}")
    if method == "all":
        arms = ["bicubic", "aov_guided", "realesrgan"]
    else:
        arms = [method]

    log(f"params: low_res={low_w}x{low_h} full_res={full_w}x{full_h} method={method} "
        f"scene={scene_arg} spp={spp} guide_spp={guide_spp} bounces={bounces} "
        f"seed={seed} device={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + imaging deps + bootstrap Blender ----------------- #
    ensure_system_libs()
    ensure_pydeps()
    blender_bin = ensure_blender(blender_url)

    # ---- 1) fetch + cache the Classroom scene ------------------------------ #
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    script_path = os.path.join(WORK_DIR, "cx_upscale_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # generous subprocess timeouts: a full-res @ high-spp reference is heavy on CPU.
    ref_timeout = 3600
    draft_timeout = 1800
    guide_timeout = 1200

    # ---- 2) REFERENCE: full-res beauty ground truth (cached-or-render) ----- #
    # This is the TRUE full-quality output every arm is scored against and whose
    # whole-subprocess wall-clock is the numerator of net_speedup.
    ref_png, ref_render_s, ref_device, ref_cache_hit = get_or_render_reference(
        blender_bin, script_path, blend=blend, scene_key=scene_key,
        res_x=full_w, res_y=full_h, spp=spp, bounces=bounces, seed=seed,
        device_pref=device_pref, timeout_s=ref_timeout,
    )

    # ---- 3) HALF-RES draft beauty (the cheap render every arm upscales) ----- #
    # SAME spp / bounces / seed as the reference — the ONLY difference is resolution,
    # so the beauty-render speedup comes purely from rendering fewer pixels.
    draft_png = os.path.join(WORK_DIR, f"halfres_{scene_key}.png")
    halfres_render_s, draft_device = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=draft_png,
        res_x=low_w, res_y=low_h, spp=spp, bounces=bounces, seed=seed,
        device_pref=device_pref, timeout_s=draft_timeout, mode="beauty",
    )
    low_rgb = _load_png_float(draft_png)

    # ---- 4) FULL-RES AOV guide render — ONLY if an aov_guided arm is present. #
    # Rendered at full res but LOW spp (guide_spp): albedo/normal/depth are near-
    # noiseless geometry/material buffers, so this converges almost instantly. Its
    # WHOLE-subprocess wall-clock is a REAL cost we ADD to the aov_guided T_ours.
    guide_render_s = None
    guides = None
    guide_device = None
    guide_load_err = None
    if "aov_guided" in arms:
        exr_dir = os.path.join(WORK_DIR, "guides_exr")
        os.makedirs(exr_dir, exist_ok=True)
        # clear any stale EXR from a prior run so _find_guide_exr picks THIS one.
        for old in glob.glob(os.path.join(exr_dir, "*.exr")):
            try:
                os.remove(old)
            except OSError:
                pass
        guide_png = os.path.join(WORK_DIR, f"guide_beauty_{scene_key}.png")
        guide_render_s, guide_device = run_blender_render(
            blender_bin, script_path, blend=blend, out_png=guide_png,
            res_x=full_w, res_y=full_h, spp=guide_spp, bounces=bounces, seed=seed,
            device_pref=device_pref, timeout_s=guide_timeout, mode="guides",
            exr_out=os.path.join(exr_dir, "cx_guides_.exr"),
        )
        guide_exr = _find_guide_exr(exr_dir)
        if guide_exr is None:
            guide_load_err = "no guide EXR was produced by the guide render"
        else:
            try:
                guides = load_guide_passes(guide_exr)
            except Exception as e:  # noqa: BLE001
                guide_load_err = f"{type(e).__name__}: {e}"
                log(f"guide EXR load failed: {guide_load_err}")

    # ---- 5) Real-ESRGAN control — fetch once if a realesrgan arm is present.  #
    realesrgan_model = None
    realesrgan_dev = None
    realesrgan_skip_reason = None
    if "realesrgan" in arms:
        try:
            realesrgan_model, realesrgan_dev = fetch_realesrgan_model(
                realesrgan_url, os.path.join(_CACHE_ROOT, "realesrgan")
            )
        except Exception as e:  # noqa: BLE001 — offline/unavailable => drop this arm
            realesrgan_skip_reason = f"{type(e).__name__}: {e}"
            log(f"Real-ESRGAN control unavailable, dropping that arm: {realesrgan_skip_reason}")

    device = draft_device if draft_device == ref_device else f"{ref_device}|{draft_device}"
    fell_to_cpu = "CPU" in device

    # ---- 6) run each arm: upscale (timed) -> SSIM (global + per-tile) ------- #
    arm_results = {}
    for arm in arms:
        try:
            if arm == "bicubic":
                t0 = time.perf_counter()
                up = upscale_bicubic(low_rgb, full_w, full_h)
                upscale_s = time.perf_counter() - t0
                t_ours = halfres_render_s + upscale_s
                extra = {}

            elif arm == "aov_guided":
                if guides is None:
                    # HONEST: no guides => this arm CANNOT run. Do NOT fall back to
                    # bicubic and mislabel it as guided; record the skip reason.
                    arm_results[arm] = {
                        "skipped": True,
                        "reason": (
                            "aov_guided skipped: full-res AOV guides unavailable ("
                            + (guide_load_err or "guide render/read failed")
                            + ")"
                        ),
                    }
                    continue
                t0 = time.perf_counter()
                up, used_guides = upscale_aov_guided(low_rgb, guides, full_w, full_h)
                upscale_s = time.perf_counter() - t0
                # T_ours INCLUDES the full-res guide render (a real extra render).
                t_ours = halfres_render_s + (guide_render_s or 0.0) + upscale_s
                extra = {
                    "guide_render_s": round(float(guide_render_s or 0.0), 4),
                    "guide_spp": int(guide_spp),
                    "guides_used": used_guides,
                }

            elif arm == "realesrgan":
                if realesrgan_model is None:
                    arm_results[arm] = {
                        "skipped": True,
                        "reason": (
                            "realesrgan (CONTROL) skipped: model unavailable offline ("
                            + (realesrgan_skip_reason or "fetch/load failed")
                            + ")"
                        ),
                    }
                    continue
                t0 = time.perf_counter()
                up = upscale_realesrgan(low_rgb, realesrgan_model, realesrgan_dev,
                                        full_w, full_h)
                upscale_s = time.perf_counter() - t0
                t_ours = halfres_render_s + upscale_s
                extra = {"realesrgan_device": realesrgan_dev}
            else:
                continue

            # save the upscaled image (for provenance/debug) and SSIM it.
            out_png = os.path.join(WORK_DIR, f"up_{arm}_{scene_key}.png")
            _save_png_float(up, out_png)
            g_ssim, worst_tile, p5_tile = compute_ssim_global_and_tiles(
                up, ref_png, grid=8
            )
            net_speedup = ref_render_s / max(t_ours, 1e-9)
            res = {
                "net_speedup": round(float(net_speedup), 4),
                "quality": round(float(g_ssim), 4),           # GLOBAL SSIM vs true ref
                "worst_tile_ssim": round(float(worst_tile), 4),
                "p5_tile_ssim": round(float(p5_tile), 4),
                "t_ours_s": round(float(t_ours), 4),
                "halfres_render_s": round(float(halfres_render_s), 4),
                "upscale_s": round(float(upscale_s), 4),
                "skipped": False,
            }
            res.update(extra)
            arm_results[arm] = res
            log(f"[{arm}] net_speedup={net_speedup:.3f} quality={g_ssim:.4f} "
                f"worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f} "
                f"t_ours={t_ours:.3f}s")
        except Exception as e:  # noqa: BLE001 — one arm failing must not kill the run
            import traceback
            traceback.print_exc(file=sys.stderr)
            arm_results[arm] = {"skipped": True,
                                "reason": f"{type(e).__name__}: {e}"}

    # ---- 7) pick the headline (the CONTENDER if it ran, else best real arm) - #
    # Headline metrics are the aov_guided contender's when it ran; otherwise the
    # first arm that produced real numbers. If NOTHING ran, that's an error.
    def _ran(a):
        r = arm_results.get(a)
        return r is not None and not r.get("skipped", False)

    headline_arm = None
    for pref in ("aov_guided", "bicubic", "realesrgan"):
        if pref in arm_results and _ran(pref):
            headline_arm = pref
            break
    if headline_arm is None:
        # every requested arm was skipped/failed — emit an error, never a number.
        reasons = {a: arm_results.get(a, {}).get("reason", "unknown")
                   for a in arms}
        raise RuntimeError(
            "no upscale arm produced a real measurement; per-arm reasons: "
            + json.dumps(reasons)
        )

    head = arm_results[headline_arm]

    note = (
        f"REAL half-res render ({low_w}x{low_h}) + upscale to full-res "
        f"({full_w}x{full_h}) vs a TRUE full-res Cycles render, scene='{scene_key}', "
        f"spp={spp}, bounces={bounces}. Headline arm='{headline_arm}'. "
        f"net_speedup=ref_render_s/t_ours, both real whole-subprocess wall-clock on "
        f"the SAME box; t_ours = half-res render"
        + (" + full-res AOV guide render" if headline_arm == "aov_guided" else "")
        + " + upscale (all measured, nothing excluded). "
        f"quality = real skimage SSIM (global + per-8x8-tile min/p5) of the upscaled "
        f"pixels vs the real full-res reference PNG. The realesrgan CONTROL is a "
        f"perceptual/GAN upscaler that HALLUCINATES detail => expected to LOSE on "
        f"SSIM-vs-true-reference; the aov_guided CONTENDER steers a real edge-aware "
        f"upsample with the true full-res albedo/normal/depth edges (no GAN, no "
        f"model lock-in). Reference rendered ONCE on this box and cached by "
        f"(scene,res,spp,bounces); ref_render_s is that real measured time"
        + (" (reused from cache, not re-paid)" if ref_cache_hit else "")
        + "."
    )
    # surface every arm's skip reason honestly in the note.
    skipped_bits = []
    for a in arms:
        r = arm_results.get(a, {})
        if r.get("skipped"):
            skipped_bits.append(r.get("reason", f"{a} skipped"))
    if skipped_bits:
        note += " SKIPPED: " + "; ".join(skipped_bits) + "."
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        # headline = the contender (or best real arm) so the ledger has top-level
        # net_speedup / quality fields like the sibling runners.
        "net_speedup": head["net_speedup"],
        "quality": head["quality"],                       # GLOBAL SSIM
        "worst_tile_ssim": head["worst_tile_ssim"],
        "p5_tile_ssim": head["p5_tile_ssim"],
        "headline_arm": headline_arm,
        "ref_render_s": round(float(ref_render_s), 4),
        "halfres_render_s": round(float(halfres_render_s), 4),
        "spp": int(spp),
        "bounces": int(bounces),
        "low_res": f"{low_w}x{low_h}",
        "full_res": f"{full_w}x{full_h}",
        "scene": scene_key,
        "requested_scene": scene_arg,
        "device": device,
        "ref_cache_hit": bool(ref_cache_hit),
        "modeled": False,
        "note": note,
        # per-arm real results (net_speedup/quality/tiles/times or skip reason).
        "arms": arm_results,
    }
    log(f"RESULT headline={headline_arm} net_speedup={metrics['net_speedup']} "
        f"quality={metrics['quality']} worst_tile={metrics['worst_tile_ssim']} "
        f"ref={ref_render_s:.2f}s halfres={halfres_render_s:.2f}s device={device}")
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
