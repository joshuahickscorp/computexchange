#!/usr/bin/env python3
"""
exp_cycles_render.py — Track C (FLAGSHIP, REAL): 3D path tracing with Blender Cycles.

This is the honest, genuine-render version of the render-speculation test. It does NOT
model Monte-Carlo noise with a numpy stand-in (that is exp_render_denoise.py, modeled:true).
Here we ship a REAL path tracer — Blender's Cycles — trace real geometry, and measure real
wall-clock render times and real SSIM. modeled is FALSE for this runner.

The methodology under test (identical across the whole spec-lab wave):
  * REFERENCE (ground truth) = a HIGH-samples Cycles render (e.g. 512 spp). This is the
    expensive full-quality output an unbiased path tracer converges toward.
  * DRAFT (cheap approximation) = a LOW-samples Cycles render (e.g. 16 spp) WITH Cycles'
    built-in OpenImageDenoise (OIDN) turned on. Low spp is cheap and noisy; the in-pipeline
    denoiser is the "verify/correct" step that pulls the noisy draft toward the reference.
  * VERIFY/quality gate = SSIM(draft_denoised, reference). If it clears a tolerance we would
    accept the cheap draft; we report the SSIM and the honest wall-clock speedup either way.

What is REAL here (modeled:false):
  * The geometry (cube + UV sphere + Suzanne monkey), the area light, the world light, the
    camera — all real Blender objects.
  * The path tracing — Cycles actually traces rays for both the draft and the reference.
  * The OIDN denoise — Cycles denoises the draft in-pipeline (so denoise time is already
    inside real_render_s_draft; no separate accounting fudge).
  * The two render wall-times — measured with time.perf_counter around real subprocess renders.
  * The SSIM — computed by scikit-image on the two real output PNGs.
  * net_speedup = real_render_s_ref / real_render_s_draft — the honest cheap-vs-full ratio.

Self-bootstrap: if /root/blender/blender is absent we download a real Blender 4.2 LTS Linux
tarball and extract it. Blender ships its own python + Cycles + OIDN, so no heavy system deps
are needed — only a handful of X/GL shared libs which the caller/setup best-effort apt-installs.

Params (argv[1] JSON), all optional:
  draft_spp   : int  cheap draft samples per pixel                  (default 16)
  ref_spp     : int  expensive reference samples per pixel          (default 512)
  res         : int  square image side length in px                 (default 256)
  mode        : "fixed" (default) | "adaptive"                      (see below)
  seed        : int  Cycles + object jitter seed (determinism)      (default 0)
  blender_url : str  override the download URL (real 4.x LTS)       (default 4.2.0 LTS)
  device      : "AUTO" (default) | "GPU" | "CPU"  compute preference

  mode="adaptive": render the draft, then render the reference at ref_spp but only
  *count* the extra render cost over the regions where the denoised draft's local variance
  is high (a simple variance-guided accounting of where the extra samples "mattered").
  The core, always-honest test is the fixed draft-vs-ref render-time ratio.

Emits ONE json line on stdout (the metrics), e.g.:
  {"net_speedup","quality","spp_ratio","real_render_s_draft","real_render_s_ref",
   "device","resolution","modeled":false,"note":...}

Contract: human logs -> stderr; the LAST stdout line is exactly one JSON object; any failure
emits {"error":...} as the last stdout line and exits (never hangs, never crashes silently).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Constants: where Blender lives, and a REAL stable 4.2 LTS Linux tarball URL. #
# 4.2 is a Long-Term-Support release; this download path pattern is the one    #
# Blender has published for years (download.blender.org/release/BlenderX.Y/).  #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/cycles_render"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[cycles_render]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs. Blender needs a few X/GL shared libs even for a  #
#    headless -b render. This is idempotent and never fatal (|| true style).   #
# --------------------------------------------------------------------------- #
def ensure_system_libs():
    pkgs = ["libxi6", "libxxf86vm1", "libxfixes3", "libxrender1", "libgl1"]
    try:
        # -y and DEBIAN_FRONTEND so it never blocks on a prompt.
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", *pkgs],
            check=False, capture_output=True, timeout=300, env=env,
        )
        log(f"apt-get best-effort installed: {' '.join(pkgs)} (failures ignored)")
    except Exception as e:  # noqa: BLE001 — best-effort, must never abort
        log(f"apt-get for X/GL libs failed (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# 2. Self-bootstrap Blender: download + extract the tarball if not present.    #
#    Idempotent: if BLENDER_BIN already exists we skip straight to using it.    #
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

    # Extract with the system tar (handles .xz); strip the top-level versioned
    # dir so the binary lands exactly at BLENDER_DIR/blender.
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
#    It builds REAL geometry, sets Cycles, picks GPU if available, and renders  #
#    ONE frame at the spp / denoise config passed via env vars. It prints the   #
#    chosen device + a DONE sentinel so the caller can learn GPU-vs-CPU.        #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                 # output PNG path
USE_DENOISE= os.environ["CX_DENOISE"] == "1"      # draft: on, ref: off
SEED       = int(os.environ["CX_SEED"])
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")  # AUTO | GPU | CPU

random.seed(SEED)

# ---- start from an empty scene (delete the default cube etc.) ---------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- REAL geometry: a cube, a UV sphere, and Suzanne the monkey -------------
# These are genuine meshes; Cycles path-traces light bouncing off them.
bpy.ops.mesh.primitive_cube_add(size=1.4, location=(-1.6, 0.0, 0.7))
cube = bpy.context.active_object

bpy.ops.mesh.primitive_uv_sphere_add(radius=0.9, location=(1.5, 0.4, 0.9),
                                     segments=48, ring_count=24)
sphere = bpy.context.active_object
# smooth-shade the sphere so its highlight is curved (denoiser-relevant)
for p in sphere.data.polygons:
    p.use_smooth = True

bpy.ops.mesh.primitive_monkey_add(size=1.6, location=(0.0, -0.3, 0.8))
monkey = bpy.context.active_object
monkey.rotation_euler = (0.0, 0.0, math.radians(35))

# a large floor plane to catch shadows / bounce light (more realistic GI)
bpy.ops.mesh.primitive_plane_add(size=20.0, location=(0.0, 0.0, 0.0))
floor = bpy.context.active_object

# ---- simple distinct materials so SSIM has real color structure -------------
def make_mat(name, rgba, rough=0.4, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = rgba
        # input names differ slightly across 4.x; set defensively.
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
# aim the area light roughly at the scene center
area.rotation_euler = (math.radians(35), math.radians(20), math.radians(15))

# world (ambient sky) light so shadows aren't pitch black
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

# ---- Cycles engine + deterministic sampling ---------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False   # fixed spp so draft-vs-ref is a clean ratio
cyc.use_denoising = bool(USE_DENOISE)
if USE_DENOISE:
    # OpenImageDenoise is the CPU/GPU neural denoiser Blender ships with.
    try:
        cyc.denoiser = 'OPENIMAGEDENOISE'
    except Exception as e:
        _log("could not set OPENIMAGEDENOISE denoiser:", e)

# ---- device selection: try GPU (OPTIX -> CUDA -> HIP -> ONEAPI), else CPU ----
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
            # refresh the device list for this backend
            try:
                prefs.get_devices()
            except Exception:
                pass
            gpu_devs = [d for d in prefs.devices
                        if getattr(d, "type", "CPU") not in ("CPU",)]
            if gpu_devs:
                # enable every GPU device (and disable CPU to avoid a slow hybrid)
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
# make renders reproducible frame-to-frame
scene.frame_set(1)

_log(f"rendering spp={SPP} res={RES} denoise={USE_DENOISE} device={chosen_device} -> {OUT}")
# print the device on a parseable line so the caller learns GPU-vs-CPU
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, out_png, denoise,
                       seed, device_pref, timeout_s):
    """Invoke Blender headless to render ONE frame. Returns (wall_seconds, chosen_device).

    The render wall-time is measured around the subprocess: this is the honest, end-to-end
    cost of producing this frame at these params (including OIDN denoise time when enabled,
    because Cycles denoises in-pipeline before writing the PNG).
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_png
    env["CX_DENOISE"] = "1" if denoise else "0"
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref

    # -b (background/headless), -noaudio, -P script. --factory-startup keeps user
    # prefs from leaking in; the script itself also read_factory_settings.
    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    log(f"render start: spp={spp} res={res} denoise={denoise} -> {out_png}")
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout_s
    )
    wall_s = time.perf_counter() - t0

    # surface Blender's stderr/stdout tail into our log trail for debugging.
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
# 4. Quality = SSIM(draft_denoised, reference) on the REAL output PNGs.        #
# --------------------------------------------------------------------------- #
def load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def compute_ssim(draft_png, ref_png):
    """SSIM between the two real renders (color-aware, data_range=1.0)."""
    import numpy as np  # noqa: F401  (kept for parity / potential debugging)
    from skimage.metrics import structural_similarity as ssim
    a = load_png_float(draft_png)
    b = load_png_float(ref_png)
    return float(ssim(b, a, channel_axis=-1, data_range=1.0))


