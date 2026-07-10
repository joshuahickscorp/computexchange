#!/usr/bin/env python3
"""
tuner.py — the autonomous speed/quality tuning LOOP for the spec-lab winners.

Goal (owner): maximize speedup while staying near-LOSSLESS. So the objective is a
CONSTRAINED optimization, optimized separately at each quality tier:

    maximize net_speedup  subject to  quality (SSIM) >= q_floor

The loop, per target (temporal reuse, transcode, …), on ONE money-safe GPU:
  PHASE 1  OFAT — sweep each knob alone off the base config (find each knob's shape).
  PHASE 2  COORDINATE ASCENT — from the best feasible OFAT point at each q_tier, climb:
           repeatedly move each knob to its best neighbor until a full pass stops
           improving (a local optimum for that tier). Deterministic.
  PHASE 3  REFINE-UNTIL-BUDGET — while balance/time remain, restart coordinate ascent
           from other Pareto points and explore around the incumbents; keep only real
           improvements. This is the "just keep going" loop.

Everything streams to docs/speed-lane-reports/spec-lab/tuning_ledger.jsonl (resumable:
a (target, config-hash) already measured is not re-run). The output is, per target:
the best config at each quality tier + the full speed/quality Pareto frontier.

Money-safety is inherited from runpod.py (tracked pods, teardown on every exit, a hard
deadline watchdog). It ALSO stops when the balance falls below --min-balance (default
$4) so it never drains the account — the owner said "exhaust if necessary" but a floor
keeps it honest and recoverable.

Fire:  RUNPOD_API_KEY=... python3 scripts/spec-lab/tuner.py [--max-minutes N] [--min-balance 4] [--only temporal,transcode]
"""

import argparse
import json
import os
import threading
import time
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runpod  # noqa: E402
import tuning_spaces as ts  # noqa: E402
import experiments as exp  # noqa: E402  (GPU_PLAN / POD_IMAGE / POD_DISK_GB)

LEDGER_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "docs", "speed-lane-reports", "spec-lab"))
LEDGER = os.path.join(LEDGER_DIR, "tuning_ledger.jsonl")


def _now():
    return datetime.now(timezone.utc).isoformat()


def log(m):
    print(f"[tuner {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(LEDGER_DIR, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": _now(), **rec}) + "\n")


def cfg_key(target_id, cfg):
    return target_id + "|" + json.dumps(cfg, sort_keys=True)


def load_cache():
    """(target,config)->metrics for every trial already recorded (resume/no-repeat)."""
    cache = {}
    if os.path.exists(LEDGER):
        for line in open(LEDGER):
            try:
                r = json.loads(line)
                if r.get("event") == "trial":
                    cache[cfg_key(r["target"], r["config"])] = r["metrics"]
            except Exception:
                pass
    return cache


# ---- watchdog ------------------------------------------------------------------

def start_deadline_watchdog(max_minutes):
    deadline = time.time() + max_minutes * 60

    def _w():
        while time.time() < deadline:
            time.sleep(5)
        log(f"DEADLINE {max_minutes}m — teardown + exit")
        runpod.terminate_all_tracked()
        os._exit(2)

    threading.Thread(target=_w, daemon=True).start()


# ---- trial exec ----------------------------------------------------------------

