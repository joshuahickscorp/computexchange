#!/usr/bin/env python3
"""
exp_cycles_render_prod.py — Track C (FLAGSHIP, PRODUCTION-REGIME, REAL): the decisive
"10x-lossless" experiment on a REAL production Blender/Cycles scene.

================================================================================
WHAT THIS TESTS (the thesis under the microscope)
================================================================================
Our "10x-lossless" render thesis: on a genuine production render you can reach
NEAR-LOSSLESS quality at a large speedup by combining two cheap-but-honest levers
that a real studio pipeline already uses —

  1. ADAPTIVE SAMPLING — Cycles stops sampling a pixel once its noise estimate falls
     under a threshold, so flat/converged regions get far fewer samples than the
     hard, high-variance regions (chair legs, specular highlights, contact shadows).
  2. AI DENOISE WITH GUIDE PASSES — OpenImageDenoise (or OptiX) run on the noisy
     draft, PREFILTERED by the scene's own ALBEDO and NORMAL guide passes. The guide
     passes are the single most important quality lever: without them the denoiser
     smears texture and edges; with them it preserves high-frequency detail while
     removing Monte-Carlo noise. This is the "verify/correct" step.

We measure it against a HIGH-samples ground-truth reference and report the HONEST
whole-subprocess wall-clock speedup and the REAL SSIM — GLOBAL and PER-TILE, so a
denoiser that lifts the average while blurring detail is caught by the worst tile.

Unlike the toy-primitive sibling (exp_cycles_render.py: procedural cube+sphere+monkey
at 256px, adaptive OFF), this runner renders a REAL downloaded production .blend
(classroom / bmw27) at 1080p with production bounce depth. That is the regime the
thesis actually has to survive.

================================================================================
WHAT IS REAL (honesty ledger — modeled is FALSE, always)
================================================================================
  * The scene is a REAL downloaded production .blend (Blender's own classroom / bmw27
    demo files) — genuine geometry, materials, lights, camera. Nothing procedural.
  * BOTH renders are genuine Cycles path traces of that scene, SAME Blender build,
    SAME resolution, SAME bounces, SAME device.
  * ref_render_s and draft_render_s are time.perf_counter() wall-clock around the
    WHOLE Blender subprocess — process launch + .blend load + BVH build + (adaptive)
    path trace + in-pipeline denoise + PNG encode. NOTHING is excluded. In particular
    the denoise cost is INSIDE draft_render_s because Cycles denoises in-pipeline
    before it writes the PNG.
  * SSIM (global + per-tile) is real scikit-image on the two real rendered PNGs.
  * net_speedup = ref_render_s / draft_render_s — both whole-subprocess wall-clock on
    the SAME box. No exclusions, no fabrication.
  * The reference is rendered ONCE on the same box and CACHED on disk keyed by
    (scene, resolution, ref_spp, bounces); a repeat invocation with different draft
    settings reuses that cached PNG and reports the cached ref_render_s from a sidecar
    json. The ref time is therefore always a real measured time on this same box — it
    is simply not RE-PAID on a cache hit.

  On ANY failure (download, unzip, Blender non-zero exit, missing denoiser, SSIM fail)
  we emit {"error": "..."} and exit 0 — we NEVER fabricate a number.

================================================================================
CONFIG (argv[1] JSON, all optional; defaults in parens)
================================================================================
  scene              : "classroom" (default) | "bmw27" | "junkshop" | <direct .blend/.zip URL>
  resolution         : "1920x1080" (default)   parsed WxH
  ref_spp            : 4096 (default)           ground-truth reference samples
  draft_spp          : 512  (default)           draft sample CAP (adaptive may use fewer)
  adaptive           : true (default)           Cycles adaptive sampling on the draft
  adaptive_threshold : 0.01 (default)           adaptive noise threshold
  adaptive_min_samples: 16 (default)            adaptive floor samples/pixel
  denoiser           : "oidn" (default) | "optix" | "none"   draft denoiser
  denoise_guides     : true (default)           albedo+normal prefiltered guide passes
  bounces            : 12 (default)              total; diffuse 6 / glossy 6 / transmission 12
  device             : "AUTO" (default) | "GPU" | "CPU"   prefer OPTIX->CUDA->HIP->ONEAPI->CPU
  require_gpu        : false (default)           GPU-or-DIE: run a cheap FUNCTIONAL GPU
                                                 probe first, and FAIL LOUD (never CPU
                                                 fallback) if Cycles cannot actually
                                                 trace on a GPU. Default false keeps
                                                 existing callers byte-identical.

Emits ONE json line on stdout via emit() (last line):
  {"net_speedup","quality","worst_tile_ssim","p5_tile_ssim","ref_render_s",
   "draft_render_s","ref_spp","draft_spp","adaptive","adaptive_threshold","denoiser",
   "denoise_guides","bounces","resolution","scene","device","modeled":false,"note":...}

Contract: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object; any
failure emits {"error":...} as the last stdout line and exits 0 (never hangs).
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
# Bootstrap constants — IDENTICAL Blender location/URL to the sibling render    #
# runners so a pod that already downloaded Blender for a prior rung reuses it.  #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/cycles_render_prod"
# Persistent scene + reference cache. Prefer the big /models volume (survives between
# runners in one pod session); fall back to /root if /models is absent.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")
REF_CACHE_DIR = os.path.join(_CACHE_ROOT, "ref_cache")

# Known production scenes on Blender's public demo server (VERIFIED reachable).
#   classroom.zip     -> classroom/classroom.blend        (~70 MB)
#   BMW27_2.blend.zip -> bmw27/bmw27_gpu.blend (+ _cpu)    (~6 MB)
# junkshop is NOT hosted on download.blender.org/demo/test/ as a stable direct URL
# (it lives on the Blender Studio demo-files page behind a non-stable link), so we
# cannot verify a CC0 direct URL for it here -> we fall back to classroom and say so.
SCENE_SOURCES = {
    "classroom": {
        "url": "https://download.blender.org/demo/test/classroom.zip",
        "is_zip": True,
        # top-level main .blend inside the extracted tree (NOT an asset sub-blend)
        "blend_relpaths": ["classroom/classroom.blend"],
    },
    "bmw27": {
        "url": "https://download.blender.org/demo/test/BMW27_2.blend.zip",
        "is_zip": True,
        # the zip contains bmw27/bmw27_gpu.blend and bmw27/bmw27_cpu.blend; prefer gpu.
        "blend_relpaths": ["bmw27/bmw27_gpu.blend", "bmw27/bmw27_cpu.blend"],
    },
}


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[cycles_render_prod]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs (same list as the sibling render runners) + unzip. #
# --------------------------------------------------------------------------- #
def ensure_system_libs():
    # unzip is not strictly required (we use python's zipfile) but harmless to add.
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
    """Best-effort ensure the SSIM/imaging deps exist. setup_base.sh already installs
    numpy/pillow/scikit-image; this is belt-and-suspenders for a pod that skipped it.
    Never fatal here — the real check is the import at SSIM time (which errors cleanly)."""
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
#    /root/blender/blender from a prior rung on the same pod. (Verbatim pattern  #
#    from exp_cycles_render.py.)                                                 #
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
# 3. Fetch + cache the production scene. Idempotent: a scene already downloaded #
#    and extracted is reused. Returns (blend_path, scene_key).                  #
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
    open an asset sub-blend like assets/desks/desks.blend). If none match (e.g. an
    unknown direct-URL zip), fall back to the largest .blend in the tree — the main
    scene file is essentially always the biggest.
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
    cached under SCENES_DIR and reused on repeat invocations (idempotent).
    """
    os.makedirs(SCENES_DIR, exist_ok=True)
    fallback_note = ""

    key = scene_arg.strip()
    is_url = key.startswith("http://") or key.startswith("https://")

    # junkshop has no verified stable CC0 direct URL on the demo server -> fall back.
    if key == "junkshop":
        fallback_note = (
            "requested scene 'junkshop' has no verified stable CC0 direct URL on "
            "download.blender.org/demo/test/ (it lives behind the non-stable Blender "
            "Studio demo-files page); FELL BACK to 'classroom'"
        )
        log("WARN: " + fallback_note)
        key = "classroom"

    if not is_url and key not in SCENE_SOURCES:
        raise RuntimeError(
            f"unknown scene {scene_arg!r}; expected one of "
            f"{list(SCENE_SOURCES) + ['junkshop', '<direct .blend/.zip URL>']}"
        )

    if is_url:
        # Direct URL: name the cache dir by a hash of the URL for idempotency.
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
        # direct .blend URL (no zip): download the file straight into the cache dir.
        blend = os.path.join(scene_root, "scene.blend")
        if not os.path.isfile(blend):
            _download(url, blend)

    # record the resolved main .blend + mark ready for the next invocation.
    with open(blend_ptr, "w") as f:
        f.write(blend)
    with open(ready_marker, "w") as f:
        f.write("ok\n")
    log(f"scene '{scene_key}' ready -> {blend}")
    return blend, scene_key, fallback_note


