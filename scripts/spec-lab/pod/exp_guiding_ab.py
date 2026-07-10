#!/usr/bin/env python3
"""
exp_guiding_ab.py — TRACK 1, Phase 0a: does Open PGL path guiding help PER-SAMPLE,
                    once OUR denoiser has already eaten the variance headroom?

This is the SIMPLE, definitely-working half of Track 1 Phase 0 (the branch build in
run_restir_branch_build.py is the risky half). It needs NO Cycles fork and NO custom
build: it flips Cycles' STOCK Open PGL path-guiding checkbox on our existing, unforked
Blender and measures the effect AFTER our OIDN denoise anchor.

The one question it answers (the Phase-0 gate, verbatim from the plan):
    "Does smarter sampling even help once the denoiser has already eaten the variance
     headroom?"  -> emitted as the boolean field `guiding_helps_post_denoise`.

METHODOLOGY (honest, and the honesty matters — see the disclosures baked into `note`):
  * EQUAL SAMPLE COUNT, not equal wall-clock. Both arms render the SAME scene at the
    SAME spp; guiding ON simply spends some of those samples learning a guiding
    distribution. We are asking "is quality-per-sample better with guiding?", so spp —
    not seconds — is the controlled variable. (Guiding ON is SLOWER per frame at equal
    spp because of the training/query overhead; that cost is real and is NOT hidden — we
    report both arms' wall time. Equal-time is a different, later question.)
  * CPU-ONLY, DISCLOSED. Cycles' Open PGL path guiding is implemented for the CPU device
    ONLY (surface + volume guiding; GPU guiding is unfunded-at-write-time upstream work).
    So this whole trial is FORCED onto device=CPU — both guiding arms AND the reference —
    so the ONLY variable between the two arms is the guiding checkbox. This is disclosed
    in `device_forced_cpu`, `cpu_disclosure`, and `note`. See the enable + disclosure
    sites flagged with the CX-DISCLOSURE / CX-GUIDING-ENABLE comments below.
  * DENOISE BOTH ARMS IDENTICALLY. Each arm is denoised in-pipeline with OpenImageDenoise
    (RGB+albedo+normal guide passes, ACCURATE prefilter) — the same OIDN path the rest of
    the spec-lab uses (exp_cycles_render_prod.py). The denoise is applied byte-identically
    to both arms; the only difference upstream is the guiding checkbox.
  * SCORE ON THE SHARED HARNESS. Both denoised arms are scored against a TRUE high-spp
    reference with compute_ssim_global_and_tiles() (global + 8x8-tile worst + p5) — the
    EXACT function every other render experiment this session used, imported verbatim so
    these numbers compose directly with the banked stack/analytical numbers.

VERDICT: `guiding_helps_post_denoise` is True iff guiding ON beats guiding OFF on the
WORST-TILE SSIM (our gating metric, not the global average) by more than VERDICT_MARGIN.
worst-tile is the honest metric because guiding's whole promise is fixing the noisiest,
hardest-lit regions (deep indirect, caustic-ish corners) — exactly the tiles a global
average washes out. We also report the global and p5 deltas so a human can judge.

HONESTY GUARD: if the render reports that guiding did NOT actually activate
(CX_GUIDING_ACTIVE=0 — e.g. a build without Open PGL, or a silent GPU no-op), the verdict
is forced to null and the note screams it. We never claim a guiding result guiding didn't
produce. modeled is FALSE throughout — real Cycles renders, real OIDN, real SSIM.

Emits ONE json line on stdout (the metrics). Contract (identical to the rest of the lab):
human logs -> stderr; the LAST stdout line is exactly one JSON object; any failure emits
{"error":...} as the last stdout line and exits 0 (never hangs, never crashes silently).

Params (argv[1] JSON), all optional:
  spp          : int   EQUAL samples/pixel for BOTH arms                   (default 64)
  ref_spp      : int   high-spp ground-truth reference                     (default 2048)
  res          : int   square image side length in px                      (default 512)
  seed         : int   Cycles + jitter seed (determinism)                  (default 0)
  bounces      : int   light-path bounces (enough GI for guiding to learn) (default 12)
  guiding_training_samples : int  Open PGL training iterations             (default 128)
  denoiser     : "oidn" (default) | "none"   applied identically to both arms
  device       : IGNORED / FORCED to CPU (path guiding is CPU-only) — disclosed, not honored
  blender_url  : str   override the Blender 4.x LTS tarball URL            (default 4.2.0 LTS)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# The SHARED quality metric — imported VERBATIM so guiding numbers compose      #
# directly with every other render experiment this session (global + 8x8-tile  #
# worst + p5 SSIM). Primary path: import the real function from the sibling     #
# runner that already ships in this same pod/ dir. Fallback (runner shipped in  #
# isolation): a byte-identical copy, so behaviour is guaranteed the same.       #
# --------------------------------------------------------------------------- #
try:
    from exp_render_stack_analytical import compute_ssim_global_and_tiles  # type: ignore
    _SSIM_SOURCE = "imported:exp_render_stack_analytical.compute_ssim_global_and_tiles"
except Exception:  # noqa: BLE001 — sibling absent; fall back to a verbatim copy
    _SSIM_SOURCE = "vendored-verbatim-copy"

    def _tone(x):
        import numpy as np
        x = np.clip(x, 0.0, None)
        return np.clip(x / (1.0 + x), 0.0, 1.0)

    def compute_ssim_global_and_tiles(delivered_rgb, true_rgb, grid=8):
        """Return (global_ssim, worst_tile_ssim, p5_tile_ssim) between two [H,W,3]
        linear-HDR frames. Tonemapped to [0,1] first. (Verbatim copy of the function
        in exp_render_stack_analytical.py so results stay directly comparable.)"""
        import numpy as np
        from skimage.metrics import structural_similarity as ssim

        A = _tone(delivered_rgb)
        B = _tone(true_rgb)
        if A.shape != B.shape:
            raise RuntimeError(f"delivered/true shape mismatch {A.shape} vs {B.shape}")

        global_ssim = float(ssim(B, A, channel_axis=-1, data_range=1.0))

        h, w = B.shape[:2]
        ty = max(1, h // grid)
        tx = max(1, w // grid)
        tile_scores = []
        for gy in range(grid):
            y0 = gy * ty
            y1 = h if gy == grid - 1 else (gy + 1) * ty
            for gx in range(grid):
                x0 = gx * tx
                x1 = w if gx == grid - 1 else (gx + 1) * tx
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


# --------------------------------------------------------------------------- #
# Constants: Blender location + a REAL stable 4.2 LTS Linux tarball (Open PGL   #
# path guiding has shipped in Cycles since 3.4, so stock 4.2 has it on CPU).    #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/guiding_ab"

# Guiding "helps" iff it beats OFF on WORST-TILE SSIM by more than this margin.
# worst-tile is noisy tile-to-tile; 0.005 (half an SSIM point) is a deliberately
# modest bar — small enough to catch a real signal, large enough to reject noise.
VERDICT_MARGIN = 0.005


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[guiding_ab]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort X/GL system libs (Blender needs a few even for headless -b).   #
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
# 2. Self-bootstrap Blender (download + extract if absent). Idempotent.         #
# --------------------------------------------------------------------------- #
def ensure_blender(url):
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
                chunk = r.read(1 << 20)
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
        raise RuntimeError(f"blender binary not found/executable at {BLENDER_BIN} after extract")
    log(f"Blender bootstrapped at {BLENDER_BIN}")
    return BLENDER_BIN


# --------------------------------------------------------------------------- #
# 3. The Blender scene+render script (run inside Blender's own python via -P).   #
#    Builds REAL geometry with meaningful indirect light (so guiding has        #
#    something to learn), FORCES the CPU device, optionally enables Open PGL     #
#    path guiding, denoises in-pipeline with OIDN, and writes a LINEAR EXR.      #
#    It prints CX_CHOSEN_DEVICE and CX_GUIDING_ACTIVE on parseable lines so the  #
#    caller learns exactly what actually happened (never assumed).              #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                    # output EXR path (exact)
USE_DENOISE= os.environ["CX_DENOISE"] == "1"
USE_GUIDING= os.environ["CX_GUIDING"] == "1"
GUIDE_TRAIN= int(os.environ.get("CX_GUIDE_TRAIN", "128"))
BOUNCES    = int(os.environ.get("CX_BOUNCES", "12"))
SEED       = int(os.environ["CX_SEED"])

random.seed(SEED)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- REAL geometry: cube + smooth UV sphere + Suzanne + floor ---------------
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

# ---- distinct materials (real color structure for SSIM) ---------------------
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

# ---- lighting: a small, bright area light (crisp shadows + strong indirect,   #
#      the regime where path guiding is supposed to earn its keep) ------------
bpy.ops.object.light_add(type='AREA', location=(2.5, -2.5, 5.0))
area = bpy.context.active_object
area.data.energy = 1200.0
area.data.size = 4.0
area.rotation_euler = (math.radians(35), math.radians(20), math.radians(15))

# a dim ambient world so deep-indirect corners aren't pitch black (but still noisy)
world = bpy.data.worlds.new("cx_world")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs["Color"].default_value = (0.15, 0.18, 0.24, 1.0)
    bg.inputs["Strength"].default_value = 0.6

bpy.ops.object.camera_add(location=(0.0, -6.5, 3.4))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(63), 0.0, 0.0)
scene.camera = cam

# ---- Cycles engine + deterministic FIXED sampling (equal-spp is the point) --
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP
cyc.seed = SEED
cyc.use_adaptive_sampling = False   # fixed spp so the OFF/ON arms are equal-sample

# enough bounces that indirect light actually exists for guiding to learn from
for attr in ("max_bounces", "diffuse_bounces", "glossy_bounces",
             "transmission_bounces", "volume_bounces"):
    if hasattr(cyc, attr):
        try:
            setattr(cyc, attr, BOUNCES)
        except Exception as e:
            _log(f"could not set {attr}:", e)

# ===== CX-DEVICE-FORCE: path guiding is CPU-ONLY, so FORCE CPU unconditionally =
# This is the honesty-critical line: we do NOT honor any GPU preference here.
# Both the OFF and ON arms (and the caller renders the reference the same way)
# run on the CPU device, so the guiding checkbox is the ONLY variable.
scene.cycles.device = 'CPU'
chosen_device = "CPU"

# ===== CX-GUIDING-ENABLE: flip the Open PGL path-guiding checkbox (or leave off) =
# use_guiding is the master switch (Cycles / Open PGL, since Blender 3.4). The
# component switches + training-sample count are set defensively (attr names are
# stable across 4.x but we never assume). We then READ BACK use_guiding and print
# CX_GUIDING_ACTIVE so the caller knows whether guiding TRULY engaged — a build
# without Open PGL would leave it False and we must not pretend otherwise.
guiding_active = False
if USE_GUIDING:
    try:
        cyc.use_guiding = True                      # <-- THE guiding enable site
        for attr, val in (("use_surface_guiding", True),
                          ("use_volume_guiding", True),
                          ("use_guiding_direct_light", True),
                          ("use_guiding_mis_weights", True)):
            if hasattr(cyc, attr):
                try:
                    setattr(cyc, attr, val)
                except Exception as e:
                    _log(f"guiding sub-attr {attr} set failed:", e)
        if hasattr(cyc, "guiding_training_samples"):
            try:
                cyc.guiding_training_samples = GUIDE_TRAIN
            except Exception as e:
                _log("guiding_training_samples set failed:", e)
        guiding_active = bool(getattr(cyc, "use_guiding", False))
    except Exception as e:
        _log("path guiding UNAVAILABLE on this Blender build:", e)
        guiding_active = False
else:
    try:
        cyc.use_guiding = False
    except Exception:
        pass
    guiding_active = False

# ===== DENOISER (in-pipeline OIDN, applied identically to both arms) =========
cyc.use_denoising = bool(USE_DENOISE)
if USE_DENOISE:
    try:
        cyc.denoiser = 'OPENIMAGEDENOISE'
    except Exception as e:
        _log("could not set OPENIMAGEDENOISE denoiser:", e)
    # albedo+normal guide passes + ACCURATE prefilter — the same OIDN config the
    # rest of the spec-lab uses (exp_cycles_render_prod.py). Set defensively.
    try:
        cyc.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
    except Exception as e:
        _log("could not set denoising_input_passes:", e)
    try:
        cyc.denoising_prefilter = 'ACCURATE'
    except Exception as e:
        _log("could not set denoising_prefilter:", e)
    try:
        vl = scene.view_layers[0]
        if hasattr(vl, "use_pass_denoising_data"):
            vl.use_pass_denoising_data = True
        if hasattr(vl, "cycles") and hasattr(vl.cycles, "denoising_store_passes"):
            vl.cycles.denoising_store_passes = True
    except Exception as e:
        _log("view-layer denoising-data pass setup failed:", e)

# ---- render settings: square res, LINEAR EXR (32-bit float), single frame ---
# EXR keeps scene-referred LINEAR radiance so compute_ssim_global_and_tiles()
# gets exactly the linear-HDR input it tonemaps internally (matches the rest of
# the lab). use_file_extension=False so it writes EXACTLY to OUT.
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '32'
scene.render.use_file_extension = False
scene.render.filepath = OUT
scene.frame_set(1)

_log(f"render spp={SPP} res={RES} denoise={USE_DENOISE} guiding_req={USE_GUIDING} "
     f"guiding_active={guiding_active} bounces={BOUNCES} device={chosen_device} -> {OUT}")
# parseable status lines the caller greps out (never assumed):
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)
print(f"CX_GUIDING_ACTIVE={1 if guiding_active else 0}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, out_exr, denoise,
                       guiding, guide_train, bounces, seed, timeout_s):
    """Invoke Blender headless to render ONE frame on the CPU.
    Returns (wall_seconds, chosen_device, guiding_active_bool)."""
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_OUT"] = out_exr
    env["CX_DENOISE"] = "1" if denoise else "0"
    env["CX_GUIDING"] = "1" if guiding else "0"
    env["CX_GUIDE_TRAIN"] = str(guide_train)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_SEED"] = str(seed)

    cmd = [blender_bin, "-b", "-noaudio", "--factory-startup", "-P", script_path]
    log(f"render start: spp={spp} res={res} denoise={denoise} guiding={guiding} -> {out_exr}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout_s)
    wall_s = time.perf_counter() - t0

    tail = (proc.stdout or "")[-1500:]
    err_tail = (proc.stderr or "")[-1500:]
    log(f"render rc={proc.returncode} wall={wall_s:.3f}s")
    if err_tail.strip():
        log("blender stderr tail:\n" + err_tail)

    chosen_device = "unknown"
    guiding_active = False
    for line in (proc.stdout or "").splitlines():
        if line.startswith("CX_CHOSEN_DEVICE="):
            chosen_device = line.split("=", 1)[1].strip()
        elif line.startswith("CX_GUIDING_ACTIVE="):
            guiding_active = line.split("=", 1)[1].strip() == "1"

    ok = ("CX_RENDER_DONE" in (proc.stdout or "")) and os.path.isfile(out_exr)
    if proc.returncode != 0 or not ok:
        raise RuntimeError(
            f"blender render failed (rc={proc.returncode}, out_exists="
            f"{os.path.isfile(out_exr)}); stdout tail: {tail[-600:]}"
        )
    return wall_s, chosen_device, guiding_active


# --------------------------------------------------------------------------- #
# 4. LINEAR EXR loader -> [H,W,3] float32 (OpenEXR -> imageio -> cv2 fallback).  #
# --------------------------------------------------------------------------- #
def load_exr_rgb(path):
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
            return np.frombuffer(f.channel(name, pt), dtype=np.float32).reshape(h, w).copy()

        def _find(names):
            for n in names:
                if n in chans:
                    return n
            return None

        r = _find(["R", "Combined.R", "RenderLayer.Combined.R"])
        g = _find(["G", "Combined.G", "RenderLayer.Combined.G"])
        b = _find(["B", "Combined.B", "RenderLayer.Combined.B"])
        if not (r and g and b):
            raise RuntimeError(
                f"EXR {path} missing an R/G/B channel (found channels: {chans}) -- "
                "refusing to silently substitute a zero channel into an SSIM score"
            )
        color = np.stack([_chan(r), _chan(g), _chan(b)], axis=-1)
        f.close()
        return color.astype(np.float32)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"OpenEXR read failed ({type(e).__name__}: {e}); trying imageio")

    try:
        import imageio.v2 as imageio  # type: ignore
        arr = np.asarray(imageio.imread(path), dtype=np.float32)
        if arr.ndim == 3 and arr.shape[-1] >= 3:
            return arr[..., :3].astype(np.float32)
        return np.stack([arr] * 3, axis=-1).astype(np.float32)
    except Exception as e:  # noqa: BLE001
        log(f"imageio EXR read failed ({type(e).__name__}: {e}); trying cv2")

    import cv2  # type: ignore
    arr = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if arr is None:
        raise RuntimeError(f"could not read EXR {path} via OpenEXR/imageio/cv2")
    if arr.ndim == 3 and arr.shape[-1] >= 3:
        return arr[..., :3][..., ::-1].astype(np.float32)  # BGR -> RGB
    return np.stack([arr] * 3, axis=-1).astype(np.float32)


def score_arm(delivered_exr, ref_exr):
    """(global, worst_tile, p5) SSIM of a denoised arm vs the true reference."""
    delivered = load_exr_rgb(delivered_exr)
    true = load_exr_rgb(ref_exr)
    g, wt, p5 = compute_ssim_global_and_tiles(delivered, true, grid=8)
    return round(float(g), 4), round(float(wt), 4), round(float(p5), 4)


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    spp = max(1, int(params.get("spp", 64)))
    ref_spp = max(spp, int(params.get("ref_spp", 2048)))
    res = max(64, int(params.get("res", 512)))
    seed = int(params.get("seed", 0))
    bounces = max(1, int(params.get("bounces", 12)))
    guide_train = max(1, int(params.get("guiding_training_samples", 128)))
    denoiser = str(params.get("denoiser", "oidn")).lower()
    if denoiser not in ("oidn", "none"):
        raise RuntimeError(f"bad denoiser {denoiser!r}; expected oidn|none")
    denoise = denoiser == "oidn"
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))

    # ---- CX-DISCLOSURE: the device honesty disclosure happens HERE ----------
    # Whatever the caller passed for `device` is IGNORED. Path guiding is CPU-only,
    # so the entire trial is forced to CPU and we record exactly what was requested
    # vs. what we did, surfaced in the emitted `cpu_disclosure` + `note`.
    device_requested = str(params.get("device", "AUTO")).upper()
    cpu_disclosure = (
        f"device={device_requested!r} was IGNORED and FORCED to CPU: Cycles' Open PGL "
        "path guiding is implemented for the CPU device ONLY, so both arms AND the "
        "reference render on CPU — the guiding checkbox is the sole variable."
    )
    log(cpu_disclosure)
    log(f"params: spp={spp} ref_spp={ref_spp} res={res} bounces={bounces} "
        f"guide_train={guide_train} denoiser={denoiser} seed={seed}")
    log(f"SSIM metric source: {_SSIM_SOURCE}")

    os.makedirs(WORK_DIR, exist_ok=True)
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_guiding_scene.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    ref_exr = os.path.join(WORK_DIR, "reference.exr")
    off_exr = os.path.join(WORK_DIR, "arm_off.exr")
    on_exr = os.path.join(WORK_DIR, "arm_on.exr")

    # generous CPU timeouts (high-spp CPU reference is the slow one)
    arm_timeout = 1200
    ref_timeout = 2400

    # ---- 1) REFERENCE: high spp, NO denoise, NO guiding, CPU (ground truth) --
    ref_wall, ref_dev, _ = run_blender_render(
        blender_bin, script_path, ref_spp, res, ref_exr,
        denoise=False, guiding=False, guide_train=guide_train, bounces=bounces,
        seed=seed, timeout_s=ref_timeout)

    # ---- 2) ARM OFF: equal spp, guiding OFF, denoise ON, CPU ----------------
    off_wall, off_dev, off_guiding = run_blender_render(
        blender_bin, script_path, spp, res, off_exr,
        denoise=denoise, guiding=False, guide_train=guide_train, bounces=bounces,
        seed=seed, timeout_s=arm_timeout)

    # ---- 3) ARM ON: equal spp, guiding ON, denoise ON, CPU ------------------
    on_wall, on_dev, on_guiding = run_blender_render(
        blender_bin, script_path, spp, res, on_exr,
        denoise=denoise, guiding=True, guide_train=guide_train, bounces=bounces,
        seed=seed, timeout_s=arm_timeout)

    # ---- 4) SCORE both denoised arms vs the true reference ------------------
    off_g, off_wt, off_p5 = score_arm(off_exr, ref_exr)
    on_g, on_wt, on_p5 = score_arm(on_exr, ref_exr)
    log(f"OFF: global={off_g} worst_tile={off_wt} p5={off_p5} wall={off_wall:.1f}s")
    log(f"ON : global={on_g} worst_tile={on_wt} p5={on_p5} wall={on_wall:.1f}s "
        f"(guiding_active={on_guiding})")

    worst_tile_delta = round(on_wt - off_wt, 4)
    global_delta = round(on_g - off_g, 4)
    p5_delta = round(on_p5 - off_p5, 4)

    # ---- 5) VERDICT (honesty-guarded) ---------------------------------------
    # The verdict is only meaningful if guiding ACTUALLY engaged on the ON arm.
    if not on_guiding:
        verdict = None
        verdict_note = ("INVALID: guiding did NOT activate on the ON arm "
                        "(CX_GUIDING_ACTIVE=0) — this Blender build lacks Open PGL or "
                        "silently no-oped; NO guiding conclusion can be drawn.")
    else:
        verdict = bool(worst_tile_delta > VERDICT_MARGIN)
        verdict_note = (
            f"guiding {'HELPS' if verdict else 'does NOT help'} post-denoise: "
            f"worst-tile SSIM moved {worst_tile_delta:+.4f} "
            f"(threshold +{VERDICT_MARGIN}); global {global_delta:+.4f}, p5 {p5_delta:+.4f}."
        )
    log(verdict_note)

    fell_to_cpu = True  # always — we forced it
    note = (
        "REAL Cycles render; Track1 Phase0a Open PGL guiding A/B at EQUAL SAMPLE COUNT "
        "(not equal wall-clock), both arms denoised identically with OIDN "
        "(RGB+albedo+normal, ACCURATE prefilter), scored vs a high-spp reference with "
        "compute_ssim_global_and_tiles (8x8 tiles). " + cpu_disclosure + " " + verdict_note
    )
    if denoiser == "none":
        note += " NOTE: denoiser=none — this measures guiding BEFORE any denoise, which " \
                "is NOT the gate question (the gate is post-denoise); use denoiser=oidn."

    metrics = {
        "experiment": "guiding_ab",
        "modeled": False,
        "device": "CPU",
        "device_forced_cpu": fell_to_cpu,
        "device_requested": device_requested,
        "cpu_disclosure": cpu_disclosure,
        "equal_sample_count": spp,
        "ref_spp": ref_spp,
        "resolution": f"{res}x{res}",
        "bounces": bounces,
        "guiding_training_samples": guide_train,
        "denoiser": denoiser,
        "seed": seed,
        "ssim_metric_source": _SSIM_SOURCE,
        "verdict_margin": VERDICT_MARGIN,
        # ---- the headline verdict field (may be null if guiding never engaged) ----
        "guiding_helps_post_denoise": verdict,
        "verdict_note": verdict_note,
        # ---- per-arm full metrics ----
        "arm_off": {
            "guiding": False, "guiding_active": bool(off_guiding),
            "global_ssim": off_g, "worst_tile_ssim": off_wt, "p5_tile_ssim": off_p5,
            "render_wall_s": round(float(off_wall), 3), "device": off_dev,
        },
        "arm_on": {
            "guiding": True, "guiding_active": bool(on_guiding),
            "global_ssim": on_g, "worst_tile_ssim": on_wt, "p5_tile_ssim": on_p5,
            "render_wall_s": round(float(on_wall), 3), "device": on_dev,
        },
        # ---- deltas (ON - OFF); worst-tile is the gating one ----
        "worst_tile_delta": worst_tile_delta,
        "global_delta": global_delta,
        "p5_delta": p5_delta,
        # ---- honest wall-clock cost of guiding at equal spp ----
        "reference_wall_s": round(float(ref_wall), 3),
        "guiding_wallclock_overhead_x": round(on_wall / max(off_wall, 1e-9), 3),
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