def run_trial(pod, target, cfg, cache):
    key = cfg_key(target["id"], cfg)
    if key in cache:
        return cache[key]
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = (f"cd /root/spec-lab && export HF_HOME=/models/hf HF_HUB_ENABLE_HF_TRANSFER=0; "
           f"python3 pod/{target['runner']} '{payload}'")
    rc, out, err = runpod.ssh(pod, cmd, timeout=target.get("timeout_s", 900))
    tail = ""
    for ln in reversed(out.strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            tail = ln
            break
    if rc != 0 or not tail:
        m = {"error": f"rc={rc}", "stderr_tail": (err or out)[-300:]}
    else:
        try:
            m = json.loads(tail)
        except Exception as e:
            m = {"error": f"bad json: {e}"}
    cache[key] = m
    ledger_append({"event": "trial", "target": target["id"], "config": cfg, "metrics": m})
    sp = m.get("net_speedup"); q = m.get("quality")
    log(f"    {target['id']} {cfg} -> speedup={sp} q={q}" + ("  ERR" if "error" in m else ""))
    return m


def feasible_speedup(m, qfloor):
    if not isinstance(m, dict) or "error" in m:
        return float("-inf")
    if m.get("quality") is None or m.get("net_speedup") is None:
        return float("-inf")
    return m["net_speedup"] if m["quality"] >= qfloor else float("-inf")


# ---- search --------------------------------------------------------------------

def ofat(pod, target, cache):
    """Sweep each knob alone off the base config. Returns list of (cfg, metrics)."""
    base = dict(target["base"])
    trials = [(dict(base), run_trial(pod, target, dict(base), cache))]
    # primary knob first (densest signal), then the rest
    knobs = [target["primary_knob"]] + [k for k in target["space"] if k != target["primary_knob"]]
    for knob in knobs:
        for val in target["space"][knob]:
            cfg = dict(base); cfg[knob] = val
            if cfg == base:
                continue
            trials.append((cfg, run_trial(pod, target, cfg, cache)))
    return trials


def best_feasible(trials, qfloor):
    best, bestm = None, float("-inf")
    for cfg, m in trials:
        s = feasible_speedup(m, qfloor)
        if s > bestm:
            best, bestm = cfg, s
    return best, bestm


def coordinate_ascent(pod, target, qfloor, start_cfg, cache, budget_ok):
    """From start_cfg, move one knob at a time to its best feasible neighbor value
    until a full pass yields no improvement. Returns (best_cfg, best_speedup)."""
    cur = dict(start_cfg)
    cur_s = feasible_speedup(run_trial(pod, target, cur, cache), qfloor)
    improved = True
    while improved and budget_ok():
        improved = False
        for knob, values in target["space"].items():
            for val in values:
                if not budget_ok():
                    return cur, cur_s
                if cur.get(knob) == val:
                    continue
                cand = dict(cur); cand[knob] = val
                s = feasible_speedup(run_trial(pod, target, cand, cache), qfloor)
                if s > cur_s + 1e-9:
                    cur, cur_s, improved = cand, s, True
    return cur, cur_s


def pareto_front(cache, target_id):
    """The non-dominated (quality, speedup) points for a target — the tradeoff curve."""
    pts = []
    for k, m in cache.items():
        if k.startswith(target_id + "|") and isinstance(m, dict) and "error" not in m \
           and m.get("quality") is not None and m.get("net_speedup") is not None:
            pts.append((m["quality"], m["net_speedup"], k.split("|", 1)[1]))
    front = []
    for q, s, c in sorted(pts, key=lambda t: (-t[0], -t[1])):
        if all(not (fq >= q and fs >= s and (fq, fs) != (q, s)) for fq, fs, _ in front):
            front.append((q, s, c))
    return sorted(front)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=int, default=int(os.environ.get("TUNER_MAX_MIN", "180")))
    ap.add_argument("--min-balance", type=float, default=float(os.environ.get("TUNER_MIN_BAL", "4")))
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    targets = ts.TARGETS
    if args.only:
        want = set(args.only.split(","))
        targets = [t for t in targets if t["id"] in want]

    runpod.register_cleanup()
    start_deadline_watchdog(args.max_minutes)
    deadline = time.time() + args.max_minutes * 60
    bal0 = runpod.balance()
    log(f"balance ${bal0['clientBalance']:.2f}; floor ${args.min_balance}; max {args.max_minutes}m; targets {[t['id'] for t in targets]}")

    _bal = {"v": bal0["clientBalance"], "checked": time.time()}

    def budget_ok():
        # Re-check balance at most every ~45s (cheap; the API call has cost/latency).
        if time.time() - _bal["checked"] > 45:
            try:
                _bal["v"] = runpod.balance()["clientBalance"]
            except Exception:
                pass
            _bal["checked"] = time.time()
        return time.time() < deadline and _bal["v"] > args.min_balance

    log("provisioning a reachable CUDA GPU…")
    pod = runpod.provision_reachable(exp.GPU_PLAN, exp.POD_IMAGE, disk_gb=exp.POD_DISK_GB)
    ledger_append({"event": "pod_up", "pod": pod})
    log(f"pod {pod['gpu']} {pod['id']} @ {pod['ip']}:{pod['port']}")

    cache = load_cache()
    try:
        ok, serr = runpod.scp_to(pod, os.path.join(HERE, "pod"), "/root/spec-lab/") if \
            runpod.ssh(pod, "mkdir -p /root/spec-lab")[0] == 0 else (False, "mkdir failed")
        if not ok:
            raise RuntimeError(f"scp pod/ failed: {serr[:200]}")
        rc, out, err = runpod.ssh(pod, "bash /root/spec-lab/pod/setup_base.sh", timeout=1800)
        if rc != 0:
            raise RuntimeError(f"setup failed rc={rc}: {(err or out)[-300:]}")
        log("base setup done.")

        for target in targets:
            if not budget_ok():
                break
            log(f"=== TARGET {target['id']}: OFAT sweep ===")
            trials = ofat(pod, target, cache)
            # Coordinate-ascent at each quality tier, from that tier's best OFAT point.
            tier_best = {}
            for qf in target["q_tiers"]:
                if not budget_ok():
                    break
                start, s0 = best_feasible(trials, qf)
                if start is None:
                    log(f"  tier q>={qf}: no feasible OFAT point — skipping")
                    tier_best[qf] = None
                    continue
                log(f"  tier q>={qf}: coordinate ascent from {start} (ofat best {s0:.2f}x)")
                bcfg, bs = coordinate_ascent(pod, target, qf, start, cache, budget_ok)
                tier_best[qf] = {"config": bcfg, "speedup": bs}
                log(f"  tier q>={qf}: BEST {bs:.2f}x @ {bcfg}")
                ledger_append({"event": "tier_best", "target": target["id"], "q_floor": qf,
                               "speedup": bs if bs != float('-inf') else None, "config": bcfg})
            front = pareto_front(cache, target["id"])
            ledger_append({"event": "pareto", "target": target["id"],
                           "front": [{"quality": q, "speedup": s} for q, s, _ in front]})
            log(f"  {target['id']} Pareto frontier ({len(front)} pts): " +
                ", ".join(f"q{q:.2f}:{s:.1f}x" for q, s, _ in front[-6:]))

        # PHASE 3 — refine-until-budget: extra coordinate-ascent restarts from Pareto
        # points for the hardest (most lossless) tier, exploring for further gains.
        rounds = 0
        while budget_ok():
            rounds += 1
            progressed = False
            for target in targets:
                if not budget_ok():
                    break
                qf = target["q_tiers"][0]  # the most-lossless tier
                front = pareto_front(cache, target["id"])
                # restart from the 2 highest-quality feasible frontier points
                seeds = [json.loads(c) for q, s, c in front if q >= qf][:2]
                for seed in seeds:
                    if not budget_ok():
                        break
                    before = best_feasible([(seed, cache.get(cfg_key(target["id"], seed), {}))], qf)[1]
                    bcfg, bs = coordinate_ascent(pod, target, qf, seed, cache, budget_ok)
                    if bs > before + 1e-9:
                        progressed = True
                        log(f"  refine[{target['id']} q>={qf}]: {bs:.2f}x @ {bcfg}")
                        ledger_append({"event": "tier_best", "target": target["id"], "q_floor": qf,
                                       "speedup": bs, "config": bcfg, "refine_round": rounds})
            if not progressed:
                log(f"refine round {rounds}: no improvement — tuning converged.")
                break
        log("tuning loop complete.")
    finally:
        log("tearing down pod…")
        runpod.terminate(pod["id"])
        b2 = runpod.balance()
        ledger_append({"event": "pod_down", "balance_after": b2["clientBalance"]})
        log(f"pod down. balance ${b2['clientBalance']:.2f}")


if __name__ == "__main__":
    main()
