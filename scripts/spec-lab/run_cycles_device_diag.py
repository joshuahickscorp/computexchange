#!/usr/bin/env python3
"""Build CX Cycles, then diagnose standalone CUDA/OptiX device discovery."""

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
LEDGER = os.path.join(
    cycles_fork.REPO,
    "docs/speed-lane-reports/spec-lab/cycles_device_diag_ledger.jsonl",
)


def log(message: str) -> None:
    print(f"[cycles-device-diag {time.strftime('%H:%M:%S')}] {message}", flush=True)


def append_ledger(record: dict) -> None:
    cycles_fork.append_ledger(record, ledger=LEDGER)


def run_stage(pod: dict, stage: cycles_fork.Stage) -> dict:
    log(f"STAGE {stage.name}: start (timeout {stage.timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, stage.cmd, timeout=stage.timeout_s)
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": stage.name,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 1),
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-1600:],
        }
        append_ledger({"event": "stage", **rec})
        return rec

    rec = {
        "stage": stage.name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-6000:],
        "err_tail": (err or "")[-1600:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {stage.name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{stage.name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{stage.name} stderr tail:\n{rec['err_tail']}")
    return rec


def diagnostic_stage(root: str) -> cycles_fork.Stage:
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    cmd = (
        "set +e; export DEBIAN_FRONTEND=noninteractive; cd " + q_root + "; "
        "apt-get update >/dev/null 2>&1; "
        "apt-get install -y gdb strace pciutils >/dev/null 2>&1; "
        "echo CX_DIAG_NVIDIA_SMI_BEGIN; nvidia-smi; echo CX_DIAG_NVIDIA_SMI_RC=$?; "
        "echo CX_DIAG_CMAKE_FLAGS_BEGIN; "
        "grep -E 'WITH_CYCLES_DEVICE_(CUDA|OPTIX)|WITH_CUDA_DYNLOAD|CUDA_|OPTIX' "
        "build/CMakeCache.txt 2>/dev/null | sort | head -120; "
        "echo CX_DIAG_LDD_CUDA_BEGIN; "
        "ldconfig -p 2>/dev/null | grep -E 'libcuda|libnvoptix|libnvidia-ptxjitcompiler' | head -80; "
        "echo CX_DIAG_CUDA_TOOLKIT_BEGIN; "
        "(command -v nvcc && nvcc --version) || "
        "(/usr/local/cuda/bin/nvcc --version 2>/dev/null) || true; "
        "echo CX_DIAG_LIST_BEGIN; "
        + q_bin + " --log-level debug --list-devices >/tmp/cx_cycles_list_devices.txt 2>&1; "
        "echo CX_DIAG_LIST_RC=$?; tail -200 /tmp/cx_cycles_list_devices.txt; "
        "echo CX_DIAG_CUDA_RENDER_BEGIN; "
        + q_bin + " --log-level debug --device CUDA --samples 1 --output /tmp/cx_cuda_diag.png "
        "examples/scene_monkey.xml >/tmp/cx_cycles_cuda_render.txt 2>&1; "
        "echo CX_DIAG_CUDA_RENDER_RC=$?; tail -200 /tmp/cx_cycles_cuda_render.txt; "
        "test -s /tmp/cx_cuda_diag.png && file /tmp/cx_cuda_diag.png && ls -lh /tmp/cx_cuda_diag.png; "
        "echo CX_DIAG_OPTIX_RENDER_BEGIN; "
        + q_bin + " --log-level debug --device OPTIX --samples 1 --output /tmp/cx_optix_diag.png "
        "examples/scene_monkey.xml >/tmp/cx_cycles_optix_render.txt 2>&1; "
        "echo CX_DIAG_OPTIX_RENDER_RC=$?; tail -120 /tmp/cx_cycles_optix_render.txt; "
        "echo CX_DIAG_GDB_CUDA_BEGIN; "
        "gdb -batch -ex 'set pagination off' "
        "-ex 'run --log-level debug --device CUDA --samples 1 --output /tmp/cx_gdb_cuda.png examples/scene_monkey.xml' "
        "-ex 'thread apply all bt' --args " + q_bin + " >/tmp/cx_cycles_gdb_cuda.txt 2>&1; "
        "echo CX_DIAG_GDB_CUDA_RC=$?; tail -220 /tmp/cx_cycles_gdb_cuda.txt; "
        "echo CX_DIAG_STRACE_CUDA_BEGIN; "
        "strace -f -o /tmp/cx_cycles_strace_cuda.txt " + q_bin +
        " --device CUDA --samples 1 --output /tmp/cx_strace_cuda.png examples/scene_monkey.xml "
        ">/tmp/cx_cycles_strace_cuda_stdout.txt 2>&1; "
        "echo CX_DIAG_STRACE_CUDA_RC=$?; tail -120 /tmp/cx_cycles_strace_cuda_stdout.txt; "
        "tail -160 /tmp/cx_cycles_strace_cuda.txt; "
        "echo CX_CYCLES_DEVICE_DIAG_OK=1"
    )
    return cycles_fork.Stage("device_diag", cmd, 2400)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default=cycles_fork.DEFAULT_REF)
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--cmake-args", default="",
                        help="extra CMake args forwarded through BUILD_CMAKE_ARGS")
    parser.add_argument("--no-patches", action="store_true")
    parser.add_argument("--skip-build", action="store_true",
                        help="assume --remote-root already contains a built install/cycles")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stages = (
        cycles_fork.runtime_smoke_stages(
            root=args.remote_root,
            require_patches=not args.no_patches,
        )
        if args.skip_build else
        cycles_fork.build_stages(
            root=args.remote_root,
            ref=args.ref,
            jobs=args.jobs,
            apply_patches=not args.no_patches,
            cmake_args=args.cmake_args,
        )
    )
    stages.append(diagnostic_stage(args.remote_root))
    manifest = {
        "remote": cycles_fork.CYCLES_REMOTE,
        "ref": args.ref,
        "root": args.remote_root,
        "jobs": args.jobs,
        "cmake_args": args.cmake_args,
        "skip_build": args.skip_build,
        "patches": [
            os.path.basename(p) for p in cycles_fork.patch_files()
        ] if not args.no_patches else [],
        "stages": [stage.to_json() for stage in stages],
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    pod = None
    records = []
    result = None
    try:
        pod = runpod.provision_reachable(
            GPU_PLAN,
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-device-diag",
        )
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        for stage in stages:
            rec = run_stage(pod, stage)
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"stage {stage.name} failed")

        diag_text = records[-1].get("out_tail", "")
        result = {
            "ok": "CX_CYCLES_DEVICE_DIAG_OK=1" in diag_text,
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
            "diag_tail": diag_text,
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
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
        append_ledger({"event": "result", "result": result})
        append_ledger({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")

    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
