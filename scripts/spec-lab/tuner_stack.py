#!/usr/bin/env python3
"""
tuner_stack.py — the autonomous coordinate-ascent tuner for the KEYSTONE compound
(pod/exp_render_stack.py), the ONE end-to-end render-stack ratio.

It reuses tuner.py's algorithm SHAPE (OFAT -> coordinate-ascent-per-quality-tier ->
refine-until-budget), but with a DECISIVE difference: the compound runner emits a
per-8x8-TILE worst SSIM alongside the global SSIM, and the first real run showed those
two can DIVERGE catastrophically — stack keyframe_every=4 measured net_speedup=14.3 while
global SSIM 0.72 hid a worst_tile SSIM of 0.14 (large disocclusion under a camera dolly
THROUGH an interior, far beyond the small orbiting scene the temporal runner was
validated on). A tuner that optimizes speedup subject to global-SSIM-only would happily
ship that. So here the objective is a TWO-constraint feasibility:

    maximize net_speedup
      subject to  quality (global SSIM) >= q_floor
             AND  worst_tile_ssim       >= tile_floor(q_floor)

    where tile_floor(q_floor) = q_floor - 0.05
      (q_floor 0.98 -> tile_floor 0.93 ; q_floor 0.90 -> tile_floor 0.85)

A trial that passes the global bar but fails the worst-tile bar is INFEASIBLE — a bad
worst tile can NEVER be hidden behind a good global average. This is the whole point of
the runner emitting worst_tile_ssim, and it is what lets the tuner HONESTLY DISCOVER
whether tighter keyframe_every (2, 3) recovers the quality the kf=4 collapse lost (the
base config below starts TIGHT at keyframe_every=2 for exactly that reason), rather than
being lured up the keyframe_every ladder by a global SSIM that looks fine.

COST — the reference render is the expensive step (~15 min for the animated 1080p 4096-spp
reference). exp_render_stack.py CACHES the reference keyed by (scene, resolution, ref_spp,
bounces, frames, seed, cam_motion). Of the knobs THIS tuner sweeps, only `resolution`
appears in that key (frames/ref_spp/bounces/seed/cam_motion/scene are all fixed across the
sweep), so ONLY a resolution change forces a fresh, expensive reference re-render. The
search space (SPACE) is therefore ORDERED with `resolution` LAST, and coordinate-ascent
walks knobs in SPACE order, so every cache-invalidating resolution change happens after the
cheap knobs are already explored on the cached reference. See the CACHE_INVALIDATING_KNOBS
note at the ordering site.

Money-safety mirrors tuner.py: this module does NOT provision its own pod — the caller
(run_stack_tuner.py) provisions ONE reachable pod, registers cleanup, ships pod/, installs
the EXR deps, and passes the pod object in. tune_stack(pod, max_minutes, min_balance_check_fn)
runs the loop and streams every result to
docs/speed-lane-reports/spec-lab/stack_tuner_ledger.jsonl (event types: trial, tier_best,
pareto — the same ledger shape as tuner.py), resumable: a config already measured is not
re-run.
"""

import json
import os
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, HERE)
import runpod  # noqa: E402

LEDGER_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "docs", "speed-lane-reports", "spec-lab")
)
LEDGER = os.path.join(LEDGER_DIR, "stack_tuner_ledger.jsonl")

TARGET_ID = "render_stack"
RUNNER = "exp_render_stack.py"
# A single stack trial renders (on a cache HIT) `frames` anchor renders + the crop calib;
# on a cache MISS it ALSO renders `frames` heavy reference frames. Give it a generous
# ceiling — the SSH round-trip must outlast a full cache-miss reference render.
TRIAL_TIMEOUT_S = 3600

# ---- Quality tiers (hardest / most lossless first), same ladder as tuning_spaces.py.
# The 0.98/0.99 tiers are the owner's near-lossless bar; 0.95/0.90 are preview tiers.
Q_TIERS = [0.99, 0.98, 0.95, 0.90]

