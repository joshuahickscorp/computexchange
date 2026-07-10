#!/usr/bin/env python3
"""Probe whether patched standalone Cycles sample subsets can be merged."""

from __future__ import annotations

import argparse
import json
import os
import re
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
    "docs/speed-lane-reports/spec-lab/cycles_sample_subset_ledger.jsonl",
)


def log(message: str) -> None:
    print(f"[cycles-subset {time.strftime('%H:%M:%S')}] {message}", flush=True)


def append_ledger(record: dict) -> None:
    cycles_fork.append_ledger(record, ledger=LEDGER)


def run_stage(pod: dict, name: str, cmd: str, timeout_s: int) -> dict:
    log(f"STAGE {name}: start (timeout {timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": name,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 1),
            "out_tail": "",
            "err_tail": f"{type(exc).__name__}: {exc}"[-1600:],
        }
        append_ledger({"event": "stage", **rec})
        return rec
    rec = {
        "stage": name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-3000:],
        "err_tail": (err or "")[-1600:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{name} stderr tail:\n{rec['err_tail']}")
    return rec


def device_arg(device: str) -> str:
    if not device:
        return ""
    return " --device " + cycles_fork.shell(device)


def adaptive_arg(disable_adaptive_sampling: bool) -> str:
    return " --disable-adaptive-sampling" if disable_adaptive_sampling else ""


def subset_probe_cmd(root: str, scene: str, samples: int, device: str = "",
                     disable_adaptive_sampling: bool = False) -> str:
    half = samples // 2
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    q_scene = cycles_fork.shell("examples/" + scene)
    return (
        "set -e -o pipefail; export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update >/dev/null 2>&1; "
        "apt-get install -y openimageio-tools >/dev/null 2>&1; "
        "cd " + q_root + "; "
        "rm -f /tmp/cx_full.exr /tmp/cx_sub0.exr /tmp/cx_sub1.exr /tmp/cx_merged.exr; "
        + q_bin + device_arg(device) + adaptive_arg(disable_adaptive_sampling) +
        f" --samples {samples} --output /tmp/cx_full.exr " + q_scene +
        " >/tmp/cx_full.log 2>&1; "
        + q_bin + device_arg(device) + adaptive_arg(disable_adaptive_sampling) +
        f" --samples {samples} --sample-subset-offset 0 --sample-subset-length {half} "
        "--output /tmp/cx_sub0.exr " + q_scene + " >/tmp/cx_sub0.log 2>&1; "
        + q_bin + device_arg(device) + adaptive_arg(disable_adaptive_sampling) +
        f" --samples {samples} --sample-subset-offset {half} --sample-subset-length {half} "
        "--output /tmp/cx_sub1.exr " + q_scene + " >/tmp/cx_sub1.log 2>&1; "
        "oiiotool /tmp/cx_sub0.exr /tmp/cx_sub1.exr --add --mulc 0.5 -o /tmp/cx_merged.exr; "
        "set +e; DIFF=$(oiiotool /tmp/cx_full.exr /tmp/cx_merged.exr --diff 2>&1); DIFF_RC=$?; set -e; "
        "echo \"$DIFF\"; "
        "echo CX_SUBSET_DIFF_RC=$DIFF_RC; "
        "ls -lh /tmp/cx_full.exr /tmp/cx_sub0.exr /tmp/cx_sub1.exr /tmp/cx_merged.exr; "
        f"echo CX_SUBSET_PROBE_OK scene={scene} samples={samples} half={half} device={device or 'default'}"
    )


def parse_probe(stage: dict) -> dict:
    text = stage.get("out_tail", "")
    diff_rc = None
    match = re.search(r"CX_SUBSET_DIFF_RC=(\d+)", text)
    if match:
        diff_rc = int(match.group(1))
    return {
        "ok": stage["ok"] and "CX_SUBSET_PROBE_OK" in text,
        "diff_rc": diff_rc,
        "elapsed_s": stage["elapsed_s"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default=cycles_fork.DEFAULT_REF)
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--cmake-args", default="",
                        help="extra CMake args forwarded through BUILD_CMAKE_ARGS")
    parser.add_argument("--scene", default="scene_monkey.xml")
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--device", default="")
    parser.add_argument("--skip-build", action="store_true",
                        help="assume --remote-root already contains a built install/cycles")
    parser.add_argument("--disable-adaptive-sampling", action="store_true",
                        help="disable Cycles adaptive sampling for fixed sample-subset math")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.samples % 2 != 0:
        raise SystemExit("--samples must be even for the two-way subset probe")

    build_stages = (
        cycles_fork.runtime_smoke_stages(root=args.remote_root)
        if args.skip_build else
        cycles_fork.build_stages(
            root=args.remote_root,
            ref=args.ref,
            jobs=args.jobs,
            cmake_args=args.cmake_args,
        )
    )
    probe = {
        "name": "sample_subset_probe",
        "cmd": subset_probe_cmd(
            args.remote_root,
            args.scene,
            args.samples,
            args.device,
            args.disable_adaptive_sampling,
        ),
        "timeout_s": 1800,
    }
    manifest = {
        "remote": cycles_fork.CYCLES_REMOTE,
        "ref": args.ref,
        "root": args.remote_root,
        "jobs": args.jobs,
        "cmake_args": args.cmake_args,
        "skip_build": args.skip_build,
        "patches": [os.path.basename(p) for p in cycles_fork.patch_files()],
        "scene": args.scene,
        "samples": args.samples,
        "device": args.device or "default",
        "disable_adaptive_sampling": args.disable_adaptive_sampling,
        "build_stages": [stage.to_json() for stage in build_stages],
        "probe": probe,
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
            name="cx-cycles-subset",
        )
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        for stage in build_stages:
            rec = run_stage(pod, stage.name, stage.cmd, stage.timeout_s)
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"build stage {stage.name} failed")

        rec = run_stage(pod, probe["name"], probe["cmd"], probe["timeout_s"])
        records.append(rec)
        probe_result = parse_probe(rec)
        if not rec["ok"]:
            raise RuntimeError("sample subset probe failed")
        result = {
            "ok": probe_result["ok"],
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "ref": args.ref,
            "scene": args.scene,
            "samples": args.samples,
            "device": args.device or "default",
            "probe": probe_result,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
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
