#!/usr/bin/env python3
"""Drive the EXHAUSTIVE multi-selector reference-free tile-repair probe on ONE
money-safe RunPod GPU.

WHAT IT MEASURES (pod/exp_multi_selector_probe.py): from ONE RUN 3-config anchor
render of classroom frame 1 at 3840x2160, the TRUE per-tile error E_oidn = 1 -
SSIM(oidn, ref) AND FOUR candidate REFERENCE-FREE tile selectors scored against it
(recall@1/4/12 + Spearman each), so we learn which — if any — localizes the
strict-gate-failing tiles that two prior selectors could not (two-draft variance
divergence recall 0.0; cross-denoiser disagreement recall ~0.08). Selectors:
S1 adaptive sample-count pass, S2 denoiser residual |noisy-oidn|, S3 content gradient,
S4 normal-AOV edge density. THREE real renders (anchor+AOVs, noisy same-seed, 4096-spp
reference); the full 64-tile arrays + per-selector recall/Spearman + availability flags
land in the ledger. Estimated spend: ~$1.0-1.6 (about 40-55 GPU-minutes on an A100;
more if the H100 rung pays the one-time sm_90 probe JIT).

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

LEDGER = REPO / "docs/speed-lane-reports/spec-lab/multi_selector_probe_ledger.jsonl"
POD_DISK_GB = 120
# One frame + reference is well under 2h even on a JIT-paying H100 rung; the pod
# self-terminates at 3h regardless of what happens to this process.
WATCHDOG_TTL_S = 10800


def log(message: str) -> None:
    print(f"[multi-selector-probe {time.strftime('%H:%M:%S')}] {message}", flush=True)


def remote_probe(pod: dict[str, Any], config: dict[str, Any], timeout: int) -> dict[str, Any]:
    payload = json.dumps(config).replace("'", "'\\''")
    cmd = "python3 pod/exp_multi_selector_probe.py '" + payload + "'"
    # The three renders total ~40-55 min — the exact run-length band where a single
    # synchronous SSH has already died to a peer reset (2026-07-09 incident).
    # Detached-on-pod + short reconnecting polls; (rc, stdout, stderr_tail) keeps the
    # final-JSON-line contract.
    rc, out, err = runpod.ssh_detached(
        pod, cmd,
        workdir="/root/spec-lab",
        tag="multi-selector-probe",
        timeout_s=timeout,
        poll_every=20,
    )
    return parse_final_json(out or "", err or "", rc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="cx-multi-selector-probe")
    parser.add_argument("--scene", default="classroom")
    parser.add_argument("--resolution", default="3840x2160")
    parser.add_argument("--frame", type=int, default=1,
                        help="which frame of the deterministic camera path to probe "
                             "(RUN 3/4's worst tiles live in frame 1)")
    parser.add_argument("--nframes", type=int, default=4,
                        help="camera-path length (4 = the RUN 3 shot, so the frame is "
                             "pixel-comparable to the integrated receipts)")
    parser.add_argument("--draft-spp", type=int, default=512)
    parser.add_argument("--ref-spp", type=int, default=4096)
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

    gpu_plan = parse_gpu_plan(args.gpu_plan, allow_unsupported=args.allow_unsupported_gpu)
    # The exact RUN 3 anchor knobs (integrated driver config), one frame.
    config = {
        "scene": args.scene, "resolution": args.resolution,
        "frame": args.frame, "nframes": args.nframes,
        "draft_spp": args.draft_spp, "ref_spp": args.ref_spp,
        "adaptive_threshold": 0.02, "adaptive_min_samples": 16,
        "denoise_guides": True, "light_tree": True, "bounces": 12,
        "cam_motion": 1.0, "seed": 0,
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
    append_jsonl(LEDGER, {"event": "multi_selector_probe_preflight", "preflight": preflight})
    log(f"preflight clear; balance ${balance['clientBalance']:.2f}; provisioning monotonic GPU plan")

    try:
        pod = runpod.provision_reachable(gpu_plan, POD_IMAGE, disk_gb=POD_DISK_GB,
                                         name=args.job_id)
    except Exception as exc:
        append_jsonl(LEDGER, {"event": "multi_selector_probe_provision_pruned",
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
            "event": "multi_selector_probe_result",
            "metrics": metrics,
            "config": config,
            "pod": {key: pod.get(key) for key in ("id", "gpu", "cloud", "cuda_driver_version")},
        })
        if "error" in metrics:
            # The honest negative/failure is already in the ledger; fail loudly.
            raise RuntimeError(f"probe run failed (ledgered): {metrics['error']}")
        avail = metrics.get("available_selectors", [])
        for name in metrics.get("selector_order", []):
            sel = metrics.get("selectors", {}).get(name, {})
            log(f"{name}: available={sel.get('available')} "
                f"recall@1={sel.get('recall_at_1')} recall@4={sel.get('recall_at_4')} "
                f"recall@12={sel.get('recall_at_12')} "
                f"spearman={sel.get('spearman_vs_E_oidn')}")
        log(f"available selectors: {avail}")
        print(json.dumps(metrics, sort_keys=True))
    finally:
        runpod.terminate_all_tracked()
        remaining = runpod._load_tracked()
        if remaining:
            raise RuntimeError(f"teardown incomplete; tracked pods remain: {remaining}")
        log("pod teardown confirmed")


if __name__ == "__main__":
    main()
