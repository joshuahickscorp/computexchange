#!/usr/bin/env python3
"""Benchmark exact N-way sample fan-out with patched standalone Cycles."""

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
# evidence). This default tier builds cx-cycles from source on the pod, so the
# native-arch kernels are compiled there — any policy rung works.
GPU_PLAN = [
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
# ARCH-PINNED capability tier, NOT a price downgrade — gpu-provisioning-policy
# exception clause: --gpu-tier=ada exists solely to run the prebuilt sm_89 (Ada)
# cx-cycles runtime tarball, which only loads on Ada silicon. An A100 (sm_80) or
# H100 (sm_90) CANNOT run an sm_89 cubin, so the policy ladder does not apply here;
# the SKU set IS the experiment substrate (test_cycles_sample_fanout_matrix.py
# asserts this plan's contents).
ADA_GPU_PLAN = [
    ("NVIDIA GeForce RTX 4090", "COMMUNITY"),
    ("NVIDIA GeForce RTX 4090", "SECURE"),
    ("NVIDIA L40", "COMMUNITY"),
    ("NVIDIA L40", "SECURE"),
    ("NVIDIA L40S", "COMMUNITY"),
    ("NVIDIA L40S", "SECURE"),
    ("NVIDIA RTX 6000 Ada Generation", "COMMUNITY"),
    ("NVIDIA RTX 6000 Ada Generation", "SECURE"),
    ("NVIDIA RTX 5000 Ada Generation", "COMMUNITY"),
    ("NVIDIA RTX 4000 Ada Generation", "COMMUNITY"),
    ("NVIDIA L4", "SECURE"),
]
# ARCH-PINNED capability tier (sm_90 Hopper runtime tarball) — same exception clause
# as ADA_GPU_PLAN above; all rungs are policy-tier (H100/H200) anyway.
HOPPER_GPU_PLAN = [
    ("NVIDIA H100 PCIe", "COMMUNITY"),
    ("NVIDIA H100 80GB HBM3", "COMMUNITY"),
    ("NVIDIA H100 NVL", "COMMUNITY"),
    ("NVIDIA H100 PCIe", "SECURE"),
    ("NVIDIA H100 80GB HBM3", "SECURE"),
    ("NVIDIA H100 NVL", "SECURE"),
    ("NVIDIA H200", "COMMUNITY"),
    ("NVIDIA H200", "SECURE"),
]
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 180
WATCHDOG_TTL_S = 14400
LEDGER = os.path.join(
    cycles_fork.REPO,
    "docs/speed-lane-reports/spec-lab/cycles_sample_fanout_ledger.jsonl",
)
NUMERIC_EQ_MEAN_TOL = 2.0e-5
NUMERIC_EQ_RMS_TOL = 2.0e-5
NUMERIC_EQ_MAX_TOL = 1.0e-4


def log(message: str) -> None:
    print(f"[cycles-fanout {time.strftime('%H:%M:%S')}] {message}", flush=True)


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
            "err_tail": f"{type(exc).__name__}: {exc}"[-1800:],
        }
        append_ledger({"event": "stage", **rec})
        return rec
    rec = {
        "stage": name,
        "ok": rc == 0,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": (out or "")[-12000:],
        "err_tail": (err or "")[-1800:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE {name}: rc={rc} elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    if rec["out_tail"].strip():
        log(f"{name} stdout tail:\n{rec['out_tail']}")
    if not rec["ok"] and rec["err_tail"].strip():
        log(f"{name} stderr tail:\n{rec['err_tail']}")
    return rec


def upload_prebuilt_root(pod: dict, tar_path: str, root: str) -> list[dict]:
    log(f"STAGE upload_prebuilt_root: start ({tar_path})")
    t0 = time.time()
    remote_tar = "/tmp/cx_cycles_prebuilt_root.tar"
    ok, err = runpod.scp_to(pod, tar_path, remote_tar, timeout=3600)
    upload_rec = {
        "stage": "upload_prebuilt_root",
        "ok": ok,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": tar_path if ok else "",
        "err_tail": (err or "")[-1800:],
    }
    append_ledger({"event": "stage", **upload_rec})
    log(f"STAGE upload_prebuilt_root: elapsed={upload_rec['elapsed_s']}s ok={upload_rec['ok']}")
    if not ok:
        return [upload_rec]
    cmd = (
        "set -e -o pipefail; "
        "rm -rf " + cycles_fork.shell(root) + "; "
        "mkdir -p " + cycles_fork.shell(root) + "; "
        "tar -xf " + cycles_fork.shell(remote_tar) + " -C " + cycles_fork.shell(root) + "; "
        "test -x " + cycles_fork.shell(cycles_fork.binary_path(root)) + "; "
        "test -d " + cycles_fork.shell(os.path.join(root, "examples")) + "; "
        "du -sh " + cycles_fork.shell(root) + "; "
        "echo CX_CYCLES_PREBUILT_ROOT_OK=1"
    )
    return [upload_rec, run_stage(pod, "extract_prebuilt_root", cmd, 1800)]


def export_runtime_root(pod: dict, root: str, local_tar_path: str) -> list[dict]:
    remote_tar = "/tmp/cx_cycles_runtime_root.tar.gz"
    pack_cmd = (
        "set -e -o pipefail; cd " + cycles_fork.shell(root) + "; "
        "test -x install/cycles; test -d examples; "
        "rm -f " + cycles_fork.shell(remote_tar) + "; "
        "tar -czf " + cycles_fork.shell(remote_tar) + " install examples; "
        "ls -lh " + cycles_fork.shell(remote_tar) + "; "
        "du -sh install examples; "
        "echo CX_CYCLES_RUNTIME_TAR_OK=1"
    )
    records = [run_stage(pod, "export_runtime_tar_pack", pack_cmd, 3600)]
    if not records[-1]["ok"]:
        return records
    log(f"STAGE export_runtime_tar_download: start ({local_tar_path})")
    t0 = time.time()
    ok, err = runpod.scp_from(pod, remote_tar, local_tar_path, timeout=7200)
    rec = {
        "stage": "export_runtime_tar_download",
        "ok": ok,
        "elapsed_s": round(time.time() - t0, 1),
        "out_tail": local_tar_path if ok else "",
        "err_tail": (err or "")[-1800:],
    }
    append_ledger({"event": "stage", **rec})
    log(f"STAGE export_runtime_tar_download: elapsed={rec['elapsed_s']}s ok={rec['ok']}")
    records.append(rec)
    return records


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_int_csv(value: str) -> list[int]:
    return [int(part) for part in parse_csv(value)]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "default"


def device_arg(device: str) -> str:
    if not device:
        return ""
    return " --device " + cycles_fork.shell(device)


def adaptive_arg(disable_adaptive_sampling: bool) -> str:
    return " --disable-adaptive-sampling" if disable_adaptive_sampling else ""


def resolve_chunk_count(fanout: int, chunks_per_worker: int = 1,
                        chunk_count: int = 0) -> int:
    if fanout <= 0:
        raise ValueError("fanout must be positive")
    if chunk_count:
        if chunk_count < fanout:
            raise ValueError("chunk_count must be at least fanout")
        return chunk_count
    if chunks_per_worker <= 0:
        raise ValueError("chunks_per_worker must be positive")
    return fanout * chunks_per_worker


def _chunk_loads_static(times: list[float], workers: int) -> list[float]:
    return [
        sum(times[worker * len(times) // workers:(worker + 1) * len(times) // workers])
        for worker in range(workers)
    ]


def _chunk_loads_dynamic(times: list[float], workers: int) -> list[float]:
    loads = [0.0 for _ in range(workers)]
    for value in times:
        worker = min(range(workers), key=lambda i: loads[i])
        loads[worker] += value
    return loads


def _chunk_loads_lpt(times: list[float], workers: int) -> list[float]:
    loads = [0.0 for _ in range(workers)]
    for value in sorted(times, reverse=True):
        worker = min(range(workers), key=lambda i: loads[i])
        loads[worker] += value
    return loads


def fanout_probe_cmd(root: str, scene: str, samples: int, fanout: int,
                     device: str = "", disable_adaptive_sampling: bool = False,
                     chunks_per_worker: int = 1, chunk_count: int = 0,
                     merge_mode: str = "linear",
                     quality_gate: bool = True,
                     execution_mode: str = "sequential",
                     parallel_slots: int = 0,
                     subset_retries: int = 0) -> str:
    if merge_mode not in ("linear", "tree", "python", "auto"):
        raise ValueError("merge_mode must be linear, tree, python, or auto")
    if execution_mode not in ("sequential", "parallel", "batch"):
        raise ValueError("execution_mode must be sequential, parallel, or batch")
    if parallel_slots < 0:
        raise ValueError("parallel_slots must be non-negative")
    if subset_retries < 0:
        raise ValueError("subset_retries must be non-negative")
    chunks = resolve_chunk_count(fanout, chunks_per_worker, chunk_count)
    if samples % chunks != 0:
        raise ValueError("samples must be divisible by chunk count")

    subset_len = samples // chunks
    scene_name = safe_name(os.path.basename(scene).replace(".xml", ""))
    device_name = safe_name(device)
    prefix = f"/tmp/cx_fanout_{scene_name}_{samples}_{fanout}w_{chunks}c_{device_name}"
    q_root = cycles_fork.shell(root)
    q_bin = cycles_fork.shell(cycles_fork.binary_path(root))
    q_scene = cycles_fork.shell("examples/" + scene)
    full = f"{prefix}_full.exr"
    merged = f"{prefix}_merged.exr"
    merged_linear = f"{prefix}_merged_linear.exr"
    merged_tree = f"{prefix}_merged_tree.exr"
    merged_python = f"{prefix}_merged_python.exr"
    full_png = f"{prefix}_full.png"
    merged_png = f"{prefix}_merged.png"
    subsets = [f"{prefix}_sub{i}.exr" for i in range(chunks)]
    tree_temps: list[str] = []

    def linear_merge_cmd(output: str, label: str) -> str:
        merge_expr = []
        for index, out in enumerate(subsets):
            merge_expr.append(cycles_fork.shell(out))
            if index >= 1:
                merge_expr.append("--add")
        merge_expr.extend(["--mulc", f"{1.0 / chunks:.12f}", "-o", cycles_fork.shell(output)])
        return (
            f"/usr/bin/time -f 'CX_MERGE_{label}_TIME_S=%e' "
            f"-o /tmp/cx_fanout_time_merge_{label.lower()}.txt "
            "oiiotool " + " ".join(merge_expr) + "; "
            f"cat /tmp/cx_fanout_time_merge_{label.lower()}.txt; "
        )

    def tree_merge_cmd(output: str) -> str:
        level_files = list(subsets)
        commands = ["rm -f " + cycles_fork.shell(f"{prefix}_tree_*.exr")]
        level = 0
        while len(level_files) > 1:
            next_files = []
            for pair_index in range(0, len(level_files), 2):
                left = level_files[pair_index]
                if pair_index + 1 >= len(level_files):
                    next_files.append(left)
                    continue
                right = level_files[pair_index + 1]
                out = f"{prefix}_tree_l{level}_{pair_index // 2}.exr"
                tree_temps.append(out)
                commands.append(
                    "oiiotool " + cycles_fork.shell(left) + " " +
                    cycles_fork.shell(right) + " --add -o " + cycles_fork.shell(out)
                )
                next_files.append(out)
            level_files = next_files
            level += 1
        commands.append(
            "oiiotool " + cycles_fork.shell(level_files[0]) +
            f" --mulc {1.0 / chunks:.12f} -o " + cycles_fork.shell(output)
        )
        script = "set -e; " + "; ".join(commands)
        return (
            "/usr/bin/time -f 'CX_MERGE_TREE_TIME_S=%e' "
            "-o /tmp/cx_fanout_time_merge_tree.txt "
            "bash -lc " + cycles_fork.shell(script) + "; "
            "cat /tmp/cx_fanout_time_merge_tree.txt; "
        )

    def python_merge_cmd(output: str) -> str:
        script = f"""
import numpy as np
import OpenImageIO as oiio

inputs = {subsets!r}
output = {output!r}
acc = None
first_spec = None
for path in inputs:
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise SystemExit(f"could not open {{path}}")
    spec = inp.spec()
    pixels = inp.read_image(format=oiio.FLOAT)
    inp.close()
    arr = np.asarray(pixels, dtype=np.float32).reshape(
        spec.height,
        spec.width,
        spec.nchannels,
    )
    if acc is None:
        acc = np.zeros_like(arr, dtype=np.float32)
        first_spec = spec
    elif arr.shape != acc.shape:
        raise SystemExit(f"shape mismatch {{path}}: {{arr.shape}} vs {{acc.shape}}")
    acc += arr
avg = acc * np.float32(1.0 / len(inputs))
out_spec = oiio.ImageSpec(first_spec.width, first_spec.height, first_spec.nchannels, oiio.FLOAT)
out_spec.channelnames = first_spec.channelnames
out = oiio.ImageOutput.create(output)
if out is None:
    raise SystemExit(f"could not create {{output}}")
if not out.open(output, out_spec):
    raise SystemExit(out.geterror())
if not out.write_image(avg):
    raise SystemExit(out.geterror())
out.close()
"""
        return (
            "/usr/bin/time -f 'CX_MERGE_PYTHON_TIME_S=%e' "
            "-o /tmp/cx_fanout_time_merge_python.txt "
            "python3 - <<'PY'\n" + script + "PY\n"
            "cat /tmp/cx_fanout_time_merge_python.txt; "
        )

    parts = [
        "set -e -o pipefail; export DEBIAN_FRONTEND=noninteractive; ",
        "apt-get update >/dev/null 2>&1; ",
        "apt-get install -y openimageio-tools python3-openimageio "
        "time python3-numpy python3-pil >/dev/null 2>&1; ",
        "cd " + q_root + "; ",
        "rm -f " + " ".join(
            cycles_fork.shell(p)
            for p in [full, merged, merged_linear, merged_tree, merged_python,
                      full_png, merged_png,
                      *subsets, *tree_temps]
        ) + "; ",
        "rm -f /tmp/cx_fanout_time_*.txt; ",
        f"echo CX_WORKER_COUNT={fanout}; ",
        f"echo CX_CHUNK_COUNT={chunks}; ",
        f"echo CX_CHUNK_SAMPLE_LENGTH={subset_len}; ",
        f"echo CX_SUBSET_EXECUTION_MODE={execution_mode}; ",
        f"echo CX_PARALLEL_SLOTS={parallel_slots or fanout}; ",
        f"echo CX_SUBSET_RETRIES={subset_retries}; ",
        "/usr/bin/time -f 'CX_FULL_TIME_S=%e' -o /tmp/cx_fanout_time_full.txt ",
        q_bin + device_arg(device) + adaptive_arg(disable_adaptive_sampling) +
        f" --samples {samples} --output " +
        cycles_fork.shell(full) + " " + q_scene + " >/tmp/cx_fanout_full.log 2>&1; ",
        "cat /tmp/cx_fanout_time_full.txt; ",
    ]

    if execution_mode == "sequential":
        for index, out in enumerate(subsets):
            offset = index * subset_len
            parts.extend([
                f"/usr/bin/time -f 'CX_SUBSET_TIME_S_{index}=%e' -o /tmp/cx_fanout_time_sub{index}.txt ",
                q_bin + device_arg(device) + adaptive_arg(disable_adaptive_sampling) +
                f" --samples {samples} --sample-subset-offset {offset} "
                f"--sample-subset-length {subset_len} --output " +
                cycles_fork.shell(out) + " " + q_scene +
                f" >/tmp/cx_fanout_sub{index}.log 2>&1; ",
                f"cat /tmp/cx_fanout_time_sub{index}.txt; ",
            ])
    elif execution_mode == "parallel":
        parallel_py = f"""
import os
import subprocess
import sys
import time

bin_path = {cycles_fork.binary_path(root)!r}
scene_path = {("examples/" + scene)!r}
device = {device!r}
disable_adaptive = {disable_adaptive_sampling!r}
samples = {samples}
subset_len = {subset_len}
fanout = {fanout}
parallel_slots = {parallel_slots or fanout}
subset_retries = {subset_retries}
outputs = {subsets!r}

pending = list(range(len(outputs)))
running = {{}}
failures = []
attempts = {{index: 0 for index in range(len(outputs))}}
totals = {{index: 0.0 for index in range(len(outputs))}}
phase_start = time.time()

def command(index):
    args = [bin_path]
    if device:
        args.extend(["--device", device])
    if disable_adaptive:
        args.append("--disable-adaptive-sampling")
    args.extend([
        "--samples", str(samples),
        "--sample-subset-offset", str(index * subset_len),
        "--sample-subset-length", str(subset_len),
        "--output", outputs[index],
        scene_path,
    ])
    return args

def start_one(index):
    attempts[index] += 1
    attempt = attempts[index]
    try:
        if os.path.exists(outputs[index]):
            os.remove(outputs[index])
    except OSError:
        pass
    log_path = f"/tmp/cx_fanout_sub{{index}}_attempt{{attempt}}.log"
    log_file = open(log_path, "w")
    started = time.time()
    proc = subprocess.Popen(
        command(index),
        cwd={root!r},
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    running[proc] = (index, attempt, started, log_file, log_path)

while pending or running:
    while pending and len(running) < parallel_slots:
        start_one(pending.pop(0))
    for proc in list(running):
        rc = proc.poll()
        if rc is None:
            continue
        index, attempt, started, log_file, log_path = running.pop(proc)
        log_file.close()
        elapsed = time.time() - started
        totals[index] += elapsed
        print(f"CX_SUBSET_ATTEMPT_TIME_S_{{index}}_{{attempt}}={{elapsed:.6f}}", flush=True)
        reason = None
        if rc != 0:
            reason = rc
        elif not os.path.exists(outputs[index]) or os.path.getsize(outputs[index]) <= 0:
            reason = "missing-output"
        if reason is not None and attempt <= subset_retries:
            print(
                f"CX_SUBSET_RETRY index={{index}} attempt={{attempt}} reason={{reason}}",
                flush=True,
            )
            pending.append(index)
            continue
        line = f"CX_SUBSET_TIME_S_{{index}}={{totals[index]:.6f}}"
        print(line, flush=True)
        with open(f"/tmp/cx_fanout_time_sub{{index}}.txt", "w") as f:
            f.write(line + "\\n")
        if reason is not None:
            failures.append((index, reason, log_path))
    if pending or running:
        time.sleep(0.05)

phase_wall = time.time() - phase_start
print(f"CX_SUBSET_PHASE_WALL_S={{phase_wall:.6f}}", flush=True)
if failures:
    for index, rc, log_path in failures:
        print(f"CX_SUBSET_FAILURE index={{index}} rc={{rc}} log={{log_path}}", flush=True)
        try:
            with open(log_path, "rb") as f:
                tail = f.read()[-2000:].decode(errors="replace")
            print(tail, flush=True)
        except OSError:
            pass
    sys.exit(1)
"""
        parts.append("python3 - <<'PY'\n" + parallel_py + "PY\n")
    else:
        batch_py = f"""
import os
import subprocess
import sys
import time

bin_path = {cycles_fork.binary_path(root)!r}
scene_path = {("examples/" + scene)!r}
device = {device!r}
disable_adaptive = {disable_adaptive_sampling!r}
samples = {samples}
subset_len = {subset_len}
fanout = {fanout}
parallel_slots = {parallel_slots or fanout}
subset_retries = {subset_retries}
outputs = {subsets!r}

assignments = {{worker: [] for worker in range(fanout)}}
for index in range(len(outputs)):
    assignments[index % fanout].append(index)

manifests = {{}}
for worker, indexes in assignments.items():
    manifest = f"/tmp/cx_fanout_batch_worker{{worker}}.txt"
    manifests[worker] = manifest
    with open(manifest, "w") as f:
        for index in indexes:
            offset = index * subset_len
            f.write(f"{{outputs[index]}} {{samples}} {{offset}} {{subset_len}}\\n")

pending = list(range(fanout))
running = {{}}
failures = []
attempts = {{worker: 0 for worker in range(fanout)}}
totals = {{worker: 0.0 for worker in range(fanout)}}
phase_start = time.time()

def command(worker):
    args = [bin_path]
    if device:
        args.extend(["--device", device])
    if disable_adaptive:
        args.append("--disable-adaptive-sampling")
    args.extend(["--cx-batch-manifest", manifests[worker], scene_path])
    return args

def outputs_ready(worker):
    for index in assignments[worker]:
        path = outputs[index]
        if not os.path.exists(path) or os.path.getsize(path) <= 0:
            return False
    return True

def start_one(worker):
    attempts[worker] += 1
    attempt = attempts[worker]
    for index in assignments[worker]:
        try:
            if os.path.exists(outputs[index]):
                os.remove(outputs[index])
        except OSError:
            pass
    log_path = f"/tmp/cx_fanout_batch_worker{{worker}}_attempt{{attempt}}.log"
    log_file = open(log_path, "w")
    started = time.time()
    proc = subprocess.Popen(
        command(worker),
        cwd={root!r},
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    running[proc] = (worker, attempt, started, log_file, log_path)

while pending or running:
    while pending and len(running) < parallel_slots:
        start_one(pending.pop(0))
    for proc in list(running):
        rc = proc.poll()
        if rc is None:
            continue
        worker, attempt, started, log_file, log_path = running.pop(proc)
        log_file.close()
        elapsed = time.time() - started
        totals[worker] += elapsed
        print(f"CX_BATCH_WORKER_ATTEMPT_TIME_S_{{worker}}_{{attempt}}={{elapsed:.6f}}", flush=True)
        reason = None
        if rc != 0:
            reason = rc
        elif not outputs_ready(worker):
            reason = "missing-output"
        if reason is not None and attempt <= subset_retries:
            print(
                f"CX_BATCH_WORKER_RETRY index={{worker}} attempt={{attempt}} reason={{reason}}",
                flush=True,
            )
            pending.append(worker)
            continue
        line = f"CX_SUBSET_TIME_S_{{worker}}={{totals[worker]:.6f}}"
        print(f"CX_BATCH_WORKER_TIME_S_{{worker}}={{totals[worker]:.6f}}", flush=True)
        print(line, flush=True)
        with open(f"/tmp/cx_fanout_time_sub{{worker}}.txt", "w") as f:
            f.write(line + "\\n")
        if reason is not None:
            failures.append((worker, reason, log_path))
    if pending or running:
        time.sleep(0.05)

phase_wall = time.time() - phase_start
print(f"CX_SUBSET_PHASE_WALL_S={{phase_wall:.6f}}", flush=True)
print(f"CX_BATCH_WORKER_COUNT={{fanout}}", flush=True)
print(f"CX_BATCH_MANIFEST_OK workers={{fanout}} chunks={{len(outputs)}}", flush=True)
if failures:
    for worker, rc, log_path in failures:
        print(f"CX_BATCH_WORKER_FAILURE index={{worker}} rc={{rc}} log={{log_path}}", flush=True)
        try:
            with open(log_path, "rb") as f:
                tail = f.read()[-3000:].decode(errors="replace")
            print(tail, flush=True)
        except OSError:
            pass
    sys.exit(1)
"""
        parts.append("python3 - <<'PY'\n" + batch_py + "PY\n")

    if merge_mode == "linear":
        parts.extend([
            linear_merge_cmd(merged, "LINEAR"),
            "awk -F= '/CX_MERGE_LINEAR_TIME_S/ {print \"CX_MERGE_TIME_S=\" $2}' "
            "/tmp/cx_fanout_time_merge_linear.txt; ",
            "echo CX_MERGE_SELECTED=linear; ",
        ])
    elif merge_mode == "tree":
        parts.extend([
            tree_merge_cmd(merged),
            "awk -F= '/CX_MERGE_TREE_TIME_S/ {print \"CX_MERGE_TIME_S=\" $2}' "
            "/tmp/cx_fanout_time_merge_tree.txt; ",
            "echo CX_MERGE_SELECTED=tree; ",
        ])
    elif merge_mode == "python":
        parts.extend([
            python_merge_cmd(merged),
            "awk -F= '/CX_MERGE_PYTHON_TIME_S/ {print \"CX_MERGE_TIME_S=\" $2}' "
            "/tmp/cx_fanout_time_merge_python.txt; ",
            "echo CX_MERGE_SELECTED=python; ",
        ])
    else:
        selection_py = f"""
import shutil

def read_time(path, key):
    with open(path) as f:
        for line in f:
            if line.startswith(key + "="):
                return float(line.split("=", 1)[1])
    raise SystemExit(f"missing {{key}} in {{path}}")

linear = read_time("/tmp/cx_fanout_time_merge_linear.txt", "CX_MERGE_LINEAR_TIME_S")
tree = read_time("/tmp/cx_fanout_time_merge_tree.txt", "CX_MERGE_TREE_TIME_S")
python = read_time("/tmp/cx_fanout_time_merge_python.txt", "CX_MERGE_PYTHON_TIME_S")
selected, value, src = min(
    [
        ("linear", linear, {merged_linear!r}),
        ("tree", tree, {merged_tree!r}),
        ("python", python, {merged_python!r}),
    ],
    key=lambda item: item[1],
)
shutil.copyfile(src, {merged!r})
print(f"CX_MERGE_SELECTED={{selected}}")
print(f"CX_MERGE_TIME_S={{value:.6f}}")
"""
        parts.extend([
            linear_merge_cmd(merged_linear, "LINEAR"),
            tree_merge_cmd(merged_tree),
            python_merge_cmd(merged_python),
            "python3 - <<'PY'\n" + selection_py + "PY\n",
        ])

    if quality_gate:
        quality_py = f"""
from PIL import Image
import numpy as np

def load(path):
    arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64) / 255.0
    return 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]

def ssim(a, b):
    c1 = 0.01 ** 2
    c2 = 0.03 ** 2
    mux = float(a.mean())
    muy = float(b.mean())
    vx = float(((a - mux) ** 2).mean())
    vy = float(((b - muy) ** 2).mean())
    cov = float(((a - mux) * (b - muy)).mean())
    denom = (mux * mux + muy * muy + c1) * (vx + vy + c2)
    if denom == 0:
        return 1.0 if np.array_equal(a, b) else 0.0
    return ((2 * mux * muy + c1) * (2 * cov + c2)) / denom

full = load({full_png!r})
merged = load({merged_png!r})
h, w = full.shape
tile = max(16, min(h, w) // 8)
tiles = []
for y in range(0, h, tile):
    for x in range(0, w, tile):
        aa = full[y:min(y + tile, h), x:min(x + tile, w)]
        bb = merged[y:min(y + tile, h), x:min(x + tile, w)]
        if aa.size:
            tiles.append(ssim(aa, bb))
delta = np.abs(full - merged)
print(f"CX_QUALITY_SSIM={{ssim(full, merged):.9f}}")
print(f"CX_QUALITY_WORST_TILE_SSIM={{min(tiles) if tiles else 1.0:.9f}}")
print(f"CX_QUALITY_TILE_COUNT={{len(tiles)}}")
print(f"CX_QUALITY_PNG_MAE={{float(delta.mean()):.9f}}")
print(f"CX_QUALITY_PNG_MAXE={{float(delta.max()):.9f}}")
"""
        parts.extend([
            "oiiotool " + cycles_fork.shell(full) + " -d uint8 -o " +
            cycles_fork.shell(full_png) + " >/tmp/cx_fanout_png_full.log 2>&1; ",
            "oiiotool " + cycles_fork.shell(merged) + " -d uint8 -o " +
            cycles_fork.shell(merged_png) + " >/tmp/cx_fanout_png_merged.log 2>&1; ",
            "python3 - <<'PY'\n" + quality_py + "PY\n",
        ])

    parts.extend([
        "cat /tmp/cx_fanout_time_full.txt; "
        "for f in /tmp/cx_fanout_time_sub*.txt; do [ -e \"$f\" ] && cat \"$f\"; done; ",
        "set +e; DIFF=$(oiiotool " + cycles_fork.shell(full) + " " +
        cycles_fork.shell(merged) + " --diff 2>&1); DIFF_RC=$?; set -e; ",
        "echo \"$DIFF\"; ",
        "echo CX_FANOUT_DIFF_RC=$DIFF_RC; ",
        "ls -lh " + " ".join(cycles_fork.shell(p) for p in [full, merged]) + "; ",
        "du -ch " + " ".join(cycles_fork.shell(p) for p in subsets) +
        " 2>/dev/null | tail -1 | sed 's/^/CX_SUBSET_TOTAL_BYTES /'; ",
        f"echo CX_FANOUT_PROBE_OK scene={scene} samples={samples} fanout={fanout} "
        f"chunks={chunks} subset_len={subset_len} merge_mode={merge_mode} "
        f"execution_mode={execution_mode} device={device or 'default'}",
    ])
    return "".join(parts)


def parse_probe(stage: dict, scene: str, samples: int, fanout: int,
                device: str = "", chunk_count: int = 0) -> dict:
    text = stage.get("out_tail", "")
    full_match = re.search(r"CX_FULL_TIME_S=([0-9.]+)", text)
    execution_match = re.search(r"CX_SUBSET_EXECUTION_MODE=([A-Za-z0-9_.-]+)", text)
    parallel_slots_match = re.search(r"CX_PARALLEL_SLOTS=(\d+)", text)
    subset_retries_match = re.search(r"CX_SUBSET_RETRIES=(\d+)", text)
    subset_phase_match = re.search(r"CX_SUBSET_PHASE_WALL_S=([0-9.]+)", text)
    merge_match = re.search(r"CX_MERGE_TIME_S=([0-9.]+)", text)
    merge_selected_match = re.search(r"CX_MERGE_SELECTED=([A-Za-z0-9_.-]+)", text)
    merge_linear_match = re.search(r"CX_MERGE_LINEAR_TIME_S=([0-9.]+)", text)
    merge_tree_match = re.search(r"CX_MERGE_TREE_TIME_S=([0-9.]+)", text)
    merge_python_match = re.search(r"CX_MERGE_PYTHON_TIME_S=([0-9.]+)", text)
    chunk_count_match = re.search(r"CX_CHUNK_COUNT=(\d+)", text)
    chunk_len_match = re.search(r"CX_CHUNK_SAMPLE_LENGTH=(\d+)", text)
    diff_match = re.search(r"CX_FANOUT_DIFF_RC=(\d+)", text)
    mean_match = re.search(r"Mean error = ([0-9.eE+-]+)", text)
    rms_match = re.search(r"RMS error = ([0-9.eE+-]+)", text)
    max_match = re.search(r"Max error\s+=\s+([0-9.eE+-]+)", text)
    ssim_match = re.search(r"CX_QUALITY_SSIM=([0-9.eE+-]+)", text)
    worst_tile_match = re.search(r"CX_QUALITY_WORST_TILE_SSIM=([0-9.eE+-]+)", text)
    tile_count_match = re.search(r"CX_QUALITY_TILE_COUNT=(\d+)", text)
    png_mae_match = re.search(r"CX_QUALITY_PNG_MAE=([0-9.eE+-]+)", text)
    png_maxe_match = re.search(r"CX_QUALITY_PNG_MAXE=([0-9.eE+-]+)", text)
    subset_by_index = {
        int(match.group(1)): float(match.group(2))
        for match in re.finditer(r"CX_SUBSET_TIME_S_(\d+)=([0-9.]+)", text)
    }
    subset_times = [subset_by_index[index] for index in sorted(subset_by_index)]
    full_time = float(full_match.group(1)) if full_match else None
    merge_time = float(merge_match.group(1)) if merge_match else None
    actual_chunk_count = (
        int(chunk_count_match.group(1)) if chunk_count_match else
        chunk_count if chunk_count else
        len(subset_times)
    )
    chunk_sample_length = int(chunk_len_match.group(1)) if chunk_len_match else (
        samples // actual_chunk_count if actual_chunk_count else None
    )
    diff_rc = int(diff_match.group(1)) if diff_match else None
    exact = diff_rc == 0
    mean_error = float(mean_match.group(1)) if mean_match else 0.0 if exact else None
    rms_error = float(rms_match.group(1)) if rms_match else 0.0 if exact else None
    max_error = float(max_match.group(1)) if max_match else 0.0 if exact else None
    numeric_equivalent = bool(
        exact or (
            mean_error is not None and
            rms_error is not None and
            max_error is not None and
            mean_error <= NUMERIC_EQ_MEAN_TOL and
            rms_error <= NUMERIC_EQ_RMS_TOL and
            max_error <= NUMERIC_EQ_MAX_TOL
        )
    )
    diff_class = "exact" if exact else "numeric" if numeric_equivalent else "drift"
    static_chunk_wall = None
    dynamic_chunk_wall = None
    lpt_chunk_wall = None
    ideal_wall = None
    ideal_speedup = None
    actual_parallel_wall = None
    actual_speedup = None
    static_speedup = None
    scheduler_gain = None
    lpt_gain = None
    execution_mode = execution_match.group(1) if execution_match else "sequential"
    subset_phase_wall = float(subset_phase_match.group(1)) if subset_phase_match else None
    if subset_times and merge_time is not None:
        static_loads = _chunk_loads_static(subset_times, fanout)
        dynamic_loads = _chunk_loads_dynamic(subset_times, fanout)
        lpt_loads = _chunk_loads_lpt(subset_times, fanout)
        static_chunk_wall = max(static_loads)
        dynamic_chunk_wall = max(dynamic_loads)
        lpt_chunk_wall = max(lpt_loads)
        ideal_wall = dynamic_chunk_wall + merge_time
        if full_time and ideal_wall > 0:
            ideal_speedup = full_time / ideal_wall
        if full_time and static_chunk_wall + merge_time > 0:
            static_speedup = full_time / (static_chunk_wall + merge_time)
        if dynamic_chunk_wall + merge_time > 0:
            scheduler_gain = (static_chunk_wall + merge_time) / (dynamic_chunk_wall + merge_time)
        if lpt_chunk_wall + merge_time > 0:
            lpt_gain = (static_chunk_wall + merge_time) / (lpt_chunk_wall + merge_time)
        if execution_mode in ("parallel", "batch") and subset_phase_wall is not None:
            actual_parallel_wall = subset_phase_wall + merge_time
            if full_time and actual_parallel_wall > 0:
                actual_speedup = full_time / actual_parallel_wall
    return {
        "scene": scene,
        "samples": samples,
        "fanout": fanout,
        "chunk_count": actual_chunk_count,
        "chunk_sample_length": chunk_sample_length,
        "execution_mode": execution_mode,
        "parallel_slots": int(parallel_slots_match.group(1)) if parallel_slots_match else fanout,
        "subset_retries": int(subset_retries_match.group(1)) if subset_retries_match else 0,
        "device": device or "default",
        "ok": stage["ok"] and "CX_FANOUT_PROBE_OK" in text,
        "exact": exact,
        "numeric_equivalent": numeric_equivalent,
        "diff_class": diff_class,
        "diff_rc": diff_rc,
        "mean_error": mean_error,
        "rms_error": rms_error,
        "max_error": max_error,
        "ssim": float(ssim_match.group(1)) if ssim_match else None,
        "worst_tile_ssim": float(worst_tile_match.group(1)) if worst_tile_match else None,
        "quality_tile_count": int(tile_count_match.group(1)) if tile_count_match else None,
        "png_mae": float(png_mae_match.group(1)) if png_mae_match else None,
        "png_maxe": float(png_maxe_match.group(1)) if png_maxe_match else None,
        "elapsed_s": stage["elapsed_s"],
        "full_time_s": full_time,
        "subset_times_s": subset_times,
        "subset_phase_wall_s": (
            round(subset_phase_wall, 4) if subset_phase_wall is not None else None
        ),
        "merge_time_s": merge_time,
        "merge_selected": merge_selected_match.group(1) if merge_selected_match else None,
        "merge_linear_time_s": float(merge_linear_match.group(1)) if merge_linear_match else None,
        "merge_tree_time_s": float(merge_tree_match.group(1)) if merge_tree_match else None,
        "merge_python_time_s": (
            float(merge_python_match.group(1)) if merge_python_match else None
        ),
        "static_chunk_wall_s": round(static_chunk_wall, 4) if static_chunk_wall is not None else None,
        "dynamic_chunk_wall_s": round(dynamic_chunk_wall, 4) if dynamic_chunk_wall is not None else None,
        "lpt_chunk_wall_s": round(lpt_chunk_wall, 4) if lpt_chunk_wall is not None else None,
        "static_speedup_vs_full": round(static_speedup, 4) if static_speedup is not None else None,
        "ideal_parallel_wall_s": round(ideal_wall, 4) if ideal_wall is not None else None,
        "ideal_speedup_vs_full": round(ideal_speedup, 4) if ideal_speedup is not None else None,
        "actual_parallel_wall_s": (
            round(actual_parallel_wall, 4) if actual_parallel_wall is not None else None
        ),
        "actual_speedup_vs_full": round(actual_speedup, 4) if actual_speedup is not None else None,
        "chunk_scheduler_gain_vs_static": round(scheduler_gain, 4) if scheduler_gain is not None else None,
        "lpt_scheduler_gain_vs_static": round(lpt_gain, 4) if lpt_gain is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default=cycles_fork.DEFAULT_REF)
    parser.add_argument("--remote-root", default=cycles_fork.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--cmake-args", default="",
                        help="extra CMake args forwarded through BUILD_CMAKE_ARGS")
    parser.add_argument("--scene", default="scene_monkey.xml",
                        help="comma-separated example XML scene names")
    parser.add_argument("--samples", default="64")
    parser.add_argument("--fanouts", default="2,4,8")
    parser.add_argument("--device", default="CUDA")
    parser.add_argument("--gpu-tier", choices=("default", "ada", "hopper"), default="default",
                        help=(
                            "default uses the gpu-provisioning-policy ladder "
                            "(A100 -> H100 -> H200, COMMUNITY then SECURE); ada tries "
                            "reachable Ada-family cards for sm_89 prebuilt runtimes "
                            "(arch-pinned exception); hopper tries H100/H200"
                        ))
    parser.add_argument("--skip-build", action="store_true",
                        help="assume --remote-root already contains a built install/cycles")
    parser.add_argument("--prebuilt-root-tar", default="",
                        help=(
                            "upload a local runtime-root tar containing install/ and examples/, "
                            "extract it to --remote-root, then use the skip-build path"
                        ))
    parser.add_argument("--export-runtime-tar", default="",
                        help=(
                            "after a normal build, download a reusable runtime-root tar "
                            "containing install/ and examples/"
                        ))
    parser.add_argument("--validate-skip-build-pass", action="store_true",
                        help="after a normal build, rerun runtime smokes and probes via the skip-build path")
    parser.add_argument("--fail-on-diff", action="store_true",
                        help="treat a nonzero EXR diff as fatal instead of ledgering it")
    parser.add_argument("--continue-on-probe-failure", action="store_true",
                        help="ledger failed probes and continue remaining probes/skip-build validation")
    parser.add_argument("--disable-adaptive-sampling", action="store_true",
                        help="disable Cycles adaptive sampling for fixed sample-subset math")
    parser.add_argument("--chunks-per-worker", type=int, default=1,
                        help="render this many equal sample chunks per modeled worker")
    parser.add_argument("--chunk-count", type=int, default=0,
                        help="explicit equal sample chunk count; overrides --chunks-per-worker")
    parser.add_argument("--merge-mode", choices=("linear", "tree", "python", "auto"),
                        default="linear",
                        help=(
                            "merge chunk EXRs with linear oiiotool, tree oiiotool, "
                            "Python/OpenImageIO, or benchmark all"
                        ))
    parser.add_argument("--execution-mode", choices=("sequential", "parallel", "batch"),
                        default="sequential",
                        help=(
                            "render chunks sequentially for modeling, concurrently as one "
                            "process per chunk, or as resident batch manifests"
                        ))
    parser.add_argument("--parallel-slots", type=int, default=0,
                        help="max concurrent subset processes in parallel mode; defaults to fanout")
    parser.add_argument("--subset-retries", type=int, default=0,
                        help="retry failed/missing-output subset renders this many times before merge")
    parser.add_argument("--no-quality-gate", action="store_true",
                        help="skip PNG SSIM/worst-tile quality metrics")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.validate_skip_build_pass and (args.skip_build or args.prebuilt_root_tar):
        raise SystemExit(
            "--validate-skip-build-pass requires a normal build pass, "
            "not --skip-build/--prebuilt-root-tar"
        )
    if args.prebuilt_root_tar and not os.path.isfile(args.prebuilt_root_tar):
        raise SystemExit(f"--prebuilt-root-tar not found: {args.prebuilt_root_tar}")
    if args.export_runtime_tar and (args.skip_build or args.prebuilt_root_tar):
        raise SystemExit("--export-runtime-tar requires a normal build pass")
    if args.parallel_slots < 0:
        raise SystemExit("--parallel-slots must be non-negative")
    if args.subset_retries < 0:
        raise SystemExit("--subset-retries must be non-negative")

    sample_counts = parse_int_csv(args.samples)
    fanouts = parse_int_csv(args.fanouts)
    scenes = parse_csv(args.scene)
    if not scenes:
        raise SystemExit("--scene must include at least one scene")
    for sample_count in sample_counts:
        for fanout in fanouts:
            chunks = resolve_chunk_count(fanout, args.chunks_per_worker, args.chunk_count)
            if sample_count % chunks != 0:
                raise SystemExit(
                    f"samples {sample_count} must be divisible by chunk count {chunks}"
                )

    use_runtime_root = args.skip_build or bool(args.prebuilt_root_tar)
    build_stages = (
        cycles_fork.runtime_smoke_stages(root=args.remote_root)
        if use_runtime_root else
        cycles_fork.build_stages(
            root=args.remote_root,
            ref=args.ref,
            jobs=args.jobs,
            cmake_args=args.cmake_args,
        )
    )
    probes = [
        {
            "name": f"fanout_{safe_name(scene)}_{sample_count}_{fanout}",
            "cmd": fanout_probe_cmd(
                args.remote_root,
                scene,
                sample_count,
                fanout,
                args.device,
                args.disable_adaptive_sampling,
                args.chunks_per_worker,
                args.chunk_count,
                args.merge_mode,
                not args.no_quality_gate,
                args.execution_mode,
                args.parallel_slots,
                args.subset_retries,
            ),
            "timeout_s": 2400,
            "scene": scene,
            "samples": sample_count,
            "fanout": fanout,
            "chunk_count": resolve_chunk_count(
                fanout,
                args.chunks_per_worker,
                args.chunk_count,
            ),
            "device": args.device or "default",
        }
        for scene in scenes
        for sample_count in sample_counts
        for fanout in fanouts
    ]
    manifest = {
        "remote": cycles_fork.CYCLES_REMOTE,
        "ref": args.ref,
        "root": args.remote_root,
        "jobs": args.jobs,
        "cmake_args": args.cmake_args,
        "skip_build": args.skip_build,
        "prebuilt_root_tar": args.prebuilt_root_tar,
        "export_runtime_tar": args.export_runtime_tar,
        "validate_skip_build_pass": args.validate_skip_build_pass,
        "fail_on_diff": args.fail_on_diff,
        "continue_on_probe_failure": args.continue_on_probe_failure,
        "disable_adaptive_sampling": args.disable_adaptive_sampling,
        "chunks_per_worker": args.chunks_per_worker,
        "chunk_count": args.chunk_count,
        "merge_mode": args.merge_mode,
        "execution_mode": args.execution_mode,
        "parallel_slots": args.parallel_slots,
        "subset_retries": args.subset_retries,
        "quality_gate": not args.no_quality_gate,
        "patches": [os.path.basename(p) for p in cycles_fork.patch_files()],
        "scene": args.scene,
        "scenes": scenes,
        "samples": sample_counts,
        "fanouts": fanouts,
        "device": args.device or "default",
        "gpu_tier": args.gpu_tier,
        "build_stages": [stage.to_json() for stage in build_stages],
        "skip_build_validation_stages": [
            stage.to_json()
            for stage in (
                cycles_fork.runtime_smoke_stages(root=args.remote_root)
                if args.validate_skip_build_pass else []
            )
        ],
        "probes": probes,
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")
    pod = None
    records = []
    probe_results = []
    skip_build_records = []
    skip_build_probe_results = []
    result = None
    try:
        gpu_plan = (
            HOPPER_GPU_PLAN if args.gpu_tier == "hopper" else
            ADA_GPU_PLAN if args.gpu_tier == "ada" else
            GPU_PLAN
        )
        pod = runpod.provision_reachable(
            gpu_plan,
            POD_IMAGE,
            disk_gb=POD_DISK_GB,
            require_cuda=True,
            name="cx-cycles-fanout",
        )
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        append_ledger({"event": "pod_up", "pod": pod, "manifest": manifest})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        if args.prebuilt_root_tar:
            for rec in upload_prebuilt_root(pod, args.prebuilt_root_tar, args.remote_root):
                records.append(rec)
                if not rec["ok"]:
                    raise RuntimeError(f"prebuilt-root stage {rec['stage']} failed")

        for stage in build_stages:
            rec = run_stage(pod, stage.name, stage.cmd, stage.timeout_s)
            records.append(rec)
            if not rec["ok"]:
                raise RuntimeError(f"build/runtime stage {stage.name} failed")

        if args.export_runtime_tar:
            for rec in export_runtime_root(pod, args.remote_root, args.export_runtime_tar):
                records.append(rec)
                if not rec["ok"]:
                    raise RuntimeError(f"runtime-tar stage {rec['stage']} failed")

        for probe in probes:
            rec = run_stage(pod, probe["name"], probe["cmd"], probe["timeout_s"])
            records.append(rec)
            parsed = parse_probe(
                rec,
                probe["scene"],
                probe["samples"],
                probe["fanout"],
                args.device,
                probe["chunk_count"],
            )
            probe_results.append(parsed)
            if not parsed["ok"] and not args.continue_on_probe_failure:
                raise RuntimeError(f"fanout probe {probe['name']} failed")
            if args.fail_on_diff and not parsed["exact"]:
                raise RuntimeError(f"fanout probe {probe['name']} diff failed")

        if args.validate_skip_build_pass:
            for stage in cycles_fork.runtime_smoke_stages(root=args.remote_root):
                rec = run_stage(
                    pod,
                    f"skipbuild_{stage.name}",
                    stage.cmd,
                    stage.timeout_s,
                )
                skip_build_records.append(rec)
                if not rec["ok"]:
                    raise RuntimeError(f"skip-build validation stage {stage.name} failed")

            for probe in probes:
                rec = run_stage(
                    pod,
                    f"skipbuild_{probe['name']}",
                    probe["cmd"],
                    probe["timeout_s"],
                )
                skip_build_records.append(rec)
                parsed = parse_probe(
                    rec,
                    probe["scene"],
                    probe["samples"],
                    probe["fanout"],
                    args.device,
                    probe["chunk_count"],
                )
                skip_build_probe_results.append(parsed)
                if not parsed["ok"] and not args.continue_on_probe_failure:
                    raise RuntimeError(f"skip-build fanout probe {probe['name']} failed")
                if args.fail_on_diff and not parsed["exact"]:
                    raise RuntimeError(f"skip-build fanout probe {probe['name']} diff failed")

        result = {
            "ok": all(p["ok"] for p in probe_results),
            "all_exact": all(p["exact"] for p in probe_results),
            "all_numeric_equivalent": all(p["numeric_equivalent"] for p in probe_results),
            "skip_build_validated": (
                bool(skip_build_probe_results) and
                all(p["ok"] for p in skip_build_probe_results)
            ),
            "prebuilt_root_uploaded": bool(args.prebuilt_root_tar),
            "runtime_tar_exported": args.export_runtime_tar or None,
            "pod_gpu": pod["gpu"],
            "pod_cloud": pod["cloud"],
            "ref": args.ref,
            "probes": probe_results,
            "skip_build_probes": skip_build_probe_results,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
            "skip_build_stages": [
                {k: r[k] for k in ("stage", "ok", "elapsed_s")}
                for r in skip_build_records
            ],
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "probes": probe_results,
            "skip_build_probes": skip_build_probe_results,
            "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")} for r in records],
            "skip_build_stages": [
                {k: r[k] for k in ("stage", "ok", "elapsed_s")}
                for r in skip_build_records
            ],
        }
        log(f"ERROR {result['error']}")
    finally:
        if pod:
            log("tearing down")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error: {exc}")
        append_ledger({"event": "result", "result": result})
        try:
            b2 = runpod.balance()["clientBalance"]
        except Exception as exc:  # noqa: BLE001
            append_ledger({
                "event": "pod_down",
                "balance_after": None,
                "balance_error": f"{type(exc).__name__}: {exc}",
            })
            log(f"pod down; balance check failed after retries: {type(exc).__name__}: {exc}")
        else:
            append_ledger({"event": "pod_down", "balance_after": b2})
            log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
