#!/usr/bin/env python3
"""run_cross_arch_gate.py — three-stage cross-architecture (Metal vs CUDA) gate driver.
================================================================================

WHY (Generalization Plan 2026-07-10, objective 2 "Any silicon"): the CX fleet is
Apple Silicon (Cycles/Metal), rented GPUs are CUDA. If a Mac drafts and a CUDA box
verifies (or produces the reference), delivery-quality claims must hold ACROSS
architectures. Cycles is NOT expected byte-identical across device kernels — the
question is how close, per grading tile, and whether the cross-arch worst tile
clears the strict delivery gate (worst-tile SSIM >= 0.95).

The runner logic lives in pod/exp_cross_arch_gate.py (works on a CUDA pod AND on
the local Mac via the same shim pattern as run_local_metal_anchor.py). This driver
has three subcommands, one per stage:

  local   STAGE 1 — LOCAL, $0, RUN FOR REAL: Metal-vs-Metal self-consistency on
          this Mac's Metal GPU. Renders the tiny canonical config THREE times
          (seed_a, seed_a again, seed_b): per-tile SSIM same-seed = determinism
          ceiling, per-tile SSIM cross-seed = Monte-Carlo noise floor. Exports the
          canonical EXR + hash-pinned manifest the CUDA half reproduces. Ledgered
          MEASURED/local-metal. No pod, no RunPod API, no network beyond the
          (cached) scene download.

  cuda    STAGE 2 — the money-safe CUDA-half driver (standard adversarially-
          verified pattern of run_reference_consistency_probe.py). Prints the
          $-estimate up front (~$0.50 on the A100 rung, built from OUR measured
          basis), refuses to start if any tracked/live pod exists, arms the
          on-pod watchdog FIRST, ships pod/ + the canonical export, runs the
          replica detached, ledgers the result, tears down in finally and asserts
          .tracked_pods.json is empty. --dry-run prints the full manifest +
          estimate with ZERO RunPod API calls. NOT run in the local-only wave —
          the orchestrator sequences it (one pod driver at a time).

  report  STAGE 3 — assembles the gate report from the ledger: same-arch baseline
          vs cross-arch delta, gate_pass = cross-arch worst-tile >= 0.95, every
          number labeled; status PENDING-CUDA-HALF until stage 2 has run. Pure
          logic (pod/exp_cross_arch_gate.gate_report), unit-tested.

USAGE:
  python3 scripts/spec-lab/run_cross_arch_gate.py local              # real $0 run
  python3 scripts/spec-lab/run_cross_arch_gate.py local --dry-run
  python3 scripts/spec-lab/run_cross_arch_gate.py cuda --dry-run     # quote only
  python3 scripts/spec-lab/run_cross_arch_gate.py cuda               # LATER (orchestrator)
  python3 scripts/spec-lab/run_cross_arch_gate.py report
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POD_DIR = os.path.join(HERE, "pod")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, POD_DIR)

# Local-lane machinery reused verbatim from the proven local Metal driver
# (discovery, private python env, shim JSON contract, ledger helpers). NOTE:
# run_local_metal_anchor imports NO cloud module — and neither does this module at
# import time. runpod / the integrated-driver constants are imported ONLY inside
# cmd_cuda(), so `local` and `report` can NEVER touch the RunPod API even by
# accident (unit-tested structurally).
import run_local_metal_anchor as lma  # noqa: E402
import exp_cross_arch_gate as xag  # noqa: E402  (pure helpers + gate_report)

LEDGER = os.path.join(
    REPO_ROOT, "docs", "speed-lane-reports", "spec-lab", "cross_arch_gate_ledger.jsonl")
EVENT_LOCAL = "cross_arch_gate_local_selfconsistency"
EVENT_CUDA_PREFLIGHT = "cross_arch_gate_cuda_preflight"
EVENT_CUDA_RESULT = "cross_arch_gate_cuda_result"
EVIDENCE_LOCAL = "MEASURED/local-metal"

POD_IMAGE_FALLBACK = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
POD_DISK_GB = 120
# Two tiny replica renders finish in minutes; the pod self-terminates at 2h
# regardless of what happens to this process.
WATCHDOG_TTL_S = 7200


def log(*a):
    print("[cross-arch-gate]", *a, file=sys.stderr, flush=True)


def emit(obj):
    """Mirror the runner contract: exactly ONE JSON object as the final stdout line."""
    print(json.dumps(obj), flush=True)


# --------------------------------------------------------------------------- #
# Cost estimate for the CUDA half — from OUR measured basis, labeled honestly.  #
# --------------------------------------------------------------------------- #
def estimate_cuda_cost(n_renders=2, per_render_s_upper=60.0,
                       overhead_min_low=8.0, overhead_min_high=18.0,
                       rate_usd_hr_low=1.42, rate_usd_hr_high=3.02):
    """Up-front $ estimate for the CUDA replica run. PURE (unit-tested).

    Basis, labeled:
      * per_render_s_upper: MEASURED upper bound — the M3 Pro Metal GPU renders the
        canonical 960x540@512spp reference frame in ~34.6s (local_metal_ledger
        2026-07-10); every measured A100 render of this scene has been faster than
        the M3 at equal settings, so 60s/render is a safe ceiling.
      * overhead: MEASURED band from campaign ledgers — pod provision->SSH-ready +
        Blender 4.2 tarball download/extract + scene fetch + pip deps + functional
        GPU probe has landed in ~8-18 min across the 2026-07 A100/H100 runs.
      * rates: MEASURED catalog — A100 SECURE $1.42/hr (cheapest policy rung),
        H100 SECURE ~$3.02/hr (upgrade rung).
    The output is a MODELED estimate from that measured basis — an estimate, never
    a receipt."""
    render_min = n_renders * per_render_s_upper / 60.0
    minutes_low = overhead_min_low + render_min
    minutes_high = overhead_min_high + render_min
    return {
        "label": "MODELED estimate from MEASURED basis (never a receipt)",
        "n_renders": int(n_renders),
        "minutes_low": round(minutes_low, 1),
        "minutes_high": round(minutes_high, 1),
        "usd_low": round(minutes_low / 60.0 * rate_usd_hr_low, 2),
        "usd_high": round(minutes_high / 60.0 * rate_usd_hr_high, 2),
        "headline": f"~${minutes_high / 60.0 * rate_usd_hr_low:.2f} on the A100 rung "
                    f"(worst-case ladder climb to H100: "
                    f"${minutes_high / 60.0 * rate_usd_hr_high:.2f})",
        "basis": {
            "per_render_s_upper": float(per_render_s_upper),
            "per_render_s_measured_m3_metal": 34.6,
            "overhead_min_band": [float(overhead_min_low), float(overhead_min_high)],
            "rate_usd_hr": [float(rate_usd_hr_low), float(rate_usd_hr_high)],
        },
    }


# --------------------------------------------------------------------------- #
# Ledger read helpers (stage 3).                                                #
# --------------------------------------------------------------------------- #
def read_ledger_rows(path=LEDGER):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def latest_success(rows, event, metrics_key):
    """Newest row of `event` whose metrics dict has no 'error' key, else None."""
    for row in reversed(rows):
        if row.get("event") != event:
            continue
        metrics = row.get(metrics_key) or {}
        if isinstance(metrics, dict) and "error" not in metrics and metrics:
            return row
    return None


def assemble_report(rows):
    """STAGE 3: ledger rows -> the honest gate report (pure; unit-tested).

    Recomputes gate_report locally from the two halves' MEASURED blocks; refuses to
    combine halves whose config_hash differs (they rendered different configs)."""
    local_row = latest_success(rows, EVENT_LOCAL, "row")
    if local_row is None:
        return {
            "kind": "cross_arch_gate_report",
            "status": "PENDING-LOCAL-HALF",
            "gate_pass": None,
            "interpretation": "no successful stage-1 (local Metal self-consistency) "
                              "ledger row yet; run `run_cross_arch_gate.py local` first",
        }
    local_metrics = local_row["row"]
    baselines = xag.baselines_from_metrics(local_metrics)

    cuda_row = latest_success(rows, EVENT_CUDA_RESULT, "metrics")
    cross_block = verifier_block = None
    cuda_meta = None
    if cuda_row is not None:
        cuda_metrics = cuda_row["metrics"]
        if cuda_metrics.get("config_hash") != local_metrics.get("config_hash"):
            raise RuntimeError(
                f"refusing to combine halves with different config_hash: local "
                f"{local_metrics.get('config_hash')!r} vs cuda "
                f"{cuda_metrics.get('config_hash')!r} — they rendered different configs")
        cross_block = cuda_metrics.get("cross_arch")
        verifier_block = cuda_metrics.get("verifier_cross_seed")
        cuda_meta = {
            "device": cuda_metrics.get("device"),
            "replica": cuda_metrics.get("replica"),
            "walls_s": cuda_metrics.get("walls_s"),
        }

    report = xag.gate_report(baselines, cross_arch=cross_block,
                             verifier_cross_seed=verifier_block)
    report["config_hash"] = local_metrics.get("config_hash")
    report["config"] = local_metrics.get("config")
    report["local_half"] = {
        "evidence": local_row.get("evidence"),
        "device": local_metrics.get("device"),
        "producer": local_metrics.get("producer"),
        "ts": local_row.get("ts"),
    }
    report["cuda_half"] = cuda_meta
    return report


# --------------------------------------------------------------------------- #
# STAGE 1 — the local Metal run (shim into pod/exp_cross_arch_gate.py).          #
# --------------------------------------------------------------------------- #
# Env-driven shim (same pattern as run_local_metal_anchor.SHIM_SOURCE): imports the
# pod runner, repoints its pod-rooted constants to local-Mac paths, then runs its
# main() under its own error contract (last stdout line is ONE JSON object).
SHIM_SOURCE = r'''
import json, os, sys

POD_DIR = os.environ["CX_SHIM_POD_DIR"]
sys.path.insert(0, POD_DIR)
import exp_render_stack as ers  # noqa: E402
import exp_cross_arch_gate as xag  # noqa: E402

# Repoint the pod-rooted constants to the local Mac (module globals are read at
# call time, so patching here reaches every helper; SCENES_DIR is derived from
# _CACHE_ROOT at import time, so it is patched explicitly too).
ers.BLENDER_DIR = os.environ["CX_SHIM_BLENDER_DIR"]
ers.BLENDER_BIN = os.environ["CX_SHIM_BLENDER_BIN"]
ers.WORK_DIR    = os.environ["CX_SHIM_WORK_DIR"]
ers._CACHE_ROOT = os.environ["CX_SHIM_CACHE_ROOT"]
ers.SCENES_DIR  = os.environ["CX_SHIM_SCENES_DIR"]
xag.WORK_DIR    = os.environ["CX_SHIM_XAG_WORK_DIR"]
xag.EXPORT_ROOT = os.environ["CX_SHIM_XAG_EXPORT_ROOT"]

sys.argv = ["exp_cross_arch_gate.py", os.environ["CX_SHIM_CONFIG_JSON"]]

try:
    xag.main()
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc(file=sys.stderr)
    xag.emit({"error": f"{type(e).__name__}: {e}"})
    sys.exit(0)
'''


def build_local_config(args):
    """The tiny local stage-1 config. require_gpu=True: a missing Metal GPU fails
    LOUD, never a silent CPU baseline."""
    return {
        "mode": "self_consistency",
        "scene": args.scene,
        "resolution": args.resolution,
        "frame": args.frame,
        "nframes": args.nframes,
        "ref_spp": args.ref_spp,
        "bounces": 12,
        "cam_motion": 1.0,
        "seed_a": args.seed_a,
        "seed_b": args.seed_b,
        "device": "GPU",
        "require_gpu": True,
        "export": True,
    }


def run_local_shim(config, blender_bin, python_bin, extra_env, cache_root,
                   timeout_s=7200, runner=subprocess.run):
    """Run pod/exp_cross_arch_gate.py via the shim on the LOCAL Blender."""
    work_dir = os.path.join(cache_root, "work", "cross_arch_gate")
    os.makedirs(work_dir, exist_ok=True)
    shim_path = os.path.join(work_dir, "cx_cross_arch_shim.py")
    with open(shim_path, "w") as f:
        f.write(SHIM_SOURCE)

    export_root = os.path.join(cache_root, "cross_arch_export")
    config = dict(config, export_root=export_root)

    env = dict(os.environ)
    env.update(extra_env or {})
    env.update({
        "CX_SHIM_POD_DIR": POD_DIR,
        "CX_SHIM_BLENDER_DIR": os.path.dirname(blender_bin),
        "CX_SHIM_BLENDER_BIN": blender_bin,
        "CX_SHIM_WORK_DIR": os.path.join(work_dir, "render_stack"),
        "CX_SHIM_CACHE_ROOT": cache_root,
        "CX_SHIM_SCENES_DIR": os.path.join(cache_root, "scenes"),
        "CX_SHIM_XAG_WORK_DIR": work_dir,
        "CX_SHIM_XAG_EXPORT_ROOT": export_root,
        "CX_SHIM_CONFIG_JSON": json.dumps(config),
    })
    log(f"launching shim: python={python_bin} blender={blender_bin}")
    log(f"config: {json.dumps(config)}")
    # stderr is INHERITED so live render progress streams through; stdout is
    # captured for the single metrics line.
    proc = runner([python_bin, shim_path], env=env, stdout=subprocess.PIPE,
                  stderr=None, text=True, timeout=timeout_s)
    metrics = lma.parse_last_json_line(proc.stdout)
    if metrics is None:
        return {"error": f"shim produced no JSON metrics line (rc={proc.returncode}, "
                         f"stdout tail: {(proc.stdout or '')[-400:]!r})"}
    return metrics


def cmd_local(args):
    config = build_local_config(args)
    blender_bin = lma.discover_blender(explicit=args.blender)
    if blender_bin is None:
        emit({
            "error": "no local Blender binary found (checked --blender, "
                     f"${lma.BLENDER_ENV_VAR}, {lma.DEFAULT_BLENDER_CANDIDATES[0]}, "
                     "and `blender` on PATH). Install Blender 4.2 LTS (macOS Apple "
                     "Silicon) then rerun.",
            "status": "PENDING-OWNER-HARDWARE",
        })
        return 0
    ver = lma.blender_version(blender_bin)
    log(f"local Blender: {blender_bin} ({ver})")

    if args.dry_run:
        emit({"dry_run": True, "blender_bin": blender_bin, "blender_version": ver,
              "config": config, "ledger": args.ledger, "cache_root": args.cache_root})
        return 0

    os.makedirs(args.cache_root, exist_ok=True)
    try:
        python_bin, extra_env, env_note = lma.ensure_python_env(args.cache_root)
    except RuntimeError as e:
        emit({"error": f"python env for the shim could not be provisioned: {e}"})
        return 0
    log(f"shim python: {python_bin} ({env_note})")

    try:
        metrics = run_local_shim(config, blender_bin, python_bin, extra_env,
                                 args.cache_root, timeout_s=args.timeout_s)
    except subprocess.TimeoutExpired:
        emit({"error": f"local stage-1 run timed out after {args.timeout_s}s"})
        return 0

    if "error" in metrics:
        emit(metrics)  # honest failure, verbatim; nothing is ledgered
        return 0

    device = str(metrics.get("device", ""))
    if "METAL" not in device.upper():
        # A receipt not traced on the Metal GPU must NEVER be labeled local-metal.
        emit({"error": f"run completed but device={device!r} is not a Metal GPU; "
                       "refusing to ledger a mislabeled receipt", "row": metrics})
        return 0

    metrics_out = dict(metrics)
    metrics_out["evidence"] = EVIDENCE_LOCAL
    if not args.no_ledger:
        row = {
            "event": EVENT_LOCAL,
            "evidence": EVIDENCE_LOCAL,
            "row": metrics,
            "config": config,
            "host": lma.host_info(blender_bin, ver),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        lma.append_ledger(args.ledger, row)
        log(f"ledger row appended -> {args.ledger}")
    emit(metrics_out)
    return 0


# --------------------------------------------------------------------------- #
# STAGE 2 — the money-safe CUDA-half driver (NOT run in the local-only wave).    #
# --------------------------------------------------------------------------- #
def resolve_export_dir(args_export_dir, rows):
    """--export-dir wins; else the newest successful stage-1 row's export_dir."""
    if args_export_dir:
        return args_export_dir
    local_row = latest_success(rows, EVENT_LOCAL, "row")
    if local_row is None:
        raise RuntimeError(
            "no --export-dir given and no successful stage-1 ledger row to take it "
            "from; run `run_cross_arch_gate.py local` first")
    export_dir = local_row["row"].get("export_dir")
    if not export_dir:
        raise RuntimeError("stage-1 ledger row has no export_dir (export disabled?)")
    return export_dir


