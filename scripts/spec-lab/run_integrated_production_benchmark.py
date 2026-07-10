#!/usr/bin/env python3
"""Run one same-GPU CX-vs-Cycles production benchmark with a shared receipt.

The render stage is the existing CX-owned temporal/speculative render stack over a
real Blender production scene. The normal Cycles baseline and CX stack are emitted
by the same remote runner, on the same GPU, then joined with the exact structured
dispatch/receipt decoding stage. The resulting headline is end-to-end wall time,
not an invented multiplication of component ratios.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import cx_integrated_speculation as integrated  # noqa: E402
import run_spec_render_token_end_to_end as end_to_end  # noqa: E402
import runpod  # noqa: E402


# Monotonic capability ordering: capacity failure may only move to a stronger GPU.
# The plan is configurable because RunPod's exact catalog names vary by region.
#
# HARD DEVICE CONSTRAINT (device bug 2026-07-09): every SKU here must actually PASS the
# functional GPU probe under the pinned Blender 4.2 tarball (DEFAULT_BLENDER_URL in
# pod/exp_render_stack.py). What we know, fact vs hypothesis:
#   MEASURED (2026-07-09): Blackwell (B200/B300 = sm_100, RTX 50xx = sm_120) has NO
#   Blender 4.2 Cycles kernel. Cycles ENUMERATES the GPU, cannot load a kernel, and
#   silently falls back to CPU — exactly how the B300 run burned ~$0.58 with no
#   receipt. Blackwell stays UNSUPPORTED until a Blender build with sm_100/sm_120
#   kernels is proven on-box; parse_gpu_plan() below rejects Blackwell SKUs unless
#   --allow-unsupported-gpu is set.
#   MEASURED (2026-07-09 evening): TWO independent H100 SECURE pods (jl6jfs1968l6m8,
#   mov07w7aw20xo4) failed the 64x64@1spp functional GPU probe with "timed out after
#   300 seconds", while every A100 (sm_80) pod that day passed the probe in seconds
#   and rendered fine. An earlier revision of this comment CLAIMED Blender 4.2 ships
#   Hopper (sm_90) kernels — that claim is now empirically doubtful and is RETRACTED.
#   HYPOTHESIS (unproven): the tarball's precompiled cubins do NOT include sm_90, so
#   the first Cycles render on Hopper triggers a driver PTX JIT of the huge kernel
#   that exceeds 300s. Mitigation either way: the config below sets
#   gpu_probe_timeout_s=1500 so the probe absorbs a one-time JIT (kernels land in the
#   per-user CUDA cache afterward); if an H100/H200 pod still fails at 1500s, treat
#   sm_90 as non-working under this tarball — the A100 rungs remain the PROVEN base.
# Plan per the standing gpu-provisioning-policy (task #20): base tier A100 then H100 —
# cheapest first, then availability, COMMUNITY then SECURE at each rung; if neither
# base is available UPGRADE to H200. NEVER downgrade to L40S/RTX A6000/A40/CPU.
DEFAULT_GPU_PLAN = (
    "NVIDIA A100 80GB PCIe:COMMUNITY,NVIDIA A100 80GB PCIe:SECURE,"
    "NVIDIA H100 80GB HBM3:COMMUNITY,NVIDIA H100 80GB HBM3:SECURE,"
    "NVIDIA H200:COMMUNITY,NVIDIA H200:SECURE"
)

# Substrings (case-insensitive) that mark a Blackwell / no-known-good-Blender-4.2-kernel
# SKU. None of these appear in A100/H100/H200/L40S/A6000/A40 catalog names.
UNSUPPORTED_GPU_MARKERS = (
    "B200", "B300", "GB200", "GB300", "BLACKWELL",
    "RTX 5090", "RTX 5080", "RTX 5070", "RTX PRO 6000 B",
)
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 120
WATCHDOG_TTL_S = 14400
LEDGER = REPO / "docs/speed-lane-reports/spec-lab/integrated_spec_render_token_ledger.jsonl"


def log(message: str) -> None:
    print(f"[integrated-production {time.strftime('%H:%M:%S')}] {message}", flush=True)


def parse_gpu_plan(value: str, allow_unsupported: bool = False) -> list[tuple[str, str]]:
    plan = []
    for entry in value.split(","):
        gpu, sep, cloud = entry.strip().rpartition(":")
        if not sep or not gpu or not cloud:
            raise ValueError("--gpu-plan entries must be GPU:COMMUNITY or GPU:SECURE")
        upper = gpu.upper()
        bad = [m for m in UNSUPPORTED_GPU_MARKERS if m in upper]
        if bad and not allow_unsupported:
            raise ValueError(
                f"GPU {gpu!r} matches unsupported-arch marker(s) {bad}: Blender 4.2 has no "
                f"Cycles kernel for Blackwell (sm_100/sm_120) and silently renders on CPU there "
                f"(this is the 2026-07-09 B300 $0.58-no-receipt bug). Use A100/L40S/H100/H200, "
                f"or pass --allow-unsupported-gpu once a Blackwell-capable Blender is proven."
            )
        plan.append((gpu, cloud))
    if not plan:
        raise ValueError("empty GPU plan")
    return plan


def parse_final_json(stdout: str, stderr: str, rc: int) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {"error": f"no stdout (rc={rc}); stderr_tail={stderr[-300:]}"}
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"error": f"unparseable final line: {exc}; last={lines[-1][:200]}"}


def remote_stack(pod: dict[str, Any], config: dict[str, Any], timeout: int) -> dict[str, Any]:
    payload = json.dumps(config).replace("'", "'\\''")
    cmd = "python3 pod/exp_render_stack.py '" + payload + "'"
    # 2026-07-09 incident: this render used to run as ONE synchronous runpod.ssh()
    # call, and a 49-minute 4K/4096-spp render was lost to a peer-side connection
    # RESET after 3 of 4 reference frames had completed on the GPU. The render is
    # the ONLY long-running remote command in this driver, so it — and only it —
    # runs detached-on-pod with short reconnecting completion polls. Every short
    # command (mkdir/scp/pip) stays on plain runpod.ssh. timeout_s is the SAME
    # budget as before (args.max_minutes * 60), which stays well inside
    # WATCHDOG_TTL_S, so the money-safety/watchdog ordering is unchanged.
    # ssh_detached returns (rc, full_stdout, stderr_tail) — the same shape ssh()
    # returned — so the final-JSON-line contract of exp_render_stack.py holds.
    rc, out, err = runpod.ssh_detached(
        pod, cmd,
        workdir="/root/spec-lab",
        tag="render-stack",
        timeout_s=timeout,
        poll_every=20,
    )
    return parse_final_json(out or "", err or "", rc)


def write_receipt(args: argparse.Namespace, metrics: dict[str, Any], pod: dict[str, Any]) -> dict[str, Any]:
    job = integrated.RenderSpecJob(
        job_id=args.job_id,
        workload="production_video" if args.frames > 1 else "production_still",
        scene=args.scene,
        resolution=args.resolution,
        frames=args.frames,
        render_policy="motion/depth draft->verify->accept/refine/fallback",
        token_policy="json-template job-event draft->byte verify->repair",
    )
    token = end_to_end.run_manifest_speculation(
        end_to_end.manifest_stream(job, args.events), args.prefix_rows,
        [4096, 2048, 1536, 1024, 768, 512, 384, 256, 128, 64, 32, 16, 8, 4, 2],
    )
    render = end_to_end.normalize_stack_metrics(metrics, f"runpod:{pod['id']}")
    decision = integrated.RenderVerifier.decide(
        token_exact=bool(token["exact"]), global_ssim=render["global_ssim"],
        worst_tile_ssim=render["worst_tile_ssim"], render_modeled=render["render_modeled"],
    )
    receipt = integrated.RenderSpecReceipt(
        job=job,
        token_baseline_s=float(token["baseline_s"]), token_spec_s=float(token["spec_s"]),
        render_baseline_s=render["render_baseline_s"], render_spec_s=render["render_spec_s"],
        global_ssim=render["global_ssim"], worst_tile_ssim=render["worst_tile_ssim"],
        token_exact=bool(token["exact"]), render_modeled=render["render_modeled"],
        evidence_type=render["evidence_type"], decision=decision,
    ).to_dict()
    receipt["token"] = token
    receipt["render_metrics"] = metrics
    receipt["gpu"] = {key: pod.get(key) for key in ("gpu", "cloud", "cuda_driver_version")}
    end_to_end.append_jsonl(LEDGER, {"event": "same_gpu_integrated_production_receipt", "receipt": receipt})
    end_to_end.write_report(receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", default="cx-production-4k-video")
    parser.add_argument("--scene", default="classroom")
    parser.add_argument("--resolution", default="3840x2160")
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--draft-spp", type=int, default=512)
    parser.add_argument("--ref-spp", type=int, default=4096)
    parser.add_argument("--keyframe-every", type=int, default=4)
    parser.add_argument("--hole-fill", choices=("rerender", "inpaint", "nearest"), default="rerender")
    parser.add_argument("--events", type=int, default=1024)
    parser.add_argument("--prefix-rows", type=int, default=12)
    parser.add_argument("--max-minutes", type=int, default=180)
    parser.add_argument("--min-balance", type=float, default=12.0)
    parser.add_argument("--gpu-plan", default=DEFAULT_GPU_PLAN)
    parser.add_argument(
        "--allow-unsupported-gpu", action="store_true",
        help="permit Blackwell (B200/B300/RTX50xx) SKUs; ONLY after a Blender build with "
             "sm_100/sm_120 Cycles kernels is proven on-box (default: reject, they render on CPU)",
    )
    parser.add_argument(
        "--repair-enabled", action="store_true",
        help="enable the reference-free tile REPAIR pass (PASS 3.5 in exp_render_stack.py): "
             "a raw two-draft divergence selector picks the worst tiles on the grading grid "
             "and re-renders ONLY them at higher spp (bordered Cycles renders, feathered "
             "composite), every second charged into T_stack. Default OFF — receipts without "
             "this flag are byte-identical to the pre-repair runner. Target: lift worst-tile "
             "past the strict delivery gate (>=0.95) while keeping the multiplier >~4x "
             "(docs/research/RENDER_REPAIR_LOOP_DESIGN.md, MODELED arithmetic).",
    )
    parser.add_argument("--repair-top-k", type=int, default=12,
                        help="global shot-wide repaired-tile budget (design default 12)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    gpu_plan = parse_gpu_plan(args.gpu_plan, allow_unsupported=args.allow_unsupported_gpu)
    config = {
        "scene": args.scene, "resolution": args.resolution, "frames": args.frames,
        "keyframe_every": args.keyframe_every, "draft_spp": args.draft_spp,
        "ref_spp": args.ref_spp, "adaptive_threshold": 0.02,
        "denoiser": "oidn", "denoise_guides": True, "light_tree": True,
        "bounces": 12, "hole_fill": args.hole_fill, "device": "GPU", "require_gpu": True,
        # sm_90 (H100/H200) first-render JIT headroom: if the pinned Blender 4.2
        # tarball lacks sm_90 cubins, the FIRST Cycles render triggers a driver PTX
        # JIT of the huge kernel that can run many minutes (hypothesis; 2026-07-09
        # two H100 SECURE pods timed out at the old 300s probe cap while every A100
        # passed in seconds — see module header). 1500s gives the probe one-time JIT
        # headroom (kernels cache per-user afterward, so the real renders under the
        # runner's ref/anchor/calib timeouts never re-pay it), while a genuinely
        # broken pod still fails loudly at the cap. Runner default stays 300 so
        # other callers see no behavior change.
        "gpu_probe_timeout_s": 1500,
    }
    if args.repair_enabled:
        # PASS 3.5 pass-through: runner defaults cover the rest (selector two_draft,
        # selection_draft_spp 64, 4x repair spp, margin 16 / feather 12 — see
        # exp_render_stack.py params parse + RENDER_REPAIR_LOOP_DESIGN.md).
        config["repair_enabled"] = True
        config["repair_top_k"] = args.repair_top_k
    manifest = {"gpu_plan": gpu_plan, "config": config, "timeout_s": args.max_minutes * 60}
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    tracked = runpod._load_tracked()
    live = runpod.live_pods()
    balance = runpod.balance()
    if tracked or live:
        raise RuntimeError(f"refusing cloud benchmark with existing pods: tracked={tracked}, live={live}")
    if float(balance["clientBalance"]) <= args.min_balance:
        raise RuntimeError(f"balance ${balance['clientBalance']:.2f} is below floor ${args.min_balance:.2f}")
    preflight = {
        "tracked": tracked,
        "live": live,
        "balance": balance,
        "gpu_plan": gpu_plan,
        "config": config,
    }
    end_to_end.append_jsonl(LEDGER, {"event": "production_benchmark_preflight", "preflight": preflight})
    log(f"preflight clear; balance ${balance['clientBalance']:.2f}; provisioning monotonic GPU plan")
    try:
        pod = runpod.provision_reachable(gpu_plan, POD_IMAGE, disk_gb=POD_DISK_GB, name=args.job_id)
    except Exception as exc:
        end_to_end.append_jsonl(
            LEDGER,
            {
                "event": "production_benchmark_provision_pruned",
                "reason": str(exc),
                "gpu_plan": gpu_plan,
                "preflight": preflight,
            },
        )
        raise
    try:
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)
        rc, _, err = runpod.ssh(pod, "mkdir -p /root/spec-lab", timeout=60)
        if rc != 0:
            raise RuntimeError(f"remote mkdir failed: {err[-200:]}")
        ok, error = runpod.scp_to(pod, str(HERE / "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"pod runner transfer failed: {error[-200:]}")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"remote dependencies failed: {(out + err)[-300:]}")
        metrics = remote_stack(pod, config, args.max_minutes * 60)
        try:
            receipt = write_receipt(args, metrics, pod)
        except Exception as exc:
            end_to_end.append_jsonl(
                LEDGER,
                {
                    "event": "production_benchmark_render_pruned",
                    "reason": str(exc),
                    "metrics": metrics,
                    "pod": {key: pod.get(key) for key in ("id", "gpu", "cloud")},
                },
            )
            raise
        print(json.dumps(receipt, sort_keys=True))
    finally:
        runpod.terminate_all_tracked()
        remaining = runpod._load_tracked()
        if remaining:
            raise RuntimeError(f"teardown incomplete; tracked pods remain: {remaining}")
        log("pod teardown confirmed")


if __name__ == "__main__":
    main()
