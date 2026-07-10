#!/usr/bin/env python3
"""run_track5_spikes.py — TRACK 5: the two remaining research spikes, real hardware.

Runs exp_render_upscale_guided.py (render low-res + AOV-guided upscale vs bicubic vs
Real-ESRGAN control) and exp_render_interp_learned.py (flow-guided/learned frame
interpolation vs the naive warp baseline), sequentially on one pod. Both were fixed for the
motion_blur bug earlier this session; neither has run on real hardware yet.

Uses runpod.arm_remote_watchdog() as a hard remote self-destruct backstop (added after an
orphaned-pod incident mid-session), independent of the local process surviving.
"""
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
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/track5_ledger.jsonl")
WATCHDOG_TTL_S = 5400  # 90m hard backstop — two experiments, generous cold-cache room

TRIALS = [
    ("upscale_guided_all", "exp_render_upscale_guided.py", 2400,
     {"scene": "classroom", "low_res": "960x540", "full_res": "1920x1080",
      "method": "all", "spp": 4096, "guide_spp": 16, "bounces": 12, "device": "AUTO"}),
    ("interp_flow_guided", "exp_render_interp_learned.py", 2400,
     {"frames": 8, "interp_every": 2, "model": "flow_guided", "scene": "animated",
      "spp": 256, "resolution": 384, "device": "AUTO"}),
]


def log(m):
    print(f"[track5 {time.strftime('%H:%M:%S')}] {m}", flush=True)


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
    ledger_append({"event": "pod_up", "pod": pod})

    runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

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
            "OpenEXR Imath imageio numpy pillow scikit-image opencv-python-headless torch "
            "2>&1 | tail -3",
            timeout=1200)
        log(f"deps rc={rc}")

        for name, runner, timeout, cfg in TRIALS:
            log(f"RUN {name} [{runner}] (timeout {timeout}s)...")
            t0 = time.time()
            res = run_runner(pod, runner, cfg, timeout)
            dt = time.time() - t0
            ledger_append({"event": "trial", "name": name, "runner": runner,
                           "config": cfg, "wall_s": round(dt, 1), "result": res})
            if res.get("error"):
                log(f"  {name} -> ERROR: {str(res['error'])[:400]}")
            else:
                log(f"  {name} -> wall={dt:.1f}s {json.dumps(res)[:500]}")
    finally:
        log("tearing down...")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