# --------------------------------------------------------------------------- #
# 4. The Blender render script (run inside Blender's own python via -P). It      #
#    OPENS the production .blend and OVERRIDES only the render controls — it     #
#    never touches the scene's geometry/lights/camera. Two modes via env:        #
#      * reference: high spp, adaptive OFF, denoise OFF.                         #
#      * draft:     draft-spp CAP, adaptive per config, denoiser + guide passes. #
#    Prints CX_CHOSEN_DEVICE + CX_RENDER_DONE sentinels like the siblings.       #
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
SPP        = int(os.environ["CX_SPP"])              # sample CAP
IS_REF     = os.environ["CX_IS_REF"] == "1"         # reference (ground truth) render?
BOUNCES    = int(os.environ["CX_BOUNCES"])          # total light bounces
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")    # AUTO | GPU | CPU
REQ_GPU    = os.environ.get("CX_REQUIRE_GPU", "0") == "1"   # GPU-or-DIE: never trace on CPU
USE_ADAPT  = os.environ.get("CX_ADAPTIVE", "0") == "1"      # draft: adaptive sampling
ADAPT_THR  = float(os.environ.get("CX_ADAPT_THR", "0.01"))
ADAPT_MIN  = int(os.environ.get("CX_ADAPT_MIN", "16"))
DENOISER   = os.environ.get("CX_DENOISER", "none")  # oidn | optix | none
GUIDES     = os.environ.get("CX_GUIDES", "0") == "1"  # albedo+normal prefiltered guides

