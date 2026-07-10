#!/usr/bin/env python3
"""
exp_render_stack.py — THE KEYSTONE COMPOUND END-TO-END ORCHESTRATOR (REAL).
================================================================================

This is the ONE experiment that composes the WHOLE render stack on a genuine
ANIMATED production scene and emits ONE honest end-to-end ratio — NEVER a product
of per-stage speedups. It is the number that says how far the whole composition
really goes at near-lossless, and where quality compounds DOWN.

It forks, verbatim where possible, the honest patterns of three siblings:
  * exp_cycles_render_prod.py — the production Cycles config (adaptive sampling +
    OpenImageDenoise + albedo/normal prefiltered guide passes), the Classroom
    scene download/cache, and the GLOBAL + per-8x8-tile SSIM (worst + p5 tile).
  * exp_render_temporal.py    — the motion-vector reproject + OUR disocclusion mask
    and the FIXED-overhead-aware crop-render cost model (O paid in FULL + area-scaled
    trace — the one we just corrected so a crop re-render can NOT scale the fixed
    Blender/BVH cost away and inflate net_speedup).
  * exp_cycles_render.py      — the Blender self-bootstrap + whole-subprocess timing
    + device ladder (OPTIX>CUDA>HIP>ONEAPI>METAL>CPU; METAL is the Apple-Silicon
    rung used by the LOCAL fleet lane — scripts/spec-lab/run_local_metal_anchor.py).

================================================================================
THE COMPOSED PIPELINE (what is under test end-to-end)
================================================================================
Take a production scene (Classroom) and ANIMATE THE CAMERA through it — we add
camera-location + camera-yaw keyframes over N frames so there is real screen-space
motion, parallax and silhouette DISOCCLUSION (geometry uncovered as the camera moves).

  REFERENCE (ground truth):
    render EVERY one of the N frames FULLY at ref_spp (default 4096), adaptive OFF,
    denoise OFF. Sum the whole-subprocess wall-clock -> T_ref. These are the TRUE frames.

  OURS (the whole anchor stack composed):
    * render only the KEYFRAMES with the FULL anchor stack — adaptive sampling +
      OpenImageDenoise + albedo/normal prefiltered guides + the light-tree (many-light
      importance sampling) — at a low draft_spp. Real whole-subprocess wall-clock.
    * REPROJECT every non-key frame from the previous keyframe by the Cycles motion
      vectors (our own numpy backward-gather warp), and detect where reprojection
      FAILS with OUR disocclusion mask (OOB | MV-divergence | depth-discontinuity |
      fwd/bwd inconsistency, dilated).
    * RE-RENDER the disoccluded region with the SAME anchored cheap render and
      composite it over the reprojection. Everything else in the frame is ~free.
    * sum it all -> T_stack.

  net_speedup = T_ref / T_stack   (ONE measured wall-clock ratio; every keyframe is a
  real whole-subprocess render, both on the SAME box). NOT a product of stage speedups.

  quality = end-to-end SSIM of the DELIVERED frames vs the TRUE frames — GLOBAL,
  worst-8x8-tile and 5th-percentile-tile, so a lever that lifts the average while
  blurring high-frequency detail is caught by the worst tile.

================================================================================
THE ONE MODELED STEP (honesty ledger — we do NOT hide it)
================================================================================
Almost everything is a REAL measurement:
  * The animated Classroom scene, EVERY reference frame, and EVERY keyframe are REAL
    Cycles path traces on the SAME Blender build / resolution / bounces / device.
  * The motion-vector, depth and albedo/normal passes are REAL Cycles EXR output.
  * The warp, the disocclusion mask, the composite and the SSIM are OUR real numpy /
    scikit-image code on real pixels.
  * T_ref is the summed MEASURED whole-subprocess wall-clock of the N full renders.
  * T_stack charges each keyframe at its FULL MEASURED whole-subprocess wall-clock and
    each reprojected frame at its REAL measured numpy warp/mask/fill wall-time.
  * The FIXED per-render overhead O (Blender start + .blend load + BVH build, independent
    of rendered-pixel count) is MEASURED once via a tiny calibration render at the SAME
    spp/scene; the per-frame pixel-trace time P is the full keyframe time minus O.

The SINGLE modeled step: a non-key frame's disocclusion patch is NOT border-rendered in
Cycles frame-by-frame; its render cost is CHARGED as O + disocc_frac*P_key — a real crop
still pays the fixed overhead O in FULL (BVH/scene load do NOT shrink with crop area) and
traces only the disoccluded pixels (Cycles trace cost is ~linear in rendered-pixel count
at fixed spp). This is honest and, if anything, CONSERVATIVE for a resident-Blender
pipeline that would amortize O across crops — it is NOT an upper bound. Because this crop
TRACE time is DERIVED (area-scaled from a measured full trace) rather than directly
wall-clocked, we set "modeled": true and name EXACTLY this step in the note. Every OTHER
reported number — T_ref, keyframe render times, the numpy per-frame times, and ALL SSIM —
is a real measurement. On ANY failure we emit {"error": ...} and exit 0; we NEVER
fabricate a number.

QUALITY IS NOT INFLATED: the disocclusion PATCH is filled with the ANCHOR-QUALITY render
of that frame (adaptive + denoiser + guides + light-tree at draft_spp), i.e. exactly what
a crop render at the SAME anchor stack would produce — NOT the 4096-spp reference. Filling
from the reference would make the patch score SSIM~1 against the truth by construction and
inflate quality. To supply those honest patch pixels (and each frame's own real Cycles
motion/depth for the mask) PASS 2 renders EVERY frame once at anchor quality; but a non-key
frame's FULL anchor render is a QUALITY/measurement input ONLY — its full cost is NOT
charged to T_stack, only the fixed-overhead-aware CROP fraction is. So we deliberately
render more pixels than the pipeline pays for, purely to keep the delivered-frame SSIM an
honest measurement of the composited stack (never an upper bound baked from the truth).

Setting hole_fill="inpaint" or "nearest" removes the modeled step entirely (the holes
are filled by our real numpy inpaint at ~0 render cost — real measured numpy time), so
that mode is fully measured; we set "modeled": false in that case. The default is
"rerender" (highest quality) which carries the one modeled crop-trace step above.

================================================================================
CONFIG (argv[1] JSON, all optional; defaults in parens)
================================================================================
  frames              : 8    (default)   animation length (>=2)
  keyframe_every      : 4    (default)   render a fresh full keyframe every K frames
                                         (K>=frames => single keyframe reused whole shot)
  draft_spp           : 512  (default)   keyframe/anchor sample CAP (adaptive may use fewer)
  ref_spp             : 4096 (default)   ground-truth reference samples per frame
  adaptive_threshold  : 0.01 (default)   Cycles adaptive noise threshold on the anchor
  adaptive_min_samples: 16   (default)   adaptive floor samples/pixel on the anchor
  denoiser            : "oidn" (default) | "optix" | "none"   anchor denoiser
  denoise_guides      : true (default)   albedo+normal prefiltered guide passes
  light_tree          : true (default)   Cycles many-light importance sampling (light-tree)
  resolution          : "1920x1080" (default)  parsed WxH
  bounces             : 12   (default)   total light bounces (SAME for ref and anchor)
  disocclusion_thresh : 0.1  (default)   round-trip MV error, as a FRACTION of the frame
                                         diagonal, above which a pixel must be re-rendered
  hole_fill           : "rerender" (default) | "inpaint" | "nearest"
                                         rerender = drop true anchored-render pixels into
                                         the disoccluded patch (one modeled crop-trace step,
                                         highest quality). inpaint/nearest = fill by numpy
                                         (0 render cost, fully measured, lower quality).
  cam_motion          : 1.0  (default)   scalar on the camera dolly/pan/yaw per frame
  seed                : 0    (default)   Cycles seed
  device              : "AUTO" (default) | "GPU" | "CPU"
  scene               : "classroom" (default) | "bmw27" | <direct .blend/.zip URL>
  blender_url         : override the Blender download URL (real 4.x LTS)

  ---- REPAIR LOOP (PASS 3.5, default OFF — see docs/research/RENDER_REPAIR_LOOP_DESIGN.md)
  repair_enabled          : false (default)  master switch for the reference-free
                                             worst-tile repair pass. OFF => the runner
                                             behaves and reports EXACTLY as before
                                             (legacy metrics byte-identical).
  repair_selector         : "two_draft" (default) | "aov_edge" — how the worst tiles are
                                             CHOSEN (both reference-free; neither reads
                                             the reference frames).
                                             * two_draft: render a SECOND cheap RAW draft
                                               per frame at a different seed; tiles where
                                               the two independent estimates diverge most
                                               are the highest-VARIANCE tiles (Noise2Noise
                                               insight; the noisy_a/noisy_b recipe of
                                               exp_mint_denoise_pairs.py).
                                             * aov_edge: score each grading tile by the
                                               NORMAL-AOV edge density of the anchor render
                                               (the exact S4 signal exp_multi_selector_probe
                                               validated — localizes the SHARED-DENOISER-BIAS
                                               edge tiles that two_draft's variance signal is
                                               blind to). The Normal AOV rides in the anchor
                                               EXR at ~zero cost, so aov_edge renders NO
                                               selection draft (cheaper than two_draft).
  repair_denoiser         : "inherit" (default) | "none" — the denoiser on the REPAIR
                                             re-render. inherit = the SAME anchor stack
                                             (adaptive + OIDN + guides) as before. none =
                                             render the selected tiles RAW at repair_spp
                                             (adaptive OFF, denoiser OFF, no guides) —
                                             matching the reference's raw config so the
                                             OIDN edge-blur bias is REMOVED on exactly the
                                             few tiles it was hurting. Same feathered
                                             composite + same honest wall-clock accounting
                                             (the raw render time is charged into T_stack
                                             exactly like the OIDN repair path).
  repair_top_k            : 12   (default)   GLOBAL shot-wide budget of tiles to repair
                                             (ranked by divergence across all frames).
  repair_max_per_frame    : 8    (default)   per-frame cap inside the global budget.
  repair_min_divergence   : 0.0  (default)   optional divergence floor (0 = rank-only;
                                             zero-divergence tiles are never selected).
  repair_spp_multiplier   : 4.0  (default)   repair sample CAP = multiplier * draft_spp
                                             (when repair_spp == 0).
  repair_spp              : 0    (default)   explicit repair sample CAP (0 => multiplier).
  repair_adaptive_threshold: 0.0 (default)   adaptive noise threshold on the REPAIR
                                             render (0 => adaptive_threshold / 2 — the
                                             halving is load-bearing: cap-only increases
                                             can no-op on threshold-bound tiles).
  selection_draft_spp     : 64   (default)   spp of the RAW selection draft B (adaptive
                                             OFF, denoiser OFF — raw MC).
  selection_seed_offset   : 7919 (default)   seed offset of draft B vs the anchor seed.
  repair_seed_offset      : 0    (default)   seed offset of the repair render (0 = keep
                                             the anchor seed; feather makes noise-field
                                             continuity moot).
  repair_margin_px        : 16   (default)   margin rendered AROUND each selected tile.
  repair_feather_px       : 12   (default)   outward linear feather across the OUTER
                                             feather px of the margin (clamped <= margin).
  Every repair second (selection drafts + divergence scoring + bordered repair renders +
  feathered compositing) is REAL measured wall-clock charged into T_stack. The repair
  pass adds NO modeled term: in kf=1 all-anchor mode "modeled" stays false.

OUTPUT (last stdout line = exactly ONE JSON metrics object):
  {"net_speedup","quality","worst_tile_ssim","p5_tile_ssim","frames","keyframes",
   "T_ref_s","T_stack_s","reproject_accept_frac","mean_disoccluded_frac","device",
   "modeled",...}

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
WORK_DIR = "/tmp/render_stack"
# Persistent scene cache (shared with exp_cycles_render_prod.py so a scene fetched
# by a prior rung is reused). Prefer the big /models volume; fall back to /root.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")

# Known production scenes on Blender's public demo server (VERIFIED reachable) —
# same table as exp_cycles_render_prod.py so the cache directory layout matches.
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
    print("[render_stack]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs + imaging deps (forked from the sibling runners).  #
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
    at SSIM/warp time (which errors cleanly)."""
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
# 2. Self-bootstrap Blender (VERBATIM pattern from exp_cycles_render.py).       #
#    Idempotent — reuses /root/blender/blender from a prior rung on the pod.     #
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
#    Returns (blend_path, scene_key, fallback_note). Idempotent.                 #
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
    """Locate the main .blend in an extracted scene tree (prefer known relpaths, else
    the largest .blend — the main scene file is essentially always the biggest)."""
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
    """Fetch/cache the scene and return (blend_path, scene_key, fallback_note)."""
    os.makedirs(SCENES_DIR, exist_ok=True)
    fallback_note = ""

    key = scene_arg.strip()
    is_url = key.startswith("http://") or key.startswith("https://")

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
#    OPENS the production .blend, ADDS CAMERA KEYFRAMES (a dolly + pan + yaw so   #
#    there is real screen-space motion + disocclusion), and OVERRIDES only the    #
#    render controls. It renders ONE frame per invocation to a MULTILAYER EXR     #
#    carrying Combined color + Vector (motion) + Z (depth). Two anchor modes via  #
#    env: reference (high spp, adaptive OFF, denoise OFF) or anchor (draft-spp     #
#    CAP, adaptive + denoiser + guides + light-tree). Prints CX_CHOSEN_DEVICE +    #
#    CX_RENDER_DONE (+ CX_DENOISER_UNAVAILABLE on a missing denoiser) sentinels.   #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
BLEND     = os.environ["CX_BLEND"]                  # production .blend to open
OUT       = os.environ["CX_OUT"]                    # output EXR path (multilayer)
RES_X     = int(os.environ["CX_RES_X"])
RES_Y     = int(os.environ["CX_RES_Y"])
SPP       = int(os.environ["CX_SPP"])               # sample CAP
IS_REF    = os.environ["CX_IS_REF"] == "1"          # reference (ground truth) render?
FRAME     = int(os.environ["CX_FRAME"])             # which animation frame to render
NFRAMES   = int(os.environ["CX_NFRAMES"])           # total frames in the shot
CAM_MOTION= float(os.environ.get("CX_CAM_MOTION", "1.0"))  # scalar on camera motion
SEED      = int(os.environ["CX_SEED"])
BOUNCES   = int(os.environ["CX_BOUNCES"])
DEV_PREF  = os.environ.get("CX_DEVICE", "AUTO")
REQ_GPU   = os.environ.get("CX_REQUIRE_GPU", "0") == "1"  # GPU-or-DIE: never trace on CPU
USE_ADAPT = os.environ.get("CX_ADAPTIVE", "0") == "1"
ADAPT_THR = float(os.environ.get("CX_ADAPT_THR", "0.01"))
ADAPT_MIN = int(os.environ.get("CX_ADAPT_MIN", "16"))
DENOISER  = os.environ.get("CX_DENOISER", "none")   # oidn | optix | none
GUIDES    = os.environ.get("CX_GUIDES", "0") == "1"
LIGHTTREE = os.environ.get("CX_LIGHTTREE", "0") == "1"
# MATCH_REF: render this frame with the EXACT reference recipe for the settings that
# otherwise diverge between the anchor stack and the reference. Today that is ONE lever:
# use_light_tree. The reference (IS_REF) leaves it at the SCENE DEFAULT and never forces
# it; when MATCH_REF is set (the RAW repair_denoiser='none' border render) we do the same
# so the repaired tiles are CONFIG-IDENTICAL to the reference. Default OFF -> zero change
# to every existing render.
MATCH_REF = os.environ.get("CX_MATCH_REF", "0") == "1"

