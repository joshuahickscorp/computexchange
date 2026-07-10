#!/usr/bin/env python3
"""Shared scaffold for the CX Cycles fork/build workflow.

This module is intentionally boring. It treats standalone Cycles as another CX
rendering component: official source in, reproducible build stages out, ledgered
driver on top. It does not persist credentials and it does not clone anything on
import, which keeps the test path cheap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import base64
import json
import os
import shlex
import time


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

CYCLES_REMOTE = "https://projects.blender.org/blender/cycles.git"
DEFAULT_REF = "main"
DEFAULT_LOCAL_ROOT = os.path.join(REPO, ".artifacts", "cycles", "fork")
DEFAULT_REMOTE_ROOT = "/root/cx-cycles"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/cycles_fork_ledger.jsonl")
PATCH_DIR = os.path.join(REPO, "patches", "cycles")


@dataclass(frozen=True)
class Stage:
    name: str
    cmd: str
    timeout_s: int

    def to_json(self) -> dict:
        return asdict(self)


def shell(value: str) -> str:
    return shlex.quote(value)


def binary_path(root: str) -> str:
    return os.path.join(root, "install", "cycles")


def patch_files(patch_dir: str = PATCH_DIR) -> list[str]:
    if not os.path.isdir(patch_dir):
        return []
    return [
        os.path.join(patch_dir, name)
        for name in sorted(os.listdir(patch_dir))
        if name.endswith(".patch")
    ]


def apply_patches_stage(root: str = DEFAULT_REMOTE_ROOT,
                        patch_dir: str = PATCH_DIR) -> Stage | None:
    patches = patch_files(patch_dir)
    if not patches:
        return None

    q_root = shell(root)
    parts = ["set -e -o pipefail; cd " + q_root + "; "]
    for index, path in enumerate(patches):
        name = os.path.basename(path)
        with open(path, "rb") as f:
            payload = base64.b64encode(f.read()).decode()
        remote_patch = f"/tmp/cx_cycles_patch_{index}.patch"
        parts.extend([
            "printf %s " + shell(payload) + " | base64 -d > " + shell(remote_patch) + "; ",
            "git apply --check " + shell(remote_patch) + "; ",
            "git apply " + shell(remote_patch) + "; ",
            "echo CX_CYCLES_PATCH_APPLIED=" + shell(name) + "; ",
        ])
    parts.append("echo CX_CYCLES_PATCH_COUNT=" + str(len(patches)))
    return Stage("apply_patches", "".join(parts), 300)


def binary_smoke_stage(root: str = DEFAULT_REMOTE_ROOT) -> Stage:
    q_root = shell(root)
    q_bin = shell(binary_path(root))
    cmd = (
        "set -e; cd " + q_root + "; "
        "test -x " + q_bin + "; "
        "echo CX_CYCLES_BIN=" + q_bin + "; "
        "file " + q_bin + "; "
        "(" + q_bin + " --help 2>&1 || true) | head -80; "
        "(" + q_bin + " --version 2>&1 || true) | head -20; "
        "ldd " + q_bin + " | head -60; "
        "echo CX_CYCLES_BINARY_SMOKE_OK=1"
    )
    return Stage("binary_smoke", cmd, 900)


def patch_cli_smoke_stage(root: str = DEFAULT_REMOTE_ROOT) -> Stage:
    q_root = shell(root)
    q_bin = shell(binary_path(root))
    cmd = (
        "set -e -o pipefail; cd " + q_root + "; " +
        q_bin + " --help 2>&1 | grep -E -- "
        + shell(
            "--sample-subset-offset|--sample-subset-length|--disable-adaptive-sampling|"
            "--cx-batch-manifest|--cx-crop"
        ) + "; "
        "rm -f /tmp/cx_cycles_subset_monkey.png; " +
        q_bin + " --samples 8 --sample-subset-offset 0 --sample-subset-length 8 "
        "--disable-adaptive-sampling "
        "--output /tmp/cx_cycles_subset_monkey.png examples/scene_monkey.xml "
        "2>&1 | tail -80; "
        "test -s /tmp/cx_cycles_subset_monkey.png; "
        "file /tmp/cx_cycles_subset_monkey.png; "
        "ls -lh /tmp/cx_cycles_subset_monkey.png; "
        "rm -f /tmp/cx_cycles_batch_0.png /tmp/cx_cycles_batch_1.png "
        "/tmp/cx_cycles_batch_crop.png; "
        "printf '/tmp/cx_cycles_batch_0.png 8 0 4\\n"
        "/tmp/cx_cycles_batch_1.png 8 4 4\\n"
        "/tmp/cx_cycles_batch_crop.png 8 0 8 0 0 64 64 1024 512\\n' "
        "> /tmp/cx_cycles_batch_manifest.txt; "
        + q_bin + " --disable-adaptive-sampling "
        "--cx-batch-manifest /tmp/cx_cycles_batch_manifest.txt "
        "examples/scene_monkey.xml 2>&1 | tail -120; "
        "test -s /tmp/cx_cycles_batch_0.png; "
        "test -s /tmp/cx_cycles_batch_1.png; "
        "test -s /tmp/cx_cycles_batch_crop.png; "
        "file /tmp/cx_cycles_batch_0.png /tmp/cx_cycles_batch_1.png "
        "/tmp/cx_cycles_batch_crop.png; "
        "ls -lh /tmp/cx_cycles_batch_0.png /tmp/cx_cycles_batch_1.png "
        "/tmp/cx_cycles_batch_crop.png; "
        "rm -f /tmp/cx_cycles_crop.png; " +
        q_bin + " --samples 8 --disable-adaptive-sampling "
        "--cx-crop 0,0,64,64,1024,512 "
        "--output /tmp/cx_cycles_crop.png examples/scene_monkey.xml "
        "2>&1 | tail -80; "
        "test -s /tmp/cx_cycles_crop.png; "
        "file /tmp/cx_cycles_crop.png; "
        "python3 - <<'PY'\n"
        "import struct\n"
        "path = '/tmp/cx_cycles_crop.png'\n"
        "with open(path, 'rb') as f:\n"
        "    assert f.read(8) == b'\\x89PNG\\r\\n\\x1a\\n'\n"
        "    _length = struct.unpack('>I', f.read(4))[0]\n"
        "    assert f.read(4) == b'IHDR'\n"
        "    width, height = struct.unpack('>II', f.read(8))\n"
        "assert (width, height) == (64, 64), (width, height)\n"
        "print('CX_CYCLES_CROP_SMOKE_OK=1')\n"
        "PY\n"
        "python3 - <<'PY'\n"
        "import struct\n"
        "path = '/tmp/cx_cycles_batch_crop.png'\n"
        "with open(path, 'rb') as f:\n"
        "    assert f.read(8) == b'\\x89PNG\\r\\n\\x1a\\n'\n"
        "    _length = struct.unpack('>I', f.read(4))[0]\n"
        "    assert f.read(4) == b'IHDR'\n"
        "    width, height = struct.unpack('>II', f.read(8))\n"
        "assert (width, height) == (64, 64), (width, height)\n"
        "print('CX_CYCLES_BATCH_CROP_SMOKE_OK=1')\n"
        "PY\n"
        "echo CX_CYCLES_PATCH_CLI_SMOKE_OK=1"
    )
    return Stage("patch_cli_smoke", cmd, 1200)


def render_smoke_stage(root: str = DEFAULT_REMOTE_ROOT) -> Stage:
    q_root = shell(root)
    q_bin = shell(binary_path(root))
    cmd = (
        "set -e -o pipefail; cd " + q_root + "; "
        "rm -f /tmp/cx_cycles_monkey.png; "
        + q_bin + " --samples 8 --output /tmp/cx_cycles_monkey.png "
        "examples/scene_monkey.xml 2>&1 | tail -80; "
        "test -s /tmp/cx_cycles_monkey.png; "
        "file /tmp/cx_cycles_monkey.png; "
        "ls -lh /tmp/cx_cycles_monkey.png; "
        "echo CX_CYCLES_RENDER_SMOKE_OK=1"
    )
    return Stage("render_smoke", cmd, 1200)


def runtime_smoke_stages(root: str = DEFAULT_REMOTE_ROOT,
                         require_patches: bool = True,
                         render_smoke: bool = True) -> list[Stage]:
    stages = [binary_smoke_stage(root)]
    if require_patches:
        stages.append(patch_cli_smoke_stage(root))
    if render_smoke:
        stages.append(render_smoke_stage(root))
    return stages


def build_stages(root: str = DEFAULT_REMOTE_ROOT, ref: str = DEFAULT_REF,
                 jobs: int = 0, apply_patches: bool = True,
                 cmake_args: str = "") -> list[Stage]:
    """Return official standalone-Cycles build stages.

    The commands follow Cycles' own BUILDING.md: clone, `make update`, `make`,
    then smoke the resulting `./install/cycles` binary. The stages are split so
    cloud drivers can ledger and timeout each step independently.
    """

    q_root = shell(root)
    q_parent = shell(os.path.dirname(root.rstrip("/")) or "/")
    q_remote = shell(CYCLES_REMOTE)
    q_ref = shell(ref)
    cmake_prefix = f"BUILD_CMAKE_ARGS={shell(cmake_args)} " if cmake_args else ""
    job_prefix = f"PARALLEL_JOBS={int(jobs)} " if jobs and jobs > 0 else ""
    q_bin = shell(binary_path(root))

    deps = (
        "set -e; export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update >/dev/null 2>&1; "
        "apt-get install -y "
        "build-essential git git-lfs subversion cmake ninja-build pkg-config "
        "python3 python3-dev make "
        "libx11-dev libxxf86vm-dev libxcursor-dev libxi-dev libxrandr-dev "
        "libxinerama-dev libegl-dev libwayland-dev wayland-protocols "
        "libxkbcommon-dev libdbus-1-dev libepoxy-dev "
        "libgl-dev libglu1-mesa-dev libglew-dev libsdl2-dev "
        "curl ca-certificates file time >/dev/null 2>&1; "
        "git lfs install >/dev/null 2>&1 || true; "
        "echo CMAKE=$(cmake --version | head -1); "
        "echo GCC=$(gcc -dumpfullversion 2>/dev/null || gcc --version | head -1); "
        "echo GIT=$(git --version); "
        "echo DISK=$(df -h /root | tail -1)"
    )

    clone = (
        "set -e; mkdir -p " + q_parent + "; "
        "if [ ! -d " + q_root + "/.git ]; then "
        "git clone --filter=blob:none " + q_remote + " " + q_root + "; "
        "fi; "
        "cd " + q_root + "; "
        "git remote set-url origin " + q_remote + "; "
        "git fetch --filter=blob:none origin " + q_ref + "; "
        "git reset --hard >/dev/null 2>&1 || true; "
        "git clean -fd >/dev/null 2>&1 || true; "
        "git checkout --detach FETCH_HEAD; "
        "git lfs install --local >/dev/null 2>&1 || true; "
        "echo CX_CYCLES_REMOTE=$(git remote get-url origin); "
        "echo CX_CYCLES_REF=" + q_ref + "; "
        "echo CX_CYCLES_HEAD=$(git rev-parse HEAD); "
        "git log --oneline -1"
    )

    sync = (
        "set -e -o pipefail; cd " + q_root + "; " +
        "make update 2>&1 | tail -80; "
        "echo CX_CYCLES_LIBDIR=$(du -sh lib 2>/dev/null || echo ABSENT)"
    )

    build = (
        "set -e -o pipefail; cd " + q_root + "; " +
        cmake_prefix + job_prefix + "make 2>&1 | tail -100; "
        "test -x install/cycles; "
        "echo CX_CYCLES_BUILD_OK=1; "
        "ls -lh install/cycles"
    )

    stages = [
        Stage("deps", deps, 1500),
        Stage("clone", clone, 2400),
        Stage("sync_libs", sync, 3600),
    ]
    patch_stage = apply_patches_stage(root) if apply_patches else None
    if patch_stage:
        stages.append(patch_stage)
    stages.extend([
        Stage("build", build, 7200),
        binary_smoke_stage(root),
    ])
    if patch_stage:
        stages.append(patch_cli_smoke_stage(root))
    stages.append(render_smoke_stage(root))
    return stages


def scaffold_manifest(root: str = DEFAULT_LOCAL_ROOT, ref: str = DEFAULT_REF,
                      jobs: int = 0, apply_patches: bool = True,
                      cmake_args: str = "") -> dict:
    patches = [os.path.basename(p) for p in patch_files()] if apply_patches else []
    return {
        "component": "cycles",
        "role": "cx render reference/fork scaffold",
        "remote": CYCLES_REMOTE,
        "ref": ref,
        "root": root,
        "binary": binary_path(root),
        "ledger": LEDGER,
        "jobs": jobs,
        "cmake_args": cmake_args,
        "patches": patches,
        "stages": [
            stage.to_json()
            for stage in build_stages(
                root=root,
                ref=ref,
                jobs=jobs,
                apply_patches=apply_patches,
                cmake_args=cmake_args,
            )
        ],
        "notes": [
            "official standalone Cycles is the compatibility oracle",
            "runtime checkout belongs under .artifacts or a pod-local path",
            "no credentials are read or written by this module",
        ],
    }


def append_ledger(record: dict, ledger: str = LEDGER) -> None:
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    with open(ledger, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the CX Cycles fork scaffold.")
    parser.add_argument("--root", default=DEFAULT_LOCAL_ROOT)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--cmake-args", default="")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    manifest = scaffold_manifest(
        root=args.root,
        ref=args.ref,
        jobs=args.jobs,
        cmake_args=args.cmake_args,
    )
    print(json.dumps(manifest, indent=2 if args.pretty else None, sort_keys=True))


if __name__ == "__main__":
    main()