# ---- worst-tile floor as a function of the global floor. A bad worst tile can NEVER
# be hidden behind a good global average — this is the second, honesty-critical
# constraint. Kept 0.05 below the global floor (per the campaign spec).
TILE_FLOOR_DELTA = 0.05


def tile_floor(q_floor):
    """worst_tile_ssim floor required at a given global q_floor."""
    return q_floor - TILE_FLOOR_DELTA


# --------------------------------------------------------------------------- #
# BASE CONFIG — the proven starting point. Starts TIGHT (keyframe_every=2), NOT
# at the kf=4 point where quality collapsed, so coordinate-ascent begins from a
# feasible near-lossless config and only LOOSENS keyframe_every if the two-constraint
# feasibility check (global AND worst-tile) actually stays satisfied.
# --------------------------------------------------------------------------- #
BASE = {
    "frames": 8,
    "keyframe_every": 2,          # TIGHT start (the collapse was at kf=4)
    "draft_spp": 512,
    "ref_spp": 4096,
    "adaptive_threshold": 0.02,
    "denoiser": "oidn",
    "denoise_guides": True,
    "light_tree": True,
    "hole_fill": "rerender",
    "resolution": "1920x1080",
    "scene": "classroom",
    "bounces": 12,
    "device": "AUTO",
}

# --------------------------------------------------------------------------- #
# SEARCH SPACE — ordered value lists (adjacency = neighbor for coordinate ascent).
#
# ORDERING IS LOAD-BEARING FOR COST: coordinate_ascent() walks these knobs in dict
# insertion order. The reference render (the ~15-min expensive step) is cached by
# exp_render_stack.py keyed by (scene, resolution, ref_spp, bounces, frames, seed,
# cam_motion). Of the knobs swept here, ONLY `resolution` is in that key — so ONLY a
# resolution change forces an expensive reference re-render. `resolution` is therefore
# placed LAST so every cheap knob is explored on the already-cached reference first, and
# the sole cache-invalidating knob is touched only after the cheap knobs are exhausted.
# (frames is NOT swept — it stays 8 — precisely so it never invalidates the cache.)
# --------------------------------------------------------------------------- #
SPACE = {
    # keyframe_every FIRST (primary knob, densest speed<->quality signal). Tight->loose;
    # 2 and 3 are the "does tighter recover the kf=4 collapse?" values the campaign asks about.
    "keyframe_every":     [2, 3, 4, 6, 8],
    "draft_spp":          [256, 384, 512, 768, 1024],
    "adaptive_threshold": [0.005, 0.01, 0.02, 0.03, 0.05],
    "hole_fill":          ["rerender", "inpaint", "nearest"],
    "light_tree":         [True, False],
    "denoiser":           ["oidn", "optix"],
    # resolution LAST — the ONLY knob here that invalidates the cached reference render
    # (it is part of the reference cache key). See the ordering note above.
    "resolution":         ["1280x720", "1920x1080"],
}

# The knobs (of those swept) that appear in exp_render_stack.py's reference-cache key and
# therefore FORCE an expensive reference re-render when changed. Only `resolution` here.
CACHE_INVALIDATING_KNOBS = {"resolution"}

# The knob OFAT sweeps first (densest speed<->quality signal); the campaign's central
# question is whether tightening it recovers the kf=4 quality collapse.
PRIMARY_KNOB = "keyframe_every"


# --------------------------------------------------------------------------- #
# ledger + logging (same shape as tuner.py)                                     #
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc).isoformat()


def log(m):
    print(f"[tuner_stack {time.strftime('%H:%M:%S')}] {m}", flush=True)


def ledger_append(rec):
    os.makedirs(LEDGER_DIR, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"ts": _now(), **rec}) + "\n")


def cfg_key(cfg):
    return TARGET_ID + "|" + json.dumps(cfg, sort_keys=True)