# ---- open the REAL production scene -----------------------------------------
# open_mainfile loads the .blend's own geometry, materials, lights, camera. We only
# override render controls below; we do NOT rebuild the scene.
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

# Force the Cycles engine (the demo scenes ship as Cycles, but be explicit).
scene.render.engine = 'CYCLES'
cyc = scene.cycles

# ===== SAMPLING =============================================================
# Reference: high fixed spp, adaptive OFF, denoise OFF (unbiased ground truth).
# Draft:     spp CAP with adaptive sampling per config (fewer samples where the
#            noise estimate is already low), then the AI denoiser + guide passes.
cyc.samples = SPP
if IS_REF:
    cyc.use_adaptive_sampling = False
    cyc.use_denoising = False
else:
    cyc.use_adaptive_sampling = bool(USE_ADAPT)
    if USE_ADAPT:
        # adaptive_threshold: lower = more samples/quality; 0 lets Cycles auto-pick.
        try:
            cyc.adaptive_threshold = ADAPT_THR
        except Exception as e:
            _log("could not set adaptive_threshold:", e)
        try:
            cyc.adaptive_min_samples = ADAPT_MIN
        except Exception as e:
            _log("could not set adaptive_min_samples:", e)

# ===== DENOISER (draft only) =================================================
# The denoise runs IN-PIPELINE (before the PNG is written) so its cost is inside
# the subprocess wall-time the caller measures — no separate accounting.
denoiser_ok = True
denoiser_note = ""
if (not IS_REF) and DENOISER in ("oidn", "optix"):
    cyc.use_denoising = True
    want = 'OPENIMAGEDENOISE' if DENOISER == "oidn" else 'OPTIX'
    try:
        cyc.denoiser = want
    except Exception as e:
        # Setting an unavailable denoiser (e.g. OPTIX with no NVIDIA/OptiX) raises.
        denoiser_ok = False
        denoiser_note = f"denoiser {want} unavailable: {type(e).__name__}: {e}"
        _log(denoiser_note)
    # ---- GUIDE PASSES: the single most important quality lever --------------
    # Prefilter the denoiser with the scene's ALBEDO + NORMAL passes so it keeps
    # texture/edge detail instead of smearing it. The API differs across 4.x:
    #   * denoising_input_passes = 'RGB_ALBEDO_NORMAL' (enum on scene.cycles)
    #   * denoising_prefilter    = 'ACCURATE'          (prefilter the guides)
    # We also enable the corresponding view-layer denoising-data passes so the
    # guides are actually generated. All set defensively (names vary by version).
    if denoiser_ok and GUIDES:
        try:
            cyc.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
        except Exception as e:
            _log("could not set denoising_input_passes=RGB_ALBEDO_NORMAL:", e)
        try:
            # ACCURATE prefilters albedo+normal (best quality); OIDN benefits most.
            cyc.denoising_prefilter = 'ACCURATE'
        except Exception as e:
            _log("could not set denoising_prefilter=ACCURATE:", e)
        # view-layer level: make sure the denoising-data (albedo/normal) passes exist.
        try:
            vl = scene.view_layers[0]
            for attr in ("use_pass_denoising_data",
                         "cycles.denoising_store_passes"):
                obj = vl
                path = attr.split(".")
                for p in path[:-1]:
                    obj = getattr(obj, p, None)
                    if obj is None:
                        break
                if obj is not None and hasattr(obj, path[-1]):
                    try:
                        setattr(obj, path[-1], True)
                    except Exception as e:
                        _log(f"could not set view-layer {attr}:", e)
        except Exception as e:
            _log("view-layer denoising-data pass setup failed:", e)
        _log(f"denoise guide passes requested (albedo+normal, prefilter=ACCURATE)")
elif (not IS_REF) and DENOISER == "none":
    cyc.use_denoising = False

