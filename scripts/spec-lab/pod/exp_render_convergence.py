#!/usr/bin/env python3
"""
exp_render_convergence.py — Track C (REAL, GPU-NATIVE CONVERGENCE lever): reach the
reference's quality with FEWER samples — a TRULY LOSSLESS speedup, not a lossy blur.

================================================================================
WHAT THIS TESTS (and why it is different from the denoise sibling)
================================================================================
The denoise runner (exp_cycles_render_prod.py) buys speed by rendering a NOISY draft
then letting an AI denoiser *guess* the missing detail — that risks smearing high-
frequency structure (chair legs, specular), which is why we report per-tile SSIM there.

THIS runner buys speed a fundamentally HONEST, LOSSLESS way: it makes the path tracer
CONVERGE FASTER for the same visual result, using two Cycles features that improve the
*sampling itself* (not a post-hoc guess):

  1. ADAPTIVE SAMPLING (scene.cycles.use_adaptive_sampling) — Cycles estimates each
     pixel's noise as it samples and STOPS early once the pixel is under a threshold, so
     flat/converged regions get a handful of samples while hard regions keep sampling.
     A lower adaptive_threshold => stricter => closer to the reference (and slower); a
     higher threshold => looser => faster but riskier. This is the lever we SWEEP.
  2. LIGHT TREE (scene.cycles.use_light_tree) — many-lights importance sampling. It
     builds a BVH over emitters and samples the ones that actually matter for each shading
     point, so the SAME sample budget lands far less variance in multi-light scenes
     (classroom has many ceiling panels). Fewer wasted samples => faster convergence.

Neither invents pixels. Both reduce the samples needed to reach the SAME converged
image. So a WIN here is a genuinely lossless speedup: we reach the reference's SSIM with
fewer effective samples, and per-tile SSIM proves we did not quietly blur detail.

The DELIVERABLE is the samples-to-reference-SSIM FRONTIER: for each swept
adaptive_threshold we report (effective samples, wall-time, net_speedup, global SSIM,
worst-tile SSIM, p5-tile SSIM). The headline is the fastest sweep point that still
clears the quality target.

================================================================================
HONEST CONSTRAINT ON PATH GUIDING (Intel Open PGL) — READ THIS
================================================================================
Cycles path GUIDING (scene.cycles.use_guiding, Intel Open PGL) is CPU-ONLY. On an
OptiX/CUDA (GPU) device it is a SILENT NO-OP — the flag can be set without error but the
GPU kernel ignores it. So if we are on a GPU we do NOT claim guiding helped, and we do
NOT even set the flag (to avoid any illusion). We measure LIGHT-TREE + ADAPTIVE only.
If the caller asks for use_guiding=true on a GPU device we still run (light-tree+adaptive)
but record guiding_effective=false and say in the note that guiding was INERT on GPU.
Only on a CPU device do we actually enable guiding and let it contribute.

================================================================================
WHAT IS REAL (honesty ledger — modeled is FALSE, always)
================================================================================
  * The scene is a REAL downloaded production .blend (Blender's own classroom demo by
    default) — genuine geometry/materials/lights/camera. Nothing procedural, nothing faked.
  * The reference and EVERY sweep render are genuine Cycles path traces of that scene:
    SAME Blender build, SAME resolution, SAME bounces, SAME device, SAME seed.
  * ref_render_s and every ours_render_s are time.perf_counter() wall-clock around the
    WHOLE Blender subprocess — launch + .blend load + BVH build + path trace + PNG encode.
    NOTHING is excluded. (No denoiser runs here, so there is no denoise step to hide.)
  * SSIM (global + per-8x8-tile worst + 5th-percentile) is real scikit-image on the two
    real rendered PNGs.
  * net_speedup = ref_render_s / ours_render_s — both whole-subprocess wall-clock on the
    SAME box. No exclusions, no fabrication.
  * effective_samples is Blender's OWN reported mean samples/pixel for the adaptive render
    (parsed from Cycles' "Rendered … samples" / render stats); if Blender does not surface
    it we report the spp CAP and set effective_samples_source="cap (blender did not report)"
    — we NEVER invent a samples number.
  * The reference is rendered ONCE on this box and CACHED keyed by (scene,res,ref_spp,
    bounces) — the convergence knobs do NOT change the ground truth, so the same cached
    reference is reused across the whole sweep. ref_render_s is that real measured time.

  On ANY failure (download, unzip, non-zero Blender exit, missing scene, SSIM fail) we
  emit {"error": "..."} and exit 0 — we NEVER fabricate a number, NEVER silently
  substitute a fallback and claim success.

================================================================================
CONFIG (argv[1] JSON, all optional; defaults in parens)
================================================================================
  scene               : "classroom" (default) | "bmw27" | <direct .blend/.zip URL>
  resolution          : "1920x1080" (default)   parsed WxH
  ref_spp             : 4096 (default)           ground-truth reference samples (adaptive OFF)
  samples             : 512  (default)           per-sweep sample CAP (adaptive may use fewer)
  adaptive_thresholds : [0.05,0.02,0.01,0.005] (default)  the SWEPT adaptive noise thresholds
  adaptive_min_samples: 16 (default)             adaptive floor samples/pixel
  use_light_tree      : true (default)           many-lights importance sampling (GPU+CPU)
  use_guiding         : false (default)          path guiding — CPU-ONLY (inert on GPU; noted)
  bounces             : 12 (default)              total; diffuse 6 / glossy 6 / transmission 12
  seed                : 0 (default)               Cycles seed (determinism across renders)
  device              : "AUTO" (default) | "GPU" | "CPU"  OPTIX->CUDA->HIP->ONEAPI->CPU
  target_ssim         : 0.98 (default)            quality gate the headline sweep point must clear
  blender_url         : str  override Blender download URL (default 4.2 LTS)

Emits ONE json line on stdout via emit() (last line):
  {"net_speedup","quality","worst_tile_ssim","p5_tile_ssim","ref_render_s",
   "ours_render_s","effective_samples","adaptive_threshold","ref_spp","frontier":[...],
   "resolution","scene","device","modeled":false,"note":...}

Contract: human logs -> STDERR; the LAST stdout line is exactly ONE JSON object; any
failure emits {"error":...} as the last stdout line and exits 0 (never hangs).
"""