# ---- open the REAL production scene -----------------------------------------
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

# ---- CAMERA ANIMATION: add camera keyframes for real screen-space motion -----
# The scene ships a still camera; we DOLLY + PAN + YAW it over the shot so frame N
# differs from N-1 by genuine parallax and silhouette disocclusion (geometry the
# camera uncovers). We keyframe the ACTIVE camera's location + yaw only — we never
# touch geometry/materials/lights. Motion per frame is small (a few % of frame) so
# reprojection is well-posed while still opening real holes at silhouettes.
cam = scene.camera
if cam is None:
    # find any camera object as a fallback
    for ob in scene.objects:
        if ob.type == 'CAMERA':
            cam = ob
            scene.camera = ob
            break
if cam is None:
    _log("no camera in scene; cannot animate — aborting")
    raise SystemExit("no camera object in scene")

scene.frame_start = 1
scene.frame_end = NFRAMES
base_loc = tuple(cam.location)
base_rot = tuple(cam.rotation_euler)
# per-frame deltas, scaled by CAM_MOTION. Units are the scene's world units; classroom
# is roomscale so ~4-6cm/frame dolly + a small yaw gives a visible but reprojectable pan.
DOLLY_PER_FRAME = 0.05 * CAM_MOTION   # +X world drift per frame
RISE_PER_FRAME  = 0.02 * CAM_MOTION   # +Z world drift per frame (a slight crane)
YAW_PER_FRAME   = math.radians(0.8) * CAM_MOTION  # camera yaw per frame
# clear any existing camera animation so OUR keyframes fully define the path
try:
    cam.animation_data_clear()
except Exception:
    pass
for t in range(NFRAMES):
    fr = t + 1
    cam.location = (base_loc[0] + DOLLY_PER_FRAME * t,
                    base_loc[1],
                    base_loc[2] + RISE_PER_FRAME * t)
    cam.rotation_euler = (base_rot[0], base_rot[1], base_rot[2] + YAW_PER_FRAME * t)
    cam.keyframe_insert(data_path="location", frame=fr)
    cam.keyframe_insert(data_path="rotation_euler", frame=fr)

# ---- Cycles engine + sampling ------------------------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
if IS_REF:
    # ground truth: high fixed spp, adaptive OFF, denoise OFF (unbiased)
    cyc.use_adaptive_sampling = False
    cyc.use_denoising = False
else:
    cyc.use_adaptive_sampling = bool(USE_ADAPT)
    if USE_ADAPT:
        try:
            cyc.adaptive_threshold = ADAPT_THR
        except Exception as e:
            _log("could not set adaptive_threshold:", e)
        try:
            cyc.adaptive_min_samples = ADAPT_MIN
        except Exception as e:
            _log("could not set adaptive_min_samples:", e)

# ---- LIGHT-TREE: many-light importance sampling (anchor stack lever) ---------
# The light-tree makes scenes with many lights converge faster per sample. Applied to
# BOTH ref and anchor would make it a hidden lever; the task's anchor stack lists it as
# an OURS lever, so we enable it ONLY on the anchor (draft) and leave the reference at
# the scene's default. Disclosed in the note. The attribute name is 'use_light_tree'.
# MATCH_REF (the RAW repair border render) also SKIPS this force so it stays config-
# identical to the reference: forcing light-tree here while the reference leaves it at
# the scene default was a one-setting mismatch that left a ~0.086 SSIM residual on the
# failing corner tiles (config-matched 4096spp renders are otherwise pixel-identical).
if (not IS_REF) and (not MATCH_REF) and LIGHTTREE:
    try:
        cyc.use_light_tree = True
        _log("light-tree (many-light importance sampling) ENABLED on anchor")
    except Exception as e:
        _log("could not enable use_light_tree:", e)
elif MATCH_REF:
    _log("MATCH_REF: use_light_tree LEFT AT SCENE DEFAULT (reference recipe, not forced)")

# ---- DENOISER (anchor only) -------------------------------------------------
# In-pipeline denoise => its cost is INSIDE the subprocess wall-time the caller measures.
denoiser_ok = True
denoiser_note = ""
if (not IS_REF) and DENOISER in ("oidn", "optix"):
    cyc.use_denoising = True
    want = 'OPENIMAGEDENOISE' if DENOISER == "oidn" else 'OPTIX'
    try:
        cyc.denoiser = want
    except Exception as e:
        denoiser_ok = False
        denoiser_note = f"denoiser {want} unavailable: {type(e).__name__}: {e}"
        _log(denoiser_note)
    if denoiser_ok and GUIDES:
        # albedo+normal prefiltered guide passes — the single most important quality lever.
        try:
            cyc.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
        except Exception as e:
            _log("could not set denoising_input_passes=RGB_ALBEDO_NORMAL:", e)
        try:
            cyc.denoising_prefilter = 'ACCURATE'
        except Exception as e:
            _log("could not set denoising_prefilter=ACCURATE:", e)
        try:
            vl0 = scene.view_layers[0]
            if hasattr(vl0, "use_pass_denoising_data"):
                vl0.use_pass_denoising_data = True
        except Exception as e:
            _log("view-layer denoising-data pass setup failed:", e)
        _log("denoise guide passes requested (albedo+normal, prefilter=ACCURATE)")
elif (not IS_REF) and DENOISER == "none":
    cyc.use_denoising = False

# ---- BOUNCES: SAME for ref AND anchor so bounce depth is not a hidden lever ---
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ---- device ladder: OPTIX -> CUDA -> HIP -> ONEAPI -> METAL, else CPU --------
# METAL is LAST so CUDA-family pods see zero behavior change: on Linux builds the
# enum does not contain 'METAL', compute_device_type raises, and the guarded
# 'continue' skips it. On macOS builds the enum is ('NONE','METAL') so the four
# CUDA-family rungs each raise/continue and METAL is the one that lands.
chosen_device = "CPU"
if DEV_PREF in ("AUTO", "GPU"):
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        picked = None
        for backend in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
            try:
                prefs.compute_device_type = backend
            except Exception:
                continue
            try:
                prefs.get_devices()
            except Exception:
                pass
            if backend == 'METAL':
                # macOS headless gotcha (observed on Apple Silicon, Blender 4.2.1):
                # get_devices() alone can leave prefs.devices EMPTY for METAL;
                # get_devices_for_type('METAL') populates it. Guarded (hasattr +
                # try) so it is a no-op anywhere else; if no GPU lands anyway the
                # existing REQ_GPU fail-loud path below still fires — never a
                # silent CPU fallback.
                try:
                    if hasattr(prefs, 'get_devices_for_type'):
                        prefs.get_devices_for_type('METAL')
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

# ---- MONEY-SAFETY: never silently render on CPU for a GPU-required job -------
# The ladder above chooses the device by ENUMERATION. On a GPU whose Cycles kernel is
# missing for this Blender build (e.g. Blender 4.2 on Blackwell sm_100/sm_120) the device
# still ENUMERATES, so chosen_device would say GPU while the trace silently falls to CPU.
# Two defenses when CX_REQUIRE_GPU=1: (1) if no GPU enumerated at all, ERROR now — before
# the costly trace — rather than tracing on CPU; (2) CPU devices are left DISABLED (d.use
# above), so if the GPU kernel then fails to load, Cycles has NO CPU to fall back to and
# bpy.ops.render.render() ERRORS (non-zero) instead of quietly producing a CPU frame.
if REQ_GPU and (scene.cycles.device != 'GPU' or chosen_device.startswith("CPU")):
    print(f"CX_DEVICE_ERROR=require_gpu set but no usable Cycles GPU device "
          f"(chosen={chosen_device}); refusing CPU fallback", flush=True)
    raise SystemExit(3)

# ---- render PASSES: Combined (color), Vector (motion), Z (depth) -------------
# The Vector pass (prev.x, prev.y, next.x, next.y screen-space motion in PIXELS) and Z
# (linear depth) fall out of the path trace for ~free and feed our warp + mask.
vl = scene.view_layers[0]
vl.use_pass_vector = True
vl.use_pass_z = True
vl.use_pass_combined = True

# ---- REPAIR aov_edge selector: geometric Normal AOV (reference-free) -----------
# Enabled ONLY when the caller sets CX_WANT_NORMAL_AOV=1 (repair_selector='aov_edge').
# The Normal pass is a geometry byproduct of the SAME anchor path trace — it rides in
# the SAME anchor multilayer EXR at ~zero extra cost (NO extra render) and NEVER
# changes the Combined result, so with the flag unset this render is byte-identical to
# the legacy one. It is the exact pass the multi-selector probe validated S4 against.
# Guarded (try) so a build that cannot produce it simply omits the channel and the
# caller reports the normal AOV missing HONESTLY (never fabricated).
if os.environ.get("CX_WANT_NORMAL_AOV", "0") == "1":
    try:
        vl.use_pass_normal = True
        print("CX_NORMAL_AOV_PASS=1", flush=True)
    except Exception as _e:
        print("CX_NORMAL_AOV_PASS=0", flush=True)
        _log("normal AOV pass unavailable:", _e)

# CRITICAL (verified on real L40S hardware, 2026-07-06): Cycles EMPTIES the Vector
# (motion) pass whenever render motion blur is ENABLED. This is documented, intentional
# Cycles behavior (Blender T48908, "by design"; the pass is even grayed out in the UI
# when motion blur is on). The Vector pass is a FULL frame-to-frame screen-space
# displacement produced by the geometry-sync pass, NOT by the shutter interval — it does
# NOT need motion blur, and motion blur being ON is exactly what was zeroing it for this
# camera-only-ego-motion-on-static-geometry scenario (mean=std=absmax=0.0 everywhere).
# With motion blur OFF the Vector.X/Y (prev) and .Z/.W (next) channels get filled with
# real camera displacement (measured: prev.X std ~4px / absmax ~27px at 480x270, i.e.
# single-digit-to-low-tens px at 1080p). A prior comment here claimed the exact opposite;
# it was false and was the direct cause of the exactly-zero motion vectors.
scene.render.use_motion_blur = False

# ---- output: full resolution, MULTILAYER EXR (float) -------------------------
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.image_settings.color_depth = '32'
scene.render.image_settings.exr_codec = 'ZIP'
scene.render.filepath = OUT
scene.frame_set(FRAME)

_log(f"rendering {'REF' if IS_REF else 'ANCHOR'} frame={FRAME}/{NFRAMES} spp={SPP} "
     f"res={RES_X}x{RES_Y} adaptive={(not IS_REF) and USE_ADAPT} "
     f"denoiser={'none' if IS_REF else DENOISER} guides={(not IS_REF) and GUIDES} "
     f"light_tree={(not IS_REF) and (not MATCH_REF) and LIGHTTREE} match_ref={MATCH_REF} "
     f"bounces={BOUNCES} device={chosen_device} -> {OUT}")
if not denoiser_ok:
    # Surface the denoiser failure so the caller errors cleanly rather than silently
    # rendering a NON-denoised anchor and mislabeling it as denoised.
    print(f"CX_DENOISER_UNAVAILABLE={denoiser_note}", flush=True)
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

# ---- REPAIR-LOOP bordered mode (CX_BORDERS) -----------------------------------
# When CX_BORDERS is set (a JSON list of pixel rects [x0,y0,x1,y1) in NUMPY TOP-LEFT
# convention) we loop the regions IN-SESSION — .blend load + BVH build are paid ONCE
# per frame instead of once per region — rendering each with a REAL Cycles render
# border. use_crop_to_border=False keeps the EXR full-resolution (untouched pixels
# black) so the caller's numpy indexing is trivial. CORRECTNESS HAZARD, named: the
# numpy rect origin is TOP-LEFT, Blender's normalized border origin is BOTTOM-LEFT —
# the Y axis FLIPS, so border_min_y takes y1 and border_max_y takes y0. The module
# helper numpy_rect_to_blender_border() mirrors this math exactly and is unit-tested.
# Absent CX_BORDERS this script behaves exactly as before (one full-frame render).
BORDERS_ENV = os.environ.get("CX_BORDERS", "").strip()
if BORDERS_ENV:
    import json as _json
    OUT_PATTERN = os.environ["CX_OUT_PATTERN"]
    regions = _json.loads(BORDERS_ENV)
    scene.render.use_border = True
    scene.render.use_crop_to_border = False
    for i, (bx0, by0, bx1, by1) in enumerate(regions):
        scene.render.border_min_x = bx0 / RES_X
        scene.render.border_max_x = bx1 / RES_X
        scene.render.border_min_y = (RES_Y - by1) / RES_Y
        scene.render.border_max_y = (RES_Y - by0) / RES_Y
        scene.render.filepath = OUT_PATTERN.format(region=i)
        _log(f"border render region {i}: numpy rect x[{bx0},{bx1}) y[{by0},{by1}) -> "
             f"blender x[{bx0 / RES_X:.4f},{bx1 / RES_X:.4f}] "
             f"y[{(RES_Y - by1) / RES_Y:.4f},{(RES_Y - by0) / RES_Y:.4f}]")
        bpy.ops.render.render(write_still=True)
        print("CX_RENDER_DONE_REGION=%d" % i, flush=True)
    print("CX_RENDER_DONE", flush=True)
