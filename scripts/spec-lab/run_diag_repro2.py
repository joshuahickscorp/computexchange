#!/usr/bin/env python3
"""run_diag_repro2.py — round 2 diagnostic: settle whether the CAMERA genuinely moves
between frames at the Blender level, independent of the Vector-pass motion-vector
reading. (1) reruns the cheap 2-frame repro so ref_0001/ref_0002 EXRs exist, dumps BOTH
reference frames to PNG for a reference-vs-reference visual/SSIM comparison (eliminates
the anchor pipeline as a variable). (2) runs a tiny no-render bpy script that opens the
blend, applies the SAME camera-keyframing code exp_render_stack.py uses, and prints
cam.matrix_world at frame 1 vs frame 2 — settles whether Blender's own camera transform
actually differs per frame. Tears down on every exit path.
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

REF_DUMP_PY = r'''
import sys, os
sys.path.insert(0, "pod")
import numpy as np
import exp_render_stack as es

WD = "/tmp/render_stack"
RX, RY = 1920, 1080

def load(fn):
    return es.read_exr_layers(os.path.join(WD, fn), RX, RY)

ref0_color, *_ = load("ref_0001.exr")
ref1_color, *_ = load("ref_0002.exr")

from skimage.metrics import structural_similarity as ssim
s = float(ssim(np.clip(ref0_color, 0, 1), np.clip(ref1_color, 0, 1), channel_axis=-1, data_range=1.0))
print("REF0_VS_REF1_SSIM", s, flush=True)

def to_u8(img):
    x = np.clip(img, 0, None)
    x = x / (1.0 + x)
    x = np.clip(x, 0, 1) ** (1 / 2.2)
    return (x * 255).astype(np.uint8)

from PIL import Image
Image.fromarray(to_u8(ref0_color)).save("/tmp/diag2_ref0.png")
Image.fromarray(to_u8(ref1_color)).save("/tmp/diag2_ref1.png")
print("REF_PNGS_WRITTEN", flush=True)
'''

# A minimal, NO-RENDER bpy check: open the blend, apply the SAME camera-keyframe setup
# code exp_render_stack.py's scene script uses, then evaluate the camera's world matrix
# at frame 1 and frame 2 (via the depsgraph, so animation is actually evaluated) and
# print both. If they're identical, the animation isn't driving the camera at all.
CAM_CHECK_PY = r'''
import bpy, math, sys

BLEND = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else None
bpy.ops.wm.open_mainfile(filepath=BLEND)
scene = bpy.context.scene
cam = scene.camera
if cam is None:
    for ob in scene.objects:
        if ob.type == 'CAMERA':
            cam = ob
            scene.camera = ob
            break

NFRAMES = 8
CAM_MOTION = 1.0
scene.frame_start = 1
scene.frame_end = NFRAMES
base_loc = tuple(cam.location)
base_rot = tuple(cam.rotation_euler)
DOLLY_PER_FRAME = 0.05 * CAM_MOTION
RISE_PER_FRAME = 0.02 * CAM_MOTION
YAW_PER_FRAME = math.radians(0.8) * CAM_MOTION
try:
    cam.animation_data_clear()
except Exception:
    pass
for t in range(NFRAMES):
    fr = t + 1
    cam.location = (base_loc[0] + DOLLY_PER_FRAME * t, base_loc[1], base_loc[2] + RISE_PER_FRAME * t)
    cam.rotation_euler = (base_rot[0], base_rot[1], base_rot[2] + YAW_PER_FRAME * t)
    cam.keyframe_insert(data_path="location", frame=fr)
    cam.keyframe_insert(data_path="rotation_euler", frame=fr)

print("BASE_LOC", base_loc, flush=True)
print("N_FCURVES", len(cam.animation_data.action.fcurves) if cam.animation_data and cam.animation_data.action else 0, flush=True)

deps = bpy.context.evaluated_depsgraph_get()
for fr in (1, 2, 4, 8):
    scene.frame_set(fr)
    deps.update()
    ev_cam = cam.evaluated_get(deps)
    print(f"FRAME {fr}: cam.location={tuple(cam.location)} "
          f"matrix_world_translation={tuple(ev_cam.matrix_world.translation)}", flush=True)
print("CAM_CHECK_DONE", flush=True)
'''


def log(m):
    print(f"[diag2 {time.strftime('%H:%M:%S')}] {m}", flush=True)


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

        cfg = {"scene": "classroom", "resolution": "1920x1080", "bounces": 12,
               "device": "AUTO", "frames": 2, "keyframe_every": 2, "draft_spp": 512,
               "ref_spp": 4096, "adaptive_threshold": 0.02, "denoiser": "oidn",
               "denoise_guides": True, "light_tree": True, "hole_fill": "rerender"}
        payload = json.dumps(cfg).replace("'", "'\\''")
        log("running minimal 2-frame repro (regenerates ref_0001/ref_0002 EXRs)...")
        rc, out, err = runpod.ssh(
            pod, f"cd /root/spec-lab && python3 pod/exp_render_stack.py '{payload}'",
            timeout=1200)
        tail = [ln for ln in (out or "").splitlines() if ln.strip()]
        result = json.loads(tail[-1]) if tail else {"error": "no output"}
        log(f"repro result: net_speedup={result.get('net_speedup')} "
            f"quality={result.get('quality')} worst_tile={result.get('worst_tile_ssim')}")

        # Which .blend path did the runner resolve? Find it so the camera-check script
        # can open the SAME file directly (skip the runner's download/cache logic).
        rc, out, err = runpod.ssh(
            pod, "find /root/spec-lab /tmp /models -iname 'classroom*.blend' 2>/dev/null | head -5",
            timeout=60)
        blend_candidates = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        log(f"blend candidates: {blend_candidates}")
        blend_path = blend_candidates[0] if blend_candidates else None

        # ---- reference-vs-reference dump ----
        rc, _, err = runpod.ssh(
            pod, f"cat > /root/spec-lab/refdump.py << 'PYEOF'\n{REF_DUMP_PY}\nPYEOF", timeout=60)
        log("dumping reference frame 0 vs frame 1 PNGs...")
        rc, out, err = runpod.ssh(pod, "cd /root/spec-lab && python3 refdump.py", timeout=300)
        log(f"refdump rc={rc}")
        print("----- REFDUMP STDOUT -----")
        print(out)
        if err.strip():
            print("----- REFDUMP STDERR (tail) -----")
            print(err[-1000:])

        # ---- camera transform check (no render) ----
        if blend_path:
            rc, _, err = runpod.ssh(
                pod, f"cat > /root/spec-lab/camcheck.py << 'PYEOF'\n{CAM_CHECK_PY}\nPYEOF", timeout=60)
            log("running bpy camera-transform check (no render)...")
            blender_bin_cmd = "find / -maxdepth 4 -iname 'blender' -type f 2>/dev/null | head -1"
            rc, out, err = runpod.ssh(pod, blender_bin_cmd, timeout=60)
            blender_bin = (out or "").strip().splitlines()[0] if out and out.strip() else "blender"
            log(f"blender bin: {blender_bin}")
            rc, out, err = runpod.ssh(
                pod,
                f"{blender_bin} -b --factory-startup -P /root/spec-lab/camcheck.py -- {blend_path}",
                timeout=300)
            log(f"camcheck rc={rc}")
            print("----- CAMCHECK STDOUT -----")
            print(out)
            if err.strip():
                print("----- CAMCHECK STDERR (tail) -----")
                print(err[-1500:])
        else:
            log("no blend path found on pod — skipping camera-transform check")

        local_diag_dir = os.path.join(HERE, "..", "..", "diag_images")
        os.makedirs(local_diag_dir, exist_ok=True)
        for fn in ["diag2_ref0.png", "diag2_ref1.png"]:
            r = subprocess.run(
                ["scp", *runpod.SSH_OPTS, "-P", str(pod["port"]),
                 f"root@{pod['ip']}:/tmp/{fn}", os.path.join(local_diag_dir, fn)],
                capture_output=True, text=True
            )
            log(f"  pulled {fn}: rc={r.returncode}")
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
