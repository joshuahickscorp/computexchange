#!/usr/bin/env python3
"""Build standalone Cycles, then benchmark small official XML scenes."""

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
    "docs/speed-lane-reports/spec-lab/cycles_baseline_matrix_ledger.jsonl",
)


def log(message: str) -> None:
    print(f"[cycles-matrix {time.strftime('%H:%M:%S')}] {message}", flush=True)


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
            "err_tail": f"{type(exc).__name__}: {exc}"[-1200:],
        }
        append_ledger({"event": "stage", **rec})
        return rec
    rec = {
        "stage": name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-1800:],
        "err_tail": (err or "")[-1200:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{name} stderr tail:\n{rec['err_tail']}")
    return rec


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def device_arg(device: str) -> str:
    if not device:
        return ""
    return " --device " + cycles_fork.shell(device)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "default"


def device_inventory_cmd(root: str) -> str:
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    return (
        "set -e -o pipefail; cd " + q_root + "; "
        "(" + q_bin + " --list-devices 2>&1 || true) | tee /tmp/cx_cycles_devices.txt; "
        "echo CX_DEVICES_OK=1"
    )


def benchmark_cmd(root: str, scene: str, samples: int, device: str = "") -> str:
    scene_name = os.path.basename(scene).replace(".xml", "")
    device_name = safe_name(device) if device else "default"
    out = f"/tmp/cx_cycles_{scene_name}_{samples}_{device_name}.png"
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    q_scene = cycles_fork.shell("examples/" + scene)
    q_out = cycles_fork.shell(out)
    return (
        "set -e -o pipefail; cd " + q_root + "; "
        "rm -f " + q_out + " /tmp/cx_cycles_time.txt; "
        "/usr/bin/time -f 'CX_TIME_S=%e' -o /tmp/cx_cycles_time.txt "
        + q_bin + device_arg(device) + f" --samples {int(samples)} --output " + q_out + " " + q_scene +
        " 2>&1 | tail -80; "
        "cat /tmp/cx_cycles_time.txt; "
        "test -s " + q_out + "; "
        "file " + q_out + "; "
        "ls -lh " + q_out + "; "
        f"echo CX_BENCH_OK scene={scene} samples={int(samples)} device={device or 'default'} output={out}"
    )


def parse_bench(stage: dict, scene: str, samples: int, device: str = "") -> dict:
    text = stage.get("out_tail", "")
    match = re.search(r"CX_TIME_S=([0-9.]+)", text)
    size_match = re.search(r"\s(\d+[KMG]?)\s+\w+\s+\d+\s+[0-9:]+\s+(/tmp/\S+\.png)", text)
    return {
        "scene": scene,
        "samples": samples,
        "device": device or "default",
        "ok": stage["ok"] and "CX_BENCH_OK" in text,
        "elapsed_s": stage["elapsed_s"],
        "cycles_time_s": float(match.group(1)) if match else None,
        "output_size": size_match.group(1) if size_match else None,
        "out_tail": text[-600:] if not stage["ok"] else "",
        "err_tail": stage.get("err_tail", "")[-600:] if not stage["ok"] else "",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default=cycles_fork.DEFAULT_REF)
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--cmake-args", default="",
                        help="extra CMake args forwarded through BUILD_CMAKE_ARGS")
    parser.add_argument("--scenes", default="scene_monkey.xml,scene_sphere_bump.xml")
    parser.add_argument("--samples", default="8,32")
    parser.add_argument("--devices", default="CPU,CUDA,OPTIX",
                        help="comma-separated Cycles --device values; empty means CLI default")
    parser.add_argument("--no-patches", action="store_true",
                        help="build pristine official Cycles without CX patch queue")
    parser.add_argument("--skip-build", action="store_true",
                        help="assume --remote-root already contains a built install/cycles")
    parser.add_argument("--fail-fast", action="store_true",
                        help="stop on the first benchmark failure instead of ledgering the matrix")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scenes = parse_csv(args.scenes)
    samples = [int(s) for s in parse_csv(args.samples)]
    devices = parse_csv(args.devices) or [""]
    build_stages = (
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
    bench_stages = [
        (f"bench_{os.path.basename(scene).replace('.xml', '')}_{s}_{safe_name(device)}",
         benchmark_cmd(args.remote_root, scene, s, device),
         1800)
        for scene in scenes
        for s in samples
        for device in devices
    ]
    manifest = {
        "remote": cycles_fork.CYCLES_REMOTE,
        "ref": args.ref,
        "root": args.remote_root,
        "jobs": args.jobs,
        "cmake_args": args.cmake_args,
        "skip_build": args.skip_build,
        "scenes": scenes,
        "samples": samples,
        "devices": devices,
        "patches": [
            os.path.basename(p) for p in cycles_fork.patch_files()
        ] if not args.no_patches else [],
        "device_inventory": device_inventory_cmd(args.remote_root),
        "build_stages": [stage.to_json() for stage in build_stages],
        "bench_stages": [
            {"name": name, "cmd": cmd, "timeout_s": timeout_s}
            for name, cmd, timeout_s in bench_stages
        ],
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    pod = None
    records = []
    benches = []
    result = None
    try:
        pod = runpod.provision_reachable(
            GPU_PLAN,
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-matrix",
        )
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        for stage in build_stages:
            rec = run_stage(pod, stage.name, stage.cmd, stage.timeout_s)
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"build stage {stage.name} failed")

        rec = run_stage(pod, "device_inventory", device_inventory_cmd(args.remote_root), 900)
        records.append(rec)

        for scene in scenes:
            for sample_count in samples:
                for device in devices:
                    name = (
                        f"bench_{os.path.basename(scene).replace('.xml', '')}_"
                        f"{sample_count}_{safe_name(device)}"
                    )
                    rec = run_stage(
                        pod,
                        name,
                        benchmark_cmd(args.remote_root, scene, sample_count, device),
                        1800,
                    )
                    records.append(rec)
                    benches.append(parse_bench(rec, scene, sample_count, device))
                    if not rec["ok"] and args.fail_fast:
                        raise RuntimeError(f"benchmark stage {name} failed")

        result = {
            "ok": all(b["ok"] for b in benches),
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "ref": args.ref,
            "benches": benches,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "benches": benches,
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