else:
    bpy.ops.render.render(write_still=True)
    print("CX_RENDER_DONE", flush=True)
'''


def numpy_rect_to_blender_border(x0, y0, x1, y1, res_x, res_y):
    """EXACT mirror of the CX_BORDERS math inside BLENDER_SCENE_SCRIPT (kept in lockstep
    by a unit test that greps the script for these formulas). Converts a half-open pixel
    rect [x0,x1) x [y0,y1) in NUMPY TOP-LEFT convention to Blender's normalized
    BOTTOM-LEFT border floats (min_x, max_x, min_y, max_y). The Y axis flips:
    min_y <- y1, max_y <- y0."""
    return (x0 / res_x, x1 / res_x, (res_y - y1) / res_y, (res_y - y0) / res_y)


def _resolve_exr_path(out_exr, frame):
    """Blender may append the frame number to the EXR path; probe the candidates."""
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


def run_blender_frame(blender_bin, script_path, *, blend, out_exr, res_x, res_y,
                      spp, is_ref, frame, nframes, cam_motion, seed, bounces,
                      device_pref, timeout_s, adaptive=False, adaptive_thr=0.01,
                      adaptive_min=16, denoiser="none", guides=False,
                      light_tree=False, match_reference=False, require_gpu=False,
                      borders=None, out_pattern=None):
    """Render ONE animation frame of the production scene to a multilayer EXR.

    Returns (wall_seconds, chosen_device, resolved_exr_path). wall_seconds is
    time.perf_counter() around the WHOLE subprocess — launch + .blend load + BVH +
    (adaptive) trace + in-pipeline denoise + EXR encode. NOTHING excluded; the denoise
    cost is INSIDE this time for the anchor. Raises on non-zero exit, missing EXR, or an
    unavailable denoiser (we refuse to mislabel a non-denoised anchor).

    REPAIR-LOOP bordered mode: pass borders=[[x0,y0,x1,y1], ...] (half-open pixel rects,
    numpy top-left convention) + out_pattern (a path containing '{region}') and ONE
    subprocess renders every region of this frame with a real Cycles render border
    (.blend load + BVH paid once per frame, not per region). The return value's third
    element is then the LIST of resolved per-region EXR paths (full-resolution EXRs;
    pixels outside each border are black). All existing call sites (borders=None) are
    unchanged."""
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_exr
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
    env["CX_IS_REF"] = "1" if is_ref else "0"
    env["CX_FRAME"] = str(frame)
    env["CX_NFRAMES"] = str(nframes)
    env["CX_CAM_MOTION"] = str(cam_motion)
    env["CX_SEED"] = str(seed)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_DEVICE"] = device_pref
    env["CX_ADAPTIVE"] = "1" if adaptive else "0"
    env["CX_ADAPT_THR"] = str(adaptive_thr)
    env["CX_ADAPT_MIN"] = str(adaptive_min)
    env["CX_DENOISER"] = denoiser
    env["CX_GUIDES"] = "1" if guides else "0"
    env["CX_LIGHTTREE"] = "1" if light_tree else "0"
    # MATCH_REF: render with the EXACT reference recipe (today: leave use_light_tree at the
    # scene default, NOT force-enabled). Set on the RAW repair_denoiser='none' border render
    # so the repaired tiles are config-identical to the reference. Default OFF for every
    # other call, so all existing renders are byte-identical.
    env["CX_MATCH_REF"] = "1" if match_reference else "0"
    env["CX_REQUIRE_GPU"] = "1" if require_gpu else "0"
    if borders is not None:
        if not borders:
            raise RuntimeError("bordered render requested with an empty region list")
        if not out_pattern or "{region}" not in out_pattern:
            raise RuntimeError("bordered render needs out_pattern containing '{region}'")
        env["CX_BORDERS"] = json.dumps(
            [[int(x0), int(y0), int(x1), int(y1)] for (x0, y0, x1, y1) in borders])
        env["CX_OUT_PATTERN"] = out_pattern

    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    kind = "REF" if is_ref else ("REPAIR" if borders is not None else "ANCHOR")
    log(f"render start [{kind}]: frame={frame} spp={spp} res={res_x}x{res_y} "
        f"adaptive={adaptive} denoiser={'none' if is_ref else denoiser} "
        f"guides={guides} light_tree={light_tree} match_ref={match_reference} "
        f"{'regions=' + str(len(borders)) + ' ' if borders is not None else ''}-> {out_exr}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1800:]
    err_tail = (proc.stderr or "")[-1800:]
    log(f"render [{kind}] frame={frame} rc={proc.returncode} wall={wall_s:.3f}s")
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

    # HONESTY: an unavailable denoiser FAILS — we never render a non-denoised anchor and
    # mislabel it as the full anchor stack.
    if denoiser_unavail is not None:
        raise RuntimeError(
            f"requested denoiser '{denoiser}' unavailable on this box "
            f"({denoiser_unavail}); refusing to mislabel a non-denoised anchor"
        )

    # MONEY-SAFETY: the render refused to run on CPU for a GPU-required job (the scene
    # script emitted CX_DEVICE_ERROR and exited non-zero). Fail loud with the clean reason
    # instead of the generic rc!=0 message below.
    if device_error is not None:
        raise RuntimeError(
            f"GPU-required render [{kind}] frame={frame} refused CPU fallback: {device_error}"
        )
    # Belt-and-suspenders: if this frame reported a CPU (or unknown) device under
    # require_gpu, refuse it — a CPU-rendered frame must never enter a GPU benchmark.
    if require_gpu and (chosen_device.startswith("CPU") or chosen_device == "unknown"):
        raise RuntimeError(
            f"require_gpu set but frame {frame} [{kind}] reported device={chosen_device!r}; "
            f"refusing a CPU-fallback benchmark (Blender Cycles kernel missing for this GPU arch)"
        )

    if borders is not None:
        resolved_list = [
            _resolve_exr_path(out_pattern.format(region=i), frame)
            for i in range(len(borders))
        ]
        ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and all(
            p is not None for p in resolved_list)
        if proc.returncode != 0 or not ok:
            missing = [i for i, p in enumerate(resolved_list) if p is None]
            raise RuntimeError(
                f"blender bordered render failed [{kind}] (frame={frame}, "
                f"rc={proc.returncode}, missing_regions={missing}); "
                f"stdout tail: {tail[-700:]}"
            )
        return wall_s, chosen_device, resolved_list

    resolved = _resolve_exr_path(out_exr, frame)
    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and (resolved is not None)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [{kind}] (frame={frame}, rc={proc.returncode}, "
            f"out_exists={resolved is not None}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device, resolved


GPU_PROBE_SCRIPT = r'''
import bpy, os, sys

def _log(*a):
    print("[gpu-probe]", *a, file=sys.stderr, flush=True)

prefs = bpy.context.preferences.addons['cycles'].preferences
picked = None
for backend in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
    try:
        prefs.compute_device_type = backend
    except Exception:
        continue
    try:
        prefs.get_devices()
    except Exception:
        pass
    if backend == 'METAL':
        # Same macOS headless gotcha as the render ladder: get_devices() alone can
        # leave prefs.devices EMPTY for METAL; get_devices_for_type('METAL')
        # populates it. Guarded no-op everywhere else; a no-GPU outcome still hits
        # the fail-loud SystemExit(3) below — never a silent CPU probe pass.
        try:
            if hasattr(prefs, 'get_devices_for_type'):
                prefs.get_devices_for_type('METAL')
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
    """FUNCTIONAL GPU gate run BEFORE the costly reference frame.

    Enumeration alone is NOT enough: Blender 4.2 enumerates a Blackwell GPU but has no
    Cycles kernel for it and would silently trace on CPU. This probe actually renders a
    64x64 @ 1spp frame with CPU devices DISABLED, so a missing/broken kernel errors
    (non-zero) and we refuse the run for pennies instead of paying for a CPU reference
    frame and emitting a mislabeled receipt.

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
# 5. EXR reader — pull Combined color + Vector (motion) + Z (depth) into numpy   #
#    (forked from exp_render_temporal.py). Prefers OpenEXR/Imath; falls back to  #
#    imageio (which loses the motion/depth passes -> mask degrades to luma-diff).#
# --------------------------------------------------------------------------- #
def read_exr_layers(path, res_x, res_y):
    """Return (color[H,W,3], motion_prev[H,W,2], depth[H,W], motion_next[H,W,2]|None)
    at (res_y,res_x). Motion vectors are in PIXELS; on a resize we scale them."""
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

        return _resize_layers(color, motion, depth, motion_next, res_x, res_y)
    except ImportError:
        pass  # fall through to imageio
    except Exception as e:  # noqa: BLE001 — try the fallback reader before giving up
        log(f"OpenEXR read failed ({type(e).__name__}: {e}); trying imageio")

    try:
        import imageio.v2 as imageio  # type: ignore
        arr = np.asarray(imageio.imread(path), dtype=np.float32)
        if arr.ndim == 3 and arr.shape[-1] >= 3:
            color = arr[..., :3]
        else:
            color = np.stack([arr] * 3, axis=-1)
        h, w = color.shape[:2]
        motion = np.zeros((h, w, 2), np.float32)
        depth = np.zeros((h, w), np.float32)
        log("imageio fallback: motion/depth passes unavailable -> luma-diff mask")
        return _resize_layers(color, motion, depth, None, res_x, res_y)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not read EXR {path}: {type(e).__name__}: {e}")


def _resize_layers(color, motion, depth, motion_next, res_x, res_y):
    """Resize every layer to (res_y,res_x); SCALE pixel-motion by the resolution ratio."""
    import numpy as np
    h, w = color.shape[:2]
    if (h, w) == (res_y, res_x):
        return color, motion, depth, motion_next

    from skimage.transform import resize as sk_resize
    sy, sx = res_y / float(h), res_x / float(w)
    color = sk_resize(color, (res_y, res_x, color.shape[-1]), order=1,
                      preserve_range=True, anti_aliasing=True).astype(np.float32)
    depth = sk_resize(depth, (res_y, res_x), order=1,
                      preserve_range=True, anti_aliasing=False).astype(np.float32)
    m = sk_resize(motion, (res_y, res_x, 2), order=1,
                  preserve_range=True, anti_aliasing=False).astype(np.float32)
    m[..., 0] *= sx
    m[..., 1] *= sy
    motion = m
    if motion_next is not None:
        mn = sk_resize(motion_next, (res_y, res_x, 2), order=1,
                       preserve_range=True, anti_aliasing=False).astype(np.float32)
        mn[..., 0] *= sx
        mn[..., 1] *= sy
        motion_next = mn
    return color, motion, depth, motion_next


# --------------------------------------------------------------------------- #
# 6. OUR warp (backward bilinear gather) + disocclusion mask + hole fills        #
#    (forked from exp_render_temporal.py). The gather is the pixels; OOB samples  #
#    feed the mask.                                                               #
# --------------------------------------------------------------------------- #
def warp_gather(src_color, motion_prev):
    """Reproject src_color into the target view by GATHERING along the target's
    prev-motion field with bilinear sampling. Returns (warped[H,W,3], valid[H,W] bool)."""
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


def disocclusion_mask(motion_prev, motion_next, depth, valid, thresh_frac):
    """Return (mask[H,W] bool, coverage dict). mask=True where the frame MUST be
    re-rendered. Four cues OR-ed: OOB gather | MV-divergence | depth-discontinuity |
    fwd/bwd round-trip inconsistency; dilated to seal the seam. thresh_frac is a fraction
    of the frame diagonal (resolution-independent)."""
    import numpy as np
    h, w = valid.shape
    diag = float(np.hypot(h, w))
    rt_thresh_px = max(0.5, thresh_frac * diag)

    mask_oob = ~valid

    mvx = motion_prev[..., 0]; mvy = motion_prev[..., 1]
    gxx, gxy = np.gradient(mvx)
    gyx, gyy = np.gradient(mvy)
    div = np.sqrt(gxx ** 2 + gxy ** 2 + gyx ** 2 + gyy ** 2)
    mask_div = div > (rt_thresh_px / max(diag, 1.0) * 4.0)

    if np.any(depth > 0):
        dz = depth.copy()
        finite = np.isfinite(dz)
        dz[~finite] = 0.0
        sel = finite & (dz > 0)
        lo, hi = np.percentile(dz[sel] if np.any(sel) else dz, [1, 99])
        span = max(hi - lo, 1e-6)
        dzn = np.clip((dz - lo) / span, 0, 1)
        gzx, gzy = np.gradient(dzn)
        zgrad = np.sqrt(gzx ** 2 + gzy ** 2)
        mask_depth = zgrad > 0.06
    else:
        mask_depth = np.zeros((h, w), bool)

    if motion_next is not None:
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        srcx = np.clip(xs + mvx, 0, w - 1)
        srcy = np.clip(ys + mvy, 0, h - 1)
        sxi = np.clip(np.round(srcx).astype(np.int32), 0, w - 1)
        syi = np.clip(np.round(srcy).astype(np.int32), 0, h - 1)
        back_x = motion_next[syi, sxi, 0]
        back_y = motion_next[syi, sxi, 1]
        rtx = srcx + back_x - xs
        rty = srcy + back_y - ys
        rt_err = np.sqrt(rtx ** 2 + rty ** 2)
        mask_consistency = rt_err > rt_thresh_px
    else:
        mask_consistency = np.zeros((h, w), bool)

    mask = mask_oob | mask_div | mask_depth | mask_consistency

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


def composite_rerender(reproj_rgb, true_rgb, mask):
    """Drop the TRUE anchored-render pixels into the disoccluded patch; keep the
    reprojection elsewhere. This is the frame our pipeline ships in 'rerender' mode."""
    import numpy as np
    out = reproj_rgb.copy()
    m = mask[..., None]
    return np.where(m, true_rgb, out).astype(np.float32)


def hole_fill_numpy(reproj_rgb, mask, method):
    """Fill the masked pixels WITHOUT a re-render (hole_fill='inpaint'|'nearest') — real
    numpy, ~0 render cost, lower quality. 'nearest' = push-pull nearest-valid flood;
    'inpaint' = cv2 Telea if present else numpy Laplacian diffusion. Returns filled RGB."""
    import numpy as np
    out = reproj_rgb.copy().astype(np.float32)
    if not mask.any():
        return out

    if method == "nearest":
        hole = mask.copy()
        valid = ~hole
        for _ in range(max(reproj_rgb.shape[:2])):
            if not hole.any():
                break
            for dyx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                shifted_valid = np.roll(valid, dyx, axis=(0, 1))
                shifted_col = np.roll(out, dyx, axis=(0, 1))
                take = hole & shifted_valid
                if take.any():
                    out[take] = shifted_col[take]
                    valid = valid | take
                    hole = hole & ~take
        return out.astype(np.float32)

    # inpaint: prefer cv2 Telea on a tonemapped 8-bit proxy, else numpy diffusion.
    try:
        import cv2  # type: ignore
        tm = np.clip(out / (1.0 + out), 0.0, 1.0)
        u8 = (tm * 255.0).astype(np.uint8)
        holes = (mask.astype(np.uint8)) * 255
        filled = cv2.inpaint(u8, holes, 3, cv2.INPAINT_TELEA).astype(np.float32) / 255.0
        t = np.clip(filled, 0.0, 0.999)
        lin = t / (1.0 - t)
        m3 = mask[..., None]
        return np.where(m3, lin, out).astype(np.float32)
    except Exception:  # noqa: BLE001 — cv2 absent/failed; numpy diffusion fill
        pass
    filled = out.copy()
    hole = mask.copy()
    for _ in range(200):
        if not hole.any():
            break
        up = np.roll(filled, 1, axis=0); dn = np.roll(filled, -1, axis=0)
        lf = np.roll(filled, 1, axis=1); rt = np.roll(filled, -1, axis=1)
        avg = (up + dn + lf + rt) / 4.0
        filled[hole] = avg[hole]
    return filled.astype(np.float32)


# --------------------------------------------------------------------------- #
# 7. Quality — GLOBAL SSIM + PER-8x8-TILE SSIM (worst + p5) on our delivered     #
#    frame vs the TRUE full render (forked from exp_cycles_render_prod.py's       #
#    per-tile scorer; tonemap the linear-HDR EXR like exp_render_temporal.py so   #
#    SSIM sees a bounded range).                                                  #
# --------------------------------------------------------------------------- #
# The grading tile grid (8x8 = 64 tiles/frame). The REPAIR loop selects/re-renders on
# THIS grid via the SAME _tile_rects(), so a repaired tile maps 1:1 onto a graded tile.
GRADING_TILE_GRID = 8


def _tone(x):
    import numpy as np
    x = np.clip(x, 0.0, None)
    return np.clip(x / (1.0 + x), 0.0, 1.0)


def _tile_rects(h, w, grid=GRADING_TILE_GRID):
    """Pixel rects of the grid x grid SSIM tiles — the SINGLE source of truth shared by
    the grading loop, the divergence scorer and the repair selector/compositor.
    Behavior-identical to the loop formerly inlined in compute_ssim_global_and_tiles
    (the last row/column absorb any remainder). Returns [(gy, gx, y0, y1, x0, x1)]."""
    ty = max(1, h // grid)
    tx = max(1, w // grid)
    rects = []
    for gy in range(grid):
        y0 = gy * ty
        y1 = h if gy == grid - 1 else (gy + 1) * ty
        for gx in range(grid):
            x0 = gx * tx
            x1 = w if gx == grid - 1 else (gx + 1) * tx
            rects.append((gy, gx, y0, y1, x0, x1))
    return rects


def compute_ssim_global_and_tiles(delivered_rgb, true_rgb, grid=GRADING_TILE_GRID):
    """Return (global_ssim, worst_tile_ssim, p5_tile_ssim) between two [H,W,3] linear-HDR
    frames. Tonemapped to [0,1] first. PER-TILE catches a lever that lifts the global
    average while blurring high-frequency detail in a small region."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    A = _tone(delivered_rgb)
    B = _tone(true_rgb)
    if A.shape != B.shape:
        raise RuntimeError(f"delivered/true shape mismatch {A.shape} vs {B.shape}")

    global_ssim = float(ssim(B, A, channel_axis=-1, data_range=1.0))

    h, w = B.shape[:2]
    tile_scores = []
    for _gy, _gx, y0, y1, x0, x1 in _tile_rects(h, w, grid):
        rt = B[y0:y1, x0:x1]
        dt = A[y0:y1, x0:x1]
        if min(rt.shape[0], rt.shape[1]) < 7:
            continue
        tile_scores.append(float(ssim(rt, dt, channel_axis=-1, data_range=1.0)))

    if tile_scores:
        arr = np.asarray(tile_scores, dtype=np.float64)
        worst = float(arr.min())
        p5 = float(np.percentile(arr, 5))
    else:
        worst = global_ssim
        p5 = global_ssim
    return global_ssim, worst, p5


