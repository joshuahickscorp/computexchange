#!/usr/bin/env python3
"""run_ultimate_no_reprojection.py — TRACK 3: the honest, ship-today product number.

Runs exp_render_ultimate.py with keyframe_every=1 — every frame gets a full anchor-quality
render (adaptive sampling + OIDN + light-tree), ZERO temporal reprojection, sidestepping the
proven wall entirely — then VP9 transcode delivery. This is the real end-to-end number for
the path we've already proven works: denoise anchor + light-tree + transcode.

SAFETY NET (added 2026-07-07 after an orphaned pod cost ~$1 when a local session died mid-
run): arms a REMOTE watchdog on the pod itself immediately after provisioning, via
runpod.arm_remote_watchdog(). This self-terminates the pod after ttl_seconds regardless of
whether this local process survives — a hard backstop independent of the local finally block.
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
# Tier note: --gpu-tier=premium DELIBERATELY starts at the H100/H200 upgrade rungs (an
# upgrade is always allowed; a downgrade never is); value is the A100 base tier.
PREMIUM_GPU_PLAN = [
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
VALUE_GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
]
# --gpu-tier=any is the full policy ladder, cheapest first: A100 base, then upgrades.
GPU_PLAN = VALUE_GPU_PLAN + PREMIUM_GPU_PLAN
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 80
RUNNER = "exp_render_ultimate.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/ultimate_ledger.jsonl")

CFG = {"frames": 8, "keyframe_every": 1, "draft_spp": 512, "ref_spp": 4096,
       "adaptive_threshold": 0.02, "denoiser": "oidn", "denoise_guides": True,
       "light_tree": True, "codec": "libvpx-vp9", "resolution": "1920x1080",
       "scene": "classroom", "bounces": 12, "device": "AUTO"}

WATCHDOG_TTL_S = 3600  # hard remote self-destruct backstop; this trial's own timeout is 3600s too


def log(m):
    print(f"[ultimate-no-reproj {time.strftime('%H:%M:%S')}] {m}", flush=True)


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
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-400:]}"}
    try:
        return json.loads(tail[-1])
    except Exception as e:
        return {"error": f"unparseable final line: {e}; last={tail[-1][:300]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout-s", type=int, default=3600)
    ap.add_argument("--frames", type=int, default=CFG["frames"])
    ap.add_argument("--ref-spp", type=int, default=CFG["ref_spp"])
    ap.add_argument("--draft-spp", type=int, default=CFG["draft_spp"])
    ap.add_argument("--resolution", default=CFG["resolution"])
    ap.add_argument("--scene", default=CFG["scene"])
    ap.add_argument("--gpu-tier", choices=("premium", "value", "any"), default="premium")
    args = ap.parse_args()
    cfg = dict(CFG)
    cfg.update({
        "frames": args.frames,
        "ref_spp": args.ref_spp,
        "draft_spp": args.draft_spp,
        "resolution": args.resolution,
        "scene": args.scene,
    })

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    gpu_plan = (
        PREMIUM_GPU_PLAN if args.gpu_tier == "premium" else
        VALUE_GPU_PLAN if args.gpu_tier == "value" else
        GPU_PLAN
    )
    log(f"provisioning ({args.gpu_tier})...")
    pod = runpod.provision_reachable(gpu_plan, POD_IMAGE, disk_gb=POD_DISK_GB)
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
    ledger_append({"event": "pod_up", "pod": pod})

    # HARD BACKSTOP: self-terminates on the pod itself even if this local process dies.
    runpod.arm_remote_watchdog(pod, max(WATCHDOG_TTL_S, args.timeout_s + 900))

    try:
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp failed: {serr[:200]}")
        rc, out, err = runpod.ssh(
            pod,
            "apt-get install -y ffmpeg >/dev/null 2>&1; "
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image opencv-python-headless "
            "2>&1 | tail -2",
            timeout=900)
        log(f"deps rc={rc}")

        log(f"RUN ultimate_no_reprojection {cfg} (timeout {args.timeout_s}s)...")
        t0 = time.time()
        res = run_runner(pod, cfg, args.timeout_s)
        dt = time.time() - t0
        ledger_append({"event": "trial", "config": cfg, "wall_s": round(dt, 1), "result": res})
        if res.get("error"):
            log(f"  -> ERROR: {str(res['error'])[:400]}")
        else:
            log(f"  -> wall={dt:.1f}s net_speedup={res.get('net_speedup')}x "
                f"quality={res.get('quality')} worst_tile={res.get('worst_tile_ssim')} "
                f"p5_tile={res.get('p5_tile_ssim')} T_ref={res.get('T_ref_s')}s "
                f"T_ours={res.get('T_ours_s')}s modeled={res.get('modeled')}")
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
