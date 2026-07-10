#!/usr/bin/env python3
"""Run renderer Track-3 micro-experiments on one money-safe RunPod pod.

This is deliberately small: ship the standalone `renderer/` crate without target/,
install a minimal Rust toolchain, run the CPU correctness/reuse gates, then run the
wgpu Vulkan smoke test on an NVIDIA pod. It answers whether our Rust renderer scaffold
survives off the Mac and whether the Vulkan backend is usable on the rental fleet.
"""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ROOT = REPO
sys.path.insert(0, HERE)

import runpod  # noqa: E402


# Cheap capability probe: GPU SKU irrelevant, policy tier not required — see
# gpu-provisioning-policy exception clause. This driver runs CPU-bound Rust
# correctness gates plus a wgpu Vulkan smoke test; it measures WHETHER Vulkan works
# on the rental fleet, not how fast a specific SKU is, so any reachable NVIDIA box
# answers the question and the A100/H100/H200 policy ladder is deliberately not
# imposed (no Cycles, no benchmark receipts emitted from this pod).
GPU_PLAN = [
    ("NVIDIA L40S", "COMMUNITY"),
    ("NVIDIA RTX A6000", "COMMUNITY"),
    ("NVIDIA L40S", "SECURE"),
    ("NVIDIA A100 80GB PCIe", "COMMUNITY"),
    ("NVIDIA A100 80GB PCIe", "SECURE"),
]
POD_IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 80
REMOTE_ROOT = "/root/cx-renderer-micro"
LEDGER = os.path.join(REPO, "docs/speed-lane-reports/spec-lab/renderer_micro_ledger.jsonl")
WATCHDOG_TTL_S = 5400


def log(message):
    print(f"[renderer-micro {time.strftime('%H:%M:%S')}] {message}", flush=True)


def ledger_append(record):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}) + "\n")


