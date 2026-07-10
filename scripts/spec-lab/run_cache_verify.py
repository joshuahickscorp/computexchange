#!/usr/bin/env python3
"""run_cache_verify.py — ONE-SHOT verification that the reference-cache fix works:
run the SAME stack config TWICE on one pod. Trial 2 should be dramatically faster
(cache hit) while reporting the SAME T_ref and consistent quality (proving the cache
returns the TRUE historical reference, not a shortcut that fabricates a number).
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


def log(m):
    print(f"[cache-verify {time.strftime('%H:%M:%S')}] {m}", flush=True)


def run_runner(pod, cfg, timeout):
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/exp_render_stack.py '{payload}'"
    rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    tail = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-300:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:
        return {"error": f"unparseable final line: {e}; last={tail[-1][:200]}"}


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

        log("TRIAL 1 (expect cache MISS, slow, real render)...")
        t0 = time.time()
        r1 = run_runner(pod, cfg, 1200)
        dt1 = time.time() - t0
        log(f"  trial1: wall={dt1:.1f}s net_speedup={r1.get('net_speedup')} "
            f"quality={r1.get('quality')} T_ref_s={r1.get('T_ref_s')} "
            f"ref_cache_hit={r1.get('ref_cache_hit')} error={r1.get('error')}")

        log("TRIAL 2 (SAME config, expect cache HIT, fast)...")
        t0 = time.time()
        r2 = run_runner(pod, cfg, 1200)
        dt2 = time.time() - t0
        log(f"  trial2: wall={dt2:.1f}s net_speedup={r2.get('net_speedup')} "
            f"quality={r2.get('quality')} T_ref_s={r2.get('T_ref_s')} "
            f"ref_cache_hit={r2.get('ref_cache_hit')} error={r2.get('error')}")

        speedup_of_caching = dt1 / dt2 if dt2 > 0 else float('inf')
        log(f"VERDICT: trial2 was {speedup_of_caching:.1f}x faster in wall-clock; "
            f"T_ref_s match: {r1.get('T_ref_s') == r2.get('T_ref_s')}; "
            f"quality match: {r1.get('quality') == r2.get('quality')}")
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