def cmd_cuda(args):
    """Standard money-safe pattern (adversarially verified in
    run_reference_consistency_probe.py / run_integrated_production_benchmark.py):
    preflight refuses concurrent pods, watchdog is armed FIRST on the pod, the
    replica runs detached, teardown in finally + tracked-pods-empty assertion."""
    rows = read_ledger_rows(args.ledger)
    export_dir = resolve_export_dir(args.export_dir, rows)
    manifest_path = os.path.join(export_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise RuntimeError(f"manifest not found at {manifest_path}")
    with open(manifest_path) as f:
        manifest = json.load(f)
    exr_local = os.path.join(export_dir, manifest.get("exr_file", ""))
    if not os.path.isfile(exr_local):
        raise RuntimeError(f"canonical EXR not found at {exr_local}")
    # Fail-closed BEFORE any spend: the export must validate exactly as the pod will.
    cfg = xag.validate_manifest(manifest, exr_sha256=xag.sha256_file(exr_local))
    chash = manifest["config_hash"]

    n_renders = 2 if args.render_cross_seed_baseline else 1
    estimate = estimate_cuda_cost(n_renders=n_renders)
    log(f"COST ESTIMATE (printed before anything cloud-touching): {estimate['headline']}")
    log(f"estimate detail: {json.dumps(estimate)}")

    remote_export = f"/root/spec-lab/cross_arch_export/{chash}"
    replica_config = {
        "mode": "replica",
        "manifest_path": f"{remote_export}/manifest.json",
        "render_cross_seed_baseline": bool(args.render_cross_seed_baseline),
        "device": "GPU",
        "require_gpu": True,
        # sm_90 first-render JIT headroom (same rationale + value as the integrated
        # driver; the functional probe absorbs the one-time JIT).
        "gpu_probe_timeout_s": 1500,
    }

    # Lazy cloud imports: `local`/`report` never load these modules, so the
    # local-only wave structurally cannot touch the RunPod API.
    import runpod  # noqa: PLC0415
    from run_integrated_production_benchmark import (  # noqa: PLC0415
        DEFAULT_GPU_PLAN, POD_IMAGE, parse_final_json, parse_gpu_plan)
    from run_spec_render_token_end_to_end import append_jsonl  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    gpu_plan = parse_gpu_plan(args.gpu_plan or DEFAULT_GPU_PLAN,
                              allow_unsupported=False)
    pod_image = POD_IMAGE or POD_IMAGE_FALLBACK
    manifest_out = {
        "gpu_plan": gpu_plan,
        "pod_image": pod_image,
        "replica_config": replica_config,
        "config": cfg,
        "config_hash": chash,
        "export_dir": export_dir,
        "estimate": estimate,
        "timeout_s": args.max_minutes * 60,
        "watchdog_ttl_s": WATCHDOG_TTL_S,
        "ledger": args.ledger,
    }
    if args.dry_run:
        print(json.dumps(manifest_out, indent=2, sort_keys=True))
        return 0

    ledger_path = Path(args.ledger)
    runpod.register_cleanup()
    tracked = runpod._load_tracked()
    live = runpod.live_pods()
    balance = runpod.balance()
    if tracked or live:
        raise RuntimeError(
            f"refusing cloud run with existing pods: tracked={tracked}, live={live} "
            f"(one driver at a time — concurrent drivers kill each other's pods)")
    if float(balance["clientBalance"]) <= args.min_balance:
        raise RuntimeError(
            f"balance ${balance['clientBalance']:.2f} is below floor ${args.min_balance:.2f}")
    append_jsonl(ledger_path, {
        "event": EVENT_CUDA_PREFLIGHT,
        "preflight": {"tracked": tracked, "live": live, "balance": balance,
                      "gpu_plan": gpu_plan, "estimate": estimate,
                      "replica_config": replica_config, "config_hash": chash},
    })
    log(f"preflight clear; balance ${balance['clientBalance']:.2f}; provisioning")

    try:
        pod = runpod.provision_reachable(gpu_plan, pod_image, disk_gb=POD_DISK_GB,
                                         name=args.job_id)
    except Exception as exc:
        append_jsonl(ledger_path, {"event": "cross_arch_gate_cuda_provision_pruned",
                                   "reason": str(exc), "gpu_plan": gpu_plan})
        raise

    try:
        # FIRST action on the pod: the self-terminate backstop.
        runpod.arm_remote_watchdog(pod, WATCHDOG_TTL_S)
        rc, _, err = runpod.ssh(
            pod, "mkdir -p /root/spec-lab/cross_arch_export", timeout=60)
        if rc != 0:
            raise RuntimeError(f"remote mkdir failed: {err[-200:]}")
        ok, error = runpod.scp_to(pod, POD_DIR, "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"pod runner transfer failed: {error[-200:]}")
        ok, error = runpod.scp_to(pod, export_dir, "/root/spec-lab/cross_arch_export/")
        if not ok:
            raise RuntimeError(f"canonical export transfer failed: {error[-200:]}")
        rc, out, err = runpod.ssh(
            pod,
            "pip install --break-system-packages --no-cache-dir "
            "OpenEXR Imath imageio numpy pillow scikit-image 2>&1 | tail -2",
            timeout=600,
        )
        if rc != 0:
            raise RuntimeError(f"remote dependencies failed: {(out + err)[-300:]}")

        payload = json.dumps(replica_config).replace("'", "'\\''")
        cmd = "python3 pod/exp_cross_arch_gate.py '" + payload + "'"
        # Detached-on-pod + short reconnecting polls (2026-07-09 peer-reset incident
        # standard) even though this run is short — uniform hardening, zero cost.
        rc, out, err = runpod.ssh_detached(
            pod, cmd, workdir="/root/spec-lab", tag="cross-arch-gate",
            timeout_s=args.max_minutes * 60, poll_every=20)
        metrics = parse_final_json(out or "", err or "", rc)
        append_jsonl(ledger_path, {
            "event": EVENT_CUDA_RESULT,
            "metrics": metrics,
            "replica_config": replica_config,
            "config_hash": chash,
            "pod": {k: pod.get(k) for k in ("id", "gpu", "cloud", "cuda_driver_version")},
        })
        if "error" in metrics:
            raise RuntimeError(f"replica run failed (ledgered): {metrics['error']}")
        report = metrics.get("gate_report") or {}
        log(f"cross_arch worst_tile={report.get('cross_arch_worst_tile')} "
            f"gate_pass={report.get('gate_pass')} "
            f"within_noise={report.get('cross_arch_within_same_arch_noise')}")
        log(str(report.get("interpretation")))
        print(json.dumps(metrics, sort_keys=True))
    finally:
        runpod.terminate_all_tracked()
        remaining = runpod._load_tracked()
        if remaining:
            raise RuntimeError(f"teardown incomplete; tracked pods remain: {remaining}")
        log("pod teardown confirmed")
    return 0


# --------------------------------------------------------------------------- #
# STAGE 3 — the gate report.                                                    #
# --------------------------------------------------------------------------- #
def cmd_report(args):
    rows = read_ledger_rows(args.ledger)
    report = assemble_report(rows)
    emit(report)
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("local", help="STAGE 1: Metal-vs-Metal self-consistency ($0, real)")
    pl.add_argument("--blender", help="explicit local Blender binary")
    pl.add_argument("--cache-root", default=lma.DEFAULT_CACHE_ROOT)
    pl.add_argument("--ledger", default=LEDGER)
    pl.add_argument("--no-ledger", action="store_true")
    pl.add_argument("--dry-run", action="store_true",
                    help="discovery + config only; do not render")
    pl.add_argument("--timeout-s", type=int, default=7200)
    pl.add_argument("--scene", default="classroom")
    pl.add_argument("--resolution", default="960x540")
    pl.add_argument("--frame", type=int, default=1)
    pl.add_argument("--nframes", type=int, default=2,
                    help="camera-path length (frame 1 sits at the path origin for "
                         "any nframes; 2 matches the local-metal lane convention)")
    pl.add_argument("--ref-spp", type=int, default=512,
                    help="canonical reference spp (tiny-config band 256-512)")
    pl.add_argument("--seed-a", type=int, default=0)
    pl.add_argument("--seed-b", type=int, default=12345)
    pl.set_defaults(func=cmd_local)

    pc = sub.add_parser("cuda", help="STAGE 2: money-safe CUDA replica (fired LATER "
                                     "by the orchestrator; --dry-run is free)")
    pc.add_argument("--job-id", default="cx-cross-arch-gate")
    pc.add_argument("--export-dir",
                    help="stage-1 export dir (default: newest stage-1 ledger row)")
    pc.add_argument("--ledger", default=LEDGER)
    pc.add_argument("--gpu-plan", default=None,
                    help="override the policy ladder (default: the integrated "
                         "driver's A100->H100->H200 plan; Blackwell rejected)")
    pc.add_argument("--max-minutes", type=int, default=60)
    pc.add_argument("--min-balance", type=float, default=2.0)
    pc.add_argument("--render-cross-seed-baseline", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="also render seed_b on the pod for the verifier-side "
                         "cross-seed noise floor (one extra tiny render)")
    pc.add_argument("--dry-run", action="store_true",
                    help="print the manifest + $-estimate and exit; ZERO API calls")
    pc.set_defaults(func=cmd_cuda)

    pr = sub.add_parser("report", help="STAGE 3: assemble the gate report from the ledger")
    pr.add_argument("--ledger", default=LEDGER)
    pr.set_defaults(func=cmd_report)

    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