def make_renderer_tar():
    tmp = tempfile.NamedTemporaryFile(prefix="cx-renderer-", suffix=".tgz", delete=False)
    tmp.close()
    renderer_dir = os.path.join(ROOT, "renderer")
    with tarfile.open(tmp.name, "w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(renderer_dir):
            dirnames[:] = [d for d in dirnames if d != "target"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                tar.add(full, arcname=rel)
    return tmp.name


def run_stage(pod, name, cmd, timeout):
    log(f"STAGE {name} start")
    t0 = time.time()
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        rec = {"stage": name, "ok": False, "elapsed_s": round(dt, 1),
               "out_tail": "", "err_tail": f"{type(exc).__name__}: {exc}"[:800]}
        ledger_append({"event": "stage", **rec})
        log(f"STAGE {name} exception after {dt:.1f}s: {exc}")
        return rec
    dt = time.time() - t0
    rec = {"stage": name, "ok": rc == 0, "elapsed_s": round(dt, 1),
           "out_tail": (out or "")[-1200:], "err_tail": (err or "")[-800:]}
    ledger_append({"event": "stage", **rec})
    log(f"STAGE {name} rc={rc} elapsed={dt:.1f}s")
    if rec["out_tail"].strip():
        log(f"  stdout tail:\n{rec['out_tail']}")
    if rc != 0 and rec["err_tail"].strip():
        log(f"  stderr tail:\n{rec['err_tail']}")
    return rec


def main():
    runpod.register_cleanup()
    bal0 = runpod.balance()["clientBalance"]
    log(f"balance ${bal0:.2f}")

    tar_path = make_renderer_tar()
    pod = None
    stages = []
    result = None
    try:
        log("provisioning NVIDIA pod for Rust/wgpu Vulkan smoke")
        pod = runpod.provision_reachable(GPU_PLAN, POD_IMAGE, disk_gb=POD_DISK_GB,
                                         require_cuda=True)
        log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")
        ledger_append({"event": "pod_up", "pod": pod})
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)

        rc, _, err = runpod.ssh(pod, f"rm -rf {REMOTE_ROOT}; mkdir -p {REMOTE_ROOT}",
                                timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir failed: {err[:200]}")
        ok, serr = runpod.scp_to(pod, tar_path, f"{REMOTE_ROOT}/renderer.tgz")
        if not ok:
            raise RuntimeError(f"scp renderer tar failed: {serr[:240]}")

        stages.append(run_stage(
            pod, "deps",
            "set -e -o pipefail; export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update >/dev/null 2>&1; "
            "apt-get install -y curl build-essential pkg-config libssl-dev "
            "libvulkan1 vulkan-tools >/dev/null 2>&1; "
            "if ! command -v cargo >/dev/null 2>&1; then "
            "curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal >/dev/null; fi; "
            ". $HOME/.cargo/env; rustc --version; cargo --version; "
            "(vulkaninfo --summary 2>&1 | head -80 || true)",
            1200,
        ))
        if not stages[-1]["ok"]:
            raise RuntimeError("deps failed")

        stages.append(run_stage(
            pod, "unpack",
            f"set -e; cd {REMOTE_ROOT}; tar -xzf renderer.tgz; "
            "du -sh renderer; find renderer -maxdepth 2 -type f | sort | head -40",
            120,
        ))
        if not stages[-1]["ok"]:
            raise RuntimeError("unpack failed")

        cargo_env = ". $HOME/.cargo/env; cd " + REMOTE_ROOT + "/renderer; "
        stages.append(run_stage(
            pod, "cpu_tests",
            cargo_env + "cargo test --release -- --nocapture",
            1200,
        ))
        stages.append(run_stage(
            pod, "decoupled_micro",
            cargo_env + "cargo run --release --example decoupled_micro -- 8 224 16",
            600,
        ))
        stages.append(run_stage(
            pod, "gpu_diag",
            "set +e; echo NVIDIA_SMI; nvidia-smi -L; "
            "echo VULKAN_ICD_DIR; ls -la /usr/share/vulkan/icd.d /etc/vulkan/icd.d 2>/dev/null; "
            "echo VULKAN_LIBS; ldconfig -p | grep -E 'libvulkan|libnvidia' | head -40; "
            "echo VULKANINFO; vulkaninfo --summary 2>&1 | head -120; "
            "echo ENV; env | grep -E 'VK_|NVIDIA|CUDA' | sort",
            180,
        ))
        stages.append(run_stage(
            pod, "wgpu_vulkan_smoke",
            cargo_env + "CX_WGPU_BACKEND=vulkan cargo run --example wgpu_smoke --features gpu",
            1800,
        ))
        stages.append(run_stage(
            pod, "wgpu_gl_smoke",
            cargo_env + "CX_WGPU_BACKEND=gl cargo run --example wgpu_smoke --features gpu",
            900,
        ))

        result = {
            "ok": all(s["ok"] for s in stages if s["stage"] != "wgpu_gl_smoke"),
            "stages": [{k: s[k] for k in ("stage", "ok", "elapsed_s")} for s in stages],
            "decoupled_json": _last_json(stages, "decoupled_micro"),
            "wgpu_json": _last_json(stages, "wgpu_vulkan_smoke"),
            "wgpu_gl_json": _last_json(stages, "wgpu_gl_smoke"),
        }
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "stages": [{k: s[k] for k in ("stage", "ok", "elapsed_s")} for s in stages]}
        log(f"ERROR {result['error']}")
    finally:
        if pod:
            log("tearing down")
            try:
                runpod.terminate(pod["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"terminate error: {exc}")
        b2 = runpod.balance()["clientBalance"]
        ledger_append({"event": "result", "result": result})
        ledger_append({"event": "pod_down", "balance_after": b2})
        log(f"pod down. balance ${b2:.2f} (spent ${bal0 - b2:.2f})")
        try:
            os.unlink(tar_path)
        except OSError:
            pass
    print(json.dumps(result), flush=True)


def _last_json(stages, name):
    for stage in stages:
        if stage["stage"] != name:
            continue
        for line in reversed(stage["out_tail"].splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except Exception:  # noqa: BLE001
                    return None
    return None


if __name__ == "__main__":
    main()