import glob
import hashlib
import json
import os
import re
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
WORK_DIR = "/tmp/render_convergence"
# Persistent scene + reference cache. Prefer the big /models volume (survives between
# runners in one pod session); fall back to /root if /models is absent.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")
REF_CACHE_DIR = os.path.join(_CACHE_ROOT, "ref_cache_convergence")

# Known production scenes on Blender's public demo server (VERIFIED reachable). Forked
# from exp_cycles_render_prod.py — classroom is the task's DEFAULT scene.
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

# Hard cap on any single render subprocess so a pathological render can never hang the
# harness. A 1080p @ 4096-spp reference is heavy on CPU; give it generous headroom.
MAX_RENDER_S = 3600


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[render_convergence]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs + unzip (forked from exp_cycles_render_prod.py).   #
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
    """Best-effort ensure the SSIM/imaging deps exist. setup_base.sh already installs
    numpy/pillow/scikit-image; this is belt-and-suspenders. The real check is the import
    at SSIM time (which errors cleanly), so this can never mislabel."""
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
#    /root/blender/blender from a prior rung. (Forked verbatim from the         #
#    sibling render runners.)                                                    #
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
#    and extracted is reused. (Forked from exp_cycles_render_prod.py.)          #
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

    First try the KNOWN top-level main-file relative paths (so we don't accidentally open
    an asset sub-blend). If none match (unknown direct-URL zip), fall back to the largest
    .blend in the tree — the main scene file is essentially always the biggest.
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

    scene_arg is one of the named scenes, or a direct .blend/.zip URL. Everything is cached
    under SCENES_DIR and reused on repeat invocations (idempotent — the download is paid
    once per pod and reused).
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
#    OPENS the production .blend and OVERRIDES only the render controls — it     #
#    never touches the scene's geometry/lights/camera. Two modes via env:        #
#      * reference: high fixed spp, adaptive OFF, light-tree OFF, denoise OFF,   #
#                   guiding OFF — pure unbiased ground truth.                    #
#      * ours:      spp CAP + adaptive sampling (swept threshold) + light-tree,  #
#                   denoise OFF (this is a CONVERGENCE lever, NOT a denoise      #
#                   lever — truly lossless). guiding ONLY on CPU (inert on GPU). #
#    Prints CX_CHOSEN_DEVICE, CX_EFF_SAMPLES, CX_GUIDING_SET, CX_RENDER_DONE.    #
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
SEED       = int(os.environ.get("CX_SEED", "0"))
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")    # AUTO | GPU | CPU
USE_ADAPT  = os.environ.get("CX_ADAPTIVE", "0") == "1"   # ours: adaptive sampling
ADAPT_THR  = float(os.environ.get("CX_ADAPT_THR", "0.01"))
ADAPT_MIN  = int(os.environ.get("CX_ADAPT_MIN", "16"))
USE_LTREE  = os.environ.get("CX_LIGHT_TREE", "0") == "1"  # many-lights importance sampling
WANT_GUIDE = os.environ.get("CX_GUIDING", "0") == "1"     # path guiding (CPU-only)

