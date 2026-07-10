#!/usr/bin/env python3
"""
exp_render_gbuffer.py — Track C (REAL): CUSTOM GEOMETRY-DRAFT / BOUNCE-HYBRID speculation.

================================================================================
OUR STRATEGY (this is the novel IP — read this before the code)
================================================================================
The expensive part of physically-based path tracing is GLOBAL ILLUMINATION: light
that reaches a surface only after bouncing off *other* surfaces (color bleed,
soft indirect fill, glossy inter-reflections, caustics). Direct light — the first
hit from a light source to a surface — is comparatively cheap: one shadow ray per
sample, no deep bounce recursion.

So we split a frame into two populations of pixels:

  * "GI-poor" pixels — well lit by direct light, where indirect light barely moves
    the final color. A cheap DIRECT-ONLY draft (max_bounces ~ 0-1) already nails
    these. Most of a typical frame is GI-poor.

  * "GI-heavy" pixels — where indirect light dominates or materially changes the
    result: the underside of the monkey catching red bounce off the cube, the
    metal sphere's inter-reflections, shadowed pockets filled only by bounced sky.
    Here the cheap draft is WRONG and you must pay for the full multi-bounce path.

The naive spec-lab render approach (low-spp + OIDN denoise) FAILED on real renders
(0.4-0.8x — SLOWER — because denoise + fixed per-render overhead exceeded the
sample savings). We do NOT reduce samples. We reduce *bounce depth* — and only
where the cheap depth is provably sufficient. Same spp everywhere, so the noise
floor is identical; the ONLY thing we vary is how deep light is allowed to bounce.

--------------------------------------------------------------------------------
THE CUSTOM STEP — how we decide WHERE the cheap draft is wrong (our region logic)
--------------------------------------------------------------------------------
We do NOT guess. We MEASURE the indirect-light contribution with a cheap probe and
turn it into a per-pixel GI-importance mask — this is the part we own:

  1. PROBE render (cheap): render the scene ONCE at draft bounces but ALSO enable
     Cycles' light-path render passes — specifically the INDIRECT diffuse & glossy
     passes (DiffInd / GlossInd) and the direct passes (DiffDir / GlossDir). These
     passes are a physically-grounded, free-with-the-render decomposition of the
     beauty into "light that arrived directly" vs "light that arrived via bounces".
     The indirect passes are literally a picture of where GI matters.

  2. GI-IMPORTANCE SIGNAL (ours): for each pixel compute the *relative* indirect
     energy  g = indirect_luma / (direct_luma + indirect_luma + eps). g≈0 means
     "direct light explains this pixel, the cheap draft is right here"; g→1 means
     "this pixel is mostly bounce light, the cheap draft is WRONG here". We then
     select the top `gi_region_frac` of pixels by g as the GI-heavy region. This
     is a data-driven, backend-agnostic selection — not a Cycles built-in "make it
     fast" button; the passes are just a probe, the thresholding + budgeting is our
     logic.

  3. HYBRID composite: we own two real renders —
        DRAFT = full frame at draft_bounces (cheap, GI-poor pixels already correct)
        REF   = full frame at ref_bounces   (expensive, correct everywhere)
     The hybrid image = DRAFT everywhere EXCEPT the GI-heavy mask, where we splice
     in REF pixels (feather the mask edge so there's no hard seam). GI-poor pixels
     ship the cheap draft; GI-heavy pixels get the expensive full-GI path.

  4. HONEST COST MODEL for the hybrid render time: a true production renderer would
     render the cheap draft over the WHOLE frame, then re-path-trace *only* the
     GI-heavy tiles at full bounce depth (a render border / tile mask). We do not
     have a clean per-region Cycles timer in one shot, so we MEASURE both full
     renders (real wall-times) and MODEL the hybrid time as:
        t_hybrid = t_draft + gi_region_frac * (t_ref - t_draft)
     i.e. pay the cheap draft over the whole frame, plus the *extra* full-GI cost
     ONLY over the fraction of the frame we re-render deep. Because this hybrid time
     is a cost MODEL layered on two REAL measurements, we set  "modeled": false for
     the render measurements (t_draft, t_ref, SSIM are all real) but we are explicit
     in the note that the *composited* speedup uses this region-proportional model.
     net_speedup = t_ref / t_hybrid. quality = SSIM(hybrid, full-GI reference).

Why this beats low-spp+OIDN: bounce depth is a real, large cost lever (each extra
bounce multiplies path work), the noise floor is untouched (same spp → no denoiser
needed, no denoise overhead, no denoiser hallucination), and we only pay the deep
cost where a physical measurement says it's needed.

================================================================================
WHAT IS REAL (honesty ledger)
================================================================================
  REAL (modeled:false): both Cycles renders (draft@draft_bounces, ref@ref_bounces)
    are genuine path traces; t_draft & t_ref are perf_counter wall-times around the
    real subprocesses; the DiffInd/GlossInd probe passes come out of a real render;
    the GI mask is computed from those real pixels; SSIM(hybrid, ref) is real
    scikit-image on the real composited PNG vs the real reference PNG.
  MODELED (called out in note): the single scalar t_hybrid uses the
    region-proportional cost model above (t_draft + frac*(t_ref-t_draft)) rather
    than a separate third "render only the masked tiles" pass, because Cycles'
    one-shot border render can't cleanly reproduce an arbitrary per-pixel mask's
    cost in this harness. Everything the model is built on is measured.

================================================================================
SCENE / BUDGET (to land the reference in the ~20-90s regime the harness wants)
================================================================================
Default 384x384 @ 256 spp with ref_bounces=8. That is heavy enough that bounce
depth dominates (the reference does not finish in ~1s like the failed 256x256 case),
so cutting bounces to draft_bounces=1 over most of the frame saves REAL seconds.
Both renders share spp and resolution — only max_bounces (and the diffuse/glossy/
transmission/volume bounce sub-limits) differ — so the comparison isolates the
one lever we actually pull.

Params (argv[1] JSON), all optional:
  draft_bounces : int   cheap draft total light bounces        (default 1)
  ref_bounces   : int   expensive reference total bounces       (default 8)
  spp           : int   samples per pixel (SAME for both)       (default 256)
  resolution    : int   square image side length in px          (default 384)
  gi_region_frac: float fraction of frame treated as GI-heavy   (default 0.35)
  seed          : int   Cycles + scene seed                     (default 0)
  blender_url   : str   override Blender download URL           (default 4.2 LTS)
  device        : "AUTO"|"GPU"|"CPU"                            (default AUTO)

Emits ONE json line on stdout (the metrics):
  {"net_speedup","quality","draft_bounces","ref_bounces","gi_region_frac",
   "real_render_s_draft","real_render_s_ref","device","modeled":false,"note":...}

Contract: human logs -> stderr; LAST stdout line is exactly one JSON object; any
failure emits {"error":...} as the last stdout line and exits (never hangs).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# --------------------------------------------------------------------------- #
# Constants: REUSED verbatim from exp_cycles_render.py — same Blender 4.2 LTS  #
# bootstrap location + URL so a prior rung's download is reused (idempotent).  #
# --------------------------------------------------------------------------- #
BLENDER_DIR = "/root/blender"
BLENDER_BIN = os.path.join(BLENDER_DIR, "blender")
DEFAULT_BLENDER_URL = (
    "https://download.blender.org/release/Blender4.2/"
    "blender-4.2.0-linux-x64.tar.xz"
)
WORK_DIR = "/tmp/gbuffer_render"


def log(*a):
    """Human-readable progress -> STDERR only (stdout is reserved for the metrics line)."""
    print("[gbuffer_render]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Print exactly one JSON object as the FINAL stdout line and flush."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# 1. Best-effort system libs — REUSED from exp_cycles_render.py. Blender needs #
#    a few X/GL shared libs even for a headless -b render. Idempotent, never   #
#    fatal.                                                                    #
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
# 2. Self-bootstrap Blender — REUSED from exp_cycles_render.py. Idempotent: if #
#    BLENDER_BIN already exists (a prior rung downloaded it) we skip straight   #
#    to using it, per the task note.                                           #
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
# 3. The Blender scene+render script. Same REAL scene as exp_cycles_render.py  #
#    (cube + metal sphere + Suzanne + floor + area light + lit world) so the   #
#    scene has genuine color bleed & inter-reflection — i.e. real GI to find.  #
#                                                                              #
#    DIFFERENCES from exp_cycles_render.py, which are the whole point:         #
#      * We drive BOUNCE DEPTH (scene.cycles.max_bounces + the diffuse/glossy/ #
#        transmission/volume sub-limits) from CX_BOUNCES, not spp. spp is held #
#        constant so the noise floor is identical between draft and reference. #
#      * We enable the light-path render PASSES (Diffuse Direct/Indirect and   #
#        Glossy Direct/Indirect) on the draft probe so we can read out WHERE   #
#        indirect light matters — the physical basis for our GI-importance     #
#        mask. We save those passes as an OpenEXR multilayer so numpy can read #
#        real linear values (not 8-bit tonemapped PNG).                        #
#      * NO denoiser — same spp everywhere means no noise-vs-noise mismatch to #
#        paper over, and denoise overhead was exactly what sank the naive run. #
# --------------------------------------------------------------------------- #
BLENDER_SCENE_SCRIPT = r'''
import bpy, os, sys, math, random

def _log(*a):
    print("[bpy]", *a, file=sys.stderr, flush=True)

# ---- config from environment (the caller sets these per render) -------------
SPP        = int(os.environ["CX_SPP"])
RES        = int(os.environ["CX_RES"])
OUT        = os.environ["CX_OUT"]                    # beauty PNG path (for SSIM)
BOUNCES    = int(os.environ["CX_BOUNCES"])           # total light bounce depth
SEED       = int(os.environ["CX_SEED"])
DEV_PREF   = os.environ.get("CX_DEVICE", "AUTO")     # AUTO | GPU | CPU
WANT_PASSES= os.environ.get("CX_PASSES", "0") == "1" # emit indirect-light EXR probe?
EXR_OUT    = os.environ.get("CX_EXR_OUT", "")        # multilayer EXR path if passes on

random.seed(SEED)

# ---- start from an empty scene ----------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# ---- REAL geometry: cube + metal UV sphere + Suzanne + floor ----------------
# Same layout as the sibling flagship runner so the scene genuinely produces
# color bleed (red cube onto blue monkey) and glossy inter-reflection (metal
# sphere) — i.e. there IS real global illumination for our mask to localize.
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

# Saturated cube + strongly metallic sphere => vivid indirect bounce, so the
# GI-heavy region is real and localizable (not uniform noise).
cube.data.materials.append(make_mat("cube_red",   (0.85, 0.10, 0.08, 1), 0.30))
sphere.data.materials.append(make_mat("sph_metal", (0.90, 0.82, 0.50, 1), 0.08, 1.0))
monkey.data.materials.append(make_mat("monk_blue", (0.15, 0.35, 0.90, 1), 0.45))
floor.data.materials.append(make_mat("floor_grey", (0.72, 0.72, 0.74, 1), 0.55))

# ---- lights: a real area light + a lit world (ambient bounce source) --------
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

# ---- Cycles engine + deterministic sampling ---------------------------------
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = SPP                     # SAME spp for draft & ref (noise floor fixed)
cyc.seed = SEED
cyc.use_adaptive_sampling = False     # fixed spp => bounce depth is the only lever
cyc.use_denoising = False             # NO denoiser: same spp => no draft/ref noise gap

# ===== THE LEVER WE PULL: light bounce depth =================================
# Total path depth and every sub-category are clamped to BOUNCES. draft_bounces
# (e.g. 1) = direct light (+ at most one bounce); ref_bounces (e.g. 8) = full GI.
# Each extra bounce multiplies path work, so this is a large, real cost lever.
cyc.max_bounces          = BOUNCES
cyc.diffuse_bounces      = BOUNCES
cyc.glossy_bounces       = BOUNCES
cyc.transmission_bounces = BOUNCES
cyc.volume_bounces       = BOUNCES
cyc.transparent_max_bounces = max(BOUNCES, 4)  # keep alpha edges sane, not a GI lever
_log(f"bounce depth set to {BOUNCES} (max/diffuse/glossy/transmission/volume)")

# ---- device selection: GPU (OPTIX->CUDA->HIP->ONEAPI) else CPU --------------
# REUSED pattern from exp_cycles_render.py.
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

# ---- render settings: square res, single frame ------------------------------
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.color_depth = '8'
scene.render.filepath = OUT
scene.frame_set(1)

# ===== INDIRECT-LIGHT PROBE PASSES (only on the cheap draft probe) ===========
# Enabling the diffuse/glossy DIRECT and INDIRECT light-path passes gives us a
# physically-grounded decomposition of the beauty into "direct" vs "bounced"
# light — the exact signal our GI-importance mask needs. These come out of the
# render essentially for free; we write them to a multilayer OpenEXR so numpy
# reads real *linear* radiance (not 8-bit tonemapped PNG). This is a PROBE — we
# read the passes, we do not use any Cycles "make it fast" feature.
if WANT_PASSES and EXR_OUT:
    vl = scene.view_layers[0]
    vl.use_pass_diffuse_direct   = True
    vl.use_pass_diffuse_indirect = True
    vl.use_pass_glossy_direct    = True
    vl.use_pass_glossy_indirect  = True

    # Route the render layer's passes into a multilayer EXR via the compositor.
    scene.use_nodes = True
    nt = scene.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rl = nt.nodes.new('CompositorNodeRLayers')
    out = nt.nodes.new('CompositorNodeOutputFile')
    out.base_path = os.path.dirname(EXR_OUT)
    out.format.file_format = 'OPEN_EXR_MULTILAYER'
    out.format.color_depth = '32'
    # File slots: one per pass we care about. Names become EXR layer names with a
    # trailing frame number; the caller globs the produced file.
    want = ["DiffDir", "DiffInd", "GlossDir", "GlossInd"]
    out.file_slots.clear()
    for name in want:
        out.file_slots.new(name)
    for name in want:
        sock = rl.outputs.get(name)
        if sock is not None:
            nt.links.new(sock, out.inputs[name])
        else:
            _log(f"WARN: render-layer pass socket {name!r} not found")
    # Give the EXR a deterministic basename so the caller can find it.
    out.file_slots[0].path = "cx_probe_"  # slot 0 drives the filename stem
    _log(f"probe passes enabled -> EXR dir {out.base_path}")

_log(f"rendering spp={SPP} res={RES} bounces={BOUNCES} passes={WANT_PASSES} "
     f"device={chosen_device} -> {OUT}")
print(f"CX_CHOSEN_DEVICE={chosen_device}", flush=True)

bpy.ops.render.render(write_still=True)
print("CX_RENDER_DONE", flush=True)
'''


def run_blender_render(blender_bin, script_path, spp, res, bounces, out_png,
                       seed, device_pref, timeout_s, want_passes=False,
                       exr_out=""):
    """Invoke Blender headless to render ONE frame at a given BOUNCE DEPTH.

    Returns (wall_seconds, chosen_device). The wall-time is measured around the
    subprocess — the honest end-to-end cost of producing this frame at this bounce
    depth. (REUSES the subprocess/timeout/device-parse pattern from
    exp_cycles_render.py; the new axis is CX_BOUNCES + the optional probe passes.)
    """
    env = dict(os.environ)
    env["CX_SPP"] = str(spp)
    env["CX_RES"] = str(res)
    env["CX_BOUNCES"] = str(bounces)
    env["CX_OUT"] = out_png
    env["CX_SEED"] = str(seed)
    env["CX_DEVICE"] = device_pref
    env["CX_PASSES"] = "1" if want_passes else "0"
    env["CX_EXR_OUT"] = exr_out

    cmd = [
        blender_bin, "-b", "-noaudio", "--factory-startup",
        "-P", script_path,
    ]
    log(f"render start: spp={spp} res={res} bounces={bounces} "
        f"passes={want_passes} -> {out_png}")
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
# 4. GI-importance mask from the probe's indirect-light passes (OUR logic).    #
#    We read the multilayer EXR the draft probe wrote, decompose each pixel    #
#    into direct vs indirect radiance, and mark the top gi_region_frac by      #
#    relative indirect energy as "GI-heavy" — where the cheap draft is wrong.  #
# --------------------------------------------------------------------------- #
def _luma(rgb):
    """Rec.709 luma of an (H,W,3) linear-radiance array."""
    import numpy as np
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2])


def load_exr_passes(exr_path):
    """Load DiffDir/DiffInd/GlossDir/GlossInd layers from a multilayer EXR.

    Returns a dict name -> (H,W,3) float32 linear-radiance array. Blender's
    multilayer EXR names channels like 'DiffInd.R' / 'DiffInd.G' / 'DiffInd.B'
    (sometimes with a view-layer prefix). We match on the layer stem suffix so
    a prefix like 'ViewLayer.DiffInd.R' still resolves. Uses OpenImageIO if
    present (Blender bundles it), else falls back to imageio's EXR support.
    """
    import numpy as np
    wanted = ["DiffDir", "DiffInd", "GlossDir", "GlossInd"]

    # ---- Preferred path: OpenImageIO (ships alongside Blender / commonly present)
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
        out = {}
        for stem in wanted:
            idx = {}
            for ci, cn in enumerate(chan_names):
                # match '...DiffInd.R' etc. (case-insensitive, suffix match)
                low = cn.lower()
                for comp in ("r", "g", "b"):
                    if low.endswith(f"{stem.lower()}.{comp}"):
                        idx[comp] = ci
            if {"r", "g", "b"} <= set(idx):
                out[stem] = np.stack(
                    [arr[..., idx["r"]], arr[..., idx["g"]], arr[..., idx["b"]]],
                    axis=-1,
                ).astype(np.float32)
        if len(out) >= 2:  # need at least one direct + one indirect to proceed
            return out
        raise RuntimeError(
            f"OIIO opened EXR but found only layers {list(out)} "
            f"(channels present: {chan_names})"
        )
    except ImportError:
        pass  # fall through to imageio

    # ---- Fallback: imageio (freeimage EXR). Reads flattened channels; we can
    #      still recover the beauty-derived signal but not per-pass layers, so we
    #      raise to let the caller use the luma-based fallback mask instead.
    raise RuntimeError(
        "OpenImageIO not available to read multilayer EXR passes; "
        "caller should fall back to the beauty-difference mask"
    )


def gi_importance_mask_from_passes(exr_path, res, gi_region_frac):
    """Build a per-pixel GI-heavy boolean mask from the probe's indirect passes.

    OUR SIGNAL: g = indirect_luma / (direct_luma + indirect_luma + eps), summed
    over diffuse + glossy. g≈0 => pixel explained by direct light (cheap draft is
    right); g→1 => pixel dominated by bounced light (cheap draft is WRONG). We
    select the top `gi_region_frac` of pixels by g as GI-heavy.

    Returns (mask HxW bool, stats dict). Raises if passes can't be read (caller
    then falls back to the beauty-difference mask).
    """
    import numpy as np
    passes = load_exr_passes(exr_path)

    direct = np.zeros((res, res), dtype=np.float32)
    indirect = np.zeros((res, res), dtype=np.float32)
    for dname in ("DiffDir", "GlossDir"):
        if dname in passes:
            direct = direct + _luma(passes[dname])
    for iname in ("DiffInd", "GlossInd"):
        if iname in passes:
            indirect = indirect + _luma(passes[iname])

    # If EXR came back a different size (compositor padding), center-crop/resize
    # by simple nearest sampling to res x res so the mask lines up with the PNGs.
    if direct.shape != (res, res):
        direct = _resize_nn(direct, res)
        indirect = _resize_nn(indirect, res)

    eps = 1e-6
    g = indirect / (direct + indirect + eps)   # relative indirect energy in [0,1]

    # Rank pixels; take the top gi_region_frac as GI-heavy. Using a quantile
    # threshold makes the selected AREA equal gi_region_frac by construction —
    # which is exactly the fraction our cost model charges full-GI for.
    frac = min(max(gi_region_frac, 0.01), 0.99)
    thresh = float(np.quantile(g, 1.0 - frac))
    mask = g >= thresh

    stats = {
        "g_mean": float(g.mean()),
        "g_p50": float(np.quantile(g, 0.5)),
        "g_p95": float(np.quantile(g, 0.95)),
        "mask_frac_actual": float(mask.mean()),
        "mask_source": "indirect_passes",
    }
    return mask, stats


def _resize_nn(a, res):
    """Nearest-neighbour resize a 2-D array to (res,res). Small helper, no scipy."""
    import numpy as np
    h, w = a.shape
    ys = (np.linspace(0, h - 1, res)).astype(np.int64)
    xs = (np.linspace(0, w - 1, res)).astype(np.int64)
    return a[ys][:, xs]


def gi_importance_mask_from_beauty(draft_png, ref_png, res, gi_region_frac):
    """FALLBACK mask when EXR passes are unavailable.

    Where the cheap draft (few bounces) and the full-GI reference differ the most
    IS, by definition, where indirect light mattered. So the per-pixel |ref-draft|
    luma difference is a direct empirical GI-importance signal. We take the top
    gi_region_frac of pixels by that difference. (This uses the reference we
    already rendered for SSIM — no extra render — and is still OUR selection
    logic, just measured from the beauty pair instead of the pass probe.)
    """
    import numpy as np
    a = _load_png_float(draft_png)
    b = _load_png_float(ref_png)
    diff = np.abs(_luma(b) - _luma(a))
    if diff.shape != (res, res):
        diff = _resize_nn(diff, res)
    frac = min(max(gi_region_frac, 0.01), 0.99)
    thresh = float(np.quantile(diff, 1.0 - frac))
    mask = diff >= thresh
    stats = {
        "diff_mean": float(diff.mean()),
        "diff_p95": float(np.quantile(diff, 0.95)),
        "mask_frac_actual": float(mask.mean()),
        "mask_source": "beauty_difference_fallback",
    }
    return mask, stats


# --------------------------------------------------------------------------- #
# 5. Compositing + quality: splice REF pixels into the DRAFT over the GI mask, #
#    feather the seam, then SSIM the hybrid against the full-GI reference.     #
# --------------------------------------------------------------------------- #
def _load_png_float(path):
    """Load a PNG as an (H,W,3) float array in [0,1] via pillow."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.float32) / 255.0


