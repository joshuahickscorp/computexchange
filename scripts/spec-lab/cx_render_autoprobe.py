#!/usr/bin/env python3
"""Autoprobe the CX renderer substrate and run the safest available proof lane.

This is the front door for the Cycles-killer overnight scaffold. It detects the
host, records runtime-tar and cloud readiness, runs local Apple/native renderer
proofs when available, and refuses to turn absence of CUDA/cloud into a fake win.
Cloud provisioning is opt-in because RunPod spend must be intentional and
teardown-safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import runpod  # noqa: E402


DATE = "2026-07-09"
LEDGER = REPO / "docs/speed-lane-reports/spec-lab/cx_render_autoprobe_ledger.jsonl"
REPORT = REPO / f"docs/speed-lane-reports/spec-lab/CX_RENDER_AUTOPROBE_{DATE}.md"
MAIN_REPORT = REPO / f"docs/speed-lane-reports/spec-lab/CX_CYCLES_KILLER_OVERNIGHT_{DATE}.md"
RUNTIME_TARS = [
    REPO / ".artifacts/cycles/runtime/cx-cycles-hopper-sm90-runtime-20260708.tar.gz",
    REPO / ".artifacts/cycles/runtime/cx-cycles-hopper-sm90-batch-runtime-20260708.tar.gz",
    REPO / ".artifacts/cycles/runtime/cx-cycles-ada-sm89-batch-runtime-20260708.tar.gz",
]


def redact_secret(text: object) -> str:
    """Remove credential-shaped values before anything reaches ledgers/reports."""
    value = str(text)
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if api_key:
        value = value.replace(api_key, "[REDACTED_RUNPOD_API_KEY]")
    value = re.sub(r"api_key=[^&\s'\"]+", "api_key=[REDACTED]", value)
    value = re.sub(r"Bearer\s+[A-Za-z0-9_.-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"rpa_[A-Za-z0-9]+", "rpa_[REDACTED]", value)
    return value


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_ledger(record: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": now(), **record}, sort_keys=True) + "\n")


def run_cmd(
    stage: str,
    cmd: list[str],
    cwd: Path | None = None,
    timeout_s: int = 300,
    env: dict[str, str] | None = None,
) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        rec = {
            "stage": stage,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_s": round(time.time() - t0, 3),
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "stdout_tail": (proc.stdout or "")[-8000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
        }
    except Exception as exc:  # noqa: BLE001
        rec = {
            "stage": stage,
            "ok": False,
            "returncode": None,
            "elapsed_s": round(time.time() - t0, 3),
            "cmd": cmd,
            "cwd": str(cwd) if cwd else None,
            "stdout_tail": "",
            "stderr_tail": redact_secret(f"{type(exc).__name__}: {exc}")[-4000:],
        }
    append_ledger({"event": "stage", **rec})
    return rec


def file_sha256(path: Path, max_bytes: int = 4 * 1024 * 1024) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        remaining = max_bytes
        while remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def runtime_tar_inventory() -> list[dict]:
    out = []
    for path in RUNTIME_TARS:
        out.append({
            "path": str(path),
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            "sha256_first_4m": file_sha256(path),
            "role": runtime_role(path.name),
        })
    return out


def runtime_role(name: str) -> str:
    if "ada-sm89" in name:
        return "ada_sm89_batch"
    if "hopper-sm90-batch" in name:
        return "hopper_sm90_batch"
    if "hopper-sm90" in name:
        return "hopper_sm90"
    return "unknown"


def detect_apple() -> dict:
    is_darwin = platform.system() == "Darwin"
    machine = platform.machine().lower()
    brand = ""
    if is_darwin:
        rec = run_cmd("sysctl_cpu_brand", ["sysctl", "-n", "machdep.cpu.brand_string"], timeout_s=10)
        brand = rec["stdout_tail"].strip()
    return {
        "is_darwin": is_darwin,
        "is_apple_silicon": is_darwin and machine in {"arm64", "aarch64"},
        "machine": platform.machine(),
        "cpu_brand": brand,
        "metal_candidate": is_darwin,
    }


def detect_nvidia() -> dict:
    exe = shutil.which("nvidia-smi")
    result = {"nvidia_smi": exe, "available": bool(exe), "gpus": []}
    if not exe:
        return result
    rec = run_cmd(
        "nvidia_smi_query",
        [
            exe,
            "--query-gpu=name,compute_cap,driver_version,memory.total",
            "--format=csv,noheader",
        ],
        timeout_s=20,
    )
    result["query_stage"] = rec
    if rec["ok"]:
        for line in rec["stdout_tail"].splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 4:
                result["gpus"].append({
                    "name": parts[0],
                    "compute_capability": parts[1],
                    "driver_version": parts[2],
                    "memory_total": parts[3],
                })
    return result


def tracked_pods() -> list[str]:
    try:
        return runpod._load_tracked()  # noqa: SLF001
    except Exception:
        return []


def cloud_state(check_api: bool) -> dict:
    key_present = bool(os.environ.get("RUNPOD_API_KEY", "").strip())
    if not key_present:
        try:
            key_present = bool(runpod._api_key_from_env_file())  # noqa: SLF001
        except Exception:
            key_present = False
    state = {
        "runpod_api_key_present": key_present,
        "ssh_pubkey_present": Path(runpod.PUBKEY_PATH).is_file(),
        "tracked_pods": tracked_pods(),
        "balance": None,
        "balance_error": None,
        "live_pods": "not_checked",
        "live_pods_error": None,
    }
    if check_api and state["runpod_api_key_present"]:
        try:
            state["balance"] = runpod.balance()
        except Exception as exc:  # noqa: BLE001
            state["balance_error"] = redact_secret(f"{type(exc).__name__}: {exc}")
        try:
            data = runpod.gql(
                "query { myself { pods { id name runtime { uptimeInSeconds } } } }",
                retries=1,
            )
            state["live_pods"] = data.get("myself", {}).get("pods", [])
        except Exception as exc:  # noqa: BLE001
            state["live_pods"] = "unknown"
            state["live_pods_error"] = redact_secret(f"{type(exc).__name__}: {exc}")
    return state


def choose_route(apple: dict, nvidia: dict, cloud: dict) -> dict:
    tar_inventory = runtime_tar_inventory()
    available_tars = [t for t in tar_inventory if t["exists"]]
    if nvidia.get("gpus"):
        gpu = nvidia["gpus"][0]
        cc = str(gpu.get("compute_capability") or "")
        role = "hopper_sm90_batch" if cc.startswith("9.") else "ada_sm89_batch"
        return {"lane": "local_cuda", "reason": f"nvidia-smi found {gpu.get('name')}", "runtime_role": role}
    if apple.get("is_apple_silicon"):
        return {"lane": "apple_silicon", "reason": "Darwin arm64 host with Metal candidate", "runtime_role": None}
    if cloud["runpod_api_key_present"] and cloud["ssh_pubkey_present"] and available_tars:
        return {"lane": "cloud_cuda_ready", "reason": "RunPod credentials and runtime tars are present", "runtime_role": "hopper_sm90_batch"}
    return {"lane": "no_gpu_scaffold", "reason": "no local CUDA and cloud is not fully available", "runtime_role": None}


def run_local_renderer_proofs(args: argparse.Namespace, apple: dict) -> list[dict]:
    stages = []
    if not args.run_local_renderer:
        stages.append({"stage": "renderer_local_tests", "ok": None, "status": "skipped", "reason": "--no-run-local-renderer"})
        return stages

    stages.append(run_cmd(
        "renderer_cargo_test_release",
        ["cargo", "test", "--release", "--", "--nocapture"],
        cwd=REPO / "renderer",
        timeout_s=args.local_timeout_s,
    ))
    stages.append(run_cmd(
        "renderer_decoupled_micro",
        ["cargo", "run", "--release", "--example", "decoupled_micro", "--", "8", "224", "16"],
        cwd=REPO / "renderer",
        timeout_s=args.local_timeout_s,
    ))
    if apple.get("is_apple_silicon") or args.force_wgpu:
        env = dict(os.environ)
        if apple.get("is_apple_silicon"):
            env.setdefault("CX_WGPU_BACKEND", "metal")
        stages.append(run_cmd(
            "renderer_wgpu_smoke",
            ["cargo", "run", "--example", "wgpu_smoke", "--features", "gpu"],
            cwd=REPO / "renderer",
            timeout_s=args.wgpu_timeout_s,
            env=env,
        ))
    else:
        stages.append({"stage": "renderer_wgpu_smoke", "ok": None, "status": "skipped", "reason": "not Apple Silicon and --force-wgpu not set"})
        append_ledger({"event": "stage", **stages[-1]})
    return stages


def dry_cloud_receipt(route: dict, cloud: dict, args: argparse.Namespace) -> dict:
    if not args.allow_cloud:
        return {"ok": None, "status": "skipped", "reason": "cloud provisioning requires --allow-cloud"}
    if route["lane"] not in {"cloud_cuda_ready", "local_cuda"}:
        return {"ok": False, "status": "unavailable", "reason": route["reason"]}
    if cloud["tracked_pods"]:
        return {"ok": False, "status": "blocked", "reason": "tracked pods are non-empty; refuse new cloud run"}
    return {
        "ok": False,
        "status": "not_executed",
        "reason": "cx_render_autoprobe cloud execution is scaffolded; invoke run_cycles_quality_ladder.py for paid proof",
        "suggested_command": [
            "python3",
            "scripts/spec-lab/run_cycles_quality_ladder.py",
            "--gpu-tier",
            "ada",
            "--include-synthetic-scene",
            "--with-oidn",
            "--oidn-device",
            "cpu",
            "--max-minutes",
            str(args.cloud_max_minutes),
        ],
    }


def write_report(result: dict) -> None:
    route = result["route"]
    lines = [
        f"# CX Render Autoprobe - {DATE}",
        "",
        "## Summary",
        "",
        f"- Final lane: `{route['lane']}`",
        f"- Reason: {route['reason']}",
        f"- Overall status: `{result['status']}`",
        f"- Ledger: `{LEDGER.relative_to(REPO)}`",
        "",
        "## Platform",
        "",
        f"- OS: `{result['platform']['system']} {result['platform']['release']}`",
        f"- Machine: `{result['platform']['machine']}`",
        f"- Python: `{result['platform']['python']}`",
        f"- Apple Silicon: `{result['apple']['is_apple_silicon']}`",
        f"- NVIDIA CUDA detected: `{bool(result['nvidia'].get('gpus'))}`",
        "",
        "## Runtime Tars",
        "",
        "| Role | Exists | Size | Path |",
        "|---|---:|---:|---|",
    ]
    for item in result["runtime_tars"]:
        lines.append(
            f"| `{item['role']}` | `{item['exists']}` | `{item['size_bytes']}` | `{item['path']}` |"
        )
    lines.extend([
        "",
        "## Cloud Safety State",
        "",
        f"- RunPod key present: `{result['cloud']['runpod_api_key_present']}`",
        f"- SSH pubkey present: `{result['cloud']['ssh_pubkey_present']}`",
        f"- Tracked pods: `{result['cloud']['tracked_pods']}`",
        f"- Balance: `{result['cloud']['balance']}`",
        f"- Live pods: `{result['cloud']['live_pods']}`",
        "",
        "## Local Proof Stages",
        "",
        "| Stage | OK | Elapsed | Note |",
        "|---|---:|---:|---|",
    ])
    for stage in result["local_stages"]:
        note = stage.get("reason") or stage.get("stderr_tail", "").splitlines()[-1:] or [""]
        if isinstance(note, list):
            note = note[0] if note else ""
        lines.append(
            f"| `{stage['stage']}` | `{stage.get('ok')}` | `{stage.get('elapsed_s')}` | {str(note)[:160]} |"
        )
    lines.extend([
        "",
        "## Cloud Proof",
        "",
        "Cloud was not treated as proven unless the script actually provisioned and tore down a pod.",
        "",
        "```json",
        json.dumps(result["cloud_proof"], indent=2, sort_keys=True),
        "```",
        "",
        "## Interpretation",
        "",
        "- This autoprobe is a routing and receipt layer, not a renderer victory claim.",
        "- On Apple Silicon it proves the native Rust renderer lane and records CUDA/cloud absence.",
        "- CUDA quality claims must come from the quality/speculative ledgers, with global and worst-tile gates.",
    ])
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")


def update_main_report(result: dict) -> None:
    lines = [
        f"# CX Cycles-Killer Overnight Report - {DATE}",
        "",
        "## Current State",
        "",
        f"- Autoprobe report: `{REPORT.relative_to(REPO)}`",
        f"- Autoprobe ledger: `{LEDGER.relative_to(REPO)}`",
        f"- Selected lane: `{result['route']['lane']}`",
        f"- Status: `{result['status']}`",
        "",
        "## Hard Boundaries",
        "",
        "- No fake Cycles-killer claim is made from scaffold-only work.",
        "- Existing Cycles attribution and license boundaries are preserved; this pass only adds orchestration scripts.",
        "- Cloud was not used unless RunPod credentials, balance, tracked pods, and explicit opt-in all cleared.",
        "- Hawking/speculative integration remains a capability/receipt boundary; no engine merge is implied.",
        "",
        "## Next Runner",
        "",
        "Run the speculative ladder next:",
        "",
        "```bash",
        "python3 scripts/spec-lab/run_speculative_render_ladder.py",
        "```",
    ]
    MAIN_REPORT.parent.mkdir(parents=True, exist_ok=True)
    if not MAIN_REPORT.exists():
        MAIN_REPORT.write_text("\n".join(lines) + "\n")
        return
    marker = "<!-- cx-render-autoprobe-refresh -->"
    existing = MAIN_REPORT.read_text()
    refresh = "\n".join([
        marker,
        "",
        "## Latest Autoprobe Refresh",
        "",
        f"- Autoprobe report: `{REPORT.relative_to(REPO)}`",
        f"- Selected lane: `{result['route']['lane']}`",
        f"- Status: `{result['status']}`",
        f"- Live pods: `{result['cloud'].get('live_pods')}`",
        f"- Tracked pods: `{result['cloud'].get('tracked_pods')}`",
        f"- Balance: `{result['cloud'].get('balance')}`",
        "",
    ])
    head = existing.split(marker, 1)[0].rstrip()
    MAIN_REPORT.write_text(head + "\n\n" + refresh)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-detect CX render substrate and run safe proofs.")
    parser.add_argument("--no-run-local-renderer", dest="run_local_renderer", action="store_false")
    parser.add_argument("--force-wgpu", action="store_true")
    parser.add_argument("--check-cloud-api", action="store_true")
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--cloud-max-minutes", type=int, default=45)
    parser.add_argument("--local-timeout-s", type=int, default=900)
    parser.add_argument("--wgpu-timeout-s", type=int, default=1200)
    args = parser.parse_args()

    append_ledger({"event": "start", "argv": sys.argv[1:]})
    apple = detect_apple()
    nvidia = detect_nvidia()
    cloud = cloud_state(args.check_cloud_api or args.allow_cloud)
    route = choose_route(apple, nvidia, cloud)
    local_stages = run_local_renderer_proofs(args, apple)
    cloud_proof = dry_cloud_receipt(route, cloud, args)

    failed_local = [s for s in local_stages if s.get("ok") is False]
    status = "passed_with_skips"
    if failed_local:
        status = "failed_local_proof"
    elif route["lane"] in {"no_gpu_scaffold", "cloud_cuda_ready"}:
        status = "scaffold_ready_gpu_unproven"

    result = {
        "ok": not failed_local,
        "status": status,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "apple": apple,
        "nvidia": nvidia,
        "runtime_tars": runtime_tar_inventory(),
        "cloud": cloud,
        "route": route,
        "local_stages": local_stages,
        "cloud_proof": cloud_proof,
        "reports": {
            "autoprobe": str(REPORT),
            "main": str(MAIN_REPORT),
            "ledger": str(LEDGER),
        },
    }
    append_ledger({"event": "result", "result": result})
    write_report(result)
    update_main_report(result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