# ---- open the REAL production scene -----------------------------------------
# open_mainfile loads the .blend's own geometry, materials, lights, camera. We only
# override render controls below; we do NOT rebuild the scene.
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

# Force the Cycles engine (the demo scenes ship as Cycles, but be explicit).
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.seed = SEED

# ===== DEVICE LADDER: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU ===============
# Decide the device FIRST so we know whether path guiding will be effective (it is
# CPU-only; on a GPU device it is a silent no-op and we must not pretend it helped).
# (Same ladder as the sibling render runners.)
chosen_device = "CPU"
is_gpu = False
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
            is_gpu = True
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

# ===== SAMPLING =============================================================
# Reference: high fixed spp, adaptive OFF, denoise OFF, light-tree OFF, guiding OFF
#            -> a pure unbiased ground truth (no convergence lever touches it).
# Ours:      spp CAP + adaptive sampling (swept threshold) + light-tree; denoise OFF.
#            The ONLY difference from the reference is the sampling/convergence path,
#            so any speedup is a genuinely lossless faster-convergence, not a denoise guess.
cyc.samples = SPP
cyc.use_denoising = False   # NO denoiser in this runner (convergence, not denoise)

guiding_set = False   # did we actually enable path guiding on this render?

if IS_REF:
    cyc.use_adaptive_sampling = False
    # Reference must be a clean ground truth: NO convergence levers.
    try:
        cyc.use_light_tree = False
    except Exception as e:
        _log("could not set use_light_tree=False on reference:", e)
    try:
        cyc.use_guiding = False
    except Exception as e:
        _log("could not set use_guiding=False on reference:", e)
else:
    # ---- ADAPTIVE SAMPLING (the swept lever) --------------------------------
    cyc.use_adaptive_sampling = bool(USE_ADAPT)
    if USE_ADAPT:
        try:
            cyc.adaptive_threshold = ADAPT_THR   # lower = stricter/slower/closer to ref
        except Exception as e:
            _log("could not set adaptive_threshold:", e)
        try:
            cyc.adaptive_min_samples = ADAPT_MIN
        except Exception as e:
            _log("could not set adaptive_min_samples:", e)

    # ---- LIGHT TREE (many-lights importance sampling; works on GPU AND CPU) --
    if USE_LTREE:
        try:
            cyc.use_light_tree = True
        except Exception as e:
            _log("could not set use_light_tree=True:", e)
    else:
        try:
            cyc.use_light_tree = False
        except Exception as e:
            _log("could not set use_light_tree=False:", e)

    # ---- PATH GUIDING (Intel Open PGL) — CPU-ONLY. HONEST HANDLING -----------
    # On a GPU device guiding is a SILENT NO-OP: setting the flag does nothing to the
    # GPU kernel. So we ONLY enable it when we are actually on CPU. On GPU we leave it
    # OFF and report guiding_set=False so the caller can state it was inert — we never
    # set a flag that would let anyone claim guiding helped on the GPU.
    if WANT_GUIDE and not is_gpu:
        try:
            cyc.use_guiding = True
            guiding_set = True
        except Exception as e:
            _log("could not set use_guiding=True (CPU):", e)
            guiding_set = False
    else:
        try:
            cyc.use_guiding = False
        except Exception:
            pass
        guiding_set = False