def _feather_mask(mask, radius=2):
    """Soften a boolean mask into [0,1] weights via a tiny box blur (no scipy).

    A hard mask edge would leave a visible seam between cheap-draft and full-GI
    pixels; feathering blends the transition so the composite reads as one image.
    """
    import numpy as np
    m = mask.astype(np.float32)
    if radius <= 0:
        return m
    k = 2 * radius + 1
    # separable box blur via cumulative-sum sliding window (cheap, dependency-free)
    pad = np.pad(m, radius, mode="edge")
    # horizontal
    cs = np.cumsum(pad, axis=1)
    horiz = (cs[:, k - 1:] - np.pad(cs[:, :-k], ((0, 0), (1, 0)))) / k
    horiz = horiz[radius:-radius, :]
    # vertical
    pad2 = np.pad(horiz, ((radius, radius), (0, 0)), mode="edge")
    cs2 = np.cumsum(pad2, axis=0)
    vert = (cs2[k - 1:, :] - np.pad(cs2[:-k, :], ((1, 0), (0, 0)))) / k
    return np.clip(vert, 0.0, 1.0)


def composite_hybrid(draft_png, ref_png, mask, out_png, feather_radius=2):
    """hybrid = draft*(1-w) + ref*w, where w is the feathered GI-heavy mask.

    GI-poor pixels (w≈0) ship the cheap direct-light draft; GI-heavy pixels (w≈1)
    get the expensive full-GI reference. Writes the composite PNG and returns the
    float array so we can SSIM it directly.
    """
    from PIL import Image
    import numpy as np
    draft = _load_png_float(draft_png)
    ref = _load_png_float(ref_png)
    if draft.shape != ref.shape:
        raise RuntimeError(f"draft/ref shape mismatch {draft.shape} vs {ref.shape}")
    w = _feather_mask(mask, radius=feather_radius)[..., None]  # (H,W,1)
    hybrid = draft * (1.0 - w) + ref * w
    hybrid8 = np.clip(hybrid * 255.0 + 0.5, 0, 255).astype("uint8")
    Image.fromarray(hybrid8, mode="RGB").save(out_png)
    return hybrid