def per_tile_ssim_map(delivered_rgb, true_rgb, grid=GRADING_TILE_GRID):
    """MEASUREMENT-ONLY per-tile SSIM map [grid,grid] vs the reference (NaN where a tile
    is too small to score). Used exclusively AFTER delivery, in the grading block, to
    report selector_recall / pre-vs-post repair tile scores. NEVER feeds the selector."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    A = _tone(delivered_rgb)
    B = _tone(true_rgb)
    if A.shape != B.shape:
        raise RuntimeError(f"delivered/true shape mismatch {A.shape} vs {B.shape}")
    h, w = B.shape[:2]
    out = np.full((grid, grid), np.nan, dtype=np.float64)
    for gy, gx, y0, y1, x0, x1 in _tile_rects(h, w, grid):
        rt = B[y0:y1, x0:x1]
        dt = A[y0:y1, x0:x1]
        if min(rt.shape[0], rt.shape[1]) < 7:
            continue
        out[gy, gx] = float(ssim(rt, dt, channel_axis=-1, data_range=1.0))
    return out


# --------------------------------------------------------------------------- #
# 7.5 REPAIR-LOOP pure helpers (unit-testable WITHOUT Blender).                 #
#     REFERENCE-FREEDOM IS STRUCTURAL: none of these functions accepts the      #
#     reference frames — the selector sees ONLY the delivered frame and the     #
#     independent second draft. SSIM-vs-reference stays measurement-only,        #
#     computed AFTER delivery in the grading block.                              #
# --------------------------------------------------------------------------- #
def tile_divergence_scores(delivered_rgb, second_raw_rgb, grid=GRADING_TILE_GRID):
    """REFERENCE-FREE per-tile divergence between two INDEPENDENT estimates of the same
    frame: score[gy,gx] = 1 - SSIM(tone(delivered)[tile], tone(second_raw)[tile]) on the
    GRADING grid (same _tone(), same _tile_rects()). Noise2Noise logic: delivered A
    (draft-spp + denoise) and raw B (cheap, different seed) are independent estimates of
    the same signal, so the tiles where they disagree most are the highest-variance
    tiles — precisely the ones that defeat the anchor stack. Known limitation (named in
    the receipt note): a shared denoiser BIAS identical across seeds is invisible here;
    the post-hoc measurement-only selector_recall quantifies that every run. Tiles too
    small to score get 0.0 (never selected — mirrors the grading skip)."""
    import numpy as np
    from skimage.metrics import structural_similarity as ssim

    A = _tone(delivered_rgb)
    B = _tone(second_raw_rgb)
    if A.shape != B.shape:
        raise RuntimeError(f"delivered/second-draft shape mismatch {A.shape} vs {B.shape}")
    h, w = A.shape[:2]
    scores = np.zeros((grid, grid), dtype=np.float64)
    for gy, gx, y0, y1, x0, x1 in _tile_rects(h, w, grid):
        at = A[y0:y1, x0:x1]
        bt = B[y0:y1, x0:x1]
        if min(at.shape[0], at.shape[1]) < 7:
            continue
        scores[gy, gx] = 1.0 - float(ssim(at, bt, channel_axis=-1, data_range=1.0))
    return scores


def select_repair_tiles(scores_per_frame, budget, max_per_frame, min_divergence=0.0):
    """GLOBAL-rank tile selection — a pure function of the reference-free divergence
    scores (it CANNOT read the reference: it is never passed one). Ranks every
    (frame, gy, gx) by divergence descending (deterministic tie-break: frame, gy, gx),
    keeps at most `budget` tiles shot-wide and at most `max_per_frame` per frame, and
    requires score > max(min_divergence, 0) so zero-divergence tiles are never repaired.
    Returns [(frame_idx, gy, gx, score)] in selection order."""
    floor = max(float(min_divergence), 0.0)
    candidates = []
    for f, sc in enumerate(scores_per_frame):
        for gy in range(sc.shape[0]):
            for gx in range(sc.shape[1]):
                s = float(sc[gy, gx])
                if s > floor:
                    candidates.append((f, gy, gx, s))
    candidates.sort(key=lambda c: (-c[3], c[0], c[1], c[2]))
    picked = []
    per_frame = {}
    budget = max(0, int(budget))
    max_per_frame = max(0, int(max_per_frame))
    for f, gy, gx, s in candidates:
        if len(picked) >= budget:
            break
        if per_frame.get(f, 0) >= max_per_frame:
            continue
        picked.append((f, gy, gx, s))
        per_frame[f] = per_frame.get(f, 0) + 1
    return picked


def merge_and_margin_rects(tile_rects_px, margin, w, h):
    """Group selected tiles of ONE frame whose margin-expanded rects overlap/touch into
    single regions (no interior feather ramps, no double render) and return
    [(core, border)] pairs, sorted, where core=(y0,y1,x0,x1) is the bounding box of the
    merged graded tiles and border is core expanded by `margin`, clamped to the frame —
    the region the bordered Cycles render must cover. Adjacent selected tiles union into
    one region by construction (their expanded rects overlap)."""
    margin = max(0, int(margin))

    def expand(r):
        y0, y1, x0, x1 = r
        return (max(0, y0 - margin), min(h, y1 + margin),
                max(0, x0 - margin), min(w, x1 + margin))

    def overlaps(a, b):
        return not (a[1] <= b[0] or b[1] <= a[0] or a[3] <= b[2] or b[3] <= a[2])

    cores = [tuple(int(v) for v in r) for r in tile_rects_px]
    changed = True
    while changed:
        changed = False
        out = []
        while cores:
            cur = cores.pop()
            i = 0
            while i < len(cores):
                if overlaps(expand(cur), expand(cores[i])):
                    o = cores.pop(i)
                    cur = (min(cur[0], o[0]), max(cur[1], o[1]),
                           min(cur[2], o[2]), max(cur[3], o[3]))
                    changed = True
                    i = 0
                else:
                    i += 1
            out.append(cur)
        cores = out
    return [(c, expand(c)) for c in sorted(cores)]


def build_feather_alpha(core, margin, feather, h, w):
    """Blend-weight field [h,w] float32 for ONE repair region. alpha == 1 across the
    ENTIRE graded core rect (and the innermost margin-feather px of the margin) — the
    graded tile receives PURE repair pixels, its score is the repair render's score —
    then ramps LINEARLY 1 -> 0 across the OUTER `feather` px of the margin and is 0
    beyond. Chebyshev distance to the core => alpha is continuous everywhere (max step
    1/feather per pixel), so no hard seam exists anywhere; the transition band lives in
    NEIGHBOR tiles as a convex blend of their accepted pixels with strictly-better
    (higher-spp, same stack) pixels. Every alpha>0 pixel lies inside the (clamped)
    bordered render region, so black out-of-border pixels never leak in."""
    import numpy as np
    y0, y1, x0, x1 = core
    margin = max(0, int(margin))
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = np.arange(w, dtype=np.float32)[None, :]
    dy = np.maximum(np.maximum(y0 - ys, ys - (y1 - 1)), 0.0)
    dx = np.maximum(np.maximum(x0 - xs, xs - (x1 - 1)), 0.0)
    d = np.maximum(dy, dx)  # Chebyshev distance outside the core rect (0 inside)
    if margin == 0:
        return (d <= 0.0).astype(np.float32)
    f = float(min(max(int(feather), 1), margin))
    return np.clip((margin - d) / f, 0.0, 1.0).astype(np.float32)


def feather_composite(delivered_rgb, repair_rgb, alpha):
    """delivered' = (1 - alpha) * delivered + alpha * repair, float32. alpha is the
    [h,w] field from build_feather_alpha (broadcast over RGB)."""
    a = alpha[..., None]
    return (delivered_rgb * (1.0 - a) + repair_rgb * a).astype("float32")


# --------------------------------------------------------------------------- #
# 7.6 REPAIR-LOOP aov_edge SELECTOR (repair_selector='aov_edge') — REFERENCE-FREE #
#     normal-AOV edge-density selection. The Normal AOV is a geometry byproduct of  #
#     the SAME anchor render (CX_WANT_NORMAL_AOV=1 => the Normal pass rides in the   #
#     anchor multilayer EXR at ~zero cost — NO extra render), read back here. The    #
#     scorer (normal_edge_field + _reduce_field_to_tiles) is COPIED VERBATIM from    #
#     exp_multi_selector_probe.py so this selector is byte-for-byte the S4 signal     #
#     that probe validated (S4 top-3 localized both strict-gate-failing tiles). It    #
#     reads ONLY the geometric normals — never the reference: reference-freedom is    #
#     structural, exactly like the two_draft divergence selector above.               #
# --------------------------------------------------------------------------- #
def read_anchor_normal(path, res_x, res_y):
    """Read the geometric Normal AOV [H,W,3] from the anchor multilayer EXR at
    (res_y,res_x), or None if the pass is absent (e.g. a Blender build/device that did
    not write it, or OpenEXR missing). Reference-free: this reads the ANCHOR render's
    own normals — never the reference. Same OpenEXR channel-read pattern as
    read_exr_layers / exp_multi_selector_probe.read_named_channels; prefers the geometric
    'Normal' pass (use_pass_normal, exactly what the probe validated) and falls back to
    the denoise-guide 'Denoising Normal' pass so it also works when only guides wrote a
    normal. NEVER fabricates: a missing pass returns None so the caller fails LOUD."""
    import numpy as np
    try:
        import OpenEXR  # type: ignore
        import Imath    # type: ignore
    except Exception:
        return None  # named passes need OpenEXR (imageio cannot read them) -> caller fails loud

    try:
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

        def _find_suffix(suffixes):
            for want in suffixes:
                for c in chans:
                    if c.endswith(want):
                        return c
            return None

        # geometric Normal first (".Normal.X" from use_pass_normal — the probe's S4 input);
        # "Normal.X" also matches the denoise-guide "Denoising Normal.X" as a fallback.
        nx = _find_suffix([".Normal.X", "Normal.X"])
        ny = _find_suffix([".Normal.Y", "Normal.Y"])
        nz = _find_suffix([".Normal.Z", "Normal.Z"])
        normal = None
        if nx and ny and nz:
            normal = np.stack([_chan(nx), _chan(ny), _chan(nz)], axis=-1)
        f.close()
    except Exception as e:  # noqa: BLE001 — treat any read failure as "unavailable"
        log(f"anchor normal read failed ({type(e).__name__}: {e}); aov_edge unavailable")
        return None

    if normal is None:
        return None
    if normal.shape[:2] == (res_y, res_x):
        return normal
    from skimage.transform import resize as sk_resize
    return sk_resize(normal, (res_y, res_x, normal.shape[-1]), order=1,
                     preserve_range=True, anti_aliasing=False).astype(np.float32)


def normal_edge_field(normal_rgb):
    """S4 pixel field (VERBATIM from exp_multi_selector_probe.normal_edge_field):
    gradient magnitude of the Normal AOV, summed over the three normal components —
    geometric-complexity / silhouette-edge density."""
    import numpy as np
    n = np.asarray(normal_rgb, dtype=np.float32)
    acc = None
    for c in range(n.shape[-1]):
        gy, gx = np.gradient(n[..., c])
        e = gx * gx + gy * gy
        acc = e if acc is None else acc + e
    return np.sqrt(acc).astype(np.float32)


def _reduce_field_to_tiles(field, grid=GRADING_TILE_GRID):
    """[grid,grid] per-tile MEAN of a [H,W] scalar field on the SAME grading grid
    (_tile_rects), NaN where a tile is too small to score (< 7px — mirrors the SSIM
    grading skip so the selector ranks the SAME 64-tile universe). VERBATIM from
    exp_multi_selector_probe._reduce_field_to_tiles."""
    import numpy as np
    h, w = field.shape[:2]
    out = np.full((grid, grid), np.nan, dtype=np.float64)
    for gy, gx, y0, y1, x0, x1 in _tile_rects(h, w, grid):
        tile = field[y0:y1, x0:x1]
        if min(tile.shape[0], tile.shape[1]) < 7:
            continue
        out[gy, gx] = float(np.mean(tile))
    return out


def aov_edge_tile_scores(normal_rgb, grid=GRADING_TILE_GRID):
    """REFERENCE-FREE aov_edge per-tile selector scores [grid,grid]: the S4 normal-edge
    density reduced onto the grading grid (higher = more silhouette-edge content =
    candidate failing tile). NaN (too-small) tiles map to 0.0 so downstream ranking +
    metrics stay finite and consistent with the two_draft convention (unscoreable tiles
    are never selected — 0.0 is below any positive floor)."""
    import numpy as np
    scores = _reduce_field_to_tiles(normal_edge_field(normal_rgb), grid=grid)
    return np.nan_to_num(np.asarray(scores, dtype=np.float64), nan=0.0)


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    import numpy as np  # local import so a missing numpy still yields a clean error

    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    frames = int(params.get("frames", 8))
    keyframe_every = int(params.get("keyframe_every", 4))
    draft_spp = int(params.get("draft_spp", 512))
    ref_spp = int(params.get("ref_spp", 4096))
    adaptive_threshold = float(params.get("adaptive_threshold", 0.01))
    adaptive_min_samples = int(params.get("adaptive_min_samples", 16))
    denoiser = str(params.get("denoiser", "oidn")).lower()
    denoise_guides = bool(params.get("denoise_guides", True))
    light_tree = bool(params.get("light_tree", True))
    resolution = str(params.get("resolution", "1920x1080"))
    bounces = int(params.get("bounces", 12))
    disocclusion_thresh = float(params.get("disocclusion_thresh", 0.1))
    hole_fill = str(params.get("hole_fill", "rerender")).lower()
    cam_motion = float(params.get("cam_motion", 1.0))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    require_gpu = bool(params.get("require_gpu", False))
    # GPU-probe subprocess cap. DEFAULT 300 so callers that don't set it see no change.
    # On sm_90 (H100/H200) the first Cycles render may PTX-JIT for many minutes if the
    # tarball lacks sm_90 cubins (hypothesis; 2026-07-09 two-pod H100 probe-timeout
    # evidence) — Hopper-targeting drivers pass a larger value (integrated driver: 1500),
    # or set CX_GPU_PROBE_TIMEOUT_S on the pod.
    gpu_probe_timeout_s = int(params.get(
        "gpu_probe_timeout_s", os.environ.get("CX_GPU_PROBE_TIMEOUT_S", 300)))
    scene_arg = str(params.get("scene", "classroom"))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- REPAIR-LOOP params (PASS 3.5). repair_enabled=False (the default) leaves the
    # legacy behavior AND the legacy metrics byte-identical — no repair key is even
    # emitted. Design: docs/research/RENDER_REPAIR_LOOP_DESIGN.md. ------------------- #
    repair_enabled = bool(params.get("repair_enabled", False))
    repair_selector = str(params.get("repair_selector", "two_draft"))
    repair_denoiser = str(params.get("repair_denoiser", "inherit")).lower()
    repair_top_k = int(params.get("repair_top_k", 12))
    repair_max_per_frame = int(params.get("repair_max_per_frame", 8))
    repair_min_divergence = float(params.get("repair_min_divergence", 0.0))
    repair_spp_multiplier = float(params.get("repair_spp_multiplier", 4.0))
    repair_spp = int(params.get("repair_spp", 0))  # 0 => multiplier * draft_spp
    repair_adaptive_threshold = float(params.get("repair_adaptive_threshold", 0.0))
    selection_draft_spp = int(params.get("selection_draft_spp", 64))
    selection_seed_offset = int(params.get("selection_seed_offset", 7919))
    repair_seed_offset = int(params.get("repair_seed_offset", 0))
    repair_margin_px = int(params.get("repair_margin_px", 16))
    repair_feather_px = int(params.get("repair_feather_px", 12))

    # ---- parse + clamp ----------------------------------------------------- #
    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 1920x1080")

    if denoiser not in ("oidn", "optix", "none"):
        raise RuntimeError(f"bad denoiser {denoiser!r}; expected oidn|optix|none")
    if hole_fill not in ("rerender", "inpaint", "nearest"):
        log("unknown hole_fill -> 'rerender'")
        hole_fill = "rerender"

    frames = max(2, frames)
    keyframe_every = max(1, keyframe_every)
    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    bounces = max(1, bounces)
    adaptive_min_samples = max(1, min(adaptive_min_samples, draft_spp))
    adaptive_threshold = max(0.0, adaptive_threshold)
    disocclusion_thresh = min(max(disocclusion_thresh, 1e-3), 0.9)
    cam_motion = max(0.0, cam_motion)

    # ---- REPAIR-LOOP clamps + derived knobs (validated even when disabled so a bad
    # config fails loudly before any money is spent) --------------------------------- #
    if repair_selector not in ("two_draft", "aov_edge"):
        raise RuntimeError(
            f"bad repair_selector {repair_selector!r}; supported: 'two_draft' "
            f"(two-independent-draft per-tile divergence) | 'aov_edge' (normal-AOV "
            f"edge density) — both reference-free")
    if repair_denoiser not in ("inherit", "none"):
        raise RuntimeError(
            f"bad repair_denoiser {repair_denoiser!r}; supported: 'inherit' (repair with "
            f"the anchor stack incl. OIDN) | 'none' (raw repair, denoiser OFF)")
    repair_top_k = max(0, repair_top_k)
    repair_max_per_frame = max(0, repair_max_per_frame)
    repair_min_divergence = max(0.0, repair_min_divergence)
    repair_spp_multiplier = max(1.0, repair_spp_multiplier)
    selection_draft_spp = max(1, selection_draft_spp)
    repair_margin_px = max(0, repair_margin_px)
    repair_feather_px = max(0, min(repair_feather_px, repair_margin_px))
    # effective repair spp: explicit repair_spp wins; else multiplier * draft_spp.
    repair_spp_eff = repair_spp if repair_spp > 0 else int(round(
        draft_spp * repair_spp_multiplier))
    repair_spp_eff = max(draft_spp, repair_spp_eff)
    # effective repair adaptive threshold: 0 => adaptive_threshold/2. LOAD-BEARING:
    # pixels that converged at the anchor threshold take no extra samples if only the
    # cap rises — halving the threshold guarantees real extra work on the failed tile.
    repair_adaptive_thr_eff = (repair_adaptive_threshold if repair_adaptive_threshold > 0
                               else adaptive_threshold / 2.0)

    log(f"params: scene={scene_arg} frames={frames} keyframe_every={keyframe_every} "
        f"res={res_x}x{res_y} draft_spp={draft_spp} ref_spp={ref_spp} "
        f"adaptive_thr={adaptive_threshold} adaptive_min={adaptive_min_samples} "
        f"denoiser={denoiser} guides={denoise_guides} light_tree={light_tree} "
        f"bounces={bounces} disocc_thresh={disocclusion_thresh} hole_fill={hole_fill} "
        f"cam_motion={cam_motion} seed={seed} device={device_pref}")
    if repair_enabled:
        log(f"repair: selector={repair_selector} denoiser={repair_denoiser} "
            f"top_k={repair_top_k} max_per_frame={repair_max_per_frame} "
            f"min_div={repair_min_divergence} "
            f"selection_spp={selection_draft_spp} (seed+{selection_seed_offset}) "
            f"repair_spp={repair_spp_eff} adaptive_thr={repair_adaptive_thr_eff} "
            f"margin={repair_margin_px}px feather={repair_feather_px}px")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + imaging deps + bootstrap Blender + fetch scene ---- #
    ensure_system_libs()
    ensure_pydeps()
    blender_bin = ensure_blender(blender_url)
    if require_gpu:
        require_gpu_probe(blender_bin, timeout_s=gpu_probe_timeout_s)
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    script_path = os.path.join(WORK_DIR, "cx_stack_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    # generous timeouts: a 1080p @ 4096-spp reference frame is heavy on CPU.
    # sm_90 JIT interaction (2026-07-09): the FIRST GPU render on the pod pays any
    # one-time PTX JIT. Under require_gpu the probe above already absorbed it (and
    # warmed the per-user CUDA cache — ~/.nv/ComputeCache on the same pod filesystem,
    # which nothing below wipes). Even cold, the first REAL render is always a PASS-1
    # reference frame under ref_timeout=3600, which dwarfs the >300s JIT observed on
    # H100 — anchor (1800) and calib (900) renders only ever run after it, cache warm.
    ref_timeout = 3600
    anchor_timeout = 1800
    calib_timeout = 900

    # ======================================================================== #
    # PASS 1 — REFERENCE: render EVERY frame FULLY at ref_spp (adaptive OFF,     #
    # denoise OFF). Sum the whole-subprocess wall-clock -> T_ref. Cache the TRUE #
    # frames' color for the end-to-end SSIM.                                     #
    #                                                                            #
    # CACHED: this is the expensive ~15-40min step. A repeat invocation with the  #
    # SAME (scene_key, res, ref_spp, bounces, frames, seed, cam_motion) reuses    #
    # the saved true-color frames + per-frame times instead of re-rendering —    #
    # this is what makes a SWEEP over draft-side knobs (draft_spp, adaptive_     #
    # threshold, keyframe_every, hole_fill, light_tree, denoiser) cheap after     #
    # the first trial. T_ref is the REAL SUM of the ORIGINAL per-frame render     #
    # wall-times (loaded from the cache manifest) — a cache hit reports the true  #
    # historical render cost, never a fabricated/zero cost for the reference.    #
    # ======================================================================== #
    ref_cache_key = "|".join(str(x) for x in (
        "v1", scene_key, res_x, res_y, ref_spp, bounces, frames, seed, cam_motion))
    ref_cache_hash = hashlib.sha1(ref_cache_key.encode()).hexdigest()[:16]
    ref_cache_dir = os.path.join(_CACHE_ROOT, "ref_cache", ref_cache_hash)
    ref_manifest_path = os.path.join(ref_cache_dir, "manifest.json")

    T_ref = 0.0
    ref_devices = set()
    true_colors = []  # per-frame TRUE color [H,W,3]
    per_frame_ref_s = []
    ref_cache_hit = False

    if os.path.isfile(ref_manifest_path):
        try:
            with open(ref_manifest_path) as f:
                manifest = json.load(f)
            if manifest.get("frames") == frames and manifest.get("key") == ref_cache_key:
                for t in range(frames):
                    arr = np.load(os.path.join(ref_cache_dir, f"color_{t:04d}.npy"))
                    true_colors.append(arr.astype(np.float32))
                per_frame_ref_s = [float(x) for x in manifest["per_frame_ref_s"]]
                T_ref = float(sum(per_frame_ref_s))
                ref_devices = set(manifest.get("devices", []))
                ref_cache_hit = True
                log(f"reference CACHE HIT ({ref_cache_hash}): reusing {frames} true frames, "
                    f"T_ref={T_ref:.1f}s (real historical render time, not re-timed)")
        except Exception as e:
            log(f"reference cache read failed ({e}); re-rendering")

    if not ref_cache_hit:
        for t in range(frames):
            frame_no = t + 1
            exr = os.path.join(WORK_DIR, f"ref_{frame_no:04d}.exr")
            wall_s, dev, resolved = run_blender_frame(
                blender_bin, script_path, blend=blend, out_exr=exr,
                res_x=res_x, res_y=res_y, spp=ref_spp, is_ref=True,
                frame=frame_no, nframes=frames, cam_motion=cam_motion, seed=seed,
                bounces=bounces, device_pref=device_pref, timeout_s=ref_timeout,
                require_gpu=require_gpu,
            )
            T_ref += wall_s
            per_frame_ref_s.append(wall_s)
            ref_devices.add(dev)
            color, _mp, _d, _mn = read_exr_layers(resolved, res_x, res_y)
            true_colors.append(color)

        try:
            os.makedirs(ref_cache_dir, exist_ok=True)
            for t, color in enumerate(true_colors):
                np.save(os.path.join(ref_cache_dir, f"color_{t:04d}.npy"),
                        color.astype(np.float32))
            with open(ref_manifest_path, "w") as f:
                json.dump({"key": ref_cache_key, "frames": frames,
                           "per_frame_ref_s": per_frame_ref_s,
                           "devices": sorted(ref_devices)}, f)
            log(f"reference cached ({ref_cache_hash}) for future trials")
        except Exception as e:
            log(f"reference cache WRITE failed ({e}) — continuing uncached, not fatal")

    # ======================================================================== #
    # PASS 2 — ANCHOR: render EVERY frame with the FULL anchor stack (adaptive + #
    # OIDN + albedo/normal guides + light-tree, draft_spp). This gives us:        #
    #   * for KEYFRAMES: the pixels we reproject FROM, charged to T_stack at full  #
    #     MEASURED whole-subprocess wall-clock (the real anchor keyframe cost).    #
    #   * for NON-KEY frames: an ANCHOR-QUALITY full render used ONLY to supply    #
    #     (a) the honest disocclusion-PATCH pixels (what a crop render at the SAME  #
    #     anchor stack would actually produce — NOT the 4096-spp reference, which   #
    #     would score SSIM~1 by construction and INFLATE quality) and (b) this      #
    #     frame's OWN real Cycles motion/depth for the mask. Its FULL render cost   #
    #     is deliberately NOT charged to T_stack — only the fixed-overhead-aware    #
    #     CROP fraction is charged in PASS 3. So a non-key anchor render is a       #
    #     QUALITY/measurement input, never a pipeline cost. This is the one place   #
    #     we render more than the pipeline pays for, purely to keep the patch       #
    #     QUALITY honest; the cost side stays the crop model. Disclosed in "note".  #
    # ======================================================================== #
    def is_keyframe(idx):
        return (idx == 0) or (idx % keyframe_every == 0)

    keyframe_indices = [t for t in range(frames) if is_keyframe(t)]
    anchor_layers = {}     # t -> {color, motion_prev, depth, motion_next} at ANCHOR quality
    anchor_exr_paths = {}  # t -> resolved anchor EXR path (reference-free aov_edge normals)
    anchor_devices = set()
    T_stack = 0.0
    n_keyframes = 0
    per_keyframe_s = []
    mean_key_render_s = 0.0

    # aov_edge selector reads the geometric Normal AOV from each frame's anchor EXR (a
    # geometry byproduct — NO extra render). Flip the pass ON for the anchor renders ONLY
    # when it is actually the selector; with the flag unset the anchor render (and thus
    # the receipt) is byte-identical to the legacy path. run_blender_frame copies
    # os.environ, so setting it here reaches the subprocess; restored in finally.
    _want_normal_aov = repair_enabled and repair_selector == "aov_edge"
    _saved_normal_aov_env = os.environ.get("CX_WANT_NORMAL_AOV")
    if _want_normal_aov:
        os.environ["CX_WANT_NORMAL_AOV"] = "1"
    try:
        for t in range(frames):
            frame_no = t + 1
            exr = os.path.join(WORK_DIR, f"anchor_{frame_no:04d}.exr")
            wall_s, dev, resolved = run_blender_frame(
                blender_bin, script_path, blend=blend, out_exr=exr,
                res_x=res_x, res_y=res_y, spp=draft_spp, is_ref=False,
                frame=frame_no, nframes=frames, cam_motion=cam_motion, seed=seed,
                bounces=bounces, device_pref=device_pref, timeout_s=anchor_timeout,
                adaptive=True, adaptive_thr=adaptive_threshold,
                adaptive_min=adaptive_min_samples, denoiser=denoiser,
                guides=denoise_guides, light_tree=light_tree, require_gpu=require_gpu,
            )
            anchor_devices.add(dev)
            color, motion_prev, depth, motion_next = read_exr_layers(resolved, res_x, res_y)
            anchor_layers[t] = {
                "color": color, "motion_prev": motion_prev,
                "depth": depth, "motion_next": motion_next,
            }
            anchor_exr_paths[t] = resolved
            if is_keyframe(t):
                # HONEST: a keyframe is charged at its FULL MEASURED whole-subprocess time —
                # this render IS in the shipped pipeline.
                T_stack += wall_s
                per_keyframe_s.append(wall_s)
                n_keyframes += 1
            else:
                # NON-KEY anchor render is a measurement/quality input ONLY (patch pixels +
                # this frame's own motion for the mask); its FULL cost is NOT charged — only
                # the crop fraction is charged in PASS 3. So DO NOT add wall_s to T_stack.
                log(f"frame {frame_no}: anchor-quality render for patch/motion "
                    f"(wall={wall_s:.3f}s, NOT charged; only the crop fraction is charged)")
    finally:
        if _want_normal_aov:
            if _saved_normal_aov_env is None:
                os.environ.pop("CX_WANT_NORMAL_AOV", None)
            else:
                os.environ["CX_WANT_NORMAL_AOV"] = _saved_normal_aov_env
    mean_key_render_s = (sum(per_keyframe_s) / len(per_keyframe_s)) if per_keyframe_s else 0.0

    # ---- CALIBRATION: measure the FIXED per-render overhead O (rerender mode only) #
    # A disocclusion crop re-render still pays Blender's fixed cost in FULL — process
    # start + .blend load + BVH build are independent of rendered-pixel count. Only the
    # path-trace term P scales with area. We measure O once (a tiny crop at the SAME
    # anchor spp/scene) so we can charge a crop as O + disocc_frac*P_key, NOT
    # disocc_frac*(O+P) which would scale the fixed cost away and INFLATE net_speedup.
    # In inpaint/nearest mode there is NO crop re-render, so O is not needed and NOT
    # charged (that mode is fully measured -> modeled:false).
    fixed_overhead_s = 0.0
    if hole_fill == "rerender":
        CALIB_RES = 8
        try:
            fixed_overhead_s, _cdev, _cres = run_blender_frame(
                blender_bin, script_path, blend=blend,
                out_exr=os.path.join(WORK_DIR, "calib_overhead.exr"),
                res_x=CALIB_RES, res_y=CALIB_RES, spp=draft_spp, is_ref=False,
                frame=1, nframes=frames, cam_motion=cam_motion, seed=seed,
                bounces=bounces, device_pref=device_pref, timeout_s=calib_timeout,
                adaptive=True, adaptive_thr=adaptive_threshold,
                adaptive_min=adaptive_min_samples, denoiser=denoiser,
                guides=denoise_guides, light_tree=light_tree, require_gpu=require_gpu,
            )
            # HONEST: the calibration render is REAL work spent to measure O — charge it.
            T_stack += fixed_overhead_s
        except Exception as _ce:  # noqa: BLE001 — calibration failed -> charge only P
            fixed_overhead_s = 0.0
            log(f"overhead calibration failed ({_ce}); fixed_overhead_s=0 (charge only P)")
    # per-keyframe pixel-trace time = keyframe wall - fixed overhead (never negative).
    key_pixel_trace_s = {
        t: max(s - fixed_overhead_s, 0.0)
        for t, s in zip(keyframe_indices, per_keyframe_s)
    }
    mean_key_pixel_trace_s = (
        sum(key_pixel_trace_s.values()) / len(key_pixel_trace_s)
    ) if key_pixel_trace_s else 0.0
    log(f"fixed render overhead O={fixed_overhead_s:.3f}s; mean keyframe wall="
        f"{mean_key_render_s:.3f}s mean pixel-trace P={mean_key_pixel_trace_s:.3f}s")

    # ======================================================================== #
    # PASS 3 — REPROJECT every non-key frame from the PREVIOUS keyframe, mask    #
    # the disocclusions, composite (rerender true patch OR numpy fill), score.   #
    # Charge the REAL numpy wall-time to T_stack + (rerender) the modeled crop.   #
    # ======================================================================== #
    delivered = {}   # t -> delivered color (keyframes are their own anchored render)
    for t in keyframe_indices:
        delivered[t] = anchor_layers[t]["color"]

    accept_fracs = []
    disoccluded_fracs = []
    per_cue_cov = []
    reproj_frame_indices = []
    modeled_crop_used = False  # did we ever charge a modeled crop-trace? (rerender + holes)

    prev_key = None
    for t in range(frames):
        if is_keyframe(t):
            prev_key = t
            continue
        # nearest PREVIOUS keyframe (frame 0 is always a keyframe, so prev_key is set).
        key_t = prev_key
        # This frame's OWN real Cycles motion/depth drive the mask, and its ANCHOR-QUALITY
        # color supplies the disocclusion patch — both from anchor_layers[t] (the full
        # anchor-stack render PASS 2 did for THIS frame). We use the anchor render, NOT the
        # 4096-spp reference: the patch must reflect what a real crop render at the SAME
        # anchor stack would produce, so the delivered-frame SSIM is honest (using the
        # reference would make the patch score SSIM~1 by construction and inflate quality).
        cur = anchor_layers[t]
        motion_prev = cur["motion_prev"]
        motion_next = cur["motion_next"]
        depth = cur["depth"]

        # ---- REAL numpy pipeline work (warp + mask + fill) is TIMED and charged to
        #      T_stack. SSIM below is measurement-only (not charged). ------------------
        _np_t0 = time.perf_counter()
        reproj, valid = warp_gather(anchor_layers[key_t]["color"], motion_prev)
        mask, coverage = disocclusion_mask(
            motion_prev, motion_next, depth, valid, disocclusion_thresh
        )
        disocc_frac = float(mask.mean())

        if hole_fill == "rerender":
            # drop the ANCHOR-QUALITY render's pixels into the disoccluded patch — exactly
            # what a crop render at the SAME anchor stack (adaptive+OIDN+guides+light-tree,
            # draft_spp) would produce for that region. The crop's RENDER cost is charged
            # by the FIXED-overhead-aware model below (NOT this full render's cost).
            comp = composite_rerender(reproj, cur["color"], mask)
        else:
            comp = hole_fill_numpy(reproj, mask, hole_fill)
        numpy_wall_s = time.perf_counter() - _np_t0

        # ---- COST: real numpy time + (rerender only) the modeled crop-trace cost ------
        frame_cost = numpy_wall_s
        if hole_fill == "rerender" and disocc_frac > 0.0:
            # ONE MODELED STEP: a crop re-render pays the fixed overhead O in FULL and
            # traces only the disoccluded fraction of the keyframe's pixel-trace time.
            crop_render_cost = fixed_overhead_s + disocc_frac * key_pixel_trace_s[key_t]
            frame_cost += crop_render_cost
            modeled_crop_used = True
        T_stack += frame_cost

        delivered[t] = comp
        accept_fracs.append(1.0 - disocc_frac)
        disoccluded_fracs.append(disocc_frac)
        per_cue_cov.append(coverage)
        reproj_frame_indices.append(t)
        log(f"frame {t + 1}: reproject from key {key_t + 1} accept={1.0 - disocc_frac:.3f} "
            f"disocc={disocc_frac:.3f} numpy={numpy_wall_s:.3f}s cost={frame_cost:.3f}s "
            f"fill={hole_fill} cues={coverage}")

    # ======================================================================== #
    # PASS 3.5 — REPAIR (reference-free) BEGIN                                   #
    # Lift the worst tiles WITHOUT paying more samples everywhere: (i) SELECT the  #
    # candidate tiles reference-free — repair_selector='two_draft' renders a       #
    # SECOND cheap RAW draft per frame at a different seed (noisy_b recipe of       #
    # exp_mint_denoise_pairs.py) and scores per-tile VARIANCE divergence vs the     #
    # delivered frame (Noise2Noise), while repair_selector='aov_edge' scores each   #
    # tile by the NORMAL-AOV EDGE DENSITY read from the anchor EXR (the S4 signal    #
    # exp_multi_selector_probe validated — no selection draft, ~zero cost);          #
    # (ii) GLOBALLY rank + select at most repair_top_k tiles; (iii) re-render ONLY   #
    # those tiles (margin-expanded, merged) via a REAL Cycles border render (one     #
    # subprocess per frame — BVH paid once) at repair_spp — repair_denoiser=         #
    # 'inherit' uses the SAME anchor stack (adaptive + OIDN + guides) + a HALVED      #
    # adaptive threshold, repair_denoiser='none' renders RAW (adaptive OFF, denoiser  #
    # OFF, no guides) to strip OIDN's edge-blur bias on exactly the failing tiles;    #
    # (iv) feather-composite the repaired regions into the delivered frames. HONEST   #
    # ACCOUNTING: every stage here — selection drafts (two_draft), normal read +      #
    # scoring, repair renders, compositing — is REAL measured wall-clock charged into #
    # T_stack (the selection stage is charged even when zero tiles get repaired). NO  #
    # modeled term is added. REFERENCE-FREE by structure: nothing in this block reads #
    # the reference frames; every function it calls sees only delivered pixels, the   #
    # second draft, or the anchor's own normals. SSIM-vs-reference remains            #
    # measurement-only, computed AFTER delivery in the grading block below.           #
    # (A unit test extracts this block's source and asserts the reference is          #
    # never touched between the BEGIN/END markers.)                                   #
    # ======================================================================== #
    pre_repair_delivered = None
    repaired_tiles_per_frame = [[] for _ in range(frames)]
    selected_tiles = []
    divergence_scores = []
    per_frame_selection_draft_s = []
    per_frame_selection_scoring_s = []
    per_frame_repair_render_s = [0.0] * frames
    per_frame_repair_composite_s = [0.0] * frames
    selection_cost_s = 0.0
    repair_render_composite_s = 0.0

    if repair_enabled:
        repair_timeout = ref_timeout  # headroom; the CHARGE is measured wall, not the cap

        if repair_selector == "two_draft":
            # ---- (i) selection draft B: raw MC, adaptive OFF, denoiser OFF, seed offset --
            selection_colors = {}
            for t in range(frames):
                frame_no = t + 1
                sel_exr = os.path.join(WORK_DIR, f"sel_{frame_no:04d}.exr")
                wall_s, dev, resolved = run_blender_frame(
                    blender_bin, script_path, blend=blend, out_exr=sel_exr,
                    res_x=res_x, res_y=res_y, spp=selection_draft_spp, is_ref=False,
                    frame=frame_no, nframes=frames, cam_motion=cam_motion,
                    seed=seed + selection_seed_offset, bounces=bounces,
                    device_pref=device_pref, timeout_s=anchor_timeout,
                    adaptive=False, denoiser="none", guides=False,
                    light_tree=light_tree, require_gpu=require_gpu,
                )
                anchor_devices.add(dev)
                T_stack += wall_s      # CHARGED: the second draft is real pipeline cost
                selection_cost_s += wall_s
                per_frame_selection_draft_s.append(wall_s)
                sel_color, _mp, _d, _mn = read_exr_layers(resolved, res_x, res_y)
                selection_colors[t] = sel_color
                log(f"frame {frame_no}: selection draft ({selection_draft_spp}spp raw, "
                    f"seed+{selection_seed_offset}) wall={wall_s:.3f}s CHARGED")

            # ---- (ii) divergence scoring (CHARGED: an in-pipeline DECISION, unlike the
            #      post-delivery grading SSIM) --------------------------------------------
            for t in range(frames):
                _sc_t0 = time.perf_counter()
                scores = tile_divergence_scores(
                    delivered[t], selection_colors[t], grid=GRADING_TILE_GRID)
                sc_s = time.perf_counter() - _sc_t0
                T_stack += sc_s
                selection_cost_s += sc_s
                per_frame_selection_scoring_s.append(sc_s)
                divergence_scores.append(scores)
        else:  # repair_selector == "aov_edge"
            # ---- (i)+(ii) aov_edge: NO selection draft — the geometric Normal AOV rides
            #      in each frame's anchor EXR (written by CX_WANT_NORMAL_AOV in PASS 2).
            #      Score = per-tile normal-edge density (VERBATIM S4). The normal read +
            #      scoring is an in-pipeline DECISION, so it is CHARGED to T_stack exactly
            #      like the two_draft divergence scoring; there is no draft RENDER cost. --
            per_frame_selection_draft_s = [0.0] * frames  # no selection render for aov_edge
            for t in range(frames):
                _sc_t0 = time.perf_counter()
                normal = read_anchor_normal(anchor_exr_paths[t], res_x, res_y)
                if normal is None:
                    raise RuntimeError(
                        f"repair_selector='aov_edge' but the Normal AOV is absent from the "
                        f"anchor EXR for frame {t + 1} ({anchor_exr_paths[t]}). The anchor "
                        f"render must write the Normal pass (CX_WANT_NORMAL_AOV=1 enables "
                        f"use_pass_normal; the denoise guides also carry a normal). Refusing "
                        f"to fabricate a selector score — a real negative beats a massaged "
                        f"positive.")
                scores = aov_edge_tile_scores(normal, grid=GRADING_TILE_GRID)
                sc_s = time.perf_counter() - _sc_t0
                T_stack += sc_s
                selection_cost_s += sc_s
                per_frame_selection_scoring_s.append(sc_s)
                divergence_scores.append(scores)

        selected_tiles = select_repair_tiles(
            divergence_scores, repair_top_k, repair_max_per_frame, repair_min_divergence)
        log(f"repair selection: {len(selected_tiles)} tile(s) of "
            f"{frames * GRADING_TILE_GRID * GRADING_TILE_GRID} "
            f"(budget={repair_top_k}, max/frame={repair_max_per_frame}): "
            + "; ".join(f"f{f + 1}[{gy},{gx}]={s:.4f}" for f, gy, gx, s in selected_tiles))

        # PRE-REPAIR copies for the post-delivery grading (bookkeeping, NOT charged).
        pre_repair_delivered = {t: delivered[t].copy() for t in range(frames)}

        # ---- (iii) bordered repair renders (one subprocess per frame) + (iv) feather
        #      composite — both REAL measured wall-clock, both CHARGED ----------------
        tiles_by_frame = {}
        for f_idx, gy, gx, s in selected_tiles:
            tiles_by_frame.setdefault(f_idx, []).append((gy, gx, s))
        rect_by_tile = {(gy, gx): (y0, y1, x0, x1)
                        for gy, gx, y0, y1, x0, x1 in _tile_rects(res_y, res_x,
                                                                  GRADING_TILE_GRID)}
        for t in sorted(tiles_by_frame):
            frame_no = t + 1
            tiles = sorted(tiles_by_frame[t], key=lambda x: (x[0], x[1]))
            repaired_tiles_per_frame[t] = [[gy, gx] for gy, gx, _s in tiles]
            core_rects = [rect_by_tile[(gy, gx)] for gy, gx, _s in tiles]
            regions = merge_and_margin_rects(core_rects, repair_margin_px, res_x, res_y)
            borders = [[bx0, by0, bx1, by1]
                       for _core, (by0, by1, bx0, bx1) in regions]
            out_pattern = os.path.join(WORK_DIR, f"repair_{frame_no:04d}_r{{region}}.exr")
            # repair_denoiser selects the sampling recipe of the re-render:
            #   'inherit' -> the SAME anchor stack (adaptive cap + HALVED threshold + the
            #                anchor denoiser + guides + forced light-tree). Unchanged
            #                legacy repair path.
            #   'none'    -> RAW Monte-Carlo at a FIXED repair_spp rendered with the EXACT
            #                reference recipe via match_reference=True: adaptive OFF,
            #                denoiser OFF, no guides, AND use_light_tree LEFT AT THE SCENE
            #                DEFAULT (the reference never forces it). This makes the border
            #                tiles CONFIG-IDENTICAL to the reference, so they converge to
            #                the SAME estimate (config-matched 4096spp renders are pixel-
            #                identical). Forcing light-tree here — as the anchor stack does
            #                — was a one-setting mismatch that left a ~0.086 SSIM residual
            #                on the failing corner tiles; match_reference is robust to
            #                whatever the scene's use_light_tree default is (we never
            #                touch it) rather than guessing light_tree=False.
            if repair_denoiser == "none":
                repair_render_kwargs = dict(
                    adaptive=False, denoiser="none", guides=False,
                    match_reference=True)
            else:  # 'inherit'
                repair_render_kwargs = dict(
                    adaptive=True, adaptive_thr=repair_adaptive_thr_eff,
                    adaptive_min=adaptive_min_samples, denoiser=denoiser,
                    guides=denoise_guides, light_tree=light_tree)
            wall_s, dev, resolved_list = run_blender_frame(
                blender_bin, script_path, blend=blend,
                out_exr=os.path.join(WORK_DIR, f"repair_{frame_no:04d}.exr"),
                res_x=res_x, res_y=res_y, spp=repair_spp_eff, is_ref=False,
                frame=frame_no, nframes=frames, cam_motion=cam_motion,
                seed=seed + repair_seed_offset, bounces=bounces,
                device_pref=device_pref, timeout_s=repair_timeout,
                require_gpu=require_gpu,
                borders=borders, out_pattern=out_pattern,
                **repair_render_kwargs,
            )
            anchor_devices.add(dev)
            T_stack += wall_s          # CHARGED: real bordered re-render wall-clock
            repair_render_composite_s += wall_s
            per_frame_repair_render_s[t] = wall_s

            _cmp_t0 = time.perf_counter()
            for (core, _border), rpath in zip(regions, resolved_list):
                repair_color, _mp, _d, _mn = read_exr_layers(rpath, res_x, res_y)
                alpha = build_feather_alpha(
                    core, repair_margin_px, repair_feather_px, res_y, res_x)
                delivered[t] = feather_composite(delivered[t], repair_color, alpha)
            comp_s = time.perf_counter() - _cmp_t0
            T_stack += comp_s          # CHARGED: real compositing (incl. EXR read) time
            repair_render_composite_s += comp_s
            per_frame_repair_composite_s[t] = comp_s
            _thr_desc = ("adaptive OFF (raw)" if repair_denoiser == "none"
                         else f"thr={repair_adaptive_thr_eff}")
            log(f"frame {frame_no}: repaired {len(tiles)} tile(s) in {len(regions)} "
                f"region(s) @ {repair_spp_eff}spp {_thr_desc} denoiser={repair_denoiser} "
                f"render={wall_s:.3f}s composite={comp_s:.3f}s (both CHARGED)")
    # ======================================================================== #
    # PASS 3.5 — REPAIR (reference-free) END                                     #
    # ======================================================================== #

    # ======================================================================== #
    # END-TO-END SSIM: our DELIVERED frame vs the TRUE frame, GLOBAL + per-tile. #
    # SSIM is a MEASUREMENT ONLY — not charged to T_stack.                       #
    # ======================================================================== #
    global_ssims = []
    worst_tiles = []
    p5_tiles = []
    for t in range(frames):
        g, wt, p5 = compute_ssim_global_and_tiles(delivered[t], true_colors[t],
                                                  grid=GRADING_TILE_GRID)
        global_ssims.append(g)
        worst_tiles.append(wt)
        p5_tiles.append(p5)

    quality = float(np.mean(global_ssims)) if global_ssims else 1.0
    # worst-tile across the WHOLE shot (min of per-frame worst tiles) + mean p5 tile.
    worst_tile_ssim = float(np.min(worst_tiles)) if worst_tiles else quality
    p5_tile_ssim = float(np.mean(p5_tiles)) if p5_tiles else quality

    # ---- REPAIR post-hoc grading (MEASUREMENT-ONLY, computed AFTER delivery; reads the
    # reference ONLY here — the selector above never saw it; none of this is charged) --
    per_frame_worst_tile_pre_repair = None
    selector_recall = None
    repaired_tile_report = None
    if repair_enabled:
        # STRICT delivery bar for a single tile (cx_integrated_speculation
        # .DELIVERY_WORST_TILE — hardcoded here because pod runners are standalone).
        DELIVERY_WORST_TILE = 0.95
        per_frame_worst_tile_pre_repair = []
        failing_pre = set()   # (t, gy, gx) tiles that graded < bar BEFORE repair
        pre_maps = []
        post_maps = []
        for t in range(frames):
            pre_map = per_tile_ssim_map(pre_repair_delivered[t], true_colors[t],
                                        grid=GRADING_TILE_GRID)
            post_map = per_tile_ssim_map(delivered[t], true_colors[t],
                                         grid=GRADING_TILE_GRID)
            pre_maps.append(pre_map)
            post_maps.append(post_map)
            finite = pre_map[np.isfinite(pre_map)]
            per_frame_worst_tile_pre_repair.append(
                float(finite.min()) if finite.size else float("nan"))
            for gy in range(GRADING_TILE_GRID):
                for gx in range(GRADING_TILE_GRID):
                    v = pre_map[gy, gx]
                    if np.isfinite(v) and v < DELIVERY_WORST_TILE:
                        failing_pre.add((t, gy, gx))
        selected_set = {(f, gy, gx) for f, gy, gx, _s in selected_tiles}
        if failing_pre:
            selector_recall = len(failing_pre & selected_set) / len(failing_pre)
        repaired_tile_report = [
            {
                "frame": int(f),
                "tile": [int(gy), int(gx)],
                "divergence": round(float(s), 6),
                "ssim_pre": (round(float(pre_maps[f][gy, gx]), 4)
                             if np.isfinite(pre_maps[f][gy, gx]) else None),
                "ssim_after": (round(float(post_maps[f][gy, gx]), 4)
                               if np.isfinite(post_maps[f][gy, gx]) else None),
            }
            for f, gy, gx, s in selected_tiles
        ]

    # ======================================================================== #
    # THE ONE END-TO-END RATIO — measured wall-clock, NOT a product of stages.   #
    # ======================================================================== #
    net_speedup = (T_ref / T_stack) if T_stack > 1e-9 else 0.0
    reproject_accept_frac = float(np.mean(accept_fracs)) if accept_fracs else 1.0
    mean_disoccluded_frac = float(np.mean(disoccluded_fracs)) if disoccluded_fracs else 0.0

    device_all = ref_devices | anchor_devices
    device = "|".join(sorted(device_all)) if device_all else "unknown"
    fell_to_cpu = "CPU" in device
    # MONEY-SAFETY (final gate): under require_gpu we must NEVER emit a receipt whose
    # frames were traced on CPU. The per-frame guards in run_blender_frame should have
    # already raised; this is the last line of defense before metrics leave the runner.
    if require_gpu and fell_to_cpu:
        raise RuntimeError(
            f"require_gpu set but render device set is {device!r} (CPU present); refusing to "
            f"emit a CPU-rendered receipt. Blender 4.2 has no Cycles kernel for Blackwell "
            f"(sm_100/sm_120); use the policy ladder A100/H100/H200 (gpu-provisioning-policy: L40S/A6000 downgrades are banned)."
        )

    # ---- honesty flag: modeled iff the rerender crop-trace step was actually charged --
    modeled = bool(hole_fill == "rerender" and modeled_crop_used)

    note = (
        f"KEYSTONE compound end-to-end render stack on ANIMATED '{scene_key}' "
        f"({res_x}x{res_y}, {frames} frames, keyframe_every={keyframe_every}, "
        f"bounces={bounces}). Camera DOLLIED+PANNED+YAWED (cam_motion={cam_motion}) for "
        f"real screen-space motion + silhouette disocclusion. "
        f"REFERENCE = every frame FULLY at {ref_spp} spp, adaptive OFF, denoise OFF "
        f"(true frames); T_ref = summed whole-subprocess wall-clock. "
        f"OURS = keyframes rendered with the FULL anchor stack [adaptive sampling "
        f"(thr={adaptive_threshold}, min={adaptive_min_samples}) + {denoiser} denoiser"
        f"{' + albedo/normal prefiltered guides' if (denoise_guides and denoiser != 'none') else ''}"
        f"{' + light-tree many-light importance sampling' if light_tree else ''}, "
        f"draft_spp={draft_spp}] at full whole-subprocess wall-clock; non-key frames "
        f"reprojected by Cycles motion vectors (our backward-gather warp) + OUR "
        f"disocclusion mask [OOB|MV-divergence|depth-discontinuity|fwd/bwd-consistency, "
        f"dilated], disocclusions handled by hole_fill={hole_fill}. "
        f"net_speedup = T_ref / T_stack = ONE measured wall-clock ratio on the SAME box "
        f"(NOT a product of per-stage speedups). quality = end-to-end SSIM of DELIVERED "
        f"frames vs TRUE frames (GLOBAL + per-8x8-tile worst/p5, tonemapped linear HDR) — "
        f"real scikit-image on real pixels; SSIM is measurement-only (not charged to "
        f"T_stack). Every keyframe/reference render TIME is real whole-subprocess "
        f"wall-clock; every reprojected frame's numpy warp/mask/fill time is real measured "
        f"wall-time."
    )
    if hole_fill == "rerender":
        note += (
            f" ONE MODELED STEP: the disocclusion crop re-render is NOT border-rendered "
            f"per-frame; its cost is charged as fixed_overhead + disocc_frac*keyframe_"
            f"pixel_trace, where fixed_overhead={fixed_overhead_s:.3f}s is the MEASURED "
            f"Blender start+.blend load+BVH cost a real crop pays in FULL (NOT scaled by "
            f"area) and keyframe_pixel_trace = keyframe_wall - fixed_overhead. Honest and "
            f"CONSERVATIVE (a resident-Blender pipeline amortizes the fixed overhead across "
            f"crops) — NOT an upper bound. Because this crop-trace time is DERIVED "
            f"(area-scaled from a measured full trace) it is the ONLY non-directly-measured "
            f"number, so modeled={str(modeled).lower()}. The disocclusion PATCH pixels are "
            f"the ANCHOR-QUALITY render of that frame (adaptive+denoiser+guides"
            f"{'+light-tree' if light_tree else ''}, draft_spp) — exactly what a crop render "
            f"at the SAME anchor stack produces — NOT the 4096-spp reference, so the "
            f"delivered-frame SSIM is a faithful measure of the composited pipeline (no "
            f"patch scores SSIM~1 by construction). Each non-key frame's anchor render is a "
            f"QUALITY/motion input only; its FULL cost is NOT charged (only the crop "
            f"fraction is), so we render more pixels than the pipeline pays for purely to "
            f"keep the patch quality honest."
        )
    else:
        note += (
            f" hole_fill={hole_fill}: disocclusions filled by real numpy "
            f"({'cv2/numpy inpaint' if hole_fill == 'inpaint' else 'push-pull nearest'}) "
            f"at 0 render cost — that fill time IS charged; NO modeled step, so "
            f"modeled=false (fully measured, lower quality on the holes)."
        )
    if repair_enabled:
        if repair_selector == "two_draft":
            _sel_desc = (
                f"a second RAW draft per frame ({selection_draft_spp}spp, adaptive OFF, "
                f"denoiser OFF, seed+{selection_seed_offset}) scored per-tile divergence "
                f"vs the delivered frame (two-independent-estimate / Noise2Noise "
                f"selection)")
            _sel_limit = ("divergence detects VARIANCE, not a denoiser bias shared across "
                          "seeds")
        else:  # aov_edge
            _sel_desc = (
                "each grading tile scored by the NORMAL-AOV edge density of the anchor "
                "render (the S4 signal exp_multi_selector_probe validated; the Normal AOV "
                "rides in the anchor EXR at ~zero cost, so NO selection draft is rendered)")
            _sel_limit = ("normal-edge density localizes geometric silhouette content — the "
                          "shared-denoiser-bias tiles two_draft variance is blind to")
        if repair_denoiser == "none":
            _rep_desc = (
                f"re-rendered RAW at {repair_spp_eff}spp with the EXACT reference recipe "
                f"(adaptive OFF, denoiser OFF, no guides, and use_light_tree LEFT AT THE "
                f"SCENE DEFAULT via match_reference — NOT force-enabled) so the border "
                f"tiles are CONFIG-IDENTICAL to the reference: OIDN's shared edge-blur bias "
                f"is removed AND the one-setting light-tree mismatch that left a ~0.086 "
                f"SSIM residual on the failing corner tiles is closed on exactly those "
                f"tiles)")
        else:  # inherit
            _rep_desc = (
                f"re-rendered with the SAME anchor stack at {repair_spp_eff}spp cap + "
                f"adaptive_thr={repair_adaptive_thr_eff:g}")
        note += (
            f" REPAIR PASS (reference-free, selector={repair_selector}, "
            f"denoiser={repair_denoiser}): {_sel_desc} — the selector NEVER reads the "
            f"reference; SSIM-vs-reference stays measurement-only, computed after "
            f"delivery. The top-{repair_top_k} tiles shot-wide (max "
            f"{repair_max_per_frame}/frame) were {_rep_desc} via REAL Cycles border "
            f"renders (margin {repair_margin_px}px) and feather-composited (outer "
            f"{repair_feather_px}px linear ramp; the graded tile gets pure repair pixels). "
            f"EVERY repair second is real measured wall-clock charged into T_stack: "
            f"selection + scoring ({selection_cost_s:.3f}s) and repair renders + "
            f"compositing ({repair_render_composite_s:.3f}s) — charged even when zero "
            f"tiles are selected. The repair pass adds NO modeled term. KNOWN LIMIT: "
            f"{_sel_limit}; selector_recall (measurement-only) quantifies missed failing "
            f"tiles each run."
        )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."
    if any(c["consistency"] == 0.0 and c["depth"] == 0.0 for c in per_cue_cov):
        note += (" NOTE: some frames lacked next-vectors and/or depth (EXR reader "
                 "fallback) — mask ran on the available cues only, still our logic.")

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),                 # GLOBAL SSIM, mean over frames
        "worst_tile_ssim": round(float(worst_tile_ssim), 4),
        "p5_tile_ssim": round(float(p5_tile_ssim), 4),
        "frames": int(frames),
        "keyframes": int(n_keyframes),
        "keyframe_every": int(keyframe_every),
        "T_ref_s": round(float(T_ref), 4),
        "T_stack_s": round(float(T_stack), 4),
        "ref_cache_hit": bool(ref_cache_hit),
        "reproject_accept_frac": round(float(reproject_accept_frac), 4),
        "mean_disoccluded_frac": round(float(mean_disoccluded_frac), 4),
        "draft_spp": int(draft_spp),
        "ref_spp": int(ref_spp),
        "adaptive_threshold": float(adaptive_threshold),
        "adaptive_min_samples": int(adaptive_min_samples),
        "denoiser": denoiser,
        "denoise_guides": bool(denoise_guides),
        "light_tree": bool(light_tree),
        "bounces": int(bounces),
        "resolution": f"{res_x}x{res_y}",
        "scene": scene_key,
        "hole_fill": hole_fill,
        "disocclusion_thresh": float(disocclusion_thresh),
        "cam_motion": float(cam_motion),
        "fixed_overhead_s": round(float(fixed_overhead_s), 4),
        "mean_keyframe_render_s": round(float(mean_key_render_s), 4),
        "mean_keyframe_pixel_trace_s": round(float(mean_key_pixel_trace_s), 4),
        "device": device,
        "modeled": modeled,
        "note": note,
        # ---- extra real diagnostics ---------------------------------------- #
        "per_frame_ref_s": [round(float(x), 4) for x in per_frame_ref_s],
        "per_keyframe_render_s": [round(float(x), 4) for x in per_keyframe_s],
        "keyframe_indices": [int(i) for i in keyframe_indices],
        "reprojected_frames": int(len(reproj_frame_indices)),
        "per_frame_global_ssim": [round(float(x), 4) for x in global_ssims],
        "per_frame_worst_tile_ssim": [round(float(x), 4) for x in worst_tiles],
        "requested_scene": scene_arg,
    }

    # ---- REPAIR receipt fields — emitted ONLY when the pass ran, so a default run's
    # metrics stay byte-identical to the legacy runner. repair_total_s is ALREADY inside
    # T_stack_s (reported for decomposition, never re-added). --------------------------
    if repair_enabled:
        sel_scores_np = [np.asarray(s, dtype=np.float64) for s in divergence_scores]
        metrics.update({
            "repair_enabled": True,
            "repair_selector": repair_selector,
            "repair_denoiser": repair_denoiser,
            "repair_top_k": int(repair_top_k),
            "repair_max_per_frame": int(repair_max_per_frame),
            "repair_min_divergence": float(repair_min_divergence),
            "repair_spp": int(repair_spp_eff),
            "repair_spp_multiplier": float(repair_spp_multiplier),
            "repair_adaptive_threshold": float(repair_adaptive_thr_eff),
            "selection_draft_spp": int(selection_draft_spp),
            "selection_seed_offset": int(selection_seed_offset),
            "repair_seed_offset": int(repair_seed_offset),
            "repair_margin_px": int(repair_margin_px),
            "repair_feather_px": int(repair_feather_px),
            # ---- honest accounting decomposition (all REAL wall-clock, all already
            # charged into T_stack_s above; selection charged even at zero repairs) ----
            "selection_cost_s": round(float(selection_cost_s), 4),
            "repair_cost_s": round(float(repair_render_composite_s), 4),
            "repair_total_s": round(float(selection_cost_s + repair_render_composite_s), 4),
            "per_frame_selection_draft_s": [round(float(x), 4)
                                            for x in per_frame_selection_draft_s],
            "per_frame_selection_scoring_s": [round(float(x), 4)
                                              for x in per_frame_selection_scoring_s],
            "per_frame_repair_render_s": [round(float(x), 4)
                                          for x in per_frame_repair_render_s],
            "per_frame_repair_composite_s": [round(float(x), 4)
                                             for x in per_frame_repair_composite_s],
            # ---- what was repaired (per-frame [gy,gx] lists on the grading grid) -----
            "repaired_tile_indices": repaired_tiles_per_frame,
            "repaired_tile_count": int(sum(len(x) for x in repaired_tiles_per_frame)),
            "selector_scores": {
                "per_frame_max": [round(float(s.max()), 6) if s.size else 0.0
                                  for s in sel_scores_np],
                "per_frame_p95": [round(float(np.percentile(s, 95)), 6) if s.size else 0.0
                                  for s in sel_scores_np],
                "selected": [
                    {"frame": int(f), "tile": [int(gy), int(gx)],
                     "divergence": round(float(s), 6)}
                    for f, gy, gx, s in selected_tiles
                ],
            },
            # ---- MEASUREMENT-ONLY (computed AFTER delivery in the grading block; the
            # selector provably never read the reference) ------------------------------
            "per_frame_worst_tile_ssim_pre_repair": [
                round(float(x), 4) for x in (per_frame_worst_tile_pre_repair or [])],
            "selector_recall": (round(float(selector_recall), 4)
                                if selector_recall is not None else None),
            "repaired_tile_ssim_after": repaired_tile_report,
        })
        # SELF-DOCUMENTING light-tree mode of the repair border render. Emitted ONLY on the
        # RAW path so 'inherit' (and repair-OFF) receipts stay BYTE-IDENTICAL to the legacy
        # runner. 'scene_default_match_ref' == config-identical to the reference (light-tree
        # never forced); the 'inherit' path keeps its historical forced-light-tree behavior.
        if repair_denoiser == "none":
            metrics["repair_light_tree"] = "scene_default_match_ref"

    log(f"RESULT net_speedup={net_speedup:.3f} quality={quality:.4f} "
        f"worst_tile={worst_tile_ssim:.4f} p5_tile={p5_tile_ssim:.4f} "
        f"T_ref={T_ref:.2f}s T_stack={T_stack:.2f}s keyframes={n_keyframes} "
        f"accept={reproject_accept_frac:.3f} disocc={mean_disoccluded_frac:.3f} "
        f"modeled={modeled} device={device}")
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
