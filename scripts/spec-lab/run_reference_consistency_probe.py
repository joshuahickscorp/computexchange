#!/usr/bin/env python3
"""Drive the reference self-consistency probe on ONE money-safe RunPod GPU.

WHAT IT MEASURES (pod/exp_reference_consistency_probe.py): whether the 4096-spp
REFERENCE is converged enough at its worst grading tile for the strict worst-tile
SSIM >= 0.95 delivery gate to be reachable AT ALL. It renders classroom frame 1 at
3840x2160 TWICE with a byte-identical reference config (adaptive OFF, denoiser OFF,
guides OFF, light-tree at the scene default — exp_render_stack.py's exact is_ref
recipe), the two renders differing ONLY in seed (0 vs 12345), then scores per-tile
SSIM(A,B) on the 8x8 grading grid. worst_tile_ref_vs_ref, global_ref_vs_ref,
p5_tile_ref_vs_ref, the full 64-tile array, the worst-consistency tile identity and
a boolean gate_reachable land in the ledger. HYPOTHESIS: at the high-variance
frame-edge tiles the reference disagrees with ITSELF by more than the gate tolerance,
so worst-tile 0.95 is unreachable by construction there (noise vs noise). Estimated
spend: ~$0.75-1.20 (about 35-45 GPU-minutes on an A100 — two reference renders; more
if the H100 rung pays the one-time sm_90 probe JIT).

MONEY-SAFETY (the adversarially-verified standard of
run_integrated_production_benchmark.py / run_cross_denoiser_probe.py):
  * ONE driver at a time — shared .tracked_pods.json means concurrent drivers kill
    each other's pods; preflight REFUSES to start if any tracked or live pod exists.
  * register_cleanup() wires teardown to every exit path before any provision.
  * arm_remote_watchdog() is the FIRST action on the pod (TTL well above the run
    budget) so an orphan self-terminates even if this process dies.
  * The GPU plan is the policy ladder A100 C/S -> H100 C/S -> H200 C/S
    (upgrade-never-downgrade; Blackwell rejected — no Blender 4.2 kernel).
  * The long render runs DETACHED on the pod (runpod.ssh_detached) so a peer reset
    cannot kill it; gpu_probe_timeout_s=1500 gives the sm_90 first-render JIT headroom.
  * Teardown in finally + a post-teardown tracked-pods-empty assertion.
  * --dry-run prints the exact manifest and exits without touching the API.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import runpod  # noqa: E402
# Reused from the integrated driver so the Blackwell guard / GPU policy ladder /
# final-JSON-line parsing stay single-sourced (import only — no writes there).
from run_integrated_production_benchmark import (  # noqa: E402
    DEFAULT_GPU_PLAN,
    POD_IMAGE,
    parse_final_json,
    parse_gpu_plan,
)
from run_spec_render_token_end_to_end import append_jsonl  # noqa: E402

LEDGER = REPO / "docs/speed-lane-reports/spec-lab/reference_consistency_ledger.jsonl"
POD_DISK_GB = 120
# Two reference renders are well under 2h even on a JIT-paying H100 rung; the pod
# self-terminates at 3h regardless of what happens to this process.
WATCHDOG_TTL_S = 10800


def log(message: str) -> None:
    print(f"[reference-consistency-probe {time.strftime('%H:%M:%S')}] {message}", flush=True)


def remote_probe(pod: dict[str, Any], config: dict[str, Any], timeout: int) -> dict[str, Any]:
    payload = json.dumps(config).replace("'", "'\\''")
    cmd = "python3 pod/exp_reference_consistency_probe.py '" + payload + "'"
    # The two 4096-spp reference renders total ~35-45 min — the exact run-length band
    # where a single synchronous SSH has already died to a peer reset (2026-07-09
    # incident). Detached-on-pod + short reconnecting polls; (rc, stdout, stderr_tail)
    # keeps the final-JSON-line contract.
    rc, out, err = runpod.ssh_detached(
        pod, cmd,
        workdir="/root/spec-lab",
        tag="reference-consistency-probe",
        timeout_s=timeout,
        poll_every=20,
    )
    return parse_final_json(out or "", err or "", rc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="cx-reference-consistency-probe")
    parser.add_argument("--scene", default="classroom")
    parser.add_argument("--resolution", default="3840x2160")
    parser.add_argument("--frame", type=int, default=1,
                        help="which frame of the deterministic camera path to probe "
                             "(the strict-gate-failing edge tiles live in frame 1)")
    parser.add_argument("--nframes", type=int, default=4,
                        help="camera-path length (4 = the RUN 3 shot, so the frame is "
                             "pixel-comparable to the integrated receipts)")
    parser.add_argument("--ref-spp", type=int, default=4096)
    parser.add_argument("--seed-a", type=int, default=0)
    parser.add_argument("--seed-b", type=int, default=12345)
    parser.add_argument("--max-minutes", type=int, default=120)
    parser.add_argument("--min-balance", type=float, default=5.0)
    parser.add_argument("--gpu-plan", default=DEFAULT_GPU_PLAN)
    parser.add_argument(
        "--allow-unsupported-gpu", action="store_true",
        help="permit Blackwell (B200/B300/RTX50xx) SKUs; ONLY after a Blender build "
             "with sm_100/sm_120 Cycles kernels is proven on-box (default: reject)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print the exact manifest (plan + config + timeout) and "
                             "exit without touching the RunPod API")
    args = parser.parse_args()

    if args.seed_a == args.seed_b:
        parser.error(
            f"--seed-a and --seed-b must differ (both {args.seed_a}); identical seeds "
            f"render the SAME image and the self-consistency SSIM would be a trivial 1.0")

    gpu_plan = parse_gpu_plan(args.gpu_plan, allow_unsupported=args.allow_unsupported_gpu)
    # The EXACT exp_render_stack.py reference recipe, one frame, two seeds. No
    # adaptive/denoiser/guides/light_tree knobs: the pod runner renders with is_ref=True,
    # which forces adaptive+denoiser OFF and skips the light-tree lever for BOTH renders.
    config = {
        "scene": args.scene, "resolution": args.resolution,
        "frame": args.frame, "nframes": args.nframes,
        "ref_spp": args.ref_spp, "bounces": 12, "cam_motion": 1.0,
        "seed_a": args.seed_a, "seed_b": args.seed_b,
        "device": "GPU", "require_gpu": True,
        # sm_90 first-render JIT headroom (same rationale + value as the integrated
        # driver; the probe absorbs the one-time JIT, real renders then hit the warmed
        # per-user CUDA cache).
        "gpu_probe_timeout_s": 1500,
    }
    manifest = {
        "gpu_plan": gpu_plan,
        "config": config,
        "timeout_s": args.max_minutes * 60,
        "watchdog_ttl_s": WATCHDOG_TTL_S,
        "ledger": str(LEDGER),
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    tracked = runpod._load_tracked()
    live = runpod.live_pods()
    balance = runpod.balance()
    if tracked or live:
        raise RuntimeError(
            f"refusing cloud probe with existing pods: tracked={tracked}, live={live} "
            f"(one driver at a time — concurrent drivers kill each other's pods)")
    if float(balance["clientBalance"]) <= args.min_balance:
        raise RuntimeError(
            f"balance ${balance['clientBalance']:.2f} is below floor ${args.min_balance:.2f}")
    preflight = {"tracked": tracked, "live": live, "balance": balance,
                 "gpu_plan": gpu_plan, "config": config}
    append_jsonl(LEDGER, {"event": "reference_consistency_probe_preflight", "preflight": preflight})
    log(f"preflight clear; balance ${balance['clientBalance']:.2f}; provisioning monotonic GPU plan")

    try:
        pod = runpod.provision_reachable(gpu_plan, POD_IMAGE, disk_gb=POD_DISK_GB,
                                         name=args.job_id)
    except Exception as exc:
        append_jsonl(LEDGER, {"event": "reference_consistency_probe_provision_pruned",
                              "reason": str(exc), "gpu_plan": gpu_plan})
        raise

    try:
        # FIRST action on the pod: the self-terminate backstop.
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"remote mkdir failed: {err[-200:]}")
        ok, error = runpod.scp_to(pod, str(HERE / "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"pod runner transfer failed: {error[-200:]}")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"remote dependencies failed: {(out + err)[-300:]}")
        metrics = remote_probe(pod, config, args.max_minutes * 60)
        append_jsonl(LEDGER, {
            "event": "reference_consistency_probe_result",
            "metrics": metrics,
            "config": config,
            "pod": {key: pod.get(key) for key in ("id", "gpu", "cloud", "cuda_driver_version")},
        })
        if "error" in metrics:
            # The honest negative/failure is already in the ledger; fail loudly.
            raise RuntimeError(f"probe run failed (ledgered): {metrics['error']}")
        log(f"global_ref_vs_ref={metrics.get('global_ref_vs_ref')} "
            f"worst_tile_ref_vs_ref={metrics.get('worst_tile_ref_vs_ref')} "
            f"p5_tile_ref_vs_ref={metrics.get('p5_tile_ref_vs_ref')} "
            f"gate_reachable={metrics.get('gate_reachable')}")
        log(str(metrics.get("interpretation")))
        print(json.dumps(metrics, sort_keys=True))
    finally:
        runpod.terminate_all_tracked()
        remaining = runpod._load_tracked()
        if remaining:
            raise RuntimeError(f"teardown incomplete; tracked pods remain: {remaining}")
        log("pod teardown confirmed")


if __name__ == "__main__":
    main()
