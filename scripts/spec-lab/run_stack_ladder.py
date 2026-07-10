#!/usr/bin/env python3
"""run_stack_ladder.py — one money-safe GPU session for the COMPOSITION-STACK ladder.

Runs the KEYSTONE compound (exp_render_stack.py) FIRST — denoise x temporal-reuse-on-
anchored-keyframes x light-tree on an animated production scene, one honest end-to-end
ratio T_ref/T_stack + end-to-end global/worst-tile SSIM — then the convergence and fan-out
levers. Answers the 100x thesis joint: do the render levers MULTIPLY at near-lossless, or
does quality compound down / do the levers overlap? Money-safe teardown on every exit path.

Usage: RUNPOD_API_KEY=... python3 run_stack_ladder.py [--max-minutes 75] [--min-balance 6]
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
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
POD_DISK_GB = 80
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/stack_ladder_ledger.jsonl")

CLASS = {"scene": "classroom", "resolution": "1920x1080", "bounces": 12, "device": "AUTO"}

# (runner, name, config, timeout_s). KEYSTONE first — it renders the animated reference
# (the expensive part) and measures the full compound; kf6 reuses that reference if cached.
LADDER = [
    ("exp_render_stack.py", "stack-kf4",
     {**CLASS, "frames": 8, "keyframe_every": 4, "draft_spp": 512, "ref_spp": 4096,
      "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True,
      "light_tree": True, "hole_fill": "rerender"}, 2700),
    ("exp_render_stack.py", "stack-kf6",
     {**CLASS, "frames": 8, "keyframe_every": 6, "draft_spp": 512, "ref_spp": 4096,
      "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True,
      "light_tree": True, "hole_fill": "rerender"}, 1500),
    ("exp_render_convergence.py", "conv-lighttree-on",
     {**CLASS, "ref_spp": 4096, "samples": 512, "adaptive_thresholds": [0.02, 0.01],
      "use_light_tree": True, "target_ssim": 0.98}, 1500),
    ("exp_render_convergence.py", "conv-lighttree-off",
     {**CLASS, "ref_spp": 4096, "samples": 512, "adaptive_thresholds": [0.02],
      "use_light_tree": False, "target_ssim": 0.98}, 1200),
    ("exp_render_faninout.py", "faninout-t8", {**CLASS, "spp": 512, "tiles": 8}, 1200),
    ("exp_render_faninout.py", "faninout-t16", {**CLASS, "spp": 512, "tiles": 16}, 1200),
]


def log(m):
    print(f"[stack-ladder {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def run_runner(pod, runner, cfg, timeout):
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{runner} '{payload}'"
    rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    tail = [ln for ln in (out or "").splitlines() if ln.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-300:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:
        return {"error": f"unparseable final line: {e}; last={tail[-1][:200]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=75)
    ap.add_argument("--min-balance", type=float, default=6.0)
    args = ap.parse_args()

    runpod.register_cleanup()
    deadline = time.time() + args.max_minutes * 60
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance}; max {args.max_minutes}m")
    if bal0 <= args.min_balance:
        log("balance already at/below floor — aborting before provisioning.")
        return

    log("provisioning a production GPU (A100 preferred)…")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
    ledger_append({"event": "pod_up", "pod": pod})
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    try:
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        # The stack/convergence runners read Cycles motion/depth AOVs out of multilayer
        # EXR; the base pod image has no EXR backend, so install OpenEXR+Imath (+imageio,
        # skimage) here. We skip the heavy setup_base.sh (it installs vLLM, unneeded for
        # rendering) — this targeted install is what the render runners actually need.
        log("installing EXR reader (OpenEXR/Imath) + skimage/imageio for the AOV runners…")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600)
        log(f"  deps install rc={rc}: {(out or '').strip()[-160:]}")
        log("runners shipped. keystone stack renders the animated reference first (slow).")

        for runner, name, cfg, timeout in LADDER:
            if time.time() > deadline:
                log("deadline reached — stopping the ladder.")
                break
            bal = runpod.balance()["clientBalance"]
            if bal <= args.min_balance:
                log(f"balance ${bal:.2f} at/below floor ${args.min_balance} — stopping.")
                break
            log(f"RUN {name} [{runner}] (timeout {timeout}s)…")
            t0 = time.time()
            res = run_runner(pod, runner, cfg, timeout)
            dt = time.time() - t0
            ledger_append({"event": "trial", "name": name, "runner": runner,
                           "config": cfg, "wall_s_incl_ssh": round(dt, 1), "result": res})
            if res.get("error"):
                log(f"  {name} -> ERROR: {str(res['error'])[:200]}")
            else:
                log(f"  {name} -> net_speedup={res.get('net_speedup')} "
                    f"quality={res.get('quality')} worst_tile={res.get('worst_tile_ssim')} "
                    f"p5={res.get('p5_tile_ssim')} "
                    f"[keyframes={res.get('keyframes')} T_serial={res.get('t_serial_s')} "
                    f"T_par_ideal={res.get('t_parallel_ideal_s')} modeled={res.get('modeled')}]")
    finally:
        log("tearing down pod…")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
