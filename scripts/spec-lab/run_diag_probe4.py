#!/usr/bin/env python3
"""run_diag_probe4.py — SINGLE-POD Vector-pass settings sweep.

One pod, one classroom.blend load, render frame 2 several ways in ONE Blender process,
read the Vector pass stats for each. Settles which Cycles setting actually populates the
screen-space motion pass for CAMERA-ONLY ego-motion on static geometry.

Variants tested in ONE render session (all frame=2, tiny res, low spp for speed):
  A BASELINE          : current stack settings (motion_blur ON, shutter 0.01, pos START)
  B MB_OFF            : motion_blur OFF (hypothesis 1)
  C MB_ON_WARMED      : MB ON + evaluate FRAME-1/FRAME+1 before FRAME (hypothesis 2)
  D MB_ON_LINEAR_WARM : MB ON + LINEAR fcurve interp + warm (hypothesis 3 cycles-retry)
  E MB_ON_BIGSHUTTER  : MB ON, pos CENTER, shutter 0.5 (does shutter width matter?)
  F MB_ON_ROOT_EMPTY  : MB ON + parent all static geometry to a keyframed-identity empty
                        (gives Cycles a per-object motion delta; hyp3 cycles-retry variant)

For each variant we print VECTOR stats: prev(X/Y) and next(Z/W) mean/std/absmax.
A variant WORKS if std/absmax are clearly NON-zero (a few px).

Tears down on every exit path.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runpod  # noqa: E402

# gpu-provisioning-policy (task #20 rewrite, 2026-07-09): base tier A100 then H100 —
# cheapest first, then availability, COMMUNITY then SECURE at each rung; if neither
# base is available UPGRADE to H200. NEVER downgrade to L40S/RTX A6000/A40/CPU.
# Blackwell (B200/B300, sm_100/sm_120) stays out until a Blackwell-capable Blender is
# proven on-box (Blender 4.2 ships no kernels — silent CPU fallback burned $0.58 on
# 2026-07-09). H100/H200 (sm_90) carry a first-render PTX-JIT caveat: give the
# functional GPU probe JIT headroom via the runner param gpu_probe_timeout_s (see
# run_integrated_production_benchmark.py; 2026-07-09 two-pod H100 probe-timeout
# evidence).
GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 60

# The probe runs INSIDE Blender (-P). It opens the blend passed after `--`, applies the
# SAME camera keyframes exp_render_stack.py uses, and renders frame 2 under each variant,
# reading the Vector pass back each time via OpenEXR.
PROBE_PY = r'''
import bpy, os, sys, math

def _log(*a):
    print("[probe]", *a, file=sys.stderr, flush=True)

BLEND = sys.argv[sys.argv.index("--") + 1]
RES_X, RES_Y = 480, 270
SPP = 24
FRAME = 2
NFRAMES = 4
CAM_MOTION = 1.0
WD = "/tmp/probe"
os.makedirs(WD, exist_ok=True)


def apply_cam_keyframes(scene, cam, linear=False):
    scene.frame_start = 1
    scene.frame_end = NFRAMES
    base_loc = tuple(cam.location)
    base_rot = tuple(cam.rotation_euler)
    DOLLY = 0.05 * CAM_MOTION
    RISE  = 0.02 * CAM_MOTION
    YAW   = math.radians(0.8) * CAM_MOTION
    try:
        cam.animation_data_clear()
    except Exception:
        pass
    for t in range(NFRAMES):
        fr = t + 1
        cam.location = (base_loc[0] + DOLLY * t, base_loc[1], base_loc[2] + RISE * t)
        cam.rotation_euler = (base_rot[0], base_rot[1], base_rot[2] + YAW * t)
        cam.keyframe_insert(data_path="location", frame=fr)
        cam.keyframe_insert(data_path="rotation_euler", frame=fr)
    if linear:
        act = cam.animation_data.action if cam.animation_data else None
        if act:
            for fc in act.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'


def setup_common(scene):
    scene.render.engine = 'CYCLES'
    cyc = scene.cycles
    cyc.samples = SPP
    cyc.seed = 0
    cyc.use_adaptive_sampling = False
    cyc.use_denoising = False
    # GPU if available
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for backend in ('OPTIX', 'CUDA'):
            try:
                prefs.compute_device_type = backend
                prefs.get_devices()
            except Exception:
                continue
            gpu = [d for d in prefs.devices if getattr(d, "type", "CPU") != "CPU"]
            if gpu:
                for d in prefs.devices:
                    d.use = (getattr(d, "type", "CPU") != "CPU")
                scene.cycles.device = 'GPU'
                break
        else:
            scene.cycles.device = 'CPU'
    except Exception:
        scene.cycles.device = 'CPU'
    vl = scene.view_layers[0]
    vl.use_pass_vector = True
    vl.use_pass_z = True
    vl.use_pass_combined = True
    r = scene.render
    r.resolution_x = RES_X
    r.resolution_y = RES_Y
    r.resolution_percentage = 100
    r.image_settings.file_format = 'OPEN_EXR_MULTILAYER'
    r.image_settings.color_mode = 'RGBA'
    r.image_settings.color_depth = '32'
    r.image_settings.exr_codec = 'ZIP'


def fresh_scene():
    bpy.ops.wm.open_mainfile(filepath=BLEND)
    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        for ob in scene.objects:
            if ob.type == 'CAMERA':
                cam = ob; scene.camera = ob; break
    return scene, cam


def render_variant(tag, *, motion_blur, shutter=0.01, position='START',
                   warm=False, linear=False, root_empty=False):
    scene, cam = fresh_scene()
    apply_cam_keyframes(scene, cam, linear=linear)
    setup_common(scene)
    scene.render.use_motion_blur = motion_blur
    if motion_blur:
        try:
            scene.cycles.motion_blur_position = position
        except Exception:
            pass
        scene.render.motion_blur_shutter = shutter
    if root_empty:
        # parent every non-camera, non-light object to a single empty and keyframe the
        # empty at IDENTITY on all frames — gives Cycles an explicit per-object transform
        # so static geometry gets motion-step data relative to the moving camera.
        bpy.ops.object.empty_add(location=(0, 0, 0))
        root = bpy.context.active_object
        root.name = "cx_root"
        for ob in list(scene.objects):
            if ob is root or ob is cam:
                continue
            if ob.type in ('CAMERA', 'LIGHT'):
                continue
            if ob.parent is None:
                try:
                    mw = ob.matrix_world.copy()
                    ob.parent = root
                    ob.matrix_world = mw
                except Exception:
                    pass
        for t in range(NFRAMES):
            root.location = (0, 0, 0)
            root.rotation_euler = (0, 0, 0)
            root.keyframe_insert(data_path="location", frame=t + 1)
            root.keyframe_insert(data_path="rotation_euler", frame=t + 1)
    out = os.path.join(WD, tag + "_")
    scene.render.filepath = out
    if warm:
        for wf in (max(scene.frame_start, FRAME - 1), min(scene.frame_end, FRAME + 1), FRAME):
            scene.frame_set(wf)
            bpy.context.view_layer.update()
    scene.frame_set(FRAME)
    _log(f"rendering variant {tag}: mb={motion_blur} shutter={shutter} pos={position} "
         f"warm={warm} linear={linear} root_empty={root_empty}")
    bpy.ops.render.render(write_still=True)
    resolved = out + f"{FRAME:04d}.exr"
    if not os.path.isfile(resolved):
        resolved = out + ".exr"
    return resolved


VARIANTS = [
    ("A_BASELINE",        dict(motion_blur=True,  shutter=0.01, position='START')),
    ("B_MB_OFF",          dict(motion_blur=False)),
    ("C_MB_ON_WARMED",    dict(motion_blur=True,  shutter=0.01, position='START', warm=True)),
    ("D_MB_LINEAR_WARM",  dict(motion_blur=True,  shutter=0.01, position='START', warm=True, linear=True)),
    ("E_MB_BIGSHUTTER",   dict(motion_blur=True,  shutter=0.5,  position='CENTER', warm=True)),
    ("F_MB_ROOT_EMPTY",   dict(motion_blur=True,  shutter=0.01, position='START', warm=True, root_empty=True)),
]

results = {}
for tag, kw in VARIANTS:
    try:
        results[tag] = render_variant(tag, **kw)
    except Exception as e:
        results[tag] = {"error": f"{type(e).__name__}: {e}"}
        _log(f"variant {tag} FAILED: {e}")

import json as _json
print("PROBE_PATHS_START", flush=True)
print(_json.dumps(results), flush=True)
print("PROBE_PATHS_END", flush=True)
print("PROBE_DONE", flush=True)
'''

# The READER runs under SYSTEM python (which has OpenEXR), not Blender's bundled python.
# It takes the variant->EXR-path map as argv[1] JSON and prints Vector-pass stats per
# variant.
READER_PY = r'''
import sys, os, json
import numpy as np
import OpenEXR, Imath

paths = json.loads(sys.argv[1])

def stats_for(path):
    if isinstance(path, dict):
        return {"error": path.get("error", "render failed")}
    if not os.path.isfile(path):
        return {"error": f"missing {path}"}
    f = OpenEXR.InputFile(path)
    header = f.header()
    dw = header["dataWindow"]
    w = dw.max.x - dw.min.x + 1
    h = dw.max.y - dw.min.y + 1
    chans = list(header["channels"].keys())
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    def chan(name):
        raw = f.channel(name, pt)
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w)
    def find(suffixes):
        for want in suffixes:
            for c in chans:
                if c.endswith(want):
                    return c
        return None
    def st(name):
        if not name:
            return None
        a = chan(name)
        return {"chan": name, "mean": float(a.mean()), "std": float(a.std()),
                "absmax": float(np.abs(a).max())}
    vx = find([".Vector.X", "Vector.X"]); vy = find([".Vector.Y", "Vector.Y"])
    vz = find([".Vector.Z", "Vector.Z"]); vw = find([".Vector.W", "Vector.W"])
    out = {"channels": chans, "vx": st(vx), "vy": st(vy), "vz": st(vz), "vw": st(vw)}
    f.close()
    return out

results = {tag: stats_for(p) for tag, p in paths.items()}
print("READER_JSON_START", flush=True)
print(json.dumps(results), flush=True)
print("READER_JSON_END", flush=True)
'''


def log(m):
    print(f"[probe4 {time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    log("provisioning...")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    try:
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp failed: {serr[:200]}")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600)
        log(f"deps rc={rc}")

        # Warm the runner ONCE at tiny cost so Blender + classroom.blend are fetched/cached.
        # frames=2 keyframe_every=2, tiny res, low spp => cheap; this also proves BASELINE zero.
        cfg = {"scene": "classroom", "resolution": "480x270", "bounces": 6,
               "device": "AUTO", "frames": 2, "keyframe_every": 2, "draft_spp": 24,
               "ref_spp": 24, "adaptive_threshold": 0.05, "denoiser": "none",
               "denoise_guides": False, "light_tree": False, "hole_fill": "inpaint"}
        payload = json.dumps(cfg).replace("'", "'\\''")
        log("warming runner (fetch Blender + classroom.blend, tiny render)...")
        rc, out, err = runpod.ssh(
            pod, f"cd /root/spec-lab && python3 pod/exp_render_stack.py '{payload}'",
            timeout=1800)
        tail = [ln for ln in (out or "").splitlines() if ln.strip()]
        try:
            result = json.loads(tail[-1]) if tail else {"error": "no output"}
        except Exception:
            result = {"raw_tail": tail[-3:] if tail else []}
        log(f"warm result: {result if 'error' in result else 'ok, runner ran'}")
        if err.strip():
            log("warm stderr tail:\n" + err[-800:])

        # locate blend + blender bin
        rc, out, err = runpod.ssh(
            pod, "find /root /models /tmp -iname 'classroom*.blend' 2>/dev/null | head -3",
            timeout=60)
        blends = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        blend_path = blends[0] if blends else None
        log(f"blend: {blend_path}")
        rc, out, err = runpod.ssh(
            pod, "ls /root/blender/blender 2>/dev/null || find / -maxdepth 5 -name blender -type f 2>/dev/null | head -1",
            timeout=60)
        blender_bin = (out or "").strip().splitlines()[0] if out and out.strip() else "blender"
        log(f"blender bin: {blender_bin}")
        if not blend_path:
            raise RuntimeError("no classroom.blend found on pod after warm")

        # ship the probe and run it (single Blender process, all variants)
        rc, _, err = runpod.ssh(
            pod, f"cat > /root/spec-lab/probe.py << 'PYEOF'\n{PROBE_PY}\nPYEOF", timeout=60)
        if rc != 0:
            raise RuntimeError(f"writing probe.py failed: {err[:200]}")
        log("running Vector-pass settings sweep (single Blender process)...")
        rc, out, err = runpod.ssh(
            pod,
            f"cd /root/spec-lab && {blender_bin} -b -noaudio --factory-startup "
            f"-P /root/spec-lab/probe.py -- {blend_path}",
            timeout=2400)
        log(f"probe(render) rc={rc}")
        # extract the variant->EXR-path map
        import re as _re
        m = _re.search(r"PROBE_PATHS_START\s*(.*?)\s*PROBE_PATHS_END", out or "", _re.S)
        if not m:
            print("----- PROBE STDOUT (no paths) -----")
            print((out or "")[-3000:])
            if err.strip():
                print("----- PROBE STDERR (tail) -----")
                print(err[-3000:])
            raise RuntimeError("probe did not emit PROBE_PATHS")
        paths_json = m.group(1).strip()
        log(f"variant EXR paths: {paths_json}")

        # ship + run the reader under SYSTEM python (has OpenEXR)
        rc, _, err = runpod.ssh(
            pod, f"cat > /root/spec-lab/reader.py << 'PYEOF'\n{READER_PY}\nPYEOF", timeout=60)
        if rc != 0:
            raise RuntimeError(f"writing reader.py failed: {err[:200]}")
        payload_paths = paths_json.replace("'", "'\\''")
        rc, out, err = runpod.ssh(
            pod, f"cd /root/spec-lab && python3 reader.py '{payload_paths}'", timeout=300)
        log(f"reader rc={rc}")
        print("----- READER STDOUT -----")
        print(out)
        if err.strip():
            print("----- READER STDERR (tail) -----")
            print(err[-2000:])
    finally:
        log("tearing down...")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