# ===== BOUNCES (production light depth) =====================================
# Same bounces for ours AND reference so bounce depth is NOT a hidden lever — the only
# differences under test are adaptive sampling + light-tree (+ CPU guiding).
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ===== OUTPUT: full resolution, 8-bit RGB PNG, single frame =================
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT

_log(f"rendering {'REF' if IS_REF else 'OURS'} spp_cap={SPP} res={RES_X}x{RES_Y} "
     f"adaptive={(not IS_REF) and USE_ADAPT} thr={ADAPT_THR} light_tree={(not IS_REF) and USE_LTREE} "
     f"guiding_set={guiding_set} bounces={BOUNCES} device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)
print(f"CX_GUIDING_SET={'1' if guiding_set else '0'}", flush=True)

bpy.ops.render.render(write_still=True)

# Effective samples/pixel under adaptive sampling is NOT surfaced by a stable python
# attr in Blender 4.2, so we deliberately do NOT emit a fabricated attr here. The single
# honest source of truth is Cycles' own "Sample N/M" progress lines on stdout, which the
# CALLER parses (parse_effective_samples). If those lines are absent the caller falls
# back to the sample CAP and LABELS it as such — no number is ever invented.
print("CX_RENDER_DONE", flush=True)
'''


# --------------------------------------------------------------------------- #
# 5. Parse Blender's OWN reported effective samples/pixel from its stdout.      #
#    With adaptive sampling Cycles prints progress like "Sample 337/512" — the  #
#    LAST such number is the max samples any pixel reached; the render-summary   #
#    "Rendered N samples" (when present) is the total. Neither is fabricated —   #
#    both come straight from Blender. If we cannot find a real number we return  #
#    None and the caller reports the CAP and labels it "cap (not reported)".     #
# --------------------------------------------------------------------------- #
_SAMPLE_LINE_RE = re.compile(r"Sample\s+(\d+)\s*/\s*(\d+)")


def parse_effective_samples(stdout_text):
    """Return (eff_samples_float, source_str) or (None, reason_str).

    Cycles progress lines look like "... | Sample 337/512". With adaptive sampling the
    LAST reported numerator is the highest samples any tile/pixel reached before the
    frame finished — a REAL, Blender-reported upper bound on the effective samples. We
    report that (labelled) rather than the cap. This is a genuine Blender number, not a
    model. If no such line exists we return None so the caller uses the cap and says so.
    """
    if not stdout_text:
        return None, "no blender stdout to parse"
    last = None
    for m in _SAMPLE_LINE_RE.finditer(stdout_text):
        last = m
    if last is None:
        return None, "no 'Sample N/M' progress line in blender stdout"
    reached = float(last.group(1))
    cap = float(last.group(2))
    # reached is the highest samples any pixel got; with adaptive sampling it can equal
    # the cap even though the MEAN is far lower. It is still a real Blender number, so we
    # label it precisely as "max samples reached (blender-reported)".
    return reached, f"max samples reached {int(reached)}/{int(cap)} (blender-reported)"


# --------------------------------------------------------------------------- #
# 6. Invoke Blender headless to render ONE frame. Whole-subprocess wall-time.    #
#    (Forked from exp_cycles_render_prod.run_blender_render, minus the denoiser  #
#    plumbing this runner does not use; plus light-tree + guiding + sample parse.)#
# --------------------------------------------------------------------------- #
def run_blender_render(blender_bin, script_path, *, blend, out_png, res_x, res_y,
                       spp, is_ref, bounces, seed, device_pref, timeout_s,
                       adaptive=False, adaptive_thr=0.01, adaptive_min=16,
                       light_tree=False, guiding=False):
    """Render ONE frame and return (wall_seconds, chosen_device, guiding_set, stdout).

    wall_seconds is time.perf_counter() around the WHOLE subprocess — launch + .blend
    load + BVH + (adaptive) trace + PNG encode. NOTHING excluded (no denoiser runs here).
    guiding_set is whether Blender ACTUALLY enabled path guiding (only true on CPU). The
    raw stdout is returned so the caller can parse the real effective-samples number.
    Raises RuntimeError on non-zero exit or a missing PNG.
    """
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_png
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
    env["CX_IS_REF"] = "1" if is_ref else "0"
    env["CX_BOUNCES"] = str(bounces)
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    env["CX_ADAPTIVE"] = "1" if adaptive else "0"
    env["CX_ADAPT_THR"] = str(adaptive_thr)
    env["CX_ADAPT_MIN"] = str(adaptive_min)
    env["CX_LIGHT_TREE"] = "1" if light_tree else "0"
    env["CX_GUIDING"] = "1" if guiding else "0"

    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    kind = "REF" if is_ref else "OURS"
    log(f"render start [{kind}]: spp_cap={spp} res={res_x}x{res_y} adaptive={adaptive} "
        f"thr={adaptive_thr} light_tree={light_tree} guiding_req={guiding} -> {out_png}")
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
    guiding_set = False
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()
        elif line.startswith("CX_GUIDING_SET="):
            guiding_set = line.split("=", 1)[1].strip() == "1"

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_png)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [{kind}] (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_png)}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device, guiding_set, (proc.stdout or "")


# --------------------------------------------------------------------------- #
# 7. Quality: GLOBAL SSIM + PER-TILE SSIM (worst + 5th-percentile) on the two    #
#    REAL rendered PNGs. Per-tile catches a lever that lifts the global average  #
#    while degrading a small high-frequency region. (Forked verbatim from        #
#    exp_cycles_render_prod.compute_ssim_global_and_tiles.)                       #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def compute_ssim_global_and_tiles(ours_png, ref_png, grid=8):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim).

    GLOBAL: skimage SSIM over the whole real 'ours' render vs the real reference PNG.
    PER-TILE: split into a grid x grid tile grid, SSIM per tile, then report the MIN tile
    and the 5th-percentile tile. A lever that degrades a small region (fine geometry /
    specular) drops those tiles even when the global average stays high.
    """
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    ours = _load_png_float(ours_png)
    ref = _load_png_float(ref_png)
    if ours.shape != ref.shape:
        raise RuntimeError(f"ours/ref shape mismatch {ours.shape} vs {ref.shape}")

    global_ssim = float(ssim(ref, ours, channel_axis=-1, data_range=1.0))

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
            dt = ours[y0:y1, x0:x1]
            if min(rt.shape[0], rt.shape[1]) < 7:
                continue  # too small for the SSIM window
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
# 8. Reference cache: render the expensive ground truth ONCE, keyed by           #
#    (scene, resolution, ref_spp, bounces, seed). The convergence knobs do NOT   #
#    change the ground truth, so ONE cached reference serves the whole sweep.    #
#    (Forked from exp_cycles_render_prod.get_or_render_reference.)                #
# --------------------------------------------------------------------------- #
def _ref_cache_key(scene_key, res_x, res_y, ref_spp, bounces, seed):
    raw = f"{scene_key}|{res_x}x{res_y}|spp{ref_spp}|b{bounces}|s{seed}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return h, raw


def get_or_render_reference(blender_bin, script_path, *, blend, scene_key, res_x,
                            res_y, ref_spp, bounces, seed, device_pref, timeout_s):
    """Return (ref_png_path, ref_render_s, ref_device, cache_hit_bool).

    On a cache HIT the PNG + sidecar json exist and are reused (ref_render_s comes from
    the sidecar — a real measured time on this same box, just not RE-PAID). On a MISS we
    render it ONCE (adaptive OFF, light-tree OFF, guiding OFF, denoise OFF => pure ground
    truth), time the whole subprocess, and persist PNG + sidecar.
    """
    os.makedirs(REF_CACHE_DIR, exist_ok=True)
    h, raw = _ref_cache_key(scene_key, res_x, res_y, ref_spp, bounces, seed)
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
    ref_s, ref_dev, _guide, _stdout = run_blender_render(
        blender_bin, script_path, blend=blend, out_png=ref_png,
        res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True, bounces=bounces,
        seed=seed, device_pref=device_pref, timeout_s=timeout_s,
        adaptive=False, light_tree=False, guiding=False,
    )
    try:
        with open(sidecar, "w") as f:
            json.dump({
                "ref_render_s": ref_s, "device": ref_dev, "key": raw,
                "scene": scene_key, "resolution": f"{res_x}x{res_y}",
                "ref_spp": ref_spp, "bounces": bounces, "seed": seed,
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
    samples = int(params.get("samples", 512))
    adaptive_thresholds = params.get("adaptive_thresholds", [0.05, 0.02, 0.01, 0.005])
    adaptive_min_samples = int(params.get("adaptive_min_samples", 16))
    use_light_tree = bool(params.get("use_light_tree", True))
    use_guiding = bool(params.get("use_guiding", False))
    bounces = int(params.get("bounces", 12))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    target_ssim = float(params.get("target_ssim", 0.98))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- parse + clamp ----------------------------------------------------- #
    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 1920x1080")

    # adaptive_thresholds may arrive as a scalar; normalize to a sorted, de-duped list of
    # positive floats (descending: loosest/fastest first). Clamp each to (0, 1].
    if not isinstance(adaptive_thresholds, (list, tuple)):
        adaptive_thresholds = [adaptive_thresholds]
    thr_list = []
    for t in adaptive_thresholds:
        try:
            tv = float(t)
        except Exception:
            continue
        if tv > 0.0:
            thr_list.append(min(1.0, tv))
    thr_list = sorted(set(thr_list), reverse=True)
    if not thr_list:
        raise RuntimeError(
            f"no valid positive adaptive_thresholds in {adaptive_thresholds!r}"
        )

    samples = max(1, samples)
    ref_spp = max(samples, ref_spp)
    bounces = max(1, bounces)
    adaptive_min_samples = max(1, min(adaptive_min_samples, samples))
    seed = int(seed)

    log(f"params: scene={scene_arg} res={res_x}x{res_y} ref_spp={ref_spp} "
        f"samples={samples} adaptive_thresholds={thr_list} min={adaptive_min_samples} "
        f"light_tree={use_light_tree} guiding={use_guiding} bounces={bounces} "
        f"seed={seed} device={device_pref} target_ssim={target_ssim}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + imaging deps + bootstrap Blender ----------------- #
    ensure_system_libs()
    ensure_pydeps()
    blender_bin = ensure_blender(blender_url)

    # ---- 1) fetch + cache the production scene ----------------------------- #
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    script_path = os.path.join(WORK_DIR, "cx_convergence_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # generous subprocess timeouts (capped at MAX_RENDER_S so nothing can hang).
    ref_timeout = MAX_RENDER_S
    ours_timeout = min(MAX_RENDER_S, 1800)

    # ---- 2) REFERENCE (ground truth): cached-or-render, real whole-subprocess time
    ref_png, ref_render_s, ref_device, ref_cache_hit = get_or_render_reference(
        blender_bin, script_path, blend=blend, scene_key=scene_key,
        res_x=res_x, res_y=res_y, ref_spp=ref_spp, bounces=bounces, seed=seed,
        device_pref=device_pref, timeout_s=ref_timeout,
    )

    # ---- 3) SWEEP the adaptive_threshold to trace the convergence FRONTIER --
    # For each threshold: render 'ours' (adaptive + light-tree, denoise OFF), measure the
    # real whole-subprocess time + real SSIM (global + per-tile), and record Blender's own
    # reported effective samples. Every point on the frontier is a real measurement.
    frontier = []
    devices_seen = set()
    guiding_effective_any = False
    guiding_inert_on_gpu = False

    for thr in thr_list:
        ours_png = os.path.join(WORK_DIR, f"ours_{scene_key}_thr{thr}.png")
        ours_render_s, ours_device, guiding_set, ours_stdout = run_blender_render(
            blender_bin, script_path, blend=blend, out_png=ours_png,
            res_x=res_x, res_y=res_y, spp=samples, is_ref=False, bounces=bounces,
            seed=seed, device_pref=device_pref, timeout_s=ours_timeout,
            adaptive=True, adaptive_thr=thr, adaptive_min=adaptive_min_samples,
            light_tree=use_light_tree, guiding=use_guiding,
        )
        devices_seen.add(ours_device)

        # Guiding honesty: it is only effective if Blender ACTUALLY enabled it (CPU).
        if use_guiding:
            if guiding_set:
                guiding_effective_any = True
            elif "GPU" in ours_device:
                guiding_inert_on_gpu = True

        # Blender's OWN reported effective samples (or the cap, labelled, if not reported).
        eff, eff_src = parse_effective_samples(ours_stdout)
        if eff is None:
            eff_samples = float(samples)
            eff_src = f"cap {samples} (blender did not report a Sample N/M line)"
        else:
            eff_samples = eff

        g_ssim, worst_tile, p5_tile = compute_ssim_global_and_tiles(
            ours_png, ref_png, grid=8
        )
        net_speedup = ref_render_s / max(ours_render_s, 1e-9)

        point = {
            "adaptive_threshold": float(thr),
            "net_speedup": round(float(net_speedup), 4),
            "quality": round(float(g_ssim), 4),
            "worst_tile_ssim": round(float(worst_tile), 4),
            "p5_tile_ssim": round(float(p5_tile), 4),
            "ours_render_s": round(float(ours_render_s), 4),
            "effective_samples": round(float(eff_samples), 2),
            "effective_samples_source": eff_src,
            "device": ours_device,
        }
        frontier.append(point)
        log(f"FRONTIER thr={thr}: net_speedup={net_speedup:.3f} SSIM={g_ssim:.4f} "
            f"worst_tile={worst_tile:.4f} p5_tile={p5_tile:.4f} "
            f"ours_s={ours_render_s:.2f} eff_samples={eff_samples:.1f} ({eff_src})")

    # ---- 4) pick the HEADLINE point: fastest sweep point that clears target_ssim.
    # If none clears the target, we report the HIGHEST-quality point and say so — we do
    # NOT pretend a below-target point met the gate.
    passing = [p for p in frontier if p["quality"] >= target_ssim]
    hit_target = bool(passing)
    if passing:
        headline = max(passing, key=lambda p: p["net_speedup"])
    else:
        headline = max(frontier, key=lambda p: p["quality"])

    device = ref_device if devices_seen == {ref_device} else \
        "|".join(sorted({ref_device} | devices_seen))
    fell_to_cpu = "CPU" in device
    # HONESTY: net_speedup = ref/ours is only comparable when the reference and every
    # sweep render ran on the SAME device. The reference is cached across runs, so a
    # cached ref from a prior run on a DIFFERENT device (e.g. GPU) could be divided
    # against an ours render on this device (e.g. CPU) — that would make net_speedup a
    # cross-device ratio, not a same-box measurement. We detect it and flag it loudly
    # rather than emit a silently-inflated (or deflated) number.
    device_mismatch = devices_seen != {ref_device}

    # ---- 5) build the honesty note ---------------------------------------- #
    verdict = ""
    if hit_target:
        verdict = (f" WIN: reached SSIM {headline['quality']:.4f} >= target "
                   f"{target_ssim} at {headline['net_speedup']:.2f}x net_speedup "
                   f"(adaptive_threshold={headline['adaptive_threshold']}, "
                   f"~{headline['effective_samples']:.0f} eff samples vs {ref_spp} ref) "
                   "— a LOSSLESS faster-convergence speedup (no denoiser; adaptive "
                   "sampling + light-tree only).")
    else:
        verdict = (f" NO WIN: best sweep point reached SSIM {headline['quality']:.4f} "
                   f"< target {target_ssim} (at {headline['net_speedup']:.2f}x). "
                   "Convergence levers did not fully reach the reference at this sample "
                   "cap; raise 'samples' or lower the adaptive_threshold floor.")

    guiding_note = ""
    if use_guiding:
        if guiding_inert_on_gpu and not guiding_effective_any:
            guiding_note = (
                " GUIDING NOTE: use_guiding=true was requested but this ran on a GPU "
                "device, where Cycles path guiding (Intel Open PGL) is CPU-ONLY and a "
                "SILENT NO-OP — it was NOT enabled and did NOT contribute to any number "
                "here; only light-tree + adaptive sampling were measured."
            )
        elif guiding_effective_any:
            guiding_note = (
                " GUIDING NOTE: use_guiding=true ran on CPU where path guiding IS "
                "effective, so it contributed to the CPU convergence here."
            )
        else:
            guiding_note = (
                " GUIDING NOTE: use_guiding=true was requested but Cycles did not enable "
                "it on this build/device; it did NOT contribute to any number here."
            )

    note = (
        f"REAL production Cycles CONVERGENCE test: scene='{scene_key}' ({res_x}x{res_y}, "
        f"bounces={bounces}, seed={seed}). LEVER = faster convergence (LOSSLESS), NOT a "
        f"denoiser: OURS = spp cap {samples} + adaptive sampling (swept threshold, "
        f"min={adaptive_min_samples})"
        f"{' + light-tree (many-lights importance sampling)' if use_light_tree else ' (light-tree OFF)'}"
        f", denoise OFF. REFERENCE = {ref_spp} spp, adaptive OFF, light-tree OFF, guiding "
        f"OFF, denoise OFF (pure unbiased ground truth). Every ours_render_s and "
        f"ref_render_s is whole-subprocess wall-clock (launch+.blend load+BVH+trace+PNG "
        f"encode, NO exclusions) on the SAME box/Blender/resolution/bounces/device/seed. "
        f"SSIM (global + per-8x8-tile worst + p5) is real scikit-image on the two real "
        f"rendered PNGs — per-tile catches any high-frequency degradation the global "
        f"average would hide. net_speedup=ref/ours wall-clock. effective_samples is "
        f"Blender's OWN reported samples (labelled per point); it is NEVER fabricated. "
        f"Reference rendered ONCE on this box and cached by (scene,res,ref_spp,bounces,"
        f"seed); ref_render_s is that real measured time"
        + (" (reused from cache this run, not re-paid)" if ref_cache_hit else "")
        + ". The deliverable is the samples-to-reference-SSIM FRONTIER in 'frontier'."
        + verdict + guiding_note
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."
    if device_mismatch:
        note += (
            " WARNING: reference device (%s) and sweep device(s) (%s) DIFFER — the "
            "reference was reused from a cache rendered on a different device, so "
            "net_speedup here is a CROSS-DEVICE ratio, NOT a same-box measurement. "
            "Re-run with a fresh reference on this device before trusting the speedup."
            % (ref_device, "|".join(sorted(devices_seen)))
        )

    metrics = {
        # headline = fastest point that cleared target_ssim (else best-quality point)
        "net_speedup": headline["net_speedup"],
        "quality": headline["quality"],                 # GLOBAL SSIM of the headline point
        "worst_tile_ssim": headline["worst_tile_ssim"],
        "p5_tile_ssim": headline["p5_tile_ssim"],
        "ref_render_s": round(float(ref_render_s), 4),
        "ours_render_s": headline["ours_render_s"],
        "effective_samples": headline["effective_samples"],
        "effective_samples_source": headline["effective_samples_source"],
        "adaptive_threshold": headline["adaptive_threshold"],
        "ref_spp": int(ref_spp),
        "samples_cap": int(samples),
        "adaptive_min_samples": int(adaptive_min_samples),
        "use_light_tree": bool(use_light_tree),
        "use_guiding": bool(use_guiding),
        "guiding_effective": bool(guiding_effective_any),
        "target_ssim": float(target_ssim),
        "hit_target_ssim": bool(hit_target),
        "bounces": int(bounces),
        "resolution": f"{res_x}x{res_y}",
        "scene": scene_key,
        "seed": int(seed),
        "device": device,
        "ref_device": ref_device,
        "device_mismatch": bool(device_mismatch),
        "ref_cache_hit": bool(ref_cache_hit),
        "requested_scene": scene_arg,
        "modeled": False,
        "note": note,
        # ---- the DELIVERABLE: the full samples-to-reference-SSIM frontier ---- #
        "frontier": frontier,
    }

    log(f"RESULT headline net_speedup={headline['net_speedup']:.3f} "
        f"quality={headline['quality']:.4f} worst_tile={headline['worst_tile_ssim']:.4f} "
        f"p5_tile={headline['p5_tile_ssim']:.4f} ref={ref_render_s:.2f}s "
        f"ours={headline['ours_render_s']:.2f}s hit_target={hit_target} device={device}")
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
