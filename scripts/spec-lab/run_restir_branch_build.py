#!/usr/bin/env python3
"""run_restir_branch_build.py — TRACK 1 Phase 0b driver: build the weizhen/blender:restir
                                fork (Blender PR #121023, "Cycles: Importance Resampling"),
                                money-safe, stage-by-stage, honest about the risk.

WHAT THIS IS
------------
The RISKIEST scaffolding item in Track 1. It provisions a pod and attempts to compile a
CUDA-capable Blender from the ReSTIR research branch so a later phase can render the
interior-dolly toy scene through spatial-only ReSTIR and ask whether smarter reuse clears
a worst-tile SSIM tier that equal-time path tracing misses. Getting a working binary is a
PREREQUISITE, not the experiment; this driver's whole job is to produce (or cleanly fail
to produce) that binary.

EXACT SOURCE (verified 2026-07-07 via `git ls-remote`, not guessed):
  * fork remote : https://projects.blender.org/weizhen/blender.git
  * branch      : restir   (HEAD = 95906c571211e216a178abf90be81ab79c3b3fd0 at verify time)
  * base repo   : https://projects.blender.org/blender/blender.git  (branch main present)
  The PR (#121023) is WIP and was paused upstream; the branch is real and fetchable.

BUILD STEPS (match Blender's OWN current Linux build docs — developer.blender.org
"Building Blender / Linux" + the Ubuntu build page; commands quoted in the handoff):
  1. deps    : apt build deps (build-essential git subversion cmake ninja + the X/GL/
               Wayland -dev libs Blender's Ubuntu page lists) + confirm nvcc is present.
  2. clone   : `git clone --filter=blob:none https://projects.blender.org/blender/blender.git`
               then add the fork as a remote, `git fetch weizhen restir`, checkout it.
               (blob:none partial clone keeps the full commit graph make_update needs while
               cutting the multi-GB history download.)
  3. sync    : `make update` — Blender's own step that fetches the PRECOMPILED LIBRARY set
               matching this branch AND syncs submodules. This is the single most fragile
               stage for a research FORK: the branch can reference a library version the
               package server no longer serves, or drift from current main's lib manifest.
  4. configure: out-of-source `cmake -S blender -B build_linux` with CUDA on. (Blender's
               `make` wrapper does configure+build in one; we split them so configure and
               build are separately timed + diagnosable.)
  5. build   : `cmake --build build_linux -j<N>`  — the long pole, HONESTLY 30-90+ minutes
               even on a capable box, and the stage most likely to OOM (parallel g++ eats
               RAM) or hit a compiler error on a stale research branch.
  6. smoke   : `blender --version` + a tiny headless Cycles render to prove the forked
               binary actually runs, plus a probe of scene.cycles for any restir/reservoir/
               resampling attribute so we CONFIRM the fork changed Cycles (never assumed).

WHY STAGE-BY-STAGE OVER SEPARATE SSH CALLS
------------------------------------------
Each stage is a SEPARATE ssh invocation with its OWN hard timeout. A stage returns the
instant it finishes (or its timeout fires), so we get incremental, ledgered visibility and
a failure is pinned to a named stage with a captured log tail — NOT a silent multi-hour
hang. The build stage gets the big timeout; clone/sync/configure get smaller ones. An
overall wall-clock deadline caps the whole run on top of the per-stage caps.

MONEY-SAFETY (all non-optional, standard spec-lab contract):
  * runpod.register_cleanup() wires teardown to every local exit path.
  * runpod.arm_remote_watchdog() arms a POD-SIDE self-destruct IMMEDIATELY after
    provisioning (survives this process dying — an orphaned build pod would bill for HOURS).
  * teardown in a finally block regardless of outcome.
  * disk sized generously (200 GB): source + precompiled libs + build tree + CUDA objects.

Emits ONE final JSON line (the contract): {"status":"ok",...} with per-stage timings + the
smoke result, OR {"error":..., "failed_stage":...} naming exactly where it broke. Never
hangs; the overall deadline + per-stage timeouts + pod watchdog are three independent stops.
"""
import argparse
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import runpod  # noqa: E402

