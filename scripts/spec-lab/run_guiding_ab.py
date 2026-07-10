#!/usr/bin/env python3
"""run_guiding_ab.py — TRACK 1 Phase 0a driver: Open PGL path-guiding A/B, money-safe.

Provisions a pod, ships pod/exp_guiding_ab.py, and runs the guiding ON-vs-OFF trial at
EQUAL SAMPLE COUNT, both arms denoised with OIDN, scored vs a high-spp reference on the
shared compute_ssim_global_and_tiles harness. No Cycles fork, no custom build — this is
the definitely-working half of Track 1 Phase 0.

Standard spec-lab safety pattern (all non-optional):
  * runpod.register_cleanup() wires teardown to every local exit path.
  * runpod.arm_remote_watchdog() arms a POD-SIDE self-destruct immediately after
    provisioning — a hard backstop if THIS process dies (an orphaned pod once cost ~$1).
  * teardown in a finally block regardless of outcome.

NOTE ON DEVICE: path guiding is CPU-only, so this trial renders entirely on CPU. We do
NOT require a working CUDA runtime (require_cuda=False) — any reachable box works — but
CPU render speed scales with vCPU count, so higher-core pods finish the (slow, CPU) high-
spp reference faster. GPU choice is irrelevant to the result; it's just the host.
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

# CPU-only trial: GPU SKU irrelevant, policy tier not required — see
# gpu-provisioning-policy exception clause. Open PGL path guiding renders entirely on
# CPU (docstring above), so any reachable box works; the order below just prefers
# generally-higher-core instances. This is deliberately NOT the A100/H100/H200 policy
# ladder — forcing expensive GPU SKUs onto a CPU render would buy nothing.
GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA L40S", "COMMUNITY"),
    ("NVIDIA RTX A6000", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
    ("NVIDIA L40S", "SECURE"),
]
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 40
RUNNER = "exp_guiding_ab.py"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/guiding_ab_ledger.jsonl")

CFG = {"spp": 64, "ref_spp": 2048, "res": 512, "bounces": 12,
       "guiding_training_samples": 128, "denoiser": "oidn", "seed": 0,
       "device": "CPU"}  # device is forced CPU by the runner regardless; stated for clarity

WATCHDOG_TTL_S = 5400  # 90m hard pod-side self-destruct backstop


def log(m):
    print(f"[guiding-ab {time.strftime('%H:%M:%S')}] {m}", flush=True)


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
    ap.add_argument("--timeout-s", type=int, default=5400)
    ap.add_argument("--spp", type=int, default=None)
    ap.add_argument("--ref-spp", type=int, default=None)
    ap.add_argument("--res", type=int, default=None)
    args = ap.parse_args()

    cfg = dict(CFG)
    if args.spp is not None:
        cfg["spp"] = args.spp
    if args.ref_spp is not None:
        cfg["ref_spp"] = args.ref_spp
    if args.res is not None:
        cfg["res"] = args.res

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    log("provisioning (CPU-only trial; require_cuda=False)...")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB,
                                     require_cuda=False)
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
    ledger_append({"event": "pod_up", "pod": pod})

    # HARD BACKSTOP: self-terminates on the pod even if this local process dies.
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
            "apt-get update >/dev/null 2>&1; "
            "apt-get install -y ffmpeg >/dev/null 2>&1; "
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image opencv-python-headless "
            "2>&1 | tail -2",
            timeout=900)
        log(f"deps rc={rc}")

        log(f"RUN guiding_ab {cfg} (timeout {args.timeout_s}s)...")
        t0 = time.time()
        res = run_runner(pod, cfg, args.timeout_s)
        dt = time.time() - t0
        ledger_append({"event": "trial", "config": cfg, "wall_s": round(dt, 1), "result": res})
        if res.get("error"):
            log(f"  -> ERROR: {str(res['error'])[:400]}")
        else:
            log(f"  -> verdict guiding_helps_post_denoise={res.get('guiding_helps_post_denoise')} "
                f"worst_tile_delta={res.get('worst_tile_delta')} "
                f"OFF_wt={(res.get('arm_off') or {}).get('worst_tile_ssim')} "
                f"ON_wt={(res.get('arm_on') or {}).get('worst_tile_ssim')} "
                f"guiding_active={(res.get('arm_on') or {}).get('guiding_active')} "
                f"overhead_x={res.get('guiding_wallclock_overhead_x')}")
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
