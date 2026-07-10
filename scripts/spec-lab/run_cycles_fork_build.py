#!/usr/bin/env python3
"""Build official standalone Cycles as the CX fork baseline on one safe RunPod pod."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cycles_fork  # noqa: E402
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
POD_DISK_GB = 180
WATCHDOG_TTL_S = 14400


def log(message: str) -> None:
    print(f"[cycles-fork {time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_stage(pod: dict, stage: cycles_fork.Stage) -> dict:
    log(f"STAGE {stage.name}: start (timeout {stage.timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, stage.cmd, timeout=stage.timeout_s)
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.time() - t0, 1)
        rec = {
            "stage": stage.name,
            "ok": False,
            "elapsed_s": elapsed,
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-1200:],
        }
        cycles_fork.append_ledger({"event": "stage", **rec})
        return rec

    elapsed = round(time.time() - t0, 1)
    rec = {
        "stage": stage.name,
        "ok": rc == 0,
        "elapsed_s": elapsed,
        "out_tail": (out or "")[-1600:],
        "err_tail": (err or "")[-1200:],
    }
    cycles_fork.append_ledger({"event": "stage", **rec})
    log(f"STAGE {stage.name}: rc={rc} elapsed={elapsed}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{stage.name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{stage.name} stderr tail:\n{rec['err_tail']}")
    return rec


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default=cycles_fork.DEFAULT_REF,
                        help="Cycles git ref to build, e.g. main or release/v5.1")
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--jobs", type=int, default=0,
                        help="parallel make jobs; 0 lets make choose")
    parser.add_argument("--cmake-args", default="",
                        help="extra CMake args forwarded through BUILD_CMAKE_ARGS")
    parser.add_argument("--no-patches", action="store_true",
                        help="build pristine official Cycles without CX patch queue")
    parser.add_argument("--overall-deadline-s", type=int, default=13200)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the stage manifest without provisioning")
    args = parser.parse_args()

    stages = cycles_fork.build_stages(
        root=args.remote_root,
        ref=args.ref,
        jobs=args.jobs,
        apply_patches=not args.no_patches,
        cmake_args=args.cmake_args,
    )
    manifest = cycles_fork.scaffold_manifest(
        root=args.remote_root,
        ref=args.ref,
        jobs=args.jobs,
        apply_patches=not args.no_patches,
        cmake_args=args.cmake_args,
    )
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    pod = None
    stage_records = []
    result = None
    try:
        log("provisioning pod for official standalone Cycles build")
        pod = runpod.provision_reachable(
            GPU_PLAN,
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-fork",
        )
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        cycles_fork.append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        start = time.time()
        for stage in stages:
            if time.time() - start > args.overall_deadline_s:
                result = {
                    "ok": False,
                    "error": f"overall deadline hit before stage {stage.name}",
                    "stages": stage_records,
                }
                break
            stage_record = run_stage(pod, stage)
            stage_records.append(stage_record)
            if not stage_record["ok"]:
                result = {
                    "ok": False,
                    "error": f"stage {stage.name} failed",
                    "failed_stage": stage.name,
                    "stages": stage_records,
                }
                break
        else:
            all_output = "\n".join(r.get("out_tail", "") for r in stage_records)
            result = {
                "ok": (
                    "CX_CYCLES_BINARY_SMOKE_OK=1" in all_output and
                    (
                        args.no_patches or
                        "CX_CYCLES_PATCH_CLI_SMOKE_OK=1" in all_output
                    ) and
                    "CX_CYCLES_RENDER_SMOKE_OK=1" in all_output
                ),
                "remote": cycles_fork.CYCLES_REMOTE,
                "ref": args.ref,
                "root": args.remote_root,
                "binary": cycles_fork.binary_path(args.remote_root),
                "stages": [
                    {k: r[k] for k in ("stage", "ok", "elapsed_s")}
                    for r in stage_records
                ],
                "total_s": round(time.time() - start, 1),
            }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stages": stage_records,
        }
        log(f"ERROR {result['error']}")
    finally:
        if pod:
            log("tearing down")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error: {exc}")
        b2 = runpod.balance()["clientBalance"]
        cycles_fork.append_ledger({"event": "result", "result": result})
        cycles_fork.append_ledger({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")

    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