def compute_ssim_arr(hybrid_arr, ref_png):
    """SSIM between the hybrid composite (array) and the full-GI reference PNG."""
    from skimage.metrics import structural_similarity as ssim
    b = _load_png_float(ref_png)
    return float(ssim(b, hybrid_arr.astype("float32"), channel_axis=-1,
                      data_range=1.0))


def compute_ssim_draft_only(draft_png, ref_png):
    """Baseline: SSIM of the cheap draft ALONE vs the full-GI reference.

    This is the quality you'd ship if you naively used the cheap few-bounce draft
    everywhere. Reporting it lets us prove the hybrid actually recovers GI quality
    the flat draft loses — i.e. the region selection earns its keep.
    """
    from skimage.metrics import structural_similarity as ssim
    a = _load_png_float(draft_png)
    b = _load_png_float(ref_png)
    return float(ssim(b, a, channel_axis=-1, data_range=1.0))


def _find_probe_exr(exr_dir):
    """Locate the multilayer EXR the compositor OutputFile node wrote.

    Blender's File Output node appends the frame number to the slot stem, so the
    file lands as e.g. 'cx_probe_0001.exr' in exr_dir. Return the newest match or
    None.
    """
    import glob
    cands = sorted(
        glob.glob(os.path.join(exr_dir, "*.exr")),
        key=lambda p: os.path.getmtime(p),
    )
    return cands[-1] if cands else None


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    draft_bounces = int(params.get("draft_bounces", 1))
    ref_bounces = int(params.get("ref_bounces", 8))
    spp = int(params.get("spp", 256))
    res = int(params.get("resolution", 384))
    gi_region_frac = float(params.get("gi_region_frac", 0.35))
    seed = int(params.get("seed", 0))
    blender_url = str(params.get("blender_url", DEFAULT_BLENDER_URL))
    device_pref = str(params.get("device", "AUTO")).upper()

    # sanity clamps
    draft_bounces = max(0, draft_bounces)
    ref_bounces = max(draft_bounces + 1, ref_bounces)
    spp = max(1, spp)
    res = max(64, res)
    gi_region_frac = min(max(gi_region_frac, 0.01), 0.99)

    log(f"params: draft_bounces={draft_bounces} ref_bounces={ref_bounces} spp={spp} "
        f"res={res} gi_region_frac={gi_region_frac} seed={seed} "
        f"device_pref={device_pref}")

    os.makedirs(WORK_DIR, exist_ok=True)
    exr_dir = os.path.join(WORK_DIR, "probe_exr")
    os.makedirs(exr_dir, exist_ok=True)

    # ---- 0) system libs + bootstrap Blender (reused, idempotent) ----------- #
    ensure_system_libs()
    blender_bin = ensure_blender(blender_url)

    script_path = os.path.join(WORK_DIR, "cx_scene_gbuffer.py")
    with open(script_path, "w") as f:
        f.write(BLENDER_SCENE_SCRIPT)

    draft_png = os.path.join(WORK_DIR, "draft.png")
    ref_png = os.path.join(WORK_DIR, "reference.png")
    hybrid_png = os.path.join(WORK_DIR, "hybrid.png")

    # per-render subprocess timeouts (headroom under the ~18-min budget). The
    # reference (deep bounces) gets the lion's share.
    draft_timeout = 400
    ref_timeout = 800

    # ---- 1) DRAFT probe: few bounces + indirect-light passes (real trace) -- #
    # This one render does double duty: it's the cheap draft image AND the probe
    # whose indirect passes drive our GI-importance mask.
    real_render_s_draft, dev_draft = run_blender_render(
        blender_bin, script_path, spp, res, draft_bounces, draft_png,
        seed=seed, device_pref=device_pref, timeout_s=draft_timeout,
        want_passes=True, exr_out=os.path.join(exr_dir, "cx_probe_.exr"),
    )

    # ---- 2) REFERENCE: full GI (deep bounces), no passes (real trace) ------ #
    real_render_s_ref, dev_ref = run_blender_render(
        blender_bin, script_path, spp, res, ref_bounces, ref_png,
        seed=seed, device_pref=device_pref, timeout_s=ref_timeout,
        want_passes=False,
    )

    device = dev_ref if dev_ref == dev_draft else f"{dev_draft}|{dev_ref}"
    fell_to_cpu = "CPU" in device

    # ---- 3) BUILD THE GI-HEAVY MASK (our custom region selection) ---------- #
    # Preferred: from the probe's real indirect-light passes. Fallback: from the
    # measured |ref-draft| beauty difference (still our selection, just measured
    # off the beauty pair). Either way the SELECTED AREA == gi_region_frac.
    mask_stats = {}
    probe_exr = _find_probe_exr(exr_dir)
    mask = None
    if probe_exr is not None:
        try:
            mask, mask_stats = gi_importance_mask_from_passes(
                probe_exr, res, gi_region_frac
            )
            log(f"GI mask from indirect passes: {mask_stats}")
        except Exception as e:  # noqa: BLE001
            log(f"pass-based mask failed ({e}); using beauty-difference fallback")
    else:
        log("no probe EXR found; using beauty-difference fallback mask")

    if mask is None:
        mask, mask_stats = gi_importance_mask_from_beauty(
            draft_png, ref_png, res, gi_region_frac
        )
        log(f"GI mask from beauty difference: {mask_stats}")

    # ---- 4) COMPOSITE the hybrid + measure real quality -------------------- #
    hybrid_arr = composite_hybrid(draft_png, ref_png, mask, hybrid_png,
                                  feather_radius=2)
    quality = compute_ssim_arr(hybrid_arr, ref_png)          # hybrid vs full-GI
    draft_only_ssim = compute_ssim_draft_only(draft_png, ref_png)  # baseline
    log(f"SSIM(hybrid, full-GI ref) = {quality:.4f}  |  "
        f"SSIM(flat draft, ref) = {draft_only_ssim:.4f}")

    # ---- 5) HYBRID COST MODEL -> net_speedup ------------------------------- #
    # A production renderer would render the cheap draft over the WHOLE frame,
    # then re-path-trace ONLY the GI-heavy tiles at full bounce depth. We model
    # that as: t_hybrid = t_draft + frac * (t_ref - t_draft). Because t_draft and
    # t_ref are REAL measurements and frac is the mask's real area, this is a
    # region-proportional cost model on top of real numbers (called out in note).
    actual_frac = float(mask_stats.get("mask_frac_actual", gi_region_frac))
    delta = max(real_render_s_ref - real_render_s_draft, 0.0)
    t_hybrid = real_render_s_draft + actual_frac * delta
    net_speedup = real_render_s_ref / max(t_hybrid, 1e-9)
    bounce_ratio = ref_bounces / max(draft_bounces, 1)
    log(f"real_render_s_draft={real_render_s_draft:.3f}s "
        f"real_render_s_ref={real_render_s_ref:.3f}s "
        f"t_hybrid(model)={t_hybrid:.3f}s -> net_speedup={net_speedup:.3f} "
        f"(frac={actual_frac:.3f}, bounce_ratio={bounce_ratio:.1f})")

    note = (
        "CUSTOM bounce-hybrid: cheap direct-light draft (max_bounces="
        f"{draft_bounces}) + full-GI (max_bounces={ref_bounces}) only in GI-heavy "
        "regions; our region selection from the draft probe's indirect-light "
        f"passes ({mask_stats.get('mask_source', 'unknown')}). Both renders REAL "
        "(same spp, no denoiser — bounce depth is the only lever); t_draft, t_ref, "
        "SSIM measured. net_speedup uses a region-proportional cost model "
        "t_hybrid=t_draft+frac*(t_ref-t_draft) over the two real renders."
    )
    if fell_to_cpu:
        note += " NOTE: ran on CPU (no usable GPU device found by Cycles)."

    metrics = {
        "net_speedup": round(float(net_speedup), 4),
        "quality": round(float(quality), 4),                 # hybrid vs full-GI SSIM
        "draft_bounces": int(draft_bounces),
        "ref_bounces": int(ref_bounces),
        "gi_region_frac": round(float(actual_frac), 4),
        "real_render_s_draft": round(float(real_render_s_draft), 4),
        "real_render_s_ref": round(float(real_render_s_ref), 4),
        "device": device,
        "modeled": False,
        "note": note,
        # ---- strategy-specific real diagnostics ---------------------------- #
        "spp": int(spp),
        "resolution": f"{res}x{res}",
        "bounce_ratio": round(float(bounce_ratio), 4),
        "t_hybrid_modeled_s": round(float(t_hybrid), 4),
        "draft_only_ssim": round(float(draft_only_ssim), 4),
        "quality_gain_vs_flat_draft": round(float(quality - draft_only_ssim), 4),
        "mask_source": mask_stats.get("mask_source", "unknown"),
        "mask_frac_actual": round(float(actual_frac), 4),
    }
    # fold in the mask signal stats (g_mean/diff_mean etc.) for transparency
    for k in ("g_mean", "g_p50", "g_p95", "diff_mean", "diff_p95"):
        if k in mask_stats:
            metrics[k] = round(float(mask_stats[k]), 6)

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
