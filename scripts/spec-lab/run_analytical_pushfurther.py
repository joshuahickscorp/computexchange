#!/usr/bin/env python3
"""run_analytical_pushfurther.py — quick, cheap follow-up: the analytical tuner's OFAT sweep
found disocclusion_thresh=0.02 (the TIGHTEST value tested) gave the best worst_tile (0.40),
a boundary effect suggesting the true optimum may lie even tighter. light_tree=False was the
second-best single lever (0.346). This tests pushing the threshold further and combining the
two best single-knob wins, on the cached reference (all prior trials already primed it).
Tears down on every exit path.
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
RUNNER = "exp_render_stack_analytical.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/stack_analytical_tuner_ledger.jsonl")
TRIAL_TIMEOUT_S = 1800  # cold pods must re-render the 4096-spp reference cache

BASE = {"frames": 8, "keyframe_every": 2, "draft_spp": 512, "ref_spp": 4096,
        "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True,
        "light_tree": True, "hole_fill": "rerender", "resolution": "1920x1080",
        "scene": "classroom", "bounces": 12, "device": "AUTO",
        "depth_convention": "auto", "probe_identity": True}

TRIALS = [
    ("thr-0.01", {"disocclusion_thresh": 0.01}),
    ("thr-0.005", {"disocclusion_thresh": 0.005}),
    ("thr-0.001", {"disocclusion_thresh": 0.001}),
    ("thr0.02-noLT", {"disocclusion_thresh": 0.02, "light_tree": False}),
    ("thr0.01-noLT", {"disocclusion_thresh": 0.01, "light_tree": False}),
    ("thr0.005-noLT", {"disocclusion_thresh": 0.005, "light_tree": False}),
]


def log(m):
    print(f"[push-further {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


def run_runner(pod, cfg, timeout):
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{RUNNER} '{payload}'"
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

        best_wt, best_name, best_cfg = -1.0, None, None
        for name, delta in TRIALS:
            cfg = {**BASE, **delta}
            log(f"RUN {name} {delta}...")
            t0 = time.time()
            res = run_runner(pod, cfg, TRIAL_TIMEOUT_S)
            dt = time.time() - t0
            ledger_append({"event": "trial", "target": "render_stack_analytical",
                           "config": cfg, "metrics": res})
            if res.get("error"):
                log(f"  {name} -> ERROR: {str(res['error'])[:200]}")
                continue
            wt = res.get("worst_tile_ssim")
            log(f"  {name} -> wall={dt:.1f}s speedup={res.get('net_speedup')}x "
                f"q={res.get('quality')} worst_tile={wt} "
                f"identity_probe={res.get('identity_probe_max_error_px')}")
            if isinstance(wt, (int, float)) and wt > best_wt:
                best_wt, best_name, best_cfg = wt, name, cfg
        log(f"BEST: {best_name} worst_tile={best_wt} cfg={best_cfg}")
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