# ===== BOUNCES (production light depth) =====================================
# Same bounces for draft AND reference so bounce depth is NOT a hidden lever —
# the only differences under test are samples + adaptive + denoise.
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ===== DEVICE LADDER: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU ===============
# (Same pattern as the sibling render runners.) OPTIX first per the task's AUTO
# preference; enabling only GPU devices avoids a slow CPU+GPU hybrid.
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

# ===== MONEY-SAFETY: never silently render on CPU for a GPU-required job ====
# The ladder above chooses the device by ENUMERATION. On a GPU whose Cycles kernel is
# missing for this Blender build (e.g. Blender 4.2 on Blackwell sm_100/sm_120) the device
# still ENUMERATES, so chosen_device would say GPU while the trace silently falls to CPU
# — the exact silent fallback that CPU-rendered a B300 pod and burned $0.58 (2026-07-09
# incident). Two defenses when CX_REQUIRE_GPU=1: (1) if no GPU enumerated at all, ERROR
# now — before the costly trace — rather than tracing on CPU; (2) CPU devices are left
# DISABLED (d.use above), so if the GPU kernel then fails to load, Cycles has NO CPU to
# fall back to and bpy.ops.render.render() ERRORS (non-zero) instead of quietly producing
# a CPU frame.
if REQ_GPU and (scene.cycles.device != 'GPU' or chosen_device.startswith("CPU")):
    print(f"CX_DEVICE_ERROR=require_gpu set but no usable Cycles GPU device "
          f"(chosen={chosen_device}); refusing CPU fallback", flush=True)
    raise SystemExit(3)

# ===== OUTPUT: full resolution, 8-bit RGB PNG, single frame =================
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT

_log(f"rendering {'REF' if IS_REF else 'DRAFT'} spp={SPP} res={RES_X}x{RES_Y} "
     f"adaptive={(not IS_REF) and USE_ADAPT} denoiser={'none' if IS_REF else DENOISER} "
     f"guides={(not IS_REF) and GUIDES} bounces={BOUNCES} device={chosen_device} -> {OUT}")
