#!/usr/bin/env python3
"""run_analytical_verify.py — TRACK 4 (real-hardware verification): does the analytical
depth+camera-matrix reprojection actually break the worst_tile_ssim~0.27 wall that Cycles-
Vector-pass-based reprojection hit overnight?

Runs the SAME minimal 2-frame case (frames=2, keyframe_every=2 -> one keyframe + one
reprojected frame) used to diagnose the original bug, on BOTH runners for a direct,
apples-to-apples comparison: the OLD (2D motion-vector) exp_render_stack.py and the NEW
(analytical 3D unproject/reproject) exp_render_stack_analytical.py. The identity probe
(built into the new runner) gates on real hardware BEFORE any cross-frame number is trusted.
Tears down on every exit path.
"""
import json
import os
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

CFG = {"scene": "classroom", "resolution": "1920x1080", "bounces": 12, "device": "AUTO",
       "frames": 2, "keyframe_every": 2, "draft_spp": 512, "ref_spp": 4096,
       "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True,
       "light_tree": True, "hole_fill": "rerender"}


def log(m):
    print(f"[analytical-verify {time.strftime('%H:%M:%S')}] {m}", flush=True)


def run_runner(pod, runner, cfg, timeout):
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{runner} '{payload}'"
    rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    tail = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-400:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:
        return {"error": f"unparseable final line: {e}; last={tail[-1][:300]}"}


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

        log("TRIAL A: exp_render_stack.py (2D motion-vector, the known ~0.27 wall)...")
        t0 = time.time()
        rA = run_runner(pod, "exp_render_stack.py", CFG, 1500)
        dtA = time.time() - t0
        log(f"  A: wall={dtA:.1f}s net_speedup={rA.get('net_speedup')} "
            f"quality={rA.get('quality')} worst_tile={rA.get('worst_tile_ssim')} "
            f"error={rA.get('error')}")

        log("TRIAL B: exp_render_stack_analytical.py (3D depth+camera-matrix reproject)...")
        t0 = time.time()
        rB = run_runner(pod, "exp_render_stack_analytical.py", CFG, 1500)
        dtB = time.time() - t0
        if rB.get("error"):
            log(f"  B: wall={dtB:.1f}s -> ERROR: {str(rB['error'])[:400]}")
        else:
            log(f"  B: wall={dtB:.1f}s net_speedup={rB.get('net_speedup')} "
                f"quality={rB.get('quality')} worst_tile={rB.get('worst_tile_ssim')} "
                f"identity_probe_px={rB.get('identity_probe_max_error_px')} "
                f"depth_convention={rB.get('depth_convention_chosen')} "
                f"pose_error={rB.get('camera_model_max_pose_error')} "
                f"intrinsics={rB.get('intrinsics')}")

        wt_a = rA.get("worst_tile_ssim")
        wt_b = rB.get("worst_tile_ssim")
        if isinstance(wt_a, (int, float)) and isinstance(wt_b, (int, float)):
            log(f"VERDICT: worst_tile 2D-vector={wt_a:.4f} vs analytical={wt_b:.4f} "
                f"(delta={wt_b - wt_a:+.4f}) — "
                f"{'ANALYTICAL WINS' if wt_b > wt_a + 0.02 else 'no meaningful improvement' if abs(wt_b - wt_a) <= 0.02 else '2D-VECTOR WINS (unexpected)'}")
        else:
            log("VERDICT: could not compare (one or both trials errored)")
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