# A CUDA box (we build WITH CUDA and smoke-render on GPU). Build speed is CPU-bound, so
# more vCPUs = faster; the GPU is for the CUDA smoke render. require_cuda=True ensures a
# genuinely GPU-capable host lands.
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
# Ubuntu 24.04 + CUDA toolkit (nvcc) already present — needed for CUDA kernel compilation.
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 200  # source + precompiled libs + build tree + CUDA objects; sized generously

FORK_REMOTE = "https://projects.blender.org/weizhen/blender.git"
FORK_BRANCH = "restir"
FORK_HEAD_AT_VERIFY = "95906c571211e216a178abf90be81ab79c3b3fd0"  # git ls-remote 2026-07-07
BASE_REPO = "https://projects.blender.org/blender/blender.git"

ROOT = "/root/blender-git"
SRC = f"{ROOT}/blender"
BUILD = f"{ROOT}/build_linux"
BLENDER_BIN = f"{BUILD}/bin/blender"

LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/restir_build_ledger.jsonl")
WATCHDOG_TTL_S = 14400  # 4h pod-side self-destruct backstop (build can be long; still bounded)


def log(m):
    print(f"[restir-build {time.strftime('%H:%M:%S')}] {m}", flush=True)


def emit(obj):
    """Final machine-parseable status line (the contract)."""
    print(json.dumps(obj), flush=True)


def ledger_append(rec):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")


# --------------------------------------------------------------------------- #
# The smoke-render script (runs inside the freshly-built Blender's python).     #
# Proves: (a) the binary runs, (b) Cycles loads + renders a frame, (c) whether  #
# the fork actually added restir/reservoir/resampling knobs to scene.cycles.    #
# base64-shipped to dodge all shell-quoting hazards.                            #
# --------------------------------------------------------------------------- #
SMOKE_SCRIPT = r'''
import bpy, os, sys, math

def _log(*a):
    print("[smoke]", *a, file=sys.stderr, flush=True)

OUT = "/tmp/restir_smoke.png"
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
bpy.ops.mesh.primitive_monkey_add(size=2.0, location=(0, 0, 0))
bpy.ops.object.light_add(type='AREA', location=(3, -3, 5)); bpy.context.active_object.data.energy = 800
bpy.ops.object.camera_add(location=(0, -6, 2)); cam = bpy.context.active_object
cam.rotation_euler = (math.radians(75), 0, 0); scene.camera = cam
scene.render.engine = 'CYCLES'
cyc = scene.cycles
cyc.samples = 8
cyc.use_adaptive_sampling = False

# ---- probe the fork: any restir / reservoir / resampling knob on scene.cycles? ----
restir_attrs = sorted([a for a in dir(cyc)
                       if any(k in a.lower() for k in ("restir", "reservoir", "resampl"))])
print("CX_RESTIR_ATTRS=" + ",".join(restir_attrs), flush=True)

# ---- device: prove CUDA if we can, else CPU (report honestly) ----
chosen = "CPU"
try:
    prefs = bpy.context.preferences.addons['cycles'].preferences
    picked = None
    for backend in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = backend
        except Exception:
            continue
        try:
            prefs.get_devices()
        except Exception:
            pass
        gpus = [d for d in prefs.devices if getattr(d, "type", "CPU") not in ("CPU",)]
        if gpus:
            for d in prefs.devices:
                d.use = (getattr(d, "type", "CPU") not in ("CPU",))
            picked = backend
            break
    if picked:
        cyc.device = 'GPU'; chosen = f"GPU/{picked}"
    else:
        cyc.device = 'CPU'
except Exception as e:
    _log("GPU setup failed, CPU:", e); cyc.device = 'CPU'; chosen = "CPU(gpu-setup-failed)"
print("CX_SMOKE_DEVICE=" + chosen, flush=True)

scene.render.resolution_x = 64; scene.render.resolution_y = 64
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = OUT
scene.frame_set(1)
bpy.ops.render.render(write_still=True)
print("CX_SMOKE_OK=" + ("1" if os.path.isfile(OUT) else "0"), flush=True)
print("CX_SMOKE_DONE", flush=True)
'''