if not denoiser_ok:
    # Surface the denoiser failure on a parseable line so the caller can error cleanly
    # rather than silently rendering a NON-denoised draft and mislabeling it.
    print(f"CX_DENOISER_UNAVAILABLE={denoiser_note}", flush=True)
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, *, blend, out_png, res_x, res_y,
                       spp, is_ref, bounces, device_pref, timeout_s,
                       adaptive=False, adaptive_thr=0.01, adaptive_min=16,
                       denoiser="none", guides=False, require_gpu=False):
    """Invoke Blender headless to render ONE frame of the production scene.

    Returns (wall_seconds, chosen_device). wall_seconds is time.perf_counter() around
    the WHOLE subprocess — launch + .blend load + BVH + (adaptive) trace + in-pipeline
    denoise + PNG encode. NOTHING excluded; the denoise cost is INSIDE this time for the
    draft. Raises RuntimeError on non-zero exit, missing PNG, an unavailable denoiser, or
    (under require_gpu) a render that would have fallen back to CPU.
    """
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_png
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
    env["CX_IS_REF"] = "1" if is_ref else "0"
    env["CX_BOUNCES"] = str(bounces)
    env["CX_DEVICE"] = device_pref
    env["CX_ADAPTIVE"] = "1" if adaptive else "0"
    env["CX_ADAPT_THR"] = str(adaptive_thr)
    env["CX_ADAPT_MIN"] = str(adaptive_min)
    env["CX_DENOISER"] = denoiser
    env["CX_GUIDES"] = "1" if guides else "0"
    env["CX_REQUIRE_GPU"] = "1" if require_gpu else "0"

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    kind = "REF" if is_ref else "DRAFT"
    log(f"render start [{kind}]: spp={spp} res={res_x}x{res_y} adaptive={adaptive} "
        f"denoiser={'none' if is_ref else denoiser} guides={guides} -> {out_png}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1800:]
    err_tail = (proc.stderr or "")[-1800:]
    log(f"render [{kind}] rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    denoiser_unavail = None
    device_error = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()
        elif line.startswith("CX_DENOISER_UNAVAILABLE="):
            denoiser_unavail = line.split("=", 1)[1].strip()
        elif line.startswith("CX_DEVICE_ERROR="):
            device_error = line.split("=", 1)[1].strip()

    # HONESTY: if the requested denoiser was unavailable we FAIL — we do NOT silently
    # render a non-denoised draft and mislabel it as denoised.
    if denoiser_unavail is not None:
        raise RuntimeError(
            f"requested denoiser '{denoiser}' unavailable on this box "
            f"({denoiser_unavail}); refusing to mislabel a non-denoised draft"
        )

    # MONEY-SAFETY: the render refused to run on CPU for a GPU-required job (the scene
    # script emitted CX_DEVICE_ERROR and exited non-zero). Fail loud with the clean reason
    # instead of the generic rc!=0 message below. (This exact silent CPU fallback burned
    # $0.58 on a B300 pod, 2026-07-09.)
    if device_error is not None:
        raise RuntimeError(
            f"GPU-required render [{kind}] refused CPU fallback: {device_error}"
        )
    # Belt-and-suspenders: if this render reported a CPU (or unknown) device under
    # require_gpu, refuse it — a CPU-rendered frame must never enter a GPU benchmark.
    if require_gpu and (chosen_device.startswith("CPU") or chosen_device == "unknown"):
        raise RuntimeError(
            f"require_gpu set but render [{kind}] reported device={chosen_device!r}; "
            f"refusing a CPU-fallback benchmark (Blender Cycles kernel missing for this "
            f"GPU arch)"
        )

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [{kind}] (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_png)}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device


# Cheap FUNCTIONAL GPU probe (ported from exp_render_stack.py after the 2026-07-09
# B300 incident: a pod whose GPU ENUMERATED but had no loadable Cycles kernel silently
# CPU-rendered and burned $0.58). Enumeration alone is NOT proof — this actually TRACES
# a 64x64 @ 1spp frame with CPU devices disabled, for pennies, BEFORE the expensive
# reference render.
GPU_PROBE_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[gpu-probe]", *a, file=sys.stderr, flush=True)

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
    gpu_devs = [d for d in prefs.devices if getattr(d, "type", "CPU") != 'CPU']
    if gpu_devs:
        # GPU-ONLY: disable every CPU device so a failed kernel load CANNOT silently
        # fall back to CPU — it must error instead.
        for d in prefs.devices:
            d.use = (getattr(d, "type", "CPU") != 'CPU')
        picked = backend + '/' + gpu_devs[0].name
        break

if not picked:
    print('CX_GPU_PROBE=CPU', flush=True)
    print('CX_GPU_PROBE_ERROR=no GPU device enumerated by Cycles', flush=True)
    raise SystemExit(3)

# FUNCTIONAL check: actually TRACE a tiny frame on the GPU. Enumeration alone does NOT
# prove a loadable kernel — Blender 4.2 enumerates a Blackwell (sm_100/sm_120) GPU but
# ships no kernel for it. With CPU disabled, a missing/broken kernel makes this render
# ERROR (non-zero) instead of quietly tracing on CPU.
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'GPU'
scene.cycles.samples = 1
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
out = os.environ.get('CX_GPU_PROBE_OUT', '/tmp/cx_gpu_probe.png')
scene.render.filepath = out
try:
    bpy.ops.render.render(write_still=True)
except Exception as e:
    print('CX_GPU_PROBE=' + picked, flush=True)
    print('CX_GPU_PROBE_ERROR=GPU render raised %s: %s' % (type(e).__name__, e), flush=True)
    raise SystemExit(4)
if not os.path.isfile(out):
    print('CX_GPU_PROBE=' + picked, flush=True)
    print('CX_GPU_PROBE_ERROR=GPU render produced no output frame', flush=True)
    raise SystemExit(5)
print('CX_GPU_PROBE=' + picked, flush=True)
print('CX_GPU_PROBE_RENDERED=1', flush=True)
'''


def require_gpu_probe(blender_bin, timeout_s=300):
    """FUNCTIONAL GPU gate run BEFORE the costly reference render.

    Enumeration alone is NOT enough: Blender 4.2 enumerates a Blackwell GPU but has no
    Cycles kernel for it and would silently trace on CPU (the 2026-07-09 B300 $0.58
    incident). This probe actually renders a 64x64 @ 1spp frame with CPU devices
    DISABLED, so a missing/broken kernel errors (non-zero) and we refuse the run for
    pennies instead of paying for a CPU reference render and emitting a mislabeled
    receipt.

    timeout_s: subprocess cap for the probe (DEFAULT 300 — existing callers see no
    behavior change). On sm_90 (H100/H200) the FIRST Cycles render may driver-PTX-JIT
    the huge kernel for many minutes if the Blender tarball ships no sm_90 cubins —
    HYPOTHESIS from the 2026-07-09 two-pod evidence (H100 SECURE pods jl6jfs1968l6m8
    and mov07w7aw20xo4 both timed out at 300s while every sm_80 A100 that day passed
    in seconds). Hopper-targeting drivers pass a larger cap via the runner param
    gpu_probe_timeout_s (the integrated driver passes 1500). A probe that survives the
    JIT warms the per-user CUDA cache (~/.nv/ComputeCache on the pod filesystem, which
    nothing in this runner wipes), so later renders never re-pay it."""
    os.makedirs(WORK_DIR, exist_ok=True)
    probe_path = os.path.join(WORK_DIR, "cx_gpu_probe.py")
    out_path = os.path.join(WORK_DIR, "cx_gpu_probe.png")
    try:
        os.remove(out_path)
    except OSError:
        pass
    with open(probe_path, "w") as handle:
        handle.write(GPU_PROBE_SCRIPT)
    env = dict(os.environ, CX_GPU_PROBE_OUT=out_path)
    try:
        proc = subprocess.run(
            [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", probe_path],
            capture_output=True, text=True, timeout=timeout_s, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Cycles FUNCTIONAL GPU probe timed out after {timeout_s}s. On sm_90 "
            "(H100/H200) this can be a one-time driver PTX JIT of the Cycles kernel "
            "when the tarball lacks sm_90 cubins (HYPOTHESIS — 2026-07-09: two H100 "
            "SECURE pods timed out at 300s while every A100 passed in seconds); retry "
            "with a larger gpu_probe_timeout_s (e.g. 1500). A genuinely broken pod "
            "still fails loudly at this cap."
        )
    chosen = "CPU"
    rendered = False
    probe_error = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_GPU_PROBE_RENDERED="):
            rendered = line.split("=", 1)[1].strip() == "1"
        elif line.startswith("CX_GPU_PROBE_ERROR="):
            probe_error = line.split("=", 1)[1].strip()
        elif line.startswith("CX_GPU_PROBE="):
            chosen = line.split("=", 1)[1].strip()
    ok = (proc.returncode == 0 and rendered and chosen != "CPU"
          and os.path.isfile(out_path))
    if not ok:
        raise RuntimeError(
            "Cycles FUNCTIONAL GPU probe failed; refusing a CPU-fallback GPU benchmark "
            f"(device={chosen}, rendered={rendered}, rc={proc.returncode}, "
            f"error={probe_error}, stderr_tail={(proc.stderr or '')[-400:]}). "
            "If this GPU is Blackwell (sm_100/sm_120), Blender 4.2 has no kernel for it — "
            "use the policy ladder A100/H100/H200 (gpu-provisioning-policy: L40S/A6000 downgrades are banned)."
        )
    log(f"FUNCTIONAL GPU probe passed: {chosen} (64x64@1spp actually traced on GPU)")
    return chosen


# --------------------------------------------------------------------------- #
# 5. Quality: GLOBAL SSIM + PER-TILE SSIM on the two REAL rendered PNGs.        #
#    Per-tile catches a denoiser that lifts the global average while blurring   #
#    high-frequency detail (chair legs, specular) — we report the min tile and  #
#    the 5th-percentile tile.                                                    #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def compute_ssim_global_and_tiles(draft_png, ref_png, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim).

    GLOBAL: skimage SSIM over the whole real draft vs real reference PNG.
    PER-TILE: split into a grid x grid tile grid, SSIM per tile, then report the MIN
    tile and the 5th-percentile tile. A denoiser that smears detail in a small region
    (fine geometry / specular) drops those tiles even when the global average is high.
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    draft = _load_png_float(draft_png)
    ref = _load_png_float(ref_png)
    if draft.shape != ref.shape:
        raise RuntimeError(
            f"draft/ref shape mismatch {draft.shape} vs {ref.shape}"
        )

    global_ssim = float(ssim(ref, draft, channel_axis=-1, data_range=1.0))

    h, w = ref.shape[:2]
    # SSIM needs a window (default 7x7) — skip tiles too small to score honestly.
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
            dt = draft[y0:y1, x0:x1]
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
        # degenerate (tiny image) — fall back to the global number for both.
        worst = global_ssim
        p5 = global_ssim
    return global_ssim, worst, p5


# --------------------------------------------------------------------------- #
# 6. Reference cache: render the expensive ground truth ONCE, keyed by          #
#    (scene, resolution, ref_spp, bounces). A repeat invocation with different  #
#    draft settings reuses the cached PNG and reads ref_render_s + device from a #
#    sidecar json (so ref_render_s is still reported honestly).                 #
# --------------------------------------------------------------------------- #
def _ref_cache_key(scene_key, res_x, res_y, ref_spp, bounces):
    raw = f"{scene_key}|{res_x}x{res_y}|spp{ref_spp}|b{bounces}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return h, raw


def get_or_render_reference(blender_bin, script_path, *, blend, scene_key, res_x,
                            res_y, ref_spp, bounces, device_pref, timeout_s,
                            require_gpu=False):
    """Return (ref_png_path, ref_render_s, ref_device, cache_hit_bool).

    On a cache HIT the PNG + sidecar json exist and are reused (ref_render_s comes from
    the sidecar — it was a real measured time on this same box, just not RE-PAID). On a
    MISS we render it once, time the whole subprocess, and persist PNG + sidecar.
    """
    os.makedirs(REF_CACHE_DIR, exist_ok=True)
    h, raw = _ref_cache_key(scene_key, res_x, res_y, ref_spp, bounces)
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

    log(f"REFERENCE cache MISS [{raw}] -> rendering ground truth once")
    ref_s, ref_dev = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=ref_png,
        res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True, bounces=bounces,
        device_pref=device_pref, timeout_s=timeout_s, require_gpu=require_gpu,
    )
    # persist the sidecar (the PNG was written by Blender to ref_png directly).
    try:
        with open(sidecar, "w") as f:
            json.dump({
                "ref_render_s": ref_s, "device": ref_dev, "key": raw,
                "scene": scene_key, "resolution": f"{res_x}x{res_y}",
                "ref_spp": ref_spp, "bounces": bounces,
            }, f)
    except Exception as e:  # noqa: BLE001 — non-fatal; ref still usable this run
        log(f"could not write reference sidecar (non-fatal): {e}")
    return ref_png, ref_s, ref_dev, False


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    scene_arg = str(params.get("scene", "classroom"))
    resolution = str(params.get("resolution", "1920x1080"))
    ref_spp = int(params.get("ref_spp", 4096))
    draft_spp = int(params.get("draft_spp", 512))
    adaptive = bool(params.get("adaptive", True))
    adaptive_threshold = float(params.get("adaptive_threshold", 0.01))
    adaptive_min_samples = int(params.get("adaptive_min_samples", 16))
    denoiser = str(params.get("denoiser", "oidn")).lower()
    denoise_guides = bool(params.get("denoise_guides", True))
    bounces = int(params.get("bounces", 12))
    device_pref = str(params.get("device", "AUTO")).upper()
    # GPU-or-DIE (default False so existing callers are untouched): fail loud instead of
    # silently CPU-rendering — the exact fallback that burned $0.58 on a B300, 2026-07-09.
    require_gpu = bool(params.get("require_gpu", False))
    # GPU-probe subprocess cap. DEFAULT 300 so callers that don't set it see no change.
    # On sm_90 (H100/H200) the first Cycles render may PTX-JIT for many minutes if the
    # tarball lacks sm_90 cubins (hypothesis; 2026-07-09 two-pod H100 probe-timeout
    # evidence) — Hopper-targeting drivers pass a larger value (integrated driver: 1500),
    # or set CX_GPU_PROBE_TIMEOUT_S on the pod.
    gpu_probe_timeout_s = int(params.get(
        "gpu_probe_timeout_s", os.environ.get("CX_GPU_PROBE_TIMEOUT_S", 300)))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- parse + clamp ----------------------------------------------------- #
    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 1920x1080")

    if denoiser not in ("oidn", "optix", "none"):
        raise RuntimeError(f"bad denoiser {denoiser!r}; expected oidn|optix|none")

    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    bounces = max(1, bounces)
    adaptive_min_samples = max(1, min(adaptive_min_samples, draft_spp))
    adaptive_threshold = max(0.0, adaptive_threshold)

    log(f"params: scene={scene_arg} res={res_x}x{res_y} ref_spp={ref_spp} "
        f"draft_spp={draft_spp} adaptive={adaptive} thr={adaptive_threshold} "
        f"min={adaptive_min_samples} denoiser={denoiser} guides={denoise_guides} "
        f"bounces={bounces} device={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + imaging deps + bootstrap Blender ----------------- #
    ensure_system_libs()
    ensure_pydeps()
    blender_bin = ensure_blender(blender_url)
    if require_gpu:
        require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)

    # ---- 1) fetch + cache the production scene ----------------------------- #
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    # write the render script Blender runs for both renders (same build, same script)
    script_path = os.path.join(WORK_DIR, "cx_prod_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # generous subprocess timeouts: a 1080p @ 4096-spp reference is heavy on CPU.
    # sm_90 JIT interaction (2026-07-09): the FIRST GPU render on the pod pays any
    # one-time PTX JIT. Under require_gpu the probe above already absorbed it (and
    # warmed the per-user CUDA cache — ~/.nv/ComputeCache on the same pod filesystem,
    # which nothing below wipes). Even cold, the first render here is the reference
    # under ref_timeout=3600, which dwarfs the >300s JIT observed on H100; the draft
    # (1800) only runs after it, cache warm, and 1800s would still absorb the observed
    # JIT anyway (a fresh pod can't hit a ref-cache-hit-then-cold-draft corner — the
    # ref cache lives on this same pod filesystem).
    ref_timeout = 3600
    draft_timeout = 1800

    # ---- 2) REFERENCE (ground truth): cached-or-render, real whole-subprocess time
    ref_png, ref_render_s, ref_device, ref_cache_hit = get_or_render_reference(
        blender_bin, script_path, blend=blend, scene_key=scene_key,
        res_x=res_x, res_y=res_y, ref_spp=ref_spp, bounces=bounces,
        device_pref=device_pref, timeout_s=ref_timeout, require_gpu=require_gpu,
    )

    # ---- 3) DRAFT (the contender): adaptive + denoiser + guides -------------
    # In-pipeline denoise => the denoise cost is INSIDE draft_render_s.
    draft_png = os.path.join(WORK_DIR, f"draft_{scene_key}.png")
    draft_render_s, draft_device = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=draft_png,
        res_x=res_x, res_y=res_y, spp=draft_spp, is_ref=False, bounces=bounces,
        device_pref=device_pref, timeout_s=draft_timeout,
        adaptive=adaptive, adaptive_thr=adaptive_threshold,
        adaptive_min=adaptive_min_samples, denoiser=denoiser, guides=denoise_guides,
        require_gpu=require_gpu,
    )

    device = draft_device if draft_device == ref_device else f"{ref_device}|{draft_device}"
    fell_to_cpu = "CPU" in device
    # MONEY-SAFETY (final gate): under require_gpu we must NEVER emit a receipt whose
    # renders were traced on CPU (the silent fallback that burned $0.58 on a B300 pod,
    # 2026-07-09). The per-render guards in run_blender_render should have already
    # raised; this is the last line of defense before metrics leave the runner — it also
    # catches a CACHED reference that an older run rendered on CPU.
    if require_gpu and fell_to_cpu:
        raise RuntimeError(
            f"require_gpu set but render device set is {device!r} (CPU present); refusing "
            f"to emit a CPU-rendered receipt. Blender 4.2 has no Cycles kernel for "
            f"Blackwell (sm_100/sm_120); use the policy ladder A100/H100/H200 (gpu-provisioning-policy: L40S/A6000 downgrades are banned)."
        )

    # ---- 4) VERIFY: GLOBAL + PER-TILE SSIM on the two REAL PNGs -------------
    quality, worst_tile, p5_tile = compute_ssim_global_and_tiles(
        draft_png, ref_png, grid=8
    )
    log(f"SSIM global={quality:.4f} worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f}")

    # ---- 5) net_speedup = honest whole-subprocess wall-clock ratio ----------
    net_speedup = ref_render_s / max(draft_render_s, 1e-9)
    log(f"ref_render_s={ref_render_s:.3f}s draft_render_s={draft_render_s:.3f}s "
        f"-> net_speedup={net_speedup:.3f}")

    note = (
        f"REAL production Cycles render: scene='{scene_key}' ({res_x}x{res_y}, "
        f"bounces={bounces}). DRAFT = spp cap {draft_spp}"
        f"{' + adaptive sampling (thr=' + str(adaptive_threshold) + ', min=' + str(adaptive_min_samples) + ')' if adaptive else ' (adaptive OFF)'}"
        f" + {denoiser} denoiser"
        f"{' with albedo+normal prefiltered guide passes (prefilter=ACCURATE)' if (denoise_guides and denoiser != 'none') else ''}"
        f", denoise IN-PIPELINE so its cost is INSIDE draft_render_s. "
        f"REFERENCE = {ref_spp} spp, adaptive OFF, denoise OFF (ground truth). "
        f"Both renders are genuine whole-subprocess wall-clock (launch+.blend load+BVH"
        f"+trace+denoise+PNG encode, NO exclusions) on the SAME box, SAME Blender build, "
        f"SAME resolution/bounces/device. SSIM (global + per-8x8-tile min/p5) is real "
        f"scikit-image on the two real rendered PNGs. net_speedup=ref/draft wall-clock. "
        f"Reference was rendered ONCE on this box and cached by (scene,resolution,ref_spp,"
        f"bounces); ref_render_s is that real measured time"
        + (" (reused from cache this run, not re-paid)" if ref_cache_hit else "")
        + "."
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),                # GLOBAL SSIM
        "worst_tile_ssim": round(float(worst_tile), 4),
        "p5_tile_ssim": round(float(p5_tile), 4),
        "ref_render_s": round(float(ref_render_s), 4),
        "draft_render_s": round(float(draft_render_s), 4),
        "ref_spp": int(ref_spp),
        "draft_spp": int(draft_spp),
        "adaptive": bool(adaptive),
        "adaptive_threshold": float(adaptive_threshold),
        "denoiser": denoiser,
        "denoise_guides": bool(denoise_guides),
        "bounces": int(bounces),
        "resolution": f"{res_x}x{res_y}",
        "scene": scene_key,
        "device": device,
        "modeled": False,
        "note": note,
        # ---- extra real diagnostics ---------------------------------------- #
        "adaptive_min_samples": int(adaptive_min_samples),
        "ref_cache_hit": bool(ref_cache_hit),
        "requested_scene": scene_arg,
    }

    log(f"RESULT net_speedup={net_speedup:.3f} quality={quality:.4f} "
        f"worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f} "
        f"ref={ref_render_s:.2f}s draft={draft_render_s:.2f}s device={device}")
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
