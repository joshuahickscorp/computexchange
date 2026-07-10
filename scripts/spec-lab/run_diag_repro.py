#!/usr/bin/env python3
"""run_diag_repro.py — ONE-SHOT money-safe diagnostic: reproduce the keystone's frame-1
quality collapse as cheaply as possible (frames=2, keyframe_every=2 => exactly one keyframe
+ one reprojected frame, the same gap=1 case that collapsed to SSIM 0.66/worst-tile 0.14
in stack-kf4/kf6), then immediately decompose WHERE the loss comes from: raw warp vs true
ref, composite vs true ref, naive static-keyframe-copy vs true ref (baseline), and the
frame's own anchor-quality render vs true ref (upper bound). Saves diagnostic PNGs too.
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

DIAG_PY = r'''
import sys, os, time
sys.path.insert(0, "pod")
import numpy as np
import exp_render_stack as es

WD = "/tmp/render_stack"
RX, RY = 1920, 1080
DISOCC = 0.1

def load(fn):
    return es.read_exr_layers(os.path.join(WD, fn), RX, RY)

ref1_color, *_ = load("ref_0002.exr")
key_color, *_  = load("anchor_0001.exr")
f1_color, f1_mprev, f1_depth, f1_mnext = load("anchor_0002.exr")

diag = {}
diag["motion_prev_mean"] = float(np.mean(f1_mprev))
diag["motion_prev_std"] = float(np.std(f1_mprev))
diag["motion_prev_absmax"] = float(np.max(np.abs(f1_mprev)))
diag["motion_next_present"] = bool(f1_mnext is not None)
if f1_mnext is not None:
    diag["motion_next_mean"] = float(np.mean(f1_mnext))
    diag["motion_next_std"] = float(np.std(f1_mnext))

reproj, valid = es.warp_gather(key_color, f1_mprev)
mask, coverage = es.disocclusion_mask(f1_mprev, f1_mnext, f1_depth, valid, DISOCC)
diag["coverage"] = {k: (float(v) if not isinstance(v, bool) else v) for k, v in coverage.items()}
diag["disocc_frac"] = float(mask.mean())
comp = es.composite_rerender(reproj, f1_color, mask)

from skimage.metrics import structural_similarity as ssim
def s(a, b):
    return float(ssim(np.clip(a, 0, 1), np.clip(b, 0, 1), channel_axis=-1, data_range=1.0))

diag["ssim_raw_warp_vs_ref"] = s(reproj, ref1_color)
diag["ssim_composite_vs_ref"] = s(comp, ref1_color)
diag["ssim_naive_static_keyframe_vs_ref"] = s(key_color, ref1_color)
diag["ssim_own_anchor_render_vs_ref_UPPERBOUND"] = s(f1_color, ref1_color)

def to_u8(img):
    x = np.clip(img, 0, None)
    x = x / (1.0 + x)
    x = np.clip(x, 0, 1) ** (1 / 2.2)
    return (x * 255).astype(np.uint8)

from PIL import Image
Image.fromarray(to_u8(ref1_color)).save("/tmp/diag_ref1.png")
Image.fromarray(to_u8(key_color)).save("/tmp/diag_key0.png")
Image.fromarray(to_u8(f1_color)).save("/tmp/diag_anchor1.png")
Image.fromarray(to_u8(reproj)).save("/tmp/diag_warp_raw.png")
Image.fromarray((mask.astype(np.uint8) * 255)).save("/tmp/diag_mask.png")
Image.fromarray(to_u8(comp)).save("/tmp/diag_composite.png")

import json as _json
print("DIAG_JSON_START")
print(_json.dumps(diag))
print("DIAG_JSON_END")
'''


def log(m):
    print(f"[diag-repro {time.strftime('%H:%M:%S')}] {m}", flush=True)


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
        log("running MINIMAL 2-frame repro (1 keyframe + 1 reprojected frame)...")
        rc, out, err = runpod.ssh(
            pod, f"cd /root/spec-lab && python3 pod/exp_render_stack.py '{payload}'",
            timeout=1200)
        tail = [ln for ln in (out or "").splitlines() if ln.strip()]
        result = json.loads(tail[-1]) if tail else {"error": "no output"}
        log(f"repro result: net_speedup={result.get('net_speedup')} "
            f"quality={result.get('quality')} worst_tile={result.get('worst_tile_ssim')}")

        # write the diag script to the pod and run it
        diag_path = "/root/spec-lab/diag.py"
        rc, _, err = runpod.ssh(
            pod, f"cat > {diag_path} << 'PYEOF'\n{DIAG_PY}\nPYEOF", timeout=60)
        if rc != 0:
            raise RuntimeError(f"writing diag.py failed: {err[:200]}")
        log("running diagnostic decomposition...")
        rc, out, err = runpod.ssh(pod, f"cd /root/spec-lab && python3 {diag_path}", timeout=300)
        log(f"diag rc={rc}")
        print("----- DIAG STDOUT -----")
        print(out)
        if err.strip():
            print("----- DIAG STDERR (tail) -----")
            print(err[-1500:])

        # pull the PNGs down for visual inspection
        local_diag_dir = os.path.join(HERE, "..", "..", "diag_images")
        os.makedirs(local_diag_dir, exist_ok=True)
        for fn in ["diag_ref1.png", "diag_key0.png", "diag_anchor1.png",
                   "diag_warp_raw.png", "diag_mask.png", "diag_composite.png"]:
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