def build_stages(cuda_binaries, cuda_arch, build_jobs):
    """Return the ordered [(name, remote_cmd, timeout_s), ...] pipeline.

    Each is one self-contained command over its OWN ssh call so a failure is pinned to a
    named stage with its own hard timeout (no silent multi-hour hang)."""

    # CUDA: WITH_CYCLES_DEVICE_CUDA is ON by default (runtime kernel compile via the
    # toolkit's nvcc on first render). WITH_CYCLES_CUDA_BINARIES=ON precompiles .cubins at
    # BUILD time — correct + faster first render, but adds significant build time and needs
    # CYCLES_CUDA_BINARIES_ARCH to match the pod GPU. Default OFF = faster build, CUDA still
    # works at runtime (the image ships nvcc). Documented, not hidden.
    cuda_flags = "-DWITH_CYCLES_DEVICE_CUDA=ON"
    if cuda_binaries:
        cuda_flags += " -DWITH_CYCLES_CUDA_BINARIES=ON"
        if cuda_arch:
            cuda_flags += f" -DCYCLES_CUDA_BINARIES_ARCH={cuda_arch}"
    else:
        cuda_flags += " -DWITH_CYCLES_CUDA_BINARIES=OFF"
    # WITH_VULKAN_BACKEND defaults ON on Linux (OFF on macOS, per CMakeLists.txt ~line 2650
    # -- why this never bit any prior Apple-side dev work) and unconditionally requires a
    # vulkan+shaderc pkg-config lookup (platform_unix.cmake ~line 145-159) independent of any
    # other WITH_* option. We only need headless CPU+CUDA Cycles path tracing, not Blender's
    # Vulkan-backed viewport/GPU draw module, so disable it rather than installing
    # libvulkan-dev/libshaderc-dev for a feature this experiment never uses.
    cuda_flags += " -DWITH_VULKAN_BACKEND=OFF"

    jobs = build_jobs if build_jobs else '"$(nproc)"'

    deps_cmd = (
        "set -e; export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update >/dev/null 2>&1; "
        # -dev list from Blender's Ubuntu build page (developer docs), PLUS libepoxy-dev:
        # Blender's own docs omit it, but intern/ghost/CMakeLists.txt links
        # bf::dependencies::epoxy unconditionally (even for -DWITH_HEADLESS=ON) and
        # platform_unix.cmake's find_package_wrapper(Epoxy REQUIRED) has no opt-out --
        # confirmed the hard way (first build attempt failed configure without it).
        "apt-get install -y build-essential git git-lfs subversion cmake ninja-build "
        "libx11-dev libxxf86vm-dev libxcursor-dev libxi-dev libxrandr-dev libxinerama-dev "
        "libegl-dev libwayland-dev wayland-protocols libxkbcommon-dev libdbus-1-dev "
        "linux-libc-dev libepoxy-dev python3 python3-dev >/dev/null 2>&1; "
        # Installing the git-lfs PACKAGE is not enough -- its smudge/clean filters must
        # also be registered once via `git lfs install`, or `make update`'s internal
        # `lib/linux_x64` submodule pull silently yields empty/pointer-stub content
        # instead of the real precompiled-library binaries (this is what actually caused
        # the 3rd build attempt's "Unable to find LIBDIR" -> missing Freetype/etc chain --
        # not actually-missing system packages, a skipped one-time git-lfs setup step).
        "git lfs install >/dev/null 2>&1; "
        "echo CMAKE=$(cmake --version | head -1); "
        "echo GCC=$(gcc -dumpfullversion 2>/dev/null || gcc --version | head -1); "
        "echo NVCC=$(nvcc --version 2>/dev/null | tail -1 || echo MISSING); "
        "echo DISK=$(df -h /root | tail -1)"
    )

    clone_cmd = (
        "set -e; mkdir -p " + ROOT + "; cd " + ROOT + "; "
        "git clone --filter=blob:none " + BASE_REPO + " blender; "
        "cd blender; "
        "git remote add weizhen " + FORK_REMOTE + "; "
        "git fetch --filter=blob:none weizhen " + FORK_BRANCH + "; "
        "git checkout -b " + FORK_BRANCH + " weizhen/" + FORK_BRANCH + "; "
        "echo HEAD=$(git rev-parse HEAD); "
        "echo EXPECT=" + FORK_HEAD_AT_VERIFY + "; "
        "git log --oneline -1"
    )

    # NOTE on `set -e -o pipefail`: plain `set -e` does NOT see a failure inside a
    # `cmd | tail -N` pipe -- the pipeline's exit status is `tail`'s (always 0), so a
    # real failure upstream is silently swallowed and the stage falsely reports ok=True.
    # This bit us for real: cmake's configure failed ("Could NOT find Epoxy") but its
    # trailing `test -f build_linux/CMakeCache.txt` still passed (CMake writes a partial
    # cache file even on a failed configure) and printed CONFIGURE_OK anyway. `pipefail`
    # makes the pipeline's exit status the first non-zero one, so `set -e` actually fires.

    # make update: fetch matching precompiled libraries + sync submodules (the fragile one).
    # The trailing echo is a diagnostic, not a gate: if the lib/linux_x64 submodule itself
    # is broken/absent on this research fork (a real possibility, distinct from the git-lfs
    # setup issue above), we want that visible in THIS stage's log instead of surfacing as
    # a confusing "missing FooLibrary" error two stages later in cmake_configure.
    sync_cmd = ("set -e -o pipefail; cd " + SRC + "; make update 2>&1 | tail -40; "
                "echo LIBDIR_CHECK=$(du -sh lib/linux_x64 2>/dev/null || echo ABSENT)")

    configure_cmd = (
        "set -e -o pipefail; cd " + ROOT + "; "
        "cmake -S blender -B build_linux -DCMAKE_BUILD_TYPE=Release " + cuda_flags +
        " 2>&1 | tail -50; "
        "test -f build_linux/CMakeCache.txt && echo CONFIGURE_OK"
    )

    build_cmd = (
        "set -e -o pipefail; cd " + ROOT + "; "
        "cmake --build build_linux -j" + jobs + " 2>&1 | tail -50; "
        "test -x " + BLENDER_BIN + " && echo BUILD_OK && ls -la " + BLENDER_BIN
    )

    smoke_b64 = base64.b64encode(SMOKE_SCRIPT.encode()).decode()
    smoke_cmd = (
        "set -e; " + BLENDER_BIN + " --version | head -1; "
        "echo " + smoke_b64 + " | base64 -d > /root/smoke.py; "
        + BLENDER_BIN + " -b -noaudio --factory-startup -P /root/smoke.py 2>&1 | "
        "grep -E 'CX_RESTIR_ATTRS|CX_SMOKE_DEVICE|CX_SMOKE_OK|CX_SMOKE_DONE|Error|error' | tail -20"
    )

    return [
        ("deps", deps_cmd, 1500),
        ("clone", clone_cmd, 2400),
        ("sync_libs", sync_cmd, 3000),
        ("cmake_configure", configure_cmd, 1200),
        ("build", build_cmd, 6000),   # the long pole — honestly 30-90+ min
        ("smoke", smoke_cmd, 1200),   # first GPU render may runtime-compile CUDA kernels
    ]


