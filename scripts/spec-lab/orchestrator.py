#!/usr/bin/env python3
"""
orchestrator.py — the deterministic, self-driving spec-lab experiment engine.

What it does, hands-off, on borrowed GPU time:
  1. Provisions ONE reachable GPU (runpod.provision_reachable), money-safe.
  2. Runs the deterministic base setup on it (idempotent).
  3. Walks the experiment LADDER (experiments.LADDER) in order. For each rung:
       run → measure → check the viability BAR →
         PASS  → record, go to the next rung automatically.
         FAIL  → walk the rung's REMEDIATIONS in order (aggressive auto-improve:
                 re-run with injected param overrides) until one PASSES or they run
                 out; then follow the rung's on_fail edge (advance | stop | skip-track).
  4. Appends EVERY attempt to a resumable JSONL ledger under
     docs/speed-lane-reports/spec-lab/ (real measured numbers, the standing discipline).
  5. A hard wall-clock DEADLINE watchdog + register_cleanup() guarantee the pod is
     always torn down — deadline hit, exception, Ctrl-C, or normal finish.

Determinism: same ladder + same seeds ⇒ same transitions. Resume: a rung already
recorded PASS in the ledger is skipped, so a re-run continues where it stopped without
re-billing finished work. This is the "one thing finishes → the next fires" the owner
asked for, with the money-safety the borrowed time demands.

Fire it:  RUNPOD_API_KEY=... python3 scripts/spec-lab/orchestrator.py [--max-minutes N] [--only ID,ID]
Dry-run the DAG logic with no GPU:  python3 scripts/spec-lab/orchestrator.py --dry-run
"""

import argparse
import json
import os
import shlex
import threading
import time
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runpod  # noqa: E402
import experiments as exp  # noqa: E402

LEDGER_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "speed-lane-reports", "spec-lab"))
LEDGER = os.path.join(LEDGER_DIR, "ledger.jsonl")


