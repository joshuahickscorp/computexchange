#!/usr/bin/env python3
"""Run the spec-lab research queue on one money-safe warm RunPod pod.

This keeps scene downloads and pod-local render caches alive across trials. It never stores
credentials; pass RUNPOD_API_KEY in the environment for each invocation.
"""

import argparse
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import runpod  # noqa: E402
from research_next_queue import QUEUE, pod_command  # noqa: E402


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
POD_DISK_GB = 110
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/research_queue_ledger.jsonl")

DEFAULT_IDS = [
    "analytical_thr_0_01",
    "analytical_thr_0_005",
    "analytical_thr_0_001",
    "analytical_thr_0_01_noLT",
    "ultimate_no_reprojection",
    "upscale_guided_all",
    "interp_flow_guided",
    "bmw27_analytical_animation",
]


def log(message):
    print(f"[research-queue {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ledger_append(record):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}) + "\n")


def run_runner(pod, item):
    cmd = pod_command(item)
    rc, out, err = runpod.ssh(pod, cmd, timeout=int(item["timeout_s"]))
    tail = [line for line in (out or "").splitlines() if line.strip()]
    if not tail:
        return {"error": f"no stdout (rc={rc}); stderr_tail={(err or '')[-500:]}"}
    try:
        result = json.loads(tail[-1])
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"unparseable final line: {type(exc).__name__}: {exc}; last={tail[-1][:240]}"}
    if rc != 0 and not result.get("error"):
        result["runner_rc"] = rc
        result["stderr_tail"] = (err or "")[-500:]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="*", help="queue ids to run; default is the aggressive campaign")
    parser.add_argument("--max-minutes", type=int, default=180)
    parser.add_argument("--min-balance", type=float, default=5.0)
    parser.add_argument("--stop-on-error", action="store_true")
    args = parser.parse_args()

    wanted = args.ids or DEFAULT_IDS
    items_by_id = {item["id"]: item for item in QUEUE}
    missing = [item_id for item_id in wanted if item_id not in items_by_id]
    if missing:
        raise SystemExit(f"unknown queue ids: {', '.join(missing)}")
    items = [items_by_id[item_id] for item_id in wanted]

    runpod.register_cleanup()
    deadline = time.time() + args.max_minutes * 60

    def deadline_handler(*_):
        log("deadline signal: terminating tracked pods before exit")
        runpod.terminate_all_tracked()
        os._exit(124)

    signal.signal(signal.SIGALRM, deadline_handler)
    signal.alarm(max(60, args.max_minutes * 60 + 300))

    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}; floor ${args.min_balance:.2f}; queue={','.join(wanted)}")
    if bal0 <= args.min_balance:
        log("balance already at/below floor; aborting before provisioning")
        return

    pod = None
    try:
        log("provisioning one warm GPU pod")
        pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB)
        ledger_append({"event": "pod_up", "pod": pod, "queue": wanted})
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:240]}")
        log("installing render deps")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image "
            "2>&1 | tail -3",
            timeout=600,
        )
        log(f"deps rc={rc}: {(out or err or '').strip()[-220:]}")

        for idx, item in enumerate(items, 1):
            if time.time() > deadline:
                log("deadline reached; stopping queue")
                break
            bal = runpod.balance()["clientBalance"]
            if bal <= args.min_balance:
                log(f"balance ${bal:.2f} at/below floor ${args.min_balance:.2f}; stopping queue")
                break

            log(f"RUN {idx}/{len(items)} {item['id']} [{item['runner']}] timeout={item['timeout_s']}s")
            t0 = time.time()
            result = run_runner(pod, item)
            dt = time.time() - t0
            ledger_append({
                "event": "trial",
                "id": item["id"],
                "priority": item["priority"],
                "runner": item["runner"],
                "why": item["why"],
                "config": item["config"],
                "wall_s_incl_ssh": round(dt, 1),
                "result": result,
            })
            if result.get("error"):
                log(f"  ERROR {item['id']}: {str(result['error'])[:260]}")
                if args.stop_on_error:
                    break
                continue
            log(
                "  RESULT {id}: speedup={speedup} quality={quality} worst={worst} "
                "p5={p5} modeled={modeled} cache={cache}".format(
                    id=item["id"],
                    speedup=result.get("net_speedup"),
                    quality=result.get("quality"),
                    worst=result.get("worst_tile_ssim"),
                    p5=result.get("p5_tile_ssim"),
                    modeled=result.get("modeled"),
                    cache=result.get("ref_cache_hit"),
                )
            )
    finally:
        if pod:
            log("tearing down pod")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error; verify in console: {exc}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")


if __name__ == "__main__":
    main()