def run_stage(pod, name, cmd, timeout_s):
    """One stage over its OWN ssh call. Returns (ok, elapsed_s, out_tail, err_tail)."""
    log(f"STAGE {name}: start (timeout {timeout_s}s)")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=timeout_s)
    except Exception as e:  # includes subprocess.TimeoutExpired
        dt = time.time() - t0
        return False, round(dt, 1), "", f"{type(e).__name__}: {str(e)[:300]}"
    dt = time.time() - t0
    out_tail = (out or "")[-1200:]
    err_tail = (err or "")[-1200:]
    ok = rc == 0
    log(f"STAGE {name}: rc={rc} elapsed={dt:.1f}s ok={ok}")
    if out_tail.strip():
        log(f"  {name} stdout tail:\n{out_tail}")
    if not ok and err_tail.strip():
        log(f"  {name} stderr tail:\n{err_tail}")
    return ok, round(dt, 1), out_tail, err_tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overall-deadline-s", type=int, default=12000,
                    help="hard wall-clock cap across ALL stages (default 200 min)")
    ap.add_argument("--cuda-binaries", action="store_true",
                    help="precompile .cubin kernels at build time (slower build; set "
                         "--cuda-arch to match the pod GPU). Default: OFF (runtime compile).")
    ap.add_argument("--cuda-arch", default="",
                    help="e.g. sm_80 (A100) / sm_89 (L40S,4090) / sm_86 (A6000). Only used "
                         "with --cuda-binaries.")
    ap.add_argument("--build-jobs", type=int, default=0,
                    help="parallel compile jobs; 0 = nproc. Lower this on low-RAM boxes to "
                         "avoid an OOM kill during the build stage.")
    args = ap.parse_args()

    stages = build_stages(args.cuda_binaries, args.cuda_arch,
                          str(args.build_jobs) if args.build_jobs else "")

    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    log("provisioning (CUDA box, 200GB disk)...")
    pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB,
                                     require_cuda=True)
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
    ledger_append({"event": "pod_up", "pod": pod, "fork": FORK_REMOTE, "branch": FORK_BRANCH})

    # HARD BACKSTOP: self-terminates on the pod even if this local process dies.
    runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

    stage_records = []
    result = None
    try:
        t_start = time.time()
        for name, cmd, timeout_s in stages:
            # overall wall-clock deadline check BEFORE launching the next (possibly long) stage
            remaining = args.overall_deadline_s - (time.time() - t_start)
            if remaining <= 60:
                result = {"error": f"overall deadline ({args.overall_deadline_s}s) hit before "
                                   f"stage '{name}'", "failed_stage": name,
                          "stages": stage_records}
                log(result["error"])
                break
            eff_timeout = int(min(timeout_s, remaining))
            ok, dt, out_tail, err_tail = run_stage(pod, name, cmd, eff_timeout)
            rec = {"stage": name, "ok": ok, "elapsed_s": dt,
                   "out_tail": out_tail[-600:], "err_tail": err_tail[-400:]}
            stage_records.append(rec)
            ledger_append({"event": "stage", **rec})
            if not ok:
                result = {"error": f"stage '{name}' FAILED (see err_tail)",
                          "failed_stage": name, "stages": stage_records}
                log(f"ABORT: stage '{name}' failed after {dt}s")
                break
        else:
            # all stages passed — parse the smoke stage's marker lines honestly
            smoke = stage_records[-1]["out_tail"] if stage_records else ""
            restir_attrs = ""
            for ln in smoke.splitlines():
                if ln.startswith("CX_RESTIR_ATTRS="):
                    restir_attrs = ln.split("=", 1)[1].strip()
            restir_present = bool(restir_attrs)  # non-empty attr list => fork changed Cycles
            smoke_ok = "CX_SMOKE_OK=1" in smoke
            result = {
                "status": "ok",
                "modeled": False,
                "blender_bin": BLENDER_BIN,
                "fork_remote": FORK_REMOTE,
                "fork_branch": FORK_BRANCH,
                "fork_head_expected": FORK_HEAD_AT_VERIFY,
                "smoke_render_ok": smoke_ok,
                "restir_cycles_attrs_present": restir_present,
                "restir_cycles_attrs": restir_attrs,
                "total_build_s": round(time.time() - t_start, 1),
                "stages": [{k: r[k] for k in ("stage", "ok", "elapsed_s")}
                           for r in stage_records],
                "note": ("Built weizhen/blender:restir with CUDA. smoke_render_ok proves the "
                         "forked binary path-traces a frame; restir_cycles_attrs_present is "
                         "the honest check that the fork actually exposed restir/reservoir/"
                         "resampling knobs on scene.cycles (see the CX_RESTIR_ATTRS line in "
                         "the smoke stage out_tail). This is Phase-0b PREREQUISITE only — the "
                         "ReSTIR-vs-equal-time render experiment is a separate follow-up."),
            }
            log(f"BUILD OK: {BLENDER_BIN} smoke_ok={smoke_ok} restir_attrs={restir_present}")
    finally:
        ledger_append({"event": "result", "result": result})
        log("tearing down...")
        try:
            runpod.terminate(pod["id"])
        except Exception as e:
            log(f"terminate error (verify in console): {e}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")

    emit(result if result is not None else {"error": "no result (unexpected)",
                                            "stages": stage_records})


if __name__ == "__main__":
    main()