def load_cache():
    """(config)->metrics for every trial already recorded (resume / no-repeat)."""
    cache = {}
    if os.path.exists(LEDGER):
        for line in open(LEDGER):
            try:
                r = json.loads(line)
                if r.get("event") == "trial" and r.get("target") == TARGET_ID:
                    cache[cfg_key(r["config"])] = r["metrics"]
            except Exception:
                pass
    return cache


# --------------------------------------------------------------------------- #
# trial exec — SSH the runner, parse the LAST stdout JSON line (runner contract).#
# --------------------------------------------------------------------------- #
def run_trial(pod, cfg, cache):
    key = cfg_key(cfg)
    if key in cache:
        return cache[key]
    payload = json.dumps(cfg).replace("'", "'\\''")
    cmd = f"cd /root/spec-lab && python3 pod/{RUNNER} '{payload}'"
    try:
        rc, out, err = runpod.ssh(pod, cmd, timeout=TRIAL_TIMEOUT_S)
    except Exception as e:  # SSH itself failed / timed out — record, don't crash the loop
        m = {"error": f"ssh: {type(e).__name__}: {e}"}
        cache[key] = m
        ledger_append({"event": "trial", "target": TARGET_ID, "config": cfg, "metrics": m})
        log(f"    {cfg} -> SSH ERR {type(e).__name__}")
        return m
    tail = ""
    for ln in reversed((out or "").strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            tail = ln
            break
    if rc != 0 and not tail:
        m = {"error": f"rc={rc}", "stderr_tail": (err or out or "")[-300:]}
    elif not tail:
        m = {"error": "no json line", "stderr_tail": (err or out or "")[-300:]}
    else:
        try:
            m = json.loads(tail)
        except Exception as e:
            m = {"error": f"bad json: {e}", "last_line": tail[:200]}
    cache[key] = m
    ledger_append({"event": "trial", "target": TARGET_ID, "config": cfg, "metrics": m})
    sp = m.get("net_speedup"); q = m.get("quality"); wt = m.get("worst_tile_ssim")
    log(f"    {cfg} -> speedup={sp} q={q} worst_tile={wt}" + ("  ERR" if "error" in m else ""))
    return m


# --------------------------------------------------------------------------- #
# THE HONESTY-CRITICAL FEASIBILITY CHECK — TWO constraints, never one.          #
# A trial is feasible at q_floor ONLY IF the global SSIM clears q_floor AND the  #
# worst 8x8 tile clears q_floor - 0.05. A good global average can NOT rescue a   #
# collapsed worst tile (the kf=4 failure mode: global 0.72 hiding worst_tile     #
# 0.14). Returns the net_speedup to MAXIMIZE, or -inf if infeasible/errored.     #
# --------------------------------------------------------------------------- #
def feasible_speedup(m, qfloor):
    if not isinstance(m, dict) or "error" in m:
        return float("-inf")
    q = m.get("quality")
    wt = m.get("worst_tile_ssim")
    sp = m.get("net_speedup")
    if q is None or wt is None or sp is None:
        return float("-inf")
    # BOTH constraints, together — this is the line the whole campaign turns on.
    if q >= qfloor and wt >= tile_floor(qfloor):
        return sp
    return float("-inf")


# --------------------------------------------------------------------------- #
# search — OFAT -> best-feasible seed per tier -> coordinate ascent -> pareto    #
# --------------------------------------------------------------------------- #
def ofat(pod, cache, budget_ok):
    """Sweep each knob alone off BASE. Primary knob first (densest signal). The
    resolution knob is swept LAST so its (expensive) cache-invalidating value change
    happens after the cheap knobs. Returns a list of (cfg, metrics)."""
    base = dict(BASE)
    trials = [(dict(base), run_trial(pod, base, cache))]
    knobs = [PRIMARY_KNOB] + [k for k in SPACE if k != PRIMARY_KNOB]
    for knob in knobs:
        for val in SPACE[knob]:
            if not budget_ok():
                return trials
            cfg = dict(base); cfg[knob] = val
            if cfg == base:
                continue
            trials.append((cfg, run_trial(pod, cfg, cache)))
    return trials


def best_feasible(trials, qfloor):
    best, bestm = None, float("-inf")
    for cfg, m in trials:
        s = feasible_speedup(m, qfloor)
        if s > bestm:
            best, bestm = cfg, s
    return best, bestm


def coordinate_ascent(pod, qfloor, start_cfg, cache, budget_ok):
    """From start_cfg, move ONE knob at a time to its best FEASIBLE neighbor value until
    a full pass yields no improvement (a local optimum for this tier). Deterministic.

    Knobs are walked in SPACE insertion order, which puts `resolution` (the only
    cache-invalidating knob) LAST — so within each pass every cheap knob is explored on
    the already-cached reference before a resolution change forces a fresh reference
    render. Returns (best_cfg, best_speedup)."""
    cur = dict(start_cfg)
    cur_s = feasible_speedup(run_trial(pod, cur, cache), qfloor)
    improved = True
    while improved and budget_ok():
        improved = False
        # SPACE order => resolution (cache-invalidating) walked LAST within the pass.
        for knob, values in SPACE.items():
            for val in values:
                if not budget_ok():
                    return cur, cur_s
                if cur.get(knob) == val:
                    continue
                cand = dict(cur); cand[knob] = val
                s = feasible_speedup(run_trial(pod, cand, cache), qfloor)
                if s > cur_s + 1e-9:
                    cur, cur_s, improved = cand, s, True
    return cur, cur_s


def pareto_front(cache):
    """Non-dominated (quality, speedup) points — the speed/quality tradeoff curve.
    We also carry worst_tile_ssim on each point so a reader can see the honesty
    constraint alongside the global SSIM."""
    pts = []
    for k, m in cache.items():
        if not k.startswith(TARGET_ID + "|"):
            continue
        if not isinstance(m, dict) or "error" in m:
            continue
        if m.get("quality") is None or m.get("net_speedup") is None:
            continue
        pts.append((m["quality"], m["net_speedup"],
                    m.get("worst_tile_ssim"), k.split("|", 1)[1]))
    front = []
    for q, s, wt, c in sorted(pts, key=lambda t: (-t[0], -t[1])):
        if all(not (fq >= q and fs >= s and (fq, fs) != (q, s))
               for fq, fs, _, _ in front):
            front.append((q, s, wt, c))
    return sorted(front)


# --------------------------------------------------------------------------- #
# THE TUNING ENTRY POINT the driver imports.                                    #
#   pod                : a reachable pod object (runpod.ssh-compatible), already  #
#                        provisioned + prepared (pod/ shipped, EXR deps installed) #
#                        by the caller. This function does NOT provision a pod.    #
#   max_minutes        : hard wall-clock ceiling for the whole tuning loop.        #
#   min_balance_check_fn: a zero-arg callable returning True while it is safe to    #
#                        keep spending (time + balance both OK). The driver owns     #
#                        the balance/deadline policy; we just consult it.            #
# --------------------------------------------------------------------------- #
def tune_stack(pod, max_minutes, min_balance_check_fn):
    """Run OFAT -> per-tier coordinate ascent -> refine-until-budget for the compound
    render stack. Streams trial/tier_best/pareto events to the stack_tuner ledger.
    Returns {"tier_best": {...}, "pareto": [...]}."""
    deadline = time.time() + max_minutes * 60

    def budget_ok():
        return time.time() < deadline and bool(min_balance_check_fn())

    cache = load_cache()
    log(f"stack tuner: {len(cache)} cached trials loaded; max {max_minutes}m; "
        f"tiers {Q_TIERS}; base keyframe_every={BASE['keyframe_every']} (TIGHT start)")

    # PHASE 1 — OFAT off the TIGHT base.
    log("=== OFAT sweep (one knob at a time off the base) ===")
    trials = ofat(pod, cache, budget_ok)

    # PHASE 2 — coordinate ascent at each quality tier from that tier's best OFAT seed.
    tier_best = {}
    for qf in Q_TIERS:
        if not budget_ok():
            log(f"  budget exhausted before tier q>={qf}")
            break
        start, s0 = best_feasible(trials, qf)
        if start is None:
            log(f"  tier q>={qf} (tile>={tile_floor(qf):.2f}): no feasible OFAT point — skip")
            tier_best[qf] = None
            ledger_append({"event": "tier_best", "target": TARGET_ID, "q_floor": qf,
                           "tile_floor": round(tile_floor(qf), 4),
                           "speedup": None, "config": None})
            continue
        log(f"  tier q>={qf} (tile>={tile_floor(qf):.2f}): coordinate ascent from {start} "
            f"(ofat best {s0:.2f}x)")
        bcfg, bs = coordinate_ascent(pod, qf, start, cache, budget_ok)
        bm = cache.get(cfg_key(bcfg), {})
        tier_best[qf] = {"config": bcfg, "speedup": bs,
                         "quality": bm.get("quality"),
                         "worst_tile_ssim": bm.get("worst_tile_ssim")}
        log(f"  tier q>={qf}: BEST {bs:.2f}x @ {bcfg} "
            f"[q={bm.get('quality')} worst_tile={bm.get('worst_tile_ssim')}]")
        ledger_append({"event": "tier_best", "target": TARGET_ID, "q_floor": qf,
                       "tile_floor": round(tile_floor(qf), 4),
                       "speedup": bs if bs != float("-inf") else None, "config": bcfg,
                       "quality": bm.get("quality"),
                       "worst_tile_ssim": bm.get("worst_tile_ssim")})

    front = pareto_front(cache)
    ledger_append({"event": "pareto", "target": TARGET_ID,
                   "front": [{"quality": q, "speedup": s, "worst_tile_ssim": wt}
                             for q, s, wt, _ in front]})
    log(f"  Pareto frontier ({len(front)} pts): " +
        ", ".join(f"q{q:.2f}/wt{(wt if wt is not None else -1):.2f}:{s:.1f}x"
                  for q, s, wt, _ in front[-6:]))

    # PHASE 3 — refine-until-budget: extra coordinate-ascent restarts from the highest-
    # quality feasible frontier points for the most-lossless tier, mining further gains.
    rounds = 0
    while budget_ok():
        rounds += 1
        progressed = False
        qf = Q_TIERS[0]  # the most-lossless tier — where the honesty bar bites hardest
        front = pareto_front(cache)
        seeds = [json.loads(c) for q, s, wt, c in front
                 if q >= qf and (wt is not None and wt >= tile_floor(qf))][:2]
        for seed in seeds:
            if not budget_ok():
                break
            before = feasible_speedup(cache.get(cfg_key(seed), {}), qf)
            bcfg, bs = coordinate_ascent(pod, qf, seed, cache, budget_ok)
            if bs > before + 1e-9:
                progressed = True
                bm = cache.get(cfg_key(bcfg), {})
                log(f"  refine[q>={qf}]: {bs:.2f}x @ {bcfg} "
                    f"[q={bm.get('quality')} worst_tile={bm.get('worst_tile_ssim')}]")
                ledger_append({"event": "tier_best", "target": TARGET_ID, "q_floor": qf,
                               "tile_floor": round(tile_floor(qf), 4),
                               "speedup": bs, "config": bcfg, "refine_round": rounds,
                               "quality": bm.get("quality"),
                               "worst_tile_ssim": bm.get("worst_tile_ssim")})
        if not progressed:
            log(f"refine round {rounds}: no improvement — stack tuning converged.")
            break

    front = pareto_front(cache)
    ledger_append({"event": "pareto", "target": TARGET_ID,
                   "front": [{"quality": q, "speedup": s, "worst_tile_ssim": wt}
                             for q, s, wt, _ in front]})
    log("stack tuning loop complete.")
    return {
        "tier_best": {str(qf): tier_best.get(qf) for qf in Q_TIERS},
        "pareto": [{"quality": q, "speedup": s, "worst_tile_ssim": wt}
                   for q, s, wt, _ in front],
    }
