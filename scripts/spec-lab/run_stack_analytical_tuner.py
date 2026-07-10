#!/usr/bin/env python3
"""run_stack_analytical_tuner.py — one money-safe GPU session that TUNES the analytical
depth+camera-matrix reprojection runner (pod/exp_render_stack_analytical.py).

Same shape as run_stack_tuner.py (which tuned the 2D-motion-vector keystone and found its
worst_tile_ssim~0.27 ceiling): provisions ONE reachable production GPU, registers teardown
on every exit path, ships pod/, installs the EXR/imaging deps, then hands the prepared pod
to tuner_stack_analytical.tune_stack_analytical for the OFAT -> per-quality-tier
coordinate-ascent -> refine-until-budget loop. Results stream to
docs/speed-lane-reports/spec-lab/stack_analytical_tuner_ledger.jsonl.

Usage: RUNPOD_API_KEY=... python3 run_stack_analytical_tuner.py [--max-minutes 180] [--min-balance 6]
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runpod  # noqa: E402
import tuner_stack_analytical as tsa  # noqa: E402

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
POD_DISK_GB = 80


def log(m):
    print(f"[stack-analytical-tuner {time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=180)
    ap.add_argument("--min-balance", type=float, default=6.0)
    args = ap.parse_args()

    runpod.register_cleanup()
    deadline = time.time() + args.max_minutes * 60
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance}; max {args.max_minutes}m")
    if bal0 <= args.min_balance:
        log("balance already at/below floor — aborting before provisioning.")
        return

    _bal = {"v": bal0, "checked": time.time()}

    def min_balance_ok():
        if time.time() - _bal["checked"] > 45:
            try:
                _bal["v"] = runpod.balance()["clientBalance"]
            except Exception:
                pass
            _bal["checked"] = time.time()
        return time.time() < deadline and _bal["v"] > args.min_balance

    log("provisioning a production GPU (A100 preferred)…")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
    tsa.ledger_append({"event": "pod_up", "target": tsa.TARGET_ID, "pod": pod})
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    try:
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        log("installing EXR reader (OpenEXR/Imath) + skimage/imageio/numpy/pillow…")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600)
        log(f"  deps install rc={rc}: {(out or '').strip()[-160:]}")
        log("runners shipped. First trial renders the animated reference (slow, ~15m); "
            "later trials reuse the cached reference until resolution changes.")

        result = tsa.tune_stack_analytical(pod, args.max_minutes, min_balance_ok)
        tb = result.get("tier_best", {})
        for qf in ("0.99", "0.98", "0.95", "0.9"):
            row = tb.get(qf)
            if row:
                log(f"tier q>={qf}: {row.get('speedup')}x @ q={row.get('quality')} "
                    f"worst_tile={row.get('worst_tile_ssim')} :: {row.get('config')}")
        log(f"pareto points: {len(result.get('pareto', []))}")
    finally:
        log("tearing down pod…")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        tsa.ledger_append({"event": "pod_down", "target": tsa.TARGET_ID,
                           "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