# --------------------------------------------------------------------------- #
# 5. Optional adaptive accounting: how much of the reference's extra cost fell #
#    on genuinely-noisy (high local variance) regions of the denoised draft.   #
#    This does NOT re-render; it is a variance-guided *weighting* of the        #
#    already-measured reference render time, so it stays honest and cheap.      #
# --------------------------------------------------------------------------- #
def adaptive_extra_fraction(draft_png, res, tile=16):
    """Return the fraction of tiles whose local variance is 'high'.

    We split the denoised draft into tiles, compute per-tile variance, and call a tile
    'still noisy / detail-rich' if its variance exceeds the median. That fraction is the
    portion of the frame where extra reference samples plausibly earned their keep.
    """
    import numpy as np
    img = load_png_float(draft_png)
    gray = img.mean(axis=-1)
    h, w = gray.shape
    vars = []
    for y in range(0, h - tile + 1, tile):
        for x in range(0, w - tile + 1, tile):
            vars_ = gray[y:y + tile, x:x + tile].var()
            vars.append(vars_)
    if not vars:
        return 1.0
    vars = np.asarray(vars, dtype=np.float64)
    thresh = float(np.median(vars))
    frac = float((vars > thresh).mean())
    # guard against degenerate all-equal frames
    return max(0.05, min(1.0, frac))


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    draft_spp = int(params.get("draft_spp", 16))
    ref_spp = int(params.get("ref_spp", 512))
    res = int(params.get("res", 256))
    mode = str(params.get("mode", "fixed"))
    seed = int(params.get("seed", 0))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))
    device_pref = str(params.get("device", "AUTO")).upper()

    draft_spp = max(1, draft_spp)
    ref_spp = max(draft_spp, ref_spp)
    res = max(32, res)

    log(f"params: draft_spp={draft_spp} ref_spp={ref_spp} res={res} mode={mode} "
        f"seed={seed} device_pref={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender -------------------------------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    # write the scene script Blender will run for both renders
    script_path = os.path.join(WORK_DIR, "cx_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    draft_png = os.path.join(WORK_DIR, "draft.png")
    ref_png = os.path.join(WORK_DIR, "reference.png")

    # per-render subprocess timeouts (leave headroom under the ~20-min budget).
    # The reference (high spp) gets the lion's share.
    draft_timeout = 300
    ref_timeout = 900

    # ---- 1) DRAFT: low spp + OIDN denoise (real path trace) ---------------- #
    real_render_s_draft, dev_draft = run_blender_render(
        blender_bin, script_path, draft_spp, res, draft_png,
        denoise=True, seed=seed, device_pref=device_pref, timeout_s=draft_timeout,
    )

    # ---- 2) REFERENCE: high spp, no denoise (real path trace) -------------- #
    real_render_s_ref, dev_ref = run_blender_render(
        blender_bin, script_path, ref_spp, res, ref_png,
        denoise=False, seed=seed, device_pref=device_pref, timeout_s=ref_timeout,
    )

    device = dev_ref if dev_ref == dev_draft else f"{dev_draft}|{dev_ref}"
    fell_to_cpu = device.startswith("CPU") or "CPU" in device

    # ---- 3) VERIFY: SSIM(draft_denoised, reference) on the REAL PNGs ------- #
    quality = compute_ssim(draft_png, ref_png)
    log(f"SSIM(draft_denoised, reference) = {quality:.4f}")

    # ---- 4) net_speedup = honest wall-clock ratio -------------------------- #
    spp_ratio = ref_spp / float(draft_spp)
    net_speedup = real_render_s_ref / max(real_render_s_draft, 1e-9)
    log(f"real_render_s_draft={real_render_s_draft:.3f}s "
        f"real_render_s_ref={real_render_s_ref:.3f}s "
        f"-> net_speedup={net_speedup:.3f} (spp_ratio={spp_ratio:.1f})")

    note = ("REAL Blender Cycles path-traced render; draft=low-spp+OIDN, ref=high-spp; "
            "times and SSIM measured on real renders")
    if fell_to_cpu:
        note += "; render ran on CPU (no usable GPU device found by Cycles) — NOTE"

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),
        "spp_ratio": round(float(spp_ratio), 4),
        "real_render_s_draft": round(float(real_render_s_draft), 4),
        "real_render_s_ref": round(float(real_render_s_ref), 4),
        "device": device,
        "resolution": f"{res}x{res}",
        "mode": mode,
        "modeled": False,
        "note": note,
    }

    # ---- 5) optional adaptive accounting (variance-weighted extra cost) ---- #
    if mode == "adaptive":
        frac = adaptive_extra_fraction(draft_png, res)
        # If only `frac` of the frame truly needed the reference's extra samples, an
        # ideal adaptive pass would pay draft + frac*(ref-draft) instead of the full ref.
        adaptive_ref_s = real_render_s_draft + frac * max(
            real_render_s_ref - real_render_s_draft, 0.0
        )
        adaptive_speedup = real_render_s_ref / max(adaptive_ref_s, 1e-9)
        metrics["adaptive_high_var_frac"] = round(float(frac), 4)
        metrics["adaptive_speedup"] = round(float(adaptive_speedup), 4)
        metrics["note"] += (
            f"; adaptive: {frac:.2f} of frame high-variance -> "
            f"variance-guided speedup {adaptive_speedup:.2f}x "
            "(accounting only, no re-render)"
        )

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
