#!/usr/bin/env python3
"""
exp_render_frame_subset.py — render a SUBSET of the animated Classroom shot's frames
at ANCHOR quality on ONE pod, and report REAL per-frame render wall-times.

================================================================================
WHAT THIS IS (and what it is NOT)
================================================================================
This is the per-pod worker for the REAL multi-pod DISTRIBUTION driver
(run_multipod_distribution.py). The driver splits the frames of ONE animated shot
across N pods; this runner is what each pod actually executes on its slice.

Given a list of frame INDICES (0-based, into an NFRAMES-long animated camera shot),
it renders EXACTLY those frames at ANCHOR quality — the SAME anchor stack the
keystone (exp_render_stack.py) renders its keyframes with: adaptive sampling +
OpenImageDenoise + albedo/normal prefiltered guide passes + light-tree many-light
importance sampling, at draft_spp. It emits ONE JSON line whose payload includes the
REAL per-frame render wall-clock (a time.perf_counter() around the WHOLE Blender
subprocess: launch + .blend load + BVH build + adaptive trace + in-pipeline denoise +
EXR encode — NOTHING excluded).

The camera-animation, the anchor-stack Blender script, the scene download/cache and
the Blender self-bootstrap are a FOCUSED COPY of exp_render_stack.py's honest logic
(same DOLLY+PAN+YAW keyframes, same anchor render config) so that frame index t here
is pixel-for-pixel the SAME frame t the keystone would render — the distribution is
measured on the identical shot, not a re-parameterized one. Copied (not imported)
because this file is scp'd to remote pods as a standalone script; a focused copy is
safer tonight than a cross-file import that must also be shipped and kept in sync.

================================================================================
HONESTY
================================================================================
  * modeled:false. EVERY per-frame time is a real time.perf_counter() whole-subprocess
    wall-clock. There is NO reprojection / crop model here — this runner renders each
    assigned frame FULLY at anchor quality and times it. It measures REAL render cost,
    which is exactly what a distribution split pays per frame.
  * The driver (run_multipod_distribution.py) is responsible for the network/provision
    honesty (T_real including vs excluding provisioning). This runner reports ONLY the
    real render times it measured, plus the total per-pod subprocess span it saw.
  * On ANY failure (download, unzip, Blender non-zero exit, missing EXR, missing dep)
    we emit {"error": "<type>: <msg>"} as the last stdout line and exit 0 — never a
    fabricated number, never a hang.

================================================================================
CONFIG (argv[1] JSON)
================================================================================
  frame_indices : [int,...]  REQUIRED   0-based frame indices to render (subset of the shot)
  nframes       : 8    (default)         total frames in the animated shot (defines the
                                         camera path so frame t matches the keystone's t)
  draft_spp     : 512  (default)         anchor sample CAP (adaptive may use fewer)
  adaptive_threshold  : 0.02 (default)   Cycles adaptive noise threshold on the anchor
  adaptive_min_samples: 16   (default)   adaptive floor samples/pixel on the anchor
  denoiser      : "oidn" (default) | "optix" | "none"   anchor denoiser
  denoise_guides: true (default)         albedo+normal prefiltered guide passes
  light_tree    : true (default)         Cycles many-light importance sampling
  resolution    : "1920x1080" (default)  parsed WxH
  bounces       : 12   (default)         total light bounces
  cam_motion    : 1.0  (default)         scalar on the camera dolly/pan/yaw per frame
  seed          : 0    (default)         Cycles seed
  device        : "AUTO" (default) | "GPU" | "CPU"
  scene         : "classroom" (default) | "bmw27" | <direct .blend/.zip URL>
  blender_url   : override the Blender download URL

OUTPUT (last stdout line = exactly ONE JSON object):
  {"frame_indices","per_frame_render_s","t_subset_render_s","t_pod_subprocess_s",
   "device","frames_rendered","scene","resolution","draft_spp","modeled":false,"note"}

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
# (Forked verbatim from exp_render_stack.py.)                                   #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/render_frame_subset"
# Persistent scene cache (shared layout with exp_render_stack.py so a scene fetched
# by a prior rung is reused). Prefer the big /models volume; fall back to /root.
_CACHE_ROOT = "/models/spec-lab" if os.path.isdir("/models") else "/root/spec-lab"
SCENES_DIR = os.path.join(_CACHE_ROOT, "scenes")

# Known production scenes on Blender's public demo server (VERIFIED reachable) —
# same table as exp_render_stack.py so the cache directory layout matches.
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
    print("[render_frame_subset]", *a, file=sys.stderr, flush=True)


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


# --------------------------------------------------------------------------- #
# 2. Self-bootstrap Blender (VERBATIM pattern from exp_render_stack.py).         #
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
# 3. Fetch + cache the production scene (forked from exp_render_stack.py).       #
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
# 4. The Blender render script — a FOCUSED COPY of exp_render_stack.py's script  #
#    with the SAME camera DOLLY+PAN+YAW keyframes and the SAME anchor render     #
#    config, so frame index t here is pixel-for-pixel the keystone's frame t.    #
#    Renders ONE frame per invocation to a multilayer EXR (Combined+Vector+Z);   #
#    we only need Combined here, but keep the passes identical to the keystone    #
#    so the render cost matches the anchor render the pipeline actually pays.     #
#    Prints CX_CHOSEN_DEVICE + CX_RENDER_DONE (+ CX_DENOISER_UNAVAILABLE).        #
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
FRAME     = int(os.environ["CX_FRAME"])             # which animation frame to render
NFRAMES   = int(os.environ["CX_NFRAMES"])           # total frames in the shot
CAM_MOTION= float(os.environ.get("CX_CAM_MOTION", "1.0"))  # scalar on camera motion
SEED      = int(os.environ["CX_SEED"])
BOUNCES   = int(os.environ["CX_BOUNCES"])
DEV_PREF  = os.environ.get("CX_DEVICE", "AUTO")
USE_ADAPT = os.environ.get("CX_ADAPTIVE", "0") == "1"
ADAPT_THR = float(os.environ.get("CX_ADAPT_THR", "0.02"))
ADAPT_MIN = int(os.environ.get("CX_ADAPT_MIN", "16"))
DENOISER  = os.environ.get("CX_DENOISER", "none")   # oidn | optix | none
GUIDES    = os.environ.get("CX_GUIDES", "0") == "1"
LIGHTTREE = os.environ.get("CX_LIGHTTREE", "0") == "1"

# ---- open the REAL production scene -----------------------------------------
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene

# ---- CAMERA ANIMATION: identical DOLLY+PAN+YAW keyframes as exp_render_stack --
# so frame N here is the SAME frame the keystone renders. We keyframe only the
# active camera's location + yaw; geometry/materials/lights are untouched.
cam = scene.camera
if cam is None:
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
DOLLY_PER_FRAME = 0.05 * CAM_MOTION   # +X world drift per frame
RISE_PER_FRAME  = 0.02 * CAM_MOTION   # +Z world drift per frame (a slight crane)
YAW_PER_FRAME   = math.radians(0.8) * CAM_MOTION  # camera yaw per frame
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

# ---- Cycles engine + ANCHOR sampling ----------------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
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

# ---- LIGHT-TREE: many-light importance sampling (anchor lever) ---------------
if LIGHTTREE:
    try:
        cyc.use_light_tree = True
        _log("light-tree (many-light importance sampling) ENABLED on anchor")
    except Exception as e:
        _log("could not enable use_light_tree:", e)

# ---- DENOISER (anchor) ------------------------------------------------------
denoiser_ok = True
denoiser_note = ""
if DENOISER in ("oidn", "optix"):
    cyc.use_denoising = True
    want = 'OPENIMAGEDENOISE' if DENOISER == "oidn" else 'OPTIX'
    try:
        cyc.denoiser = want
    except Exception as e:
        denoiser_ok = False
        denoiser_note = f"denoiser {want} unavailable: {type(e).__name__}: {e}"
        _log(denoiser_note)
    if denoiser_ok and GUIDES:
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
else:
    cyc.use_denoising = False

# ---- BOUNCES ----------------------------------------------------------------
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = min(6, BOUNCES)
cyc.glossy_bounces       = min(6, BOUNCES)
cyc.transmission_bounces = BOUNCES
try:
    cyc.volume_bounces = min(6, BOUNCES)
except Exception:
    pass

# ---- device ladder: OPTIX -> CUDA -> HIP -> ONEAPI, else CPU -----------------
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

# ---- render PASSES: Combined (color), Vector (motion), Z (depth) — same as the
# keystone anchor render so the per-frame render COST matches the pipeline's.
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

_log(f"rendering ANCHOR frame={FRAME}/{NFRAMES} spp={SPP} res={RES_X}x{RES_Y} "
     f"adaptive={USE_ADAPT} denoiser={DENOISER} guides={GUIDES} "
     f"light_tree={LIGHTTREE} bounces={BOUNCES} device={chosen_device} -> {OUT}")
if not denoiser_ok:
    print(f"CX_DENOISER_UNAVAILABLE={denoiser_note}", flush=True)
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


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
                      spp, frame, nframes, cam_motion, seed, bounces,
                      device_pref, timeout_s, adaptive=True, adaptive_thr=0.02,
                      adaptive_min=16, denoiser="oidn", guides=True,
                      light_tree=True):
    """Render ONE animation frame of the production scene at ANCHOR quality.

    Returns (wall_seconds, chosen_device, resolved_exr_path). wall_seconds is
    time.perf_counter() around the WHOLE subprocess — launch + .blend load + BVH +
    adaptive trace + in-pipeline denoise + EXR encode. NOTHING excluded. Raises on
    non-zero exit, missing EXR, or an unavailable denoiser (we refuse to mislabel a
    non-denoised anchor render)."""
    env = dict(os.environ)
    env["CX_BLEND"] = blend
    env["CX_OUT"] = out_exr
    env["CX_RES_X"] = str(res_x)
    env["CX_RES_Y"] = str(res_y)
    env["CX_SPP"] = str(spp)
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

    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    log(f"render start [ANCHOR]: frame={frame} spp={spp} res={res_x}x{res_y} "
        f"adaptive={adaptive} denoiser={denoiser} guides={guides} "
        f"light_tree={light_tree} -> {out_exr}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1800:]
    err_tail = (proc.stderr or "")[-1800:]
    log(f"render [ANCHOR] frame={frame} rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    denoiser_unavail = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()
        elif line.startswith("CX_DENOISER_UNAVAILABLE="):
            denoiser_unavail = line.split("=", 1)[1].strip()

    # HONESTY: an unavailable denoiser FAILS — we never render a non-denoised anchor
    # and mislabel it as the full anchor stack.
    if denoiser_unavail is not None:
        raise RuntimeError(
            f"requested denoiser '{denoiser}' unavailable on this box "
            f"({denoiser_unavail}); refusing to mislabel a non-denoised anchor"
        )

    resolved = _resolve_exr_path(out_exr, frame)
    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and (resolved is not None)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed [ANCHOR] (frame={frame}, rc={proc.returncode}, "
            f"out_exists={resolved is not None}); stdout tail: {tail[-700:]}"
        )
    return wall_s, chosen_device, resolved


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    # Time the WHOLE per-pod subprocess span (bootstrap + scene + every frame render)
    # so the driver can cross-check its SSH-observed wall-clock against what this pod
    # actually spent. This is a real time.perf_counter() around all of main().
    t_pod0 = time.perf_counter()

    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}

    frame_indices = params.get("frame_indices", None)
    if frame_indices is None:
        raise RuntimeError("frame_indices is REQUIRED (0-based list of frames to render)")
    frame_indices = [int(i) for i in frame_indices]

    nframes = int(params.get("nframes", 8))
    draft_spp = int(params.get("draft_spp", 512))
    adaptive_threshold = float(params.get("adaptive_threshold", 0.02))
    adaptive_min_samples = int(params.get("adaptive_min_samples", 16))
    denoiser = str(params.get("denoiser", "oidn")).lower()
    denoise_guides = bool(params.get("denoise_guides", True))
    light_tree = bool(params.get("light_tree", True))
    resolution = str(params.get("resolution", "1920x1080"))
    bounces = int(params.get("bounces", 12))
    cam_motion = float(params.get("cam_motion", 1.0))
    seed = int(params.get("seed", 0))
    device_pref = str(params.get("device", "AUTO")).upper()
    scene_arg = str(params.get("scene", "classroom"))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- parse + clamp ----------------------------------------------------- #
    try:
        rx, ry = resolution.lower().split("x")
        res_x, res_y = max(16, int(rx)), max(16, int(ry))
    except Exception:
        raise RuntimeError(f"bad resolution {resolution!r}; expected WxH e.g. 1920x1080")

    if denoiser not in ("oidn", "optix", "none"):
        raise RuntimeError(f"bad denoiser {denoiser!r}; expected oidn|optix|none")

    nframes = max(2, nframes)
    draft_spp = max(1, draft_spp)
    bounces = max(1, bounces)
    adaptive_min_samples = max(1, min(adaptive_min_samples, draft_spp))
    adaptive_threshold = max(0.0, adaptive_threshold)
    cam_motion = max(0.0, cam_motion)

    # validate indices against the shot; a frame outside [0,nframes) is a driver bug.
    for i in frame_indices:
        if i < 0 or i >= nframes:
            raise RuntimeError(
                f"frame index {i} out of range for nframes={nframes} (expected 0..{nframes - 1})"
            )
    if not frame_indices:
        raise RuntimeError("frame_indices is empty — nothing to render")

    log(f"params: scene={scene_arg} nframes={nframes} frame_indices={frame_indices} "
        f"res={res_x}x{res_y} draft_spp={draft_spp} adaptive_thr={adaptive_threshold} "
        f"adaptive_min={adaptive_min_samples} denoiser={denoiser} guides={denoise_guides} "
        f"light_tree={light_tree} bounces={bounces} cam_motion={cam_motion} "
        f"seed={seed} device={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender + fetch scene ------------------ #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)
    blend, scene_key, fallback_note = resolve_scene(scene_arg)

    script_path = os.path.join(WORK_DIR, "cx_frame_subset_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    anchor_timeout = 1800

    # ---- render EXACTLY the assigned frames at ANCHOR quality, timing each --- #
    per_frame_render_s = {}
    devices = set()
    t_render0 = time.perf_counter()
    for t in frame_indices:
        frame_no = t + 1  # Blender frames are 1-based; index t maps to frame t+1
        exr = os.path.join(WORK_DIR, f"anchor_{frame_no:04d}.exr")
        wall_s, dev, _resolved = run_blender_frame(
            blender_bin, script_path, blend=blend, out_exr=exr,
            res_x=res_x, res_y=res_y, spp=draft_spp, frame=frame_no,
            nframes=nframes, cam_motion=cam_motion, seed=seed, bounces=bounces,
            device_pref=device_pref, timeout_s=anchor_timeout,
            adaptive=True, adaptive_thr=adaptive_threshold,
            adaptive_min=adaptive_min_samples, denoiser=denoiser,
            guides=denoise_guides, light_tree=light_tree,
        )
        per_frame_render_s[t] = wall_s
        devices.add(dev)
        log(f"frame index {t} (blender frame {frame_no}) rendered in {wall_s:.3f}s")
    t_subset_render_s = time.perf_counter() - t_render0

    device = "|".join(sorted(devices)) if devices else "unknown"
    fell_to_cpu = "CPU" in device
    t_pod_subprocess_s = time.perf_counter() - t_pod0

    note = (
        f"REAL per-frame ANCHOR-quality render times for frame indices {frame_indices} "
        f"of the animated '{scene_key}' shot ({res_x}x{res_y}, nframes={nframes}, "
        f"draft_spp={draft_spp}, adaptive_thr={adaptive_threshold}, denoiser={denoiser}, "
        f"guides={denoise_guides}, light_tree={light_tree}, bounces={bounces}, "
        f"cam_motion={cam_motion}). Same DOLLY+PAN+YAW camera keyframes and same anchor "
        f"stack as exp_render_stack.py so frame t is the keystone's frame t. Every "
        f"per_frame_render_s is a real time.perf_counter() whole-subprocess wall-clock "
        f"(launch + .blend load + BVH + adaptive trace + in-pipeline denoise + EXR "
        f"encode, NO exclusions). t_subset_render_s = summed render wall-clock of the "
        f"assigned frames; t_pod_subprocess_s = the whole per-pod span this runner saw "
        f"(bootstrap + scene fetch + all frame renders). modeled:false — no reprojection "
        f"or crop model here; each assigned frame is rendered FULLY and timed."
    )
    if fallback_note:
        note += " NOTE: " + fallback_note + "."
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        "frame_indices": [int(i) for i in frame_indices],
        "frames_rendered": int(len(frame_indices)),
        # per-frame render times in the SAME order as frame_indices (a real measurement each)
        "per_frame_render_s": [round(float(per_frame_render_s[t]), 4) for t in frame_indices],
        "t_subset_render_s": round(float(t_subset_render_s), 4),
        "t_pod_subprocess_s": round(float(t_pod_subprocess_s), 4),
        "nframes": int(nframes),
        "draft_spp": int(draft_spp),
        "adaptive_threshold": float(adaptive_threshold),
        "adaptive_min_samples": int(adaptive_min_samples),
        "denoiser": denoiser,
        "denoise_guides": bool(denoise_guides),
        "light_tree": bool(light_tree),
        "bounces": int(bounces),
        "resolution": f"{res_x}x{res_y}",
        "cam_motion": float(cam_motion),
        "seed": int(seed),
        "scene": scene_key,
        "requested_scene": scene_arg,
        "device": device,
        "modeled": False,
        "note": note,
    }

    log(f"RESULT frames={len(frame_indices)} t_subset_render_s={t_subset_render_s:.2f}s "
        f"t_pod_subprocess_s={t_pod_subprocess_s:.2f}s device={device}")
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