def _now():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[spec-lab {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ledger_append(rec):
    os.makedirs(LEDGER_DIR, exist_ok=True)
    rec = {"ts": _now(), **rec}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def ledger_passed_ids():
    if not os.path.exists(LEDGER):
        return set()
    passed = set()
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "rung_result" and r.get("passed"):
                    passed.add(r["id"])
            except Exception:
                pass
    return passed


def setup_env_exports():
    keys = ("SPEC_LAB_VLLM_VERSION", "SPEC_LAB_TRANSFORMERS_VERSION")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def remote_env_prefix(env):
    if not env:
        return ""
    return " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items())) + " "


def min_cuda_driver_version():
    raw = os.environ.get("SPEC_LAB_MIN_CUDA_DRIVER", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise SystemExit(f"SPEC_LAB_MIN_CUDA_DRIVER must be an integer CUDA driver API version: {e}")


# ---- watchdog: hard money-safe deadline ---------------------------------------

def start_deadline_watchdog(max_minutes):
    deadline = time.time() + max_minutes * 60

    def _watch():
        while time.time() < deadline:
            time.sleep(5)
        log(f"DEADLINE {max_minutes}m hit — force teardown + exit")
        runpod.terminate_all_tracked()
        os._exit(2)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return deadline


# ---- one rung ------------------------------------------------------------------

def run_runner(pod, rung, params):
    """Run a rung's pod-side runner with `params`, return parsed metrics dict.

    Contract: each runner prints exactly one JSON object as its LAST stdout line —
    the metrics. Non-zero exit or unparseable tail ⇒ {'error': ...} (a hard fail
    the bar will reject, triggering remediation)."""
    # Sync the growing ledger to the pod so ledger-aware runners (e.g. the cost
    # model) can read THIS run's already-measured net_speedups. Best-effort.
    if os.path.exists(LEDGER):
        runpod.scp_to(pod, LEDGER, "/root/spec-lab/ledger.jsonl")
    payload = json.dumps(params).replace("'", "'\\''")
    cmd = (f"cd /root/spec-lab && source $HOME/.cargo/env 2>/dev/null; "
           f"export HF_HOME=/models/hf HF_HUB_ENABLE_HF_TRANSFER=0; "
           f"python3 pod/{rung['runner']} '{payload}'")
    rc, out, err = runpod.ssh(pod, cmd, timeout=rung.get("timeout_s", 1800))
    tail = ""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            tail = line
            break
    if rc != 0 or not tail:
        return {"error": f"rc={rc}", "stderr_tail": (err or out)[-400:]}
    try:
        return json.loads(tail)
    except Exception as e:
        return {"error": f"bad metrics json: {e}", "raw": tail[:400]}


def run_rung(pod, rung):
    """Run a rung with its base params, then its remediations until one passes.
    Returns (passed: bool, best_metrics: dict, attempts: list)."""
    attempts = []
    base = dict(rung.get("params", {}))
    variants = [("base", base)] + [
        (rem.get("name", f"remedy{i}"), {**base, **rem["override"]})
        for i, rem in enumerate(rung.get("remediations", []))
    ]
    best = None
    for variant_name, params in variants:
        log(f"  {rung['id']} [{variant_name}] running…")
        m = run_runner(pod, rung, params)
        ok = False
        try:
            ok = bool(rung["bar"](m))
        except Exception:
            ok = False
        attempts.append({"variant": variant_name, "params": params, "metrics": m, "passed": ok})
        ledger_append({"event": "attempt", "id": rung["id"], "variant": variant_name,
                       "params": params, "metrics": m, "passed": ok})
        log(f"  {rung['id']} [{variant_name}] → {m if 'error' in m else _fmt(m)}  {'PASS' if ok else 'miss'}")
        if best is None or _score(m) > _score(best):
            best = m
        if ok:
            return True, m, attempts
    return False, best, attempts


def _fmt(m):
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}


def _score(m):
    if not isinstance(m, dict) or "error" in m:
        return -1
    return m.get("speedup", 0) or m.get("acceptance", 0) or m.get("quality", 0) or 0


# ---- the driver ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=int(os.environ.get("SPEC_LAB_MAX_MIN", "180")))
    ap.add_argument("--only", default="", help="comma-sep rung ids to run (default: full ladder)")
    ap.add_argument("--dry-run", action="store_true", help="print the DAG + bars, no GPU")
    args = ap.parse_args()

    ladder = exp.LADDER
    if args.only:
        want = set(args.only.split(","))
        ladder = [r for r in ladder if r["id"] in want]

    if args.dry_run:
        print(f"Spec-lab ladder — {len(ladder)} rungs across tracks "
              f"{sorted(set(r['track'] for r in exp.LADDER))}:")
        for r in ladder:
            print(f"  [{r['track']}] {r['id']:<10} {r['description']}")
            print(f"       bar: {r.get('bar_desc','—')}  |  remediations: "
                  f"{[rem.get('name') for rem in r.get('remediations', [])]}  |  on_fail: {r.get('on_fail','advance')}")
        return

    runpod.register_cleanup()
    start_deadline_watchdog(args.max_minutes)
    bal = runpod.balance()
    log(f"balance ${bal['clientBalance']:.2f}, spend ${bal['currentSpendPerHr']}/hr; max {args.max_minutes}m")

    min_driver = min_cuda_driver_version()
    if min_driver is not None:
        log(f"requiring CUDA driver API >= {min_driver}")

    log("provisioning a REACHABLE GPU…")
    pod = runpod.provision_reachable(
        exp.GPU_PLAN,
        exp.POD_IMAGE,
        disk_gb=exp.POD_DISK_GB,
        min_cuda_driver_version=min_driver,
    )
    ledger_append({"event": "pod_up", "pod": pod})
    log(f"pod up: {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    try:
        log("deterministic base setup on pod (idempotent)…")
        runpod.ssh(pod, "mkdir -p /root/spec-lab")  # scp needs the parent to exist
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        setup_env = setup_env_exports()
        if setup_env:
            log(f"setup override: {setup_env}")
        setup_cmd = remote_env_prefix(setup_env) + "bash /root/spec-lab/pod/setup_base.sh"
        rc, out, err = runpod.ssh(pod, setup_cmd, timeout=1800)
        ledger_append({"event": "setup", "rc": rc, "setup_env": setup_env, "tail": (out + err)[-500:]})
        if rc != 0:
            raise RuntimeError(f"base setup failed rc={rc}: {(err or out)[-300:]}")
        log("base setup done.")

        done = ledger_passed_ids()
        for rung in ladder:
            if rung["id"] in done:
                log(f"{rung['id']} already PASSED (ledger) — skipping (resume).")
                continue
            log(f"RUNG {rung['id']} [{rung['track']}] — {rung['description']}")
            passed, best, attempts = run_rung(pod, rung)
            ledger_append({"event": "rung_result", "id": rung["id"], "track": rung["track"],
                           "passed": passed, "best_metrics": best, "n_attempts": len(attempts)})
            log(f"RUNG {rung['id']} → {'PASS' if passed else 'FAIL(all remediations)'}")
            if not passed and rung.get("on_fail") == "stop":
                log(f"{rung['id']} failed and on_fail=stop — halting the ladder honestly.")
                break
        log("ladder complete.")
    finally:
        log("tearing down pod (money-safety)…")
        runpod.terminate(pod["id"])
        b2 = runpod.balance()
        ledger_append({"event": "pod_down", "pod_id": pod["id"], "balance_after": b2["clientBalance"]})
        log(f"pod down. balance ${b2['clientBalance']:.2f}")


if __name__ == "__main__":
    main()
